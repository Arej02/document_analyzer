"""report/leak_gate.py — design_report.md §8.1 gate 2: secrets, source overlap, code shape, fail closed."""

import copy

import pytest

from document_analyzer.report.leak_gate import (
    WITHHELD_SUMMARY,
    LeakGateError,
    apply_content_gate,
    build_source_index,
    load_source_index,
    scan_text,
)
from tests.fakes import FakeClient

SOURCE = """function charge_card($order) {
    $result = $wpdb->query("UPDATE orders SET paid = 1 WHERE id = " . $order->id);
    return apply_filters('shop_charge_result', $result, $order);
}"""
AWS = "AKIAABCDEFGHIJKLMNOP"


def _report():
    return {
        "executive_summary": {"text": "A small shop with a few risky payment functions.", "status": "verified", "note": None},
        "architecture": {"subsystems": [{"name": "checkout", "summary": None, "risk_level": "high", "modules": ["a.php"]}], "hubs": []},
        "risk": {
            "hotspots": [
                {
                    "subsystem": "checkout",
                    "entities": [
                        {"name": "charge_card", "file_path": "a.php", "risk_level": "high", "signal": "raw_sql", "explanation": "Takes payment and updates the order."},
                    ],
                    "more": 0,
                }
            ]
        },
        "limitations": [{"code": "x", "text": "y"}],
    }


@pytest.fixture
def index():
    return build_source_index([SOURCE])


def test_clean_report_is_unchanged(index):
    report = _report()
    out, hits, limits = apply_content_gate(report, index)
    assert out == report and hits == [] and limits == []


def test_does_not_mutate_input(index):
    report = _report()
    report["risk"]["hotspots"][0]["entities"][0]["explanation"] = SOURCE
    before = copy.deepcopy(report)
    apply_content_gate(report, index)
    assert report == before


def test_source_overlap_replaces_explanation_and_records_limitation(index):
    report = _report()
    report["risk"]["hotspots"][0]["entities"][0]["explanation"] = (
        "It runs UPDATE orders SET paid = 1 WHERE id = then returns the result"
    )
    out, hits, limits = apply_content_gate(report, index)
    assert out["risk"]["hotspots"][0]["entities"][0]["explanation"] == WITHHELD_SUMMARY
    assert [(h.path, h.reasons) for h in hits] == [("risk.hotspots.0.entities.0.explanation", ("source_overlap",))]
    assert limits[0]["code"] == "summaries_withheld" and "1 summary" in limits[0]["text"]
    assert limits[0] in out["limitations"] and {"code": "x", "text": "y"} in out["limitations"]
    assert out["executive_summary"]["status"] == "verified"


def test_overlap_ignores_punctuation_so_tidied_quotes_do_not_evade_it():
    # Regression (leak test): a model that drops or normalises quotes and semicolons still quotes the source.
    tidied = "It runs UPDATE orders SET paid = 1 WHERE id = then returns"
    assert scan_text(tidied, build_source_index(['$wpdb->query("UPDATE orders SET paid = 1 WHERE id = " . $order->id);'])) == ["source_overlap"]
    assert scan_text("It runs UPDATE orders SET paid 1 WHERE id", build_source_index([SOURCE])) == ["source_overlap"]


def test_overlap_needs_six_consecutive_words_and_three_distinct_ones(index):
    assert scan_text("update orders set paid 1", index) == []  # five words
    assert scan_text("update orders set paid 1 where id", index) == ["source_overlap"]
    assert scan_text("0 0 0 0 0 0 0", build_source_index(["0 0 0 0 0 0 0 0"])) == []


def test_paraphrase_is_not_overlap(index):
    text = "The function charges the customer and then marks the order as paid in the database."
    assert scan_text(text, index) == []


def test_exec_summary_withheld(index):
    report = _report()
    report["executive_summary"]["text"] = "It does " + SOURCE.splitlines()[1]
    out, hits, limits = apply_content_gate(report, index)
    es = out["executive_summary"]
    assert es["status"] == "withheld" and es["text"] == "" and es["note"]
    assert [l["code"] for l in limits] == ["executive_summary_withheld"]


def test_secret_in_free_text_is_withheld_and_value_never_reported(index):
    report = _report()
    report["architecture"]["subsystems"][0]["summary"] = f"Uses the key {AWS} for uploads."
    out, hits, _ = apply_content_gate(report, index)
    assert out["architecture"]["subsystems"][0]["summary"] == WITHHELD_SUMMARY
    assert hits[0].reasons == ("aws_access_key",)
    assert AWS not in repr(hits) and AWS not in repr(out)


def test_redaction_marker_in_free_text(index):
    report = _report()
    report["risk"]["hotspots"][0]["entities"][0]["explanation"] = "Sets password to [REDACTED:hardcoded_credential_field]."
    _, hits, _ = apply_content_gate(report, index)
    assert hits[0].reasons == ("redaction_marker",)


@pytest.mark.parametrize(
    "text",
    [
        "Run ```php\necho 1;\n``` to see it.",
        "It starts with <?php and ends.",
        "Calls `update_option('shop_total', $total, true)` each time.",
        "Wrapped in { $a = 1; $b = 2; return $a + $b; } like so.",
        "if ($a) { foo($b); } else { bar($c[0]); } $x = [1];",
    ],
)
def test_code_shape(text):
    assert "code_shape" in scan_text(text)


def test_several_short_inline_names_are_not_one_long_backticked_run():
    # Regression (STOP sample, hospital_test): the text *between* two spans must not count as a span.
    text = "The `down` migration method changes the `end_time` column in the `doctor_availabilities` table back."
    assert scan_text(text) == []
    assert scan_text("Renders the `user.edit` view and passes it the provided `User` model as `user`.") == []


def test_prose_with_arrows_and_short_code_names_is_fine():
    assert scan_text("Moves data A -> B and calls charge_card() once per order.") == []


def test_secret_outside_free_text_fails_closed(index):
    report = _report()
    report["architecture"]["subsystems"][0]["modules"] = [f"config/{AWS}.php"]
    with pytest.raises(LeakGateError) as err:
        apply_content_gate(report, index)
    assert "architecture.subsystems.0.modules.0" in str(err.value) and AWS not in str(err.value)


def test_marker_outside_free_text_fails_closed(index):
    report = _report()
    report["risk"]["hotspots"][0]["entities"][0]["name"] = "[REDACTED:jwt]"
    with pytest.raises(LeakGateError):
        apply_content_gate(report, index)


def test_no_index_skips_overlap_only():
    comment = "Remember to reconcile the ledger totals before closing each fiscal quarter"
    index = build_source_index(["// " + comment + "\n$a = 1;"])
    report = _report()
    report["risk"]["hotspots"][0]["entities"][0]["explanation"] = comment
    assert apply_content_gate(report, index)[1][0].reasons == ("source_overlap",)
    assert apply_content_gate(report, None)[1] == []


def test_index_holds_no_source_text():
    index = build_source_index([SOURCE])
    blob = repr(vars(index))
    assert "charge_card" not in blob and "UPDATE" not in blob


def test_load_source_index_is_scoped_and_explicit():
    client = FakeClient("org-1")
    client.table("source_chunks").insert(
        [
            {"id": "c1", "org_id": "org-1", "job_id": "job-1", "content": SOURCE},
            {"id": "c2", "org_id": "org-1", "job_id": "job-2", "content": "other job tokens here that are quite distinct indeed"},
            {"id": "c3", "org_id": "org-2", "job_id": "job-1", "content": "other org tokens here that are quite distinct indeed"},
        ]
    ).execute()
    index = load_source_index(client, "org-1", "job-1")
    assert index.overlaps(SOURCE)
    assert not index.overlaps("other job tokens here that are quite distinct indeed")
    assert not index.overlaps("other org tokens here that are quite distinct indeed")


# --- gate 3: the rendered artifact ------------------------------------------------------------------------------------


def test_html_text_is_the_visible_text_only():
    from document_analyzer.report.leak_gate import html_text

    html = "<html><head><style>.a { color: red } /* AKIAABCDEFGHIJKLMNOP */</style><script>var x = 1</script></head><body><p>Hello <b>there</b></p>\n<p>again</p></body></html>"
    assert html_text(html) == "Hello there again"


def test_scan_rendered_checks_secrets_markers_and_overlap_but_not_code_shape(index):
    from document_analyzer.report.leak_gate import scan_rendered

    assert scan_rendered("Plain text about orders and totals.", index) == []
    assert scan_rendered(f"A key {AWS} appears", index) == ["aws_access_key"]
    assert "redaction_marker" in scan_rendered("Value [REDACTED:jwt] shown", index)
    assert scan_rendered("update orders set paid 1 where id", index) == ["source_overlap"]
    assert scan_rendered("Braces { $a = 1; $b = 2; return $a + $b; } and ```fences``` are the content gate's business", index) == []
    assert scan_rendered("update orders set paid 1 where id", None) == []  # no index, no overlap check
