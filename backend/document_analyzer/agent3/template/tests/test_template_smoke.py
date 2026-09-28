"""The template's own tests (design_agent3.md §8) — proves the fixed
infrastructure works before any generated unit is assembled on top of it.
These run inside the sandbox exactly like a generated unit's tests
(tests/test_agent3_template.py's live test exercises this file through
sandbox.run_pytest).
"""

from fastapi.testclient import TestClient

from app import events
from app.main import app
from app.wp_compat import (
    __,
    esc_attr,
    esc_html,
    esc_url,
    get_option,
    get_post_meta,
    get_transient,
    sanitize_text_field,
    set_transient,
    update_option,
    update_post_meta,
    wp_remote_get,
)


def test_app_starts_with_no_units():
    client = TestClient(app)
    assert client is not None


def test_events_fire_calls_registered_handlers():
    calls = []
    events.on("init")(lambda: calls.append("a"))

    events.fire("init")

    assert calls == ["a"]


def test_events_run_in_priority_order_then_registration_order():
    calls = []
    events.on("init", priority=20)(lambda: calls.append("second-priority-20"))
    events.on("init", priority=5)(lambda: calls.append("first-priority-5"))
    events.on("init", priority=5)(lambda: calls.append("second-priority-5-registered-later"))

    events.fire("init")

    assert calls == ["first-priority-5", "second-priority-5-registered-later", "second-priority-20"]


def test_apply_filters_threads_value_through_handlers_in_order():
    events.on("the_title")(lambda value: value.upper())
    events.on("the_title", priority=20)(lambda value: f"{value}!")

    result = events.apply_filters("the_title", "hello")

    assert result == "HELLO!"


def test_handlers_for_is_empty_when_nothing_registered():
    assert events.handlers_for("no_such_hook") == []


def test_wp_compat_fake_is_active_by_default_and_records_calls(wp_compat_fake):
    update_option("site_name", "Test Site")

    assert get_option("site_name") == "Test Site"
    assert ("update_option", ("site_name", "Test Site"), {}) in wp_compat_fake.calls


def test_wp_compat_fake_round_trips_options_meta_and_transients(wp_compat_fake):
    update_option("site_name", "Test Site")
    assert get_option("site_name") == "Test Site"
    assert get_option("missing_option", default="fallback") == "fallback"

    update_post_meta(42, "_thumbnail_id", "7")
    assert get_post_meta(42, "_thumbnail_id") == "7"

    set_transient("cache_key", "cached-value", expire_seconds=60)
    assert get_transient("cache_key") == "cached-value"


def test_wp_compat_fake_records_remote_calls_without_network(wp_compat_fake):
    result = wp_remote_get("https://example.invalid/webhook")

    assert result["response"]["code"] == 200
    assert any(call[0] == "wp_remote_get" for call in wp_compat_fake.calls)


def test_real_wp_compat_persists_across_calls(real_wp_compat):
    real_wp_compat.update_option("site_name", "Real Site")

    assert real_wp_compat.get_option("site_name") == "Real Site"


def test_escaping_helpers():
    assert esc_html("<b>hi</b>") == "&lt;b&gt;hi&lt;/b&gt;"
    assert esc_attr('say "hi"') == "say &quot;hi&quot;"
    assert esc_url("https://example.com/a b") == "https://example.com/a%20b"
    assert sanitize_text_field("  a   b  ") == "a b"


def test_i18n_is_a_no_op():
    assert __("Hello") == "Hello"
