import pytest

from document_analyzer.worker.extract import ExtractedEdge, ExtractionResult
from document_analyzer.worker.php_extractor import PhpExtractionError, PhpExtractor


def test_detect_true_when_php_files_present(tmp_path):
    (tmp_path / "index.php").write_text("<?php echo 'hi';")
    assert PhpExtractor().detect(tmp_path) is True


def test_detect_false_when_no_php_files(tmp_path):
    (tmp_path / "readme.md").write_text("hello")
    assert PhpExtractor().detect(tmp_path) is False


def test_extract_finds_top_level_function(tmp_path):
    (tmp_path / "helpers.php").write_text(
        "<?php\nfunction add($a, $b) {\n    return $a + $b;\n}\n"
    )

    result = PhpExtractor().extract(tmp_path)

    assert isinstance(result, ExtractionResult)
    [entity] = result.entities
    assert entity.entity_type == "function"
    assert entity.name == "add"
    assert entity.file_path == "helpers.php"
    assert entity.raw_facts["params"] == ["a", "b"]
    assert result.edges == []


def test_extract_records_end_line_for_multi_line_function(tmp_path):
    (tmp_path / "helpers.php").write_text(
        "<?php\nfunction add($a, $b) {\n    return $a + $b;\n}\n"
    )

    [entity] = PhpExtractor().extract(tmp_path).entities

    assert entity.raw_facts["line"] == 2
    assert entity.raw_facts["end_line"] == 4


def test_extract_finds_class_and_methods(tmp_path):
    (tmp_path / "widget.php").write_text(
        """<?php
class Widget extends BaseWidget implements Renderable {
    public function render() {
        return 'ok';
    }

    private static function helper() {
        return 1;
    }
}
"""
    )

    result = PhpExtractor().extract(tmp_path)

    class_entities = [e for e in result.entities if e.entity_type == "class"]
    method_entities = [e for e in result.entities if e.entity_type == "function"]

    [widget] = class_entities
    assert widget.name == "Widget"
    assert widget.raw_facts["extends"] == "BaseWidget"
    assert widget.raw_facts["implements"] == ["Renderable"]
    assert sorted(widget.raw_facts["methods"]) == ["helper", "render"]

    render = next(m for m in method_entities if m.name == "render")
    assert render.raw_facts["class"] == "Widget"
    assert render.raw_facts["visibility"] == "public"
    assert render.raw_facts["static"] is False

    helper = next(m for m in method_entities if m.name == "helper")
    assert helper.raw_facts["visibility"] == "private"
    assert helper.raw_facts["static"] is True

    assert widget.raw_facts["line"] == 2
    assert widget.raw_facts["end_line"] == 10
    assert render.raw_facts["line"] == 3
    assert render.raw_facts["end_line"] == 5
    assert helper.raw_facts["line"] == 7
    assert helper.raw_facts["end_line"] == 9


def test_extract_resolves_call_edge_between_two_functions(tmp_path):
    (tmp_path / "app.php").write_text(
        """<?php
function do_thing($a, $b) {
    return helper($a) + $b;
}

function helper($x) {
    return $x * 2;
}
"""
    )

    result = PhpExtractor().extract(tmp_path)

    do_thing = next(e for e in result.entities if e.name == "do_thing")
    helper = next(e for e in result.entities if e.name == "helper")

    assert result.edges == [
        ExtractedEdge(from_local_id=do_thing.local_id, edge_type="calls", to_local_id=helper.local_id)
    ]


def test_extract_falls_back_to_class_level_edge_for_inherited_static_call(tmp_path):
    (tmp_path / "controller.php").write_text(
        """<?php
class AppointmentController {
    public function store() {
        return Appointment::create([]);
    }
}
"""
    )
    (tmp_path / "model.php").write_text(
        """<?php
class Appointment extends Model {
}
"""
    )

    result = PhpExtractor().extract(tmp_path)

    store = next(e for e in result.entities if e.name == "store")
    appointment_class = next(e for e in result.entities if e.entity_type == "class" and e.name == "Appointment")

    assert result.edges == [
        ExtractedEdge(from_local_id=store.local_id, edge_type="calls", to_local_id=appointment_class.local_id)
    ]


def test_extract_class_level_fallback_skipped_when_class_name_is_ambiguous(tmp_path):
    (tmp_path / "controller.php").write_text(
        """<?php
class Controller1 {
    public function store() {
        return Appointment::create([]);
    }
}
"""
    )
    (tmp_path / "model_a.php").write_text("<?php\nclass Appointment extends Model {}\n")
    (tmp_path / "model_b.php").write_text("<?php\nclass Appointment extends OtherModel {}\n")

    result = PhpExtractor().extract(tmp_path)

    assert result.edges == []


def test_extract_class_level_fallback_does_not_shadow_exact_method_match(tmp_path):
    (tmp_path / "controller.php").write_text(
        """<?php
class AppointmentController {
    public function store() {
        return Appointment::customMethod();
    }
}
"""
    )
    (tmp_path / "model.php").write_text(
        """<?php
class Appointment {
    public function customMethod() {}
}
"""
    )

    result = PhpExtractor().extract(tmp_path)

    store = next(e for e in result.entities if e.name == "store")
    custom_method = next(e for e in result.entities if e.name == "customMethod")

    assert result.edges == [
        ExtractedEdge(from_local_id=store.local_id, edge_type="calls", to_local_id=custom_method.local_id)
    ]


def test_extract_skips_unresolved_call_but_keeps_it_in_raw_facts(tmp_path):
    (tmp_path / "app.php").write_text(
        """<?php
function do_thing() {
    return wp_die('nope');
}
"""
    )

    result = PhpExtractor().extract(tmp_path)

    [entity] = result.entities
    assert entity.raw_facts["params"] == []
    assert entity.raw_facts["calls"] == ["wp_die"]
    assert result.edges == []


def test_extract_records_method_calls_deduplicated_and_sorted(tmp_path):
    (tmp_path / "app.php").write_text(
        """<?php
class Booking {
    public function save() {
        $this->validate();
        Doctor::find(1);
        $this->validate();
        exec('ls');
    }
}
"""
    )

    result = PhpExtractor().extract(tmp_path)

    save = next(e for e in result.entities if e.name == "save")
    assert save.raw_facts["calls"] == ["Doctor::find", "exec", "validate"]


def test_extract_include_inside_function_becomes_edge_with_no_target(tmp_path):
    (tmp_path / "app.php").write_text(
        """<?php
function bootstrap() {
    require_once 'config.php';
}
"""
    )

    result = PhpExtractor().extract(tmp_path)

    [entity] = result.entities
    [edge] = result.edges
    assert edge.from_local_id == entity.local_id
    assert edge.edge_type == "include"
    assert edge.to_local_id is None


def test_extract_skips_file_with_syntax_error_without_raising(tmp_path):
    (tmp_path / "broken.php").write_text("<?php function broken( {")
    (tmp_path / "ok.php").write_text("<?php function fine() { return 1; }")

    result = PhpExtractor().extract(tmp_path)

    assert [e.name for e in result.entities] == ["fine"]


def test_extract_raises_on_missing_directory(tmp_path):
    with pytest.raises(PhpExtractionError):
        PhpExtractor().extract(tmp_path / "does-not-exist")


def test_extract_raises_when_php_binary_missing(tmp_path):
    (tmp_path / "a.php").write_text("<?php")
    with pytest.raises(PhpExtractionError):
        PhpExtractor(php_binary="php-binary-that-does-not-exist").extract(tmp_path)
