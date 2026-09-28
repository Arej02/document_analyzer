"""Target-architecture template — design_agent3.md §8, tasks_agent3.md Phase 2.

Offline checks import the template's `app` package directly (its own dependency
pins — sqlalchemy, fastapi, httpx, pydantic — are a dev dependency of this
project too, `sqlalchemy==2.1.0` matching the runner image exactly, so this
exercises the real code, not a mock of it). The live test at the bottom proves
the same files actually pass `pytest` inside the real sandbox container,
mounted read-only exactly as stage `units`/`final_test` will use them.
"""

import ast
import shutil
import sys
from pathlib import Path

import pytest

from document_analyzer.agent3.worker import sandbox
from document_analyzer.agent3.worker.sandbox import SandboxParams, SandboxUnavailableError, run_pytest

TEMPLATE_DIR = (
    Path(__file__).resolve().parents[1] / "backend" / "document_analyzer" / "agent3" / "template"
)


def test_template_dir_exists():
    assert TEMPLATE_DIR.is_dir()


def test_every_template_python_file_parses():
    py_files = sorted(TEMPLATE_DIR.rglob("*.py"))
    assert py_files, "expected at least one template .py file"
    for path in py_files:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.fixture()
def template_app():
    """Puts the template directory on sys.path and imports `app` fresh, exactly as
    it will be imported from /work inside the sandbox. Reloaded per test since
    app.events/app.wp_compat carry process-global state."""
    sys.path.insert(0, str(TEMPLATE_DIR))
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    import app  # noqa: PLC0415
    import app.db  # noqa: PLC0415
    import app.events  # noqa: PLC0415
    import app.main  # noqa: PLC0415
    import app.wp_compat  # noqa: PLC0415
    import app.wp_compat.fake  # noqa: PLC0415
    import app.wp_compat.real  # noqa: PLC0415

    try:
        yield app
    finally:
        for name in list(sys.modules):
            if name == "app" or name.startswith("app."):
                del sys.modules[name]
        sys.path.remove(str(TEMPLATE_DIR))


def test_main_app_starts_with_no_units(template_app):
    from fastapi.testclient import TestClient

    client = TestClient(template_app.main.app)

    assert client is not None


def test_events_priority_then_registration_order(template_app):
    events = template_app.events
    calls = []
    events.on("init", priority=20)(lambda: calls.append("b"))
    events.on("init", priority=5)(lambda: calls.append("a1"))
    events.on("init", priority=5)(lambda: calls.append("a2"))

    events.fire("init")

    assert calls == ["a1", "a2", "b"]


def test_apply_filters_threads_value(template_app):
    events = template_app.events
    events.on("title")(lambda value: value.upper())
    events.on("title", priority=20)(lambda value: f"{value}!")

    assert events.apply_filters("title", "hi") == "HI!"


def test_handlers_for_reflects_registrations(template_app):
    events = template_app.events
    assert template_app.events.handlers_for("save_post") == []

    def handler():
        pass

    events.on("save_post")(handler)

    assert events.handlers_for("save_post") == [handler]


def test_wp_compat_fake_backend_records_calls_without_network_or_db(template_app):
    wp_compat = template_app.wp_compat
    fake = wp_compat.fake.FakeWPCompatBackend()
    previous = wp_compat.set_backend(fake)
    try:
        wp_compat.update_option("site_name", "Test Site")
        assert wp_compat.get_option("site_name") == "Test Site"

        wp_compat.update_post_meta(7, "_thumbnail_id", "3")
        assert wp_compat.get_post_meta(7, "_thumbnail_id") == "3"

        wp_compat.set_transient("k", "v", expire_seconds=60)
        assert wp_compat.get_transient("k") == "v"

        result = wp_compat.wp_remote_get("https://example.invalid/webhook")
        assert result == {"response": {"code": 200}, "body": ""}

        assert [call[0] for call in fake.calls] == [
            "update_option",
            "get_option",
            "update_post_meta",
            "get_post_meta",
            "set_transient",
            "get_transient",
            "wp_remote_get",
        ]
    finally:
        wp_compat.set_backend(previous)


def test_wp_compat_real_backend_persists_via_sqlalchemy(template_app):
    wp_compat = template_app.wp_compat
    db = template_app.db
    real_backend = wp_compat.RealWPCompatBackend()
    db.Base.metadata.create_all(db.engine)
    previous = wp_compat.set_backend(real_backend)
    try:
        wp_compat.update_option("site_name", "Real Site")
        assert wp_compat.get_option("site_name") == "Real Site"
        assert wp_compat.get_option("missing", default="fallback") == "fallback"
    finally:
        wp_compat.set_backend(previous)
        db.Base.metadata.drop_all(db.engine)


def test_escaping_and_i18n_helpers(template_app):
    wp_compat = template_app.wp_compat

    assert wp_compat.esc_html("<b>hi</b>") == "&lt;b&gt;hi&lt;/b&gt;"
    assert wp_compat.esc_attr('say "hi"') == "say &quot;hi&quot;"
    assert wp_compat.sanitize_text_field("  a   b  ") == "a b"
    assert wp_compat.__("Hello") == "Hello"
    assert wp_compat._e("Hello") is None


# ---------------------------------------------------------------- live: runs the
# template's own tests inside the real sandbox container.


def _sandbox_ready() -> bool:
    try:
        sandbox.check_sandbox_available(SandboxParams())
        return True
    except SandboxUnavailableError:
        return False


live = pytest.mark.skipif(not _sandbox_ready(), reason="needs a running Docker daemon and the galvanise-agent3-runner image")


@live
def test_live_template_tests_pass_inside_the_sandbox(tmp_path):
    workspace = tmp_path / "ws"
    shutil.copytree(TEMPLATE_DIR, workspace)

    result = run_pytest(workspace, SandboxParams(timeout_seconds=60, memory_mb=512))

    assert result.status == "completed", result.output_tail
    failed = [t for t in result.tests if t.outcome not in ("passed", "skipped")]
    assert failed == [], result.output_tail
    assert result.passed > 0
