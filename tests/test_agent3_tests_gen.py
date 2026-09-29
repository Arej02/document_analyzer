"""Stage `units` step 3: tests_gen.py — design_agent3.md §4 step 3, §6.

Four groups:
1. Structural + live-execution tests per §6 mapping row (contract tests): the
   generated source is inspected for the right shape, and (for the
   AST-scanning rows, where the logic is non-trivial) actually executed
   against a small hand-built `app` package tree, to prove the emitted scan
   really catches what §6 says it should.
2. Behaviour-test generation (`generate_behaviour_tests`, one LLM call): a
   fake LLM client, same style as `test_agent3_generate.py`'s.
3. `unit_slug.py`'s label-stand-in rule (own label / merged_scc join /
   override-merge join / no-label fallback).
4. A golden-file test over `tests/fixtures/agent2_cycle_repo/`'s real
   `billing_total` contract (design_agent3.md §6's determinism requirement:
   "the same contract must give byte-identical tests"), PHP-skip-guarded like
   `test_agent2_pipeline_fixture.py`.
"""

import ast
import importlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from document_analyzer.agent3.worker.context import UnitContext
from document_analyzer.agent3.worker.generate import GeneratedFile, GeneratedUnit
from document_analyzer.agent3.worker.run import ExecutionState
from document_analyzer.agent3.worker.tests_gen import generate_behaviour_tests, generate_contract_tests
from document_analyzer.agent3.worker.unit_slug import derive_unit_slug

UNIT_SLUG = "01_01_cart"


# --------------------------------------------------------------------------- #
# Group 1: structural + live-execution tests per §6 mapping row
# --------------------------------------------------------------------------- #


def _files_by_path(files):
    return {f.path: f.content for f in files}


def test_output_is_a_single_parseable_file_and_deterministic():
    contract = {
        "public_interface": [{"entity_id": "e-a", "name": "calc_total", "kind": "function", "params": ["order"]}],
        "hooks_bound": ["init"],
        "routes_bound": ["GET /widgets"],
        "external_calls": ["wp_remote_get"],
    }
    symbol_map = {"e-a": {"python_module": "app.units.01_01_cart.module", "python_name": "calc_total", "kind": "function"}}

    first = generate_contract_tests(UNIT_SLUG, contract, symbol_map, [])
    second = generate_contract_tests(UNIT_SLUG, contract, symbol_map, [])

    assert len(first) == 1
    [file] = first
    assert file.path == f"tests/units/{UNIT_SLUG}/test_contract.py"
    ast.parse(file.content)  # never a syntax error, whatever the contract contains
    assert _files_by_path(first) == _files_by_path(second)  # byte-identical on rerun


def test_public_interface_function_checks_import_and_params():
    contract = {
        "public_interface": [
            {"entity_id": "e-a", "name": "calc_total", "kind": "function", "params": ["order", "discountAmount"]}
        ]
    }
    symbol_map = {"e-a": {"python_module": "app.units.01_01_cart.module", "python_name": "calc_total", "kind": "function"}}

    [file] = generate_contract_tests(UNIT_SLUG, contract, symbol_map, [])

    assert 'importlib.import_module("app.units.01_01_cart.module")' in file.content.replace("'", '"')
    assert "'calc_total'" in file.content or '"calc_total"' in file.content
    # camelCase param snake_cased per §6.
    assert "discount_amount" in file.content


def test_public_interface_method_uses_dotted_class_convention():
    contract = {"public_interface": [{"entity_id": "e-a", "name": "total", "kind": "method", "params": []}]}
    symbol_map = {"e-a": {"python_module": "app.units.01_01_cart.module", "python_name": "Cart.total", "kind": "method"}}

    [file] = generate_contract_tests(UNIT_SLUG, contract, symbol_map, [])

    assert "getattr(module, 'Cart'" in file.content or 'getattr(module, "Cart"' in file.content
    assert "getattr(cls, 'total'" in file.content or 'getattr(cls, "total"' in file.content


def test_public_interface_missing_symbol_fails_loudly():
    contract = {"public_interface": [{"entity_id": "e-missing", "name": "calc_total", "kind": "function", "params": []}]}

    [file] = generate_contract_tests(UNIT_SLUG, contract, {}, [])

    assert "pytest.fail" in file.content
    assert "e-missing" in file.content


def test_no_test_generated_for_hint_only_or_no_test_fields():
    contract = {
        "side_effect_signals": [{"entity_id": "e-a", "risk_level": "high", "summary_excerpt": "..."}],
        "unverified_member_ids": ["e-b"],
        "merged_cluster_ids": ["cluster-x"],
    }

    [file] = generate_contract_tests(UNIT_SLUG, contract, {}, [])

    assert "@pytest.mark.contract" not in file.content


# --- live-execution fixtures for the AST-scanning rows ---------------------- #


def _clear_app_modules():
    for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[name]


@pytest.fixture
def fake_app(tmp_path, monkeypatch):
    """A minimal `app` package tree: two units (cart, tax), events, wp_compat —
    enough to actually run the generated contract tests against, not just read
    their source."""
    _clear_app_modules()
    app_dir = tmp_path / "app"
    (app_dir / "units" / "01_01_cart").mkdir(parents=True)
    (app_dir / "units" / "01_02_tax").mkdir(parents=True)

    (app_dir / "__init__.py").write_text("")
    (app_dir / "units" / "__init__.py").write_text("")
    (app_dir / "wp_compat.py").write_text("def wp_remote_get(url):\n    return {'body': 'ok'}\n")
    (app_dir / "events.py").write_text(
        "_handlers = {}\n"
        "def on(name, priority=10):\n"
        "    def deco(fn):\n"
        "        _handlers.setdefault(name, []).append(fn)\n"
        "        return fn\n"
        "    return deco\n"
        "def handlers_for(name):\n"
        "    return _handlers.get(name, [])\n"
    )
    (app_dir / "units" / "01_02_tax" / "__init__.py").write_text("")
    (app_dir / "units" / "01_02_tax" / "module.py").write_text(
        "def tax_rate(order):\n    return 0.1\n"
    )
    (app_dir / "units" / "01_01_cart" / "__init__.py").write_text(
        "from .module import router  # noqa: F401\n"
    )
    (app_dir / "units" / "01_01_cart" / "module.py").write_text(
        "import importlib\n"
        "\n"
        "from fastapi import APIRouter\n"
        "from app import events\n"
        "from app.wp_compat import wp_remote_get\n"
        "\n"
        "router = APIRouter()\n"
        "\n"
        "\n"
        "@router.get('/widgets')\n"
        "def list_widgets():\n"
        "    return []\n"
        "\n"
        "\n"
        "@events.on('init')\n"
        "def on_init():\n"
        "    return None\n"
        "\n"
        "\n"
        "def calc_total(order, discount=0):\n"
        "    return order - discount\n"
        "\n"
        "\n"
        "class Cart:\n"
        "    def total(self, extra=0):\n"
        "        return 1\n"
        "\n"
        "\n"
        "def good_wp_call():\n"
        "    return wp_remote_get('http://x')\n"
        "\n"
        "\n"
        "def call_tax(order):\n"
        "    tax = importlib.import_module('app.units.01_02_tax.module')\n"
        "    return tax.tax_rate(order)\n"
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    yield tmp_path
    _clear_app_modules()


def _load_generated_module(tmp_path: Path, unit_slug: str, content: str):
    test_dir = tmp_path / "tests" / "units" / unit_slug
    test_dir.mkdir(parents=True, exist_ok=True)
    test_file = test_dir / "test_contract.py"
    test_file.write_text(content, encoding="utf-8")
    spec = importlib.util.spec_from_file_location(f"_generated_{unit_slug}", test_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_test(module, name: str):
    getattr(module, name)()


def _sha(key: str) -> str:
    import hashlib

    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]


def test_hooks_bound_passes_when_registered(fake_app):
    contract = {"hooks_bound": ["init"]}
    [file] = generate_contract_tests(UNIT_SLUG, contract, {}, [])
    module = _load_generated_module(fake_app, UNIT_SLUG, file.content)
    _run_test(module, "test_hook_bound_init_" + _sha("init"))


def test_hooks_bound_fails_for_unregistered_hook(fake_app):
    contract = {"hooks_bound": ["save_post"]}
    [file] = generate_contract_tests(UNIT_SLUG, contract, {}, [])
    module = _load_generated_module(fake_app, UNIT_SLUG, file.content)
    with pytest.raises(AssertionError):
        _run_test(module, "test_hook_bound_save_post_" + _sha("save_post"))


def test_routes_bound_passes_for_a_real_route(fake_app):
    contract = {"routes_bound": ["GET /widgets"]}
    [file] = generate_contract_tests(UNIT_SLUG, contract, {}, [])
    module = _load_generated_module(fake_app, UNIT_SLUG, file.content)
    _run_test(module, "test_route_bound_get_widgets_" + _sha("GET /widgets"))


def test_routes_bound_fails_for_a_missing_route(fake_app):
    contract = {"routes_bound": ["POST /widgets"]}
    [file] = generate_contract_tests(UNIT_SLUG, contract, {}, [])
    module = _load_generated_module(fake_app, UNIT_SLUG, file.content)
    with pytest.raises(AssertionError):
        _run_test(module, "test_route_bound_post_widgets_" + _sha("POST /widgets"))


def test_outgoing_dependency_passes_when_not_reimplemented(fake_app):
    contract = {"outgoing_dependencies": [{"to_unit_id": "unit-tax", "call_count": 1, "hook_binding_count": 0}]}
    dependencies = [
        {
            "to_unit_id": "unit-tax",
            "symbol_map": {"e-tax": {"python_module": "app.units.01_02_tax.module", "python_name": "tax_rate", "kind": "function"}},
        }
    ]
    [file] = generate_contract_tests(UNIT_SLUG, contract, {}, dependencies)
    module = _load_generated_module(fake_app, UNIT_SLUG, file.content)
    _run_test(module, "test_outgoing_dependency_unit_tax_" + _sha("unit-tax"))


def test_outgoing_dependency_fails_when_reimplemented(fake_app):
    # The cart unit locally defines its own `tax_rate`, shadowing the dependency's export.
    (fake_app / "app" / "units" / "01_01_cart" / "module.py").write_text(
        (fake_app / "app" / "units" / "01_01_cart" / "module.py").read_text()
        + "\n\ndef tax_rate(order):\n    return 0.05\n"
    )
    contract = {"outgoing_dependencies": [{"to_unit_id": "unit-tax", "call_count": 1, "hook_binding_count": 0}]}
    dependencies = [
        {
            "to_unit_id": "unit-tax",
            "symbol_map": {"e-tax": {"python_module": "app.units.01_02_tax.module", "python_name": "tax_rate", "kind": "function"}},
        }
    ]
    [file] = generate_contract_tests(UNIT_SLUG, contract, {}, dependencies)
    module = _load_generated_module(fake_app, UNIT_SLUG, file.content)
    with pytest.raises(AssertionError, match="re-implements"):
        _run_test(module, "test_outgoing_dependency_unit_tax_" + _sha("unit-tax"))


def test_outgoing_dependency_skips_when_dependency_has_no_symbol_map(fake_app):
    contract = {"outgoing_dependencies": [{"to_unit_id": "unit-tax", "call_count": 1, "hook_binding_count": 0}]}
    dependencies = [{"to_unit_id": "unit-tax", "symbol_map": None}]
    [file] = generate_contract_tests(UNIT_SLUG, contract, {}, dependencies)
    module = _load_generated_module(fake_app, UNIT_SLUG, file.content)
    with pytest.raises(pytest.skip.Exception):
        _run_test(module, "test_outgoing_dependency_unit_tax_" + _sha("unit-tax"))


def test_external_call_passes_when_routed_through_wp_compat(fake_app):
    contract = {"external_calls": ["wp_remote_get"]}
    [file] = generate_contract_tests(UNIT_SLUG, contract, {}, [])
    module = _load_generated_module(fake_app, UNIT_SLUG, file.content)
    _run_test(module, "test_external_call_wp_remote_get_" + _sha("wp_remote_get"))


def test_external_call_fails_when_called_bare(fake_app):
    # No `from app.wp_compat import wp_remote_get` anywhere: the call bypasses
    # wp_compat entirely (the scan is purely syntactic — this never runs).
    (fake_app / "app" / "units" / "01_01_cart" / "module.py").write_text(
        "def bad_wp_call():\n    return wp_remote_get('http://x')\n"
    )
    contract = {"external_calls": ["wp_remote_get"]}
    [file] = generate_contract_tests(UNIT_SLUG, contract, {}, [])
    module = _load_generated_module(fake_app, UNIT_SLUG, file.content)
    with pytest.raises(AssertionError, match="without importing"):
        _run_test(module, "test_external_call_wp_remote_get_" + _sha("wp_remote_get"))


def test_external_call_fails_when_reimplemented(fake_app):
    (fake_app / "app" / "units" / "01_01_cart" / "module.py").write_text(
        (fake_app / "app" / "units" / "01_01_cart" / "module.py").read_text()
        + "\n\ndef wp_remote_get(url):\n    return {}\n"
    )
    contract = {"external_calls": ["wp_remote_get"]}
    [file] = generate_contract_tests(UNIT_SLUG, contract, {}, [])
    module = _load_generated_module(fake_app, UNIT_SLUG, file.content)
    with pytest.raises(AssertionError, match="reimplemented"):
        _run_test(module, "test_external_call_wp_remote_get_" + _sha("wp_remote_get"))


def test_public_interface_and_incoming_call_site_pass_against_real_module(fake_app):
    contract = {
        "public_interface": [{"entity_id": "e-a", "name": "calc_total", "kind": "function", "params": ["order"]}],
        "incoming_call_sites": [
            {"from_unit_id": "other", "from_entity_id": "e-x", "to_entity_id": "e-a", "call_count": 1}
        ],
    }
    symbol_map = {"e-a": {"python_module": "app.units.01_01_cart.module", "python_name": "calc_total", "kind": "function"}}
    [file] = generate_contract_tests(UNIT_SLUG, contract, symbol_map, [])
    module = _load_generated_module(fake_app, UNIT_SLUG, file.content)

    _run_test(module, "test_public_interface_calc_total_" + _sha("e-a"))
    _run_test(module, "test_incoming_call_site_calc_total_" + _sha("e-a"))


def test_public_interface_method_passes_against_real_module(fake_app):
    contract = {"public_interface": [{"entity_id": "e-cart", "name": "total", "kind": "method", "params": []}]}
    symbol_map = {"e-cart": {"python_module": "app.units.01_01_cart.module", "python_name": "Cart.total", "kind": "method"}}
    [file] = generate_contract_tests(UNIT_SLUG, contract, symbol_map, [])
    module = _load_generated_module(fake_app, UNIT_SLUG, file.content)

    _run_test(module, "test_public_interface_total_" + _sha("e-cart"))


# --------------------------------------------------------------------------- #
# Group 2: generate_behaviour_tests (one LLM call)
# --------------------------------------------------------------------------- #


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class FakeLLM:
    """`responses` is popped one per call; the last one repeats if exhausted.
    Same shape as `test_agent3_generate.py`'s."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[list[dict]] = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.calls.append(kwargs["messages"])
        content = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return _Resp(content)


def _behaviour_context() -> UnitContext:
    test_contract = {"side_effect_signals": [{"entity_id": "e-a", "risk_level": "high", "summary_excerpt": "..."}]}
    return UnitContext(
        cluster_id="unit-a",
        test_contract=test_contract,
        # In real usage build_unit_context derives `text` from `test_contract`; kept in
        # sync here so the "hint passed through" assertion below actually means something.
        text=json.dumps({"member_slices": [], "test_contract": test_contract}),
    )


def _behaviour_generated() -> GeneratedUnit:
    return GeneratedUnit(
        files=[GeneratedFile(path=f"app/units/{UNIT_SLUG}/module.py", content="def calc_total():\n    return 1\n")],
        symbol_map={"e-a": {"python_module": f"app.units.{UNIT_SLUG}.module", "python_name": "calc_total", "kind": "function"}},
        attempts=1,
    )


def _valid_behaviour_response(body: str = "def test_calc_total():\n    assert True\n") -> str:
    content = (
        "import importlib\n\nimport pytest\n\n\n"
        "@pytest.mark.behaviour\n" + body
    )
    return json.dumps({"content": content})


def test_behaviour_valid_output_on_first_attempt():
    llm = FakeLLM([_valid_behaviour_response()])

    result = generate_behaviour_tests(_behaviour_context(), UNIT_SLUG, _behaviour_generated(), llm, "gpt-x", max_attempts=3)

    assert result.generated is True
    assert result.attempts == 1
    [file] = result.files
    assert file.path == f"tests/units/{UNIT_SLUG}/test_behaviour.py"
    ast.parse(file.content)
    assert len(llm.calls) == 1


def test_behaviour_invalid_json_is_retried():
    llm = FakeLLM(["not json at all", _valid_behaviour_response()])

    result = generate_behaviour_tests(_behaviour_context(), UNIT_SLUG, _behaviour_generated(), llm, "gpt-x", max_attempts=3)

    assert result.generated is True
    assert result.attempts == 2
    assert len(llm.calls) == 2
    # The second call's conversation includes the first (invalid) response and feedback.
    feedback_messages = llm.calls[1][2:]
    assert feedback_messages[0]["role"] == "assistant"
    assert feedback_messages[0]["content"] == "not json at all"
    assert "invalid" in feedback_messages[1]["content"]


def test_behaviour_syntax_error_is_retried():
    bad = json.dumps({"content": "def test_x(:\n"})
    llm = FakeLLM([bad, _valid_behaviour_response()])

    result = generate_behaviour_tests(_behaviour_context(), UNIT_SLUG, _behaviour_generated(), llm, "gpt-x", max_attempts=3)

    assert result.generated is True
    assert result.attempts == 2


def test_behaviour_missing_marker_is_retried():
    bad = json.dumps({"content": "def test_calc_total():\n    assert True\n"})
    llm = FakeLLM([bad, _valid_behaviour_response()])

    result = generate_behaviour_tests(_behaviour_context(), UNIT_SLUG, _behaviour_generated(), llm, "gpt-x", max_attempts=3)

    assert result.generated is True
    assert result.attempts == 2
    feedback_messages = llm.calls[1][2:]
    assert "behaviour" in feedback_messages[1]["content"]


def test_behaviour_non_test_helper_functions_are_not_required_to_be_marked():
    # A helper (not named test_*) doesn't need @pytest.mark.behaviour.
    content = (
        "import pytest\n\n\n"
        "def _helper():\n    return 1\n\n\n"
        "@pytest.mark.behaviour\n"
        "def test_calc_total():\n    assert _helper() == 1\n"
    )
    llm = FakeLLM([json.dumps({"content": content})])

    result = generate_behaviour_tests(_behaviour_context(), UNIT_SLUG, _behaviour_generated(), llm, "gpt-x", max_attempts=3)

    assert result.generated is True
    assert result.attempts == 1


def test_behaviour_exhausting_every_attempt_returns_not_generated_without_raising():
    llm = FakeLLM(["still not json"])

    result = generate_behaviour_tests(_behaviour_context(), UNIT_SLUG, _behaviour_generated(), llm, "gpt-x", max_attempts=2)

    assert result.generated is False
    assert result.files == []
    assert result.attempts == 2
    assert len(llm.calls) == 2


def test_behaviour_prompt_includes_generated_code_and_context():
    llm = FakeLLM([_valid_behaviour_response()])

    generate_behaviour_tests(_behaviour_context(), UNIT_SLUG, _behaviour_generated(), llm, "gpt-x", max_attempts=3)

    [messages] = llm.calls
    system, user = messages[0]["content"], messages[1]["content"]
    assert UNIT_SLUG in system
    assert "behaviour" in system
    assert "importlib" in system
    assert "calc_total" in user  # the generated code's content made it into the prompt
    assert "side_effect_signals" in user and "e-a" in user  # the contract's hint is passed through as-is


# --------------------------------------------------------------------------- #
# Group 3: unit_slug.py's label stand-in rule
# --------------------------------------------------------------------------- #


def _item(cluster_id, phase_order=1, unit_order=1, report_source_cluster_ids=None):
    return {
        "cluster_id": cluster_id,
        "phase_order": phase_order,
        "unit_order": unit_order,
        "report_source_cluster_ids": report_source_cluster_ids or [cluster_id],
    }


def _state_with_clusters(clusters: dict) -> ExecutionState:
    state = ExecutionState()
    state.clusters = clusters
    return state


def test_own_label_used_when_not_merged():
    item = _item("u-1")
    state = _state_with_clusters({"u-1": {"kind": "community", "label": "Billing"}})

    assert derive_unit_slug(item, state) == "01_01_billing"


def test_merged_scc_uses_its_own_label_first():
    item = _item("u-merged", report_source_cluster_ids=["u-a", "u-b"])
    state = _state_with_clusters(
        {
            "u-merged": {"kind": "merged_scc", "label": "Cycle"},
            "u-a": {"kind": "community", "label": "Billing"},
            "u-b": {"kind": "community", "label": "Tax"},
        }
    )

    assert derive_unit_slug(item, state) == "01_01_cycle"


def test_merged_scc_joins_sources_when_it_has_no_label_of_its_own():
    item = _item("u-merged", report_source_cluster_ids=["u-a", "u-b"])
    state = _state_with_clusters(
        {
            "u-merged": {"kind": "merged_scc", "label": None},
            "u-a": {"kind": "community", "label": "Billing"},
            "u-b": {"kind": "community", "label": "Tax"},
        }
    )

    assert derive_unit_slug(item, state) == "01_01_billing_tax"  # sorted, joined, slugified


def test_override_merge_always_joins_sources():
    item = _item("u-override", report_source_cluster_ids=["u-a", "u-b"])
    state = _state_with_clusters(
        {
            "u-override": {"kind": "community", "label": "Kept label"},  # override merges always join anyway
            "u-a": {"kind": "community", "label": "Billing"},
            "u-b": {"kind": "community", "label": "Tax"},
        }
    )

    assert derive_unit_slug(item, state) == "01_01_billing_tax"


def test_no_label_anywhere_falls_back_to_a_short_id_slug():
    item = _item("11111111-2222-3333-4444-555555555555")
    state = _state_with_clusters({"11111111-2222-3333-4444-555555555555": {"kind": "community", "label": None}})

    slug = derive_unit_slug(item, state)

    assert slug == "01_01_u11111111"


def test_phase_and_unit_order_are_zero_padded():
    item = _item("u-1", phase_order=12, unit_order=3)
    state = _state_with_clusters({"u-1": {"kind": "community", "label": "X"}})

    assert derive_unit_slug(item, state) == "12_03_x"


# --------------------------------------------------------------------------- #
# Group 4: golden-file test over the real fixture repo's contract
# --------------------------------------------------------------------------- #

FIXTURE = Path(__file__).parent / "fixtures" / "agent2_cycle_repo"
GOLDEN = Path(__file__).parent / "fixtures" / "agent3_tests_gen" / "billing_total_test_contract.golden.txt"

pytestmark_php = pytest.mark.skipif(shutil.which("php") is None, reason="needs the php binary for Agent 1's extractors")


@pytestmark_php
def test_golden_file_billing_total_contract_is_byte_identical(monkeypatch):
    from document_analyzer.agent2.worker import run as run_module
    from document_analyzer.worker.persist import persist_extraction_result
    from document_analyzer.worker.php_extractor import PhpExtractor
    from document_analyzer.worker.wordpress_extractor import WordPressExtractor
    from fakes import FakeClient

    import json as _json

    class _Resp:
        def __init__(self, content):
            self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]

    class FakeLLM:
        def __init__(self):
            self.chat = type("Chat", (), {"completions": self})()

        def create(self, **kwargs):
            if "fact-checking" in kwargs["messages"][0]["content"]:
                return _Resp(_json.dumps({"grounded": True, "issues": []}))
            return _Resp(_json.dumps({"label": "A unit", "summary_text": "Does things."}))

    client = FakeClient()
    client.cascades["clusters"] = [
        ("clusters", "parent_cluster_id"),
        ("cluster_members", "cluster_id"),
        ("cluster_reports", "cluster_id"),
        ("cluster_edges", "from_cluster_id"),
    ]
    for extractor in (PhpExtractor(), WordPressExtractor()):
        persist_extraction_result(client, "job-1", "org-1", extractor.extract(FIXTURE))
    params = {"seed": 42, "alpha": 0.5, "max_unit_size": 25, "hub_percentile": 99, "leiden_resolution": 1.0}
    client.table("planning_jobs").insert(
        {
            "id": "pj-1",
            "org_id": "org-1",
            "project_id": "project-1",
            "source_job_id": "job-1",
            "status": "queued",
            "progress_stage": None,
            "params": params,
        }
    ).execute()

    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-test")
    monkeypatch.setattr(run_module, "get_azure_openai_client", FakeLLM)
    monkeypatch.setattr(run_module, "embed_text", lambda text: [0.0] * 384)
    monkeypatch.setattr(run_module, "get_service_role_client", lambda: client)
    run_module.process_planning_job("pj-1", "org-1")

    names = {e["id"]: e["name"] for e in client.table("extracted_entities").rows}
    unit_of = {names[m["canonical_entity_id"]]: m["cluster_id"] for m in client.table("cluster_members").rows}
    [plan] = client.table("migration_plans").rows
    items = {i["cluster_id"]: i for i in client.table("migration_plan_items").rows if i["plan_id"] == plan["id"]}

    billing_contract = items[unit_of["billing_total"]]["test_contract"]
    tax_contract = items[unit_of["tax_rate"]]["test_contract"]

    def _symbol_map_for(contract, module_name):
        return {
            e["entity_id"]: {
                "python_module": module_name,
                "python_name": e["name"],
                "kind": "method" if e.get("class") else "function",
            }
            for e in contract["public_interface"]
        }

    billing_symbol_map = _symbol_map_for(billing_contract, "app.units.01_01_billing.module")
    tax_symbol_map = _symbol_map_for(tax_contract, "app.units.01_02_tax.module")
    dependencies = [
        {"to_unit_id": unit_of["tax_rate"], "symbol_map": tax_symbol_map},
    ]

    [file] = generate_contract_tests("01_01_billing", billing_contract, billing_symbol_map, dependencies)
    ast.parse(file.content)

    if not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(file.content)
        pytest.fail(f"golden file didn't exist; wrote it to {GOLDEN} — re-run to verify")

    assert file.content == GOLDEN.read_text()
