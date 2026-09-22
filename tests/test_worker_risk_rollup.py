from document_analyzer.worker.risk_rollup import rollup_risk


def test_rollup_risk_empty_list_defaults_to_low():
    assert rollup_risk([]) == ("low", [])


def test_rollup_risk_all_same_level():
    assert rollup_risk([("a", "low"), ("b", "low")]) == ("low", ["a", "b"])


def test_rollup_risk_picks_highest_and_names_only_those_at_it():
    result = rollup_risk([("a", "low"), ("b", "medium"), ("c", "medium"), ("d", "high")])

    assert result == ("high", ["d"])
