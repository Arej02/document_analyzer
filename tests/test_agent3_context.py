"""Stage `units` step 1: build_unit_context — design_agent3.md §4 step 1.

Built directly against a hand-assembled ExecutionState (no DB, no FakeClient):
context.py is pure over the state prepare.py would have loaded.
"""

import os

import pytest

from document_analyzer.agent3.worker.context import ContextError, ContextTooLargeError, build_unit_context
from document_analyzer.agent3.worker.run import ExecutionState

PHP_SOURCE = """<?php
class Cart {
    public function total() {
        return 1;
    }
}
function helper() {
    return 2;
}
"""


def _write_source(tmp_path):
    (tmp_path / "app.php").write_text(PHP_SOURCE)
    return tmp_path


def _entity(**kwargs):
    base = {"entity_type": "function", "name": "helper", "file_path": "app.php", "raw_facts": {}}
    base.update(kwargs)
    return base


def _state(tmp_path, *, entities=None, summaries=None, items=None, cluster_reports=None, symbol_maps=None):
    state = ExecutionState()
    state.snapshot_dir = tmp_path
    state.entities = entities or {}
    state.summaries = summaries or {}
    state.items = items or []
    state.cluster_reports = cluster_reports or {}
    if symbol_maps is not None:
        state.symbol_maps = symbol_maps
    return state


def _item(cluster_id="unit-a", member_entity_ids=(), test_contract=None, report_source_cluster_ids=None):
    return {
        "cluster_id": cluster_id,
        "phase_cluster_id": "phase-1",
        "phase_order": 1,
        "unit_order": 1,
        "member_entity_ids": list(member_entity_ids),
        "test_contract": test_contract or {},
        "report_source_cluster_ids": report_source_cluster_ids or [cluster_id],
    }


def test_happy_path_slices_source_and_includes_raw_facts_and_contract(tmp_path):
    _write_source(tmp_path)
    entities = {
        "e-helper": _entity(id="e-helper", name="helper", entity_type="function", raw_facts={"line": 7, "end_line": 9})
    }
    item = _item(member_entity_ids=["e-helper"], test_contract={"public_interface": []})
    state = _state(tmp_path, entities=entities)

    context = build_unit_context(item, state, max_context_chars=100_000)

    [slice_] = context.member_slices
    assert slice_["entity_id"] == "e-helper"
    assert slice_["source"] == "function helper() {\n    return 2;\n}"
    assert slice_["raw_facts"] == {"line": 7, "end_line": 9}
    assert context.test_contract == item["test_contract"]


def test_method_slice_prepends_class_header(tmp_path):
    _write_source(tmp_path)
    entities = {
        "e-class": _entity(id="e-class", name="Cart", entity_type="class", raw_facts={"line": 2, "end_line": 6}),
        "e-method": _entity(
            id="e-method",
            name="total",
            entity_type="function",
            raw_facts={"line": 3, "end_line": 5, "class": "Cart"},
        ),
    }
    item = _item(member_entity_ids=["e-method"])
    state = _state(tmp_path, entities=entities)

    context = build_unit_context(item, state, max_context_chars=100_000)

    [slice_] = context.member_slices
    assert slice_["source"] == "class Cart {\n    public function total() {\n        return 1;\n    }"


def test_member_with_no_line_has_raw_facts_but_no_source_slice(tmp_path):
    _write_source(tmp_path)
    entities = {"e-hook": _entity(id="e-hook", name="init", entity_type="hook", raw_facts={"name": "init"})}
    item = _item(member_entity_ids=["e-hook"])
    state = _state(tmp_path, entities=entities)

    context = build_unit_context(item, state, max_context_chars=100_000)

    [slice_] = context.member_slices
    assert slice_["source"] is None
    assert slice_["raw_facts"] == {"name": "init"}


def test_member_with_line_but_no_end_line_raises_context_error(tmp_path):
    _write_source(tmp_path)
    entities = {"e-bad": _entity(id="e-bad", raw_facts={"line": 7})}
    item = _item(member_entity_ids=["e-bad"])
    state = _state(tmp_path, entities=entities)

    with pytest.raises(ContextError, match="end_line"):
        build_unit_context(item, state, max_context_chars=100_000)


def test_unverified_summary_is_dropped_and_flagged(tmp_path):
    entities = {"e-a": _entity(id="e-a")}
    summaries = {"e-a": {"summary_text": "does a thing", "grounding_status": "unverified"}}
    item = _item(member_entity_ids=["e-a"], test_contract={"unverified_member_ids": ["e-a"]})
    state = _state(tmp_path, entities=entities, summaries=summaries)

    context = build_unit_context(item, state, max_context_chars=100_000)

    [slice_] = context.member_slices
    assert slice_["summary_text"] is None
    assert context.unverified_member_ids == ["e-a"]


def test_report_text_uses_own_report_when_present(tmp_path):
    item = _item(report_source_cluster_ids=["unit-a"])
    state = _state(tmp_path, cluster_reports={"unit-a": {"summary_text": "does stuff", "risk_level": "low"}})

    context = build_unit_context(item, state, max_context_chars=100_000)

    assert "does stuff" in context.report_text
    assert "low" in context.report_text


def test_report_text_falls_back_to_constituent_reports_for_a_merged_item(tmp_path):
    item = _item(cluster_id="unit-merged", report_source_cluster_ids=["unit-a", "unit-b"])
    state = _state(
        tmp_path,
        cluster_reports={
            "unit-a": {"summary_text": "part a", "risk_level": "low"},
            "unit-b": {"summary_text": "part b", "risk_level": "high"},
        },
    )

    context = build_unit_context(item, state, max_context_chars=100_000)

    assert "part a" in context.report_text
    assert "part b" in context.report_text


def test_dependency_signature_comes_from_contract_and_symbol_map(tmp_path):
    dep_item = _item(
        cluster_id="unit-dep",
        test_contract={
            "public_interface": [
                {"entity_id": "e-dep", "name": "calc", "kind": "function", "class": None, "params": ["x"]}
            ]
        },
    )
    item = _item(
        cluster_id="unit-a",
        test_contract={"outgoing_dependencies": [{"to_unit_id": "unit-dep", "call_count": 3, "hook_binding_count": 0}]},
    )
    symbol_maps = {"unit-dep": {"e-dep": {"python_module": "app.units.dep", "python_name": "calc", "kind": "function"}}}
    state = _state(tmp_path, items=[dep_item, item], symbol_maps=symbol_maps)

    context = build_unit_context(item, state, max_context_chars=100_000)

    [dep] = context.dependencies
    assert dep["to_unit_id"] == "unit-dep"
    [sig] = dep["signatures"]
    assert sig["python_module"] == "app.units.dep"
    assert sig["params"] == ["x"]


def test_dependency_without_a_symbol_map_yet_has_no_signatures(tmp_path):
    item = _item(
        test_contract={"outgoing_dependencies": [{"to_unit_id": "unit-dep", "call_count": 1, "hook_binding_count": 0}]}
    )
    state = _state(tmp_path, items=[item])

    context = build_unit_context(item, state, max_context_chars=100_000)

    [dep] = context.dependencies
    assert dep["symbol_map"] is None
    assert dep["signatures"] == []


def test_earlier_unit_symbols_include_units_not_declared_as_dependencies(tmp_path):
    """design_agent3.md §13: the sparse-call-graph mitigation surfaces every earlier
    unit's symbols, not only this unit's declared outgoing_dependencies."""
    item = _item(test_contract={})  # no declared dependencies at all
    symbol_maps = {
        "unit-other": {"e-x": {"python_module": "app.units.other", "python_name": "x", "kind": "function"}}
    }
    state = _state(tmp_path, symbol_maps=symbol_maps)

    context = build_unit_context(item, state, max_context_chars=100_000)

    assert context.earlier_unit_symbols == ["app.units.other.x"]


def test_context_too_large_raises_with_budget_accounting_for_reserved_rules(tmp_path):
    entities = {"e-a": _entity(id="e-a", raw_facts={})}
    item = _item(member_entity_ids=["e-a"])
    state = _state(tmp_path, entities=entities)

    with pytest.raises(ContextTooLargeError, match="budget of 10"):
        build_unit_context(item, state, max_context_chars=50, rules_reserved_chars=40)


def test_context_fits_exactly_at_the_budget_boundary(tmp_path):
    entities = {"e-a": _entity(id="e-a", raw_facts={})}
    item = _item(member_entity_ids=["e-a"])
    state = _state(tmp_path, entities=entities)

    context = build_unit_context(item, state, max_context_chars=100_000)
    exact_budget = context.total_chars

    # Must not raise when the payload exactly fills the budget.
    build_unit_context(item, state, max_context_chars=exact_budget)
    with pytest.raises(ContextTooLargeError):
        build_unit_context(item, state, max_context_chars=exact_budget - 1)


def test_rejects_path_traversal_in_raw_facts_file_path(tmp_path):
    entities = {
        "e-a": _entity(id="e-a", file_path="../outside.php", raw_facts={"line": 1, "end_line": 1}),
    }
    item = _item(member_entity_ids=["e-a"])
    state = _state(tmp_path, entities=entities)

    with pytest.raises(ContextError, match="unsafe file_path"):
        build_unit_context(item, state, max_context_chars=100_000)


def test_rejects_absolute_file_path(tmp_path):
    entities = {"e-a": _entity(id="e-a", file_path="/etc/passwd", raw_facts={"line": 1, "end_line": 1})}
    item = _item(member_entity_ids=["e-a"])
    state = _state(tmp_path, entities=entities)

    with pytest.raises(ContextError, match="unsafe file_path"):
        build_unit_context(item, state, max_context_chars=100_000)


@pytest.mark.skipif(os.name == "nt", reason="symlink escape guard is exercised on POSIX in CI; Windows symlinks need admin")
def test_rejects_symlink_escaping_the_snapshot(tmp_path):
    outside = tmp_path.parent / "outside-secret.php"
    outside.write_text("<?php secret();\n")
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    (snapshot_dir / "link.php").symlink_to(outside)

    entities = {"e-a": _entity(id="e-a", file_path="link.php", raw_facts={"line": 1, "end_line": 1})}
    item = _item(member_entity_ids=["e-a"])
    state = _state(snapshot_dir, entities=entities)

    with pytest.raises(ContextError, match="escapes the snapshot"):
        build_unit_context(item, state, max_context_chars=100_000)
