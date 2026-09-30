"""Stage `units` step 2: generate_unit_code — design_agent3.md §4 step 2.

A fake Azure OpenAI client answers a queue of canned responses, same style as
tests/test_agent2_report.py's FakeLLM.
"""

import ast
import json

import pytest

from document_analyzer.agent3.worker.context import UnitContext
from document_analyzer.agent3.worker.generate import GenerationError, generate_stub_unit, generate_unit_code

UNIT_SLUG = "01_01_cart"


class _Resp:
    def __init__(self, content):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class FakeLLM:
    """`responses` is popped one per call; the last one repeats if exhausted."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[list[dict]] = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.calls.append(kwargs["messages"])
        content = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        return _Resp(content)


def _context(public_interface_ids=("e-a",)) -> UnitContext:
    return UnitContext(
        cluster_id="unit-a",
        test_contract={
            "public_interface": [{"entity_id": eid, "name": eid, "kind": "function"} for eid in public_interface_ids]
        },
        text=json.dumps({"member_slices": [], "test_contract": {}}),
    )


def _valid_response(entity_id="e-a") -> str:
    return json.dumps(
        {
            "files": [{"path": f"app/units/{UNIT_SLUG}/cart.py", "content": "def total():\n    return 1\n"}],
            "symbol_map": {entity_id: {"python_module": f"app.units.{UNIT_SLUG}.cart", "python_name": "total", "kind": "function"}},
        }
    )


def test_valid_output_on_first_attempt():
    llm = FakeLLM([_valid_response()])

    result = generate_unit_code(_context(), UNIT_SLUG, llm, "gpt-x", max_attempts=3)

    assert result.attempts == 1
    [file] = result.files
    assert file.path == f"app/units/{UNIT_SLUG}/cart.py"
    assert result.symbol_map["e-a"]["python_name"] == "total"
    assert len(llm.calls) == 1


def test_bad_path_outside_unit_package_is_rejected_and_retried():
    bad = json.dumps(
        {
            "files": [{"path": "app/main.py", "content": "x = 1\n"}],
            "symbol_map": {"e-a": {"python_module": "m", "python_name": "n", "kind": "function"}},
        }
    )
    llm = FakeLLM([bad, _valid_response()])

    result = generate_unit_code(_context(), UNIT_SLUG, llm, "gpt-x", max_attempts=3)

    assert result.attempts == 2
    assert len(llm.calls) == 2
    # The second call's conversation includes the first (invalid) response and feedback.
    feedback_messages = llm.calls[1][2:]
    assert feedback_messages[0]["role"] == "assistant"
    assert "app/main.py" in feedback_messages[0]["content"]
    assert "invalid" in feedback_messages[1]["content"]


def test_syntax_error_is_rejected_and_retried():
    bad = json.dumps(
        {
            "files": [{"path": f"app/units/{UNIT_SLUG}/cart.py", "content": "def total(:\n"}],
            "symbol_map": {"e-a": {"python_module": "m", "python_name": "n", "kind": "function"}},
        }
    )
    llm = FakeLLM([bad, _valid_response()])

    result = generate_unit_code(_context(), UNIT_SLUG, llm, "gpt-x", max_attempts=3)

    assert result.attempts == 2


def test_missing_public_interface_symbol_is_rejected_and_retried():
    bad = json.dumps(
        {
            "files": [{"path": f"app/units/{UNIT_SLUG}/cart.py", "content": "def total():\n    return 1\n"}],
            "symbol_map": {},
        }
    )
    llm = FakeLLM([bad, _valid_response()])

    result = generate_unit_code(_context(public_interface_ids=("e-a",)), UNIT_SLUG, llm, "gpt-x", max_attempts=3)

    assert result.attempts == 2
    assert "e-a" in result.symbol_map


def test_recovers_on_the_second_attempt(monkeypatch):
    bad_json = "not json at all"
    llm = FakeLLM([bad_json, _valid_response()])

    result = generate_unit_code(_context(), UNIT_SLUG, llm, "gpt-x", max_attempts=3)

    assert result.attempts == 2
    assert len(llm.calls) == 2


def test_exhausting_every_attempt_raises_generation_error():
    llm = FakeLLM(["still not json"])

    with pytest.raises(GenerationError, match="failed after 2 attempts"):
        generate_unit_code(_context(), UNIT_SLUG, llm, "gpt-x", max_attempts=2)

    assert len(llm.calls) == 2


def _exec_module(source: str):
    module = type(ast)("stub")
    exec(compile(source, "<stub>", "exec"), module.__dict__)
    return module


def test_stub_unit_route_bound_name_is_valid_python():
    """design_agent3.md §4 'Failed dependencies' / §6 `routes_bound[]`: a route-bound
    entry's `name` is literally "METHOD /path", not a Python identifier."""
    item = {
        "test_contract": {
            "public_interface": [
                {"entity_id": "e-a", "name": "POST /appointment", "kind": "function", "params": []},
                {"entity_id": "e-b", "name": "GET /get-available-dates", "kind": "function", "params": []},
            ]
        }
    }

    result = generate_stub_unit(item, UNIT_SLUG)

    [file] = result.files
    ast.parse(file.content, filename=file.path)  # would raise SyntaxError before the fix
    module = _exec_module(file.content)

    for entity_id, expected_ident in (("e-a", "post_appointment"), ("e-b", "get_get_available_dates")):
        python_name = result.symbol_map[entity_id]["python_name"]
        assert python_name == expected_ident
        assert callable(getattr(module, python_name))
        with pytest.raises(NotImplementedError):
            getattr(module, python_name)()


def test_stub_unit_route_bound_method_name_is_valid_python():
    item = {
        "test_contract": {
            "public_interface": [
                {"entity_id": "e-a", "name": "POST /appointment", "kind": "method", "class": "Scheduler", "params": []},
            ]
        }
    }

    result = generate_stub_unit(item, UNIT_SLUG)

    [file] = result.files
    ast.parse(file.content, filename=file.path)
    module = _exec_module(file.content)

    python_name = result.symbol_map["e-a"]["python_name"]
    assert python_name == "Scheduler.post_appointment"
    class_name, _, method_name = python_name.partition(".")
    cls = getattr(module, class_name)
    with pytest.raises(NotImplementedError):
        getattr(cls, method_name)(cls())
