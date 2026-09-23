import pytest

from document_analyzer.worker.extract import ExtractionResult
from document_analyzer.worker.wordpress_extractor import WordPressExtractionError, WordPressExtractor


def test_detect_true_when_wordpress_signal_call_present(tmp_path):
    (tmp_path / "plugin.php").write_text("<?php add_action('init', 'my_init');")
    assert WordPressExtractor().detect(tmp_path) is True


def test_detect_false_for_plain_php(tmp_path):
    (tmp_path / "helpers.php").write_text("<?php function add($a, $b) { return $a + $b; }")
    assert WordPressExtractor().detect(tmp_path) is False


def test_extract_hook_registration_creates_hook_and_callback_entities_with_edge(tmp_path):
    (tmp_path / "plugin.php").write_text(
        "<?php\nadd_action('init', 'my_plugin_init', 20, 1);\nfunction my_plugin_init() {}\n"
    )

    result = WordPressExtractor().extract(tmp_path)
    assert isinstance(result, ExtractionResult)

    hook = next(e for e in result.entities if e.entity_type == "hook")
    assert hook.name == "init"
    assert hook.raw_facts["kinds"] == ["action"]
    [occurrence] = hook.raw_facts["occurrences"]
    assert occurrence == {
        "role": "register",
        "kind": "action",
        "file": "plugin.php",
        "line": 2,
        "callback": "my_plugin_init",
        "priority": 20,
        "accepted_args": 1,
    }

    callback = next(e for e in result.entities if e.entity_type == "function")
    assert callback.name == "my_plugin_init"
    assert callback.raw_facts["role"] == "hook_callback"

    [edge] = result.edges
    assert edge.from_local_id == callback.local_id
    assert edge.to_local_id == hook.local_id
    assert edge.edge_type == "hook_binding"


def test_extract_method_callback_array_resolves_to_self_method_name(tmp_path):
    (tmp_path / "plugin.php").write_text(
        "<?php\nadd_filter('the_content', array($this, 'filter_content'));\n"
    )

    result = WordPressExtractor().extract(tmp_path)

    callback = next(e for e in result.entities if e.entity_type == "function")
    assert callback.name == "self::filter_content"


def test_extract_closure_callback_produces_no_callback_entity_or_edge(tmp_path):
    (tmp_path / "plugin.php").write_text(
        "<?php\nadd_action('init', function () { return 1; });\n"
    )

    result = WordPressExtractor().extract(tmp_path)

    assert [e for e in result.entities if e.entity_type == "function"] == []
    assert result.edges == []
    hook = next(e for e in result.entities if e.entity_type == "hook")
    assert hook.raw_facts["occurrences"][0]["callback"] is None


def test_extract_hook_entity_carries_a_non_null_file_path(tmp_path):
    """Regression test: hook_entity() used to pre-seed its dict with `"file_path": None`,
    which made the later `hook.setdefault("file_path", file_path)` calls no-ops (setdefault
    only fires when the key is absent, not when it's None) — every hook entity's file_path
    stayed null forever, which extracted_entities.file_path's NOT NULL constraint rejects
    at persist time (caught by tasks.md Phase 6's load test, not by any extractor-level
    test until this one)."""
    (tmp_path / "plugin.php").write_text("<?php\nadd_action('init', 'my_plugin_init');\n")

    result = WordPressExtractor().extract(tmp_path)

    [hook] = [e for e in result.entities if e.entity_type == "hook"]
    assert hook.file_path == "plugin.php"


def test_extract_hook_invocation_adds_occurrence_but_no_edge(tmp_path):
    (tmp_path / "plugin.php").write_text(
        "<?php\nfunction bootstrap() {\n    do_action('my_plugin_after_init');\n}\n"
    )

    result = WordPressExtractor().extract(tmp_path)

    [hook] = [e for e in result.entities if e.entity_type == "hook"]
    assert hook.name == "my_plugin_after_init"
    assert hook.raw_facts["occurrences"] == [
        {"role": "invoke", "kind": "action", "file": "plugin.php", "line": 3}
    ]
    assert result.edges == []


def test_extract_same_hook_name_across_calls_aggregates_into_one_entity(tmp_path):
    (tmp_path / "plugin.php").write_text(
        "<?php\n"
        "add_action('init', 'first_callback');\n"
        "add_action('init', 'second_callback');\n"
    )

    result = WordPressExtractor().extract(tmp_path)

    hooks = [e for e in result.entities if e.entity_type == "hook"]
    assert len(hooks) == 1
    assert len(hooks[0].raw_facts["occurrences"]) == 2


def test_extract_register_post_type_creates_schema_entity(tmp_path):
    (tmp_path / "plugin.php").write_text(
        "<?php\nregister_post_type('book', array('label' => 'Books', 'public' => true, 'supports' => array('title')));\n"
    )

    result = WordPressExtractor().extract(tmp_path)

    [entity] = result.entities
    assert entity.entity_type == "schema"
    assert entity.name == "book"
    assert entity.raw_facts["kind"] == "post_type"
    assert entity.raw_facts["args"] == {"label": "Books", "public": True, "supports": ["title"]}
    assert result.edges == []


def test_extract_register_post_meta_creates_schema_entity(tmp_path):
    (tmp_path / "plugin.php").write_text(
        "<?php\nregister_post_meta('book', 'isbn', array('type' => 'string', 'single' => true));\n"
    )

    result = WordPressExtractor().extract(tmp_path)

    [entity] = result.entities
    assert entity.entity_type == "schema"
    assert entity.name == "isbn"
    assert entity.raw_facts["kind"] == "meta"
    assert entity.raw_facts["object_type"] == "book"
    assert entity.raw_facts["args"] == {"type": "string", "single": True}


def test_extract_dynamic_cpt_slug_has_no_name_but_is_still_captured(tmp_path):
    (tmp_path / "plugin.php").write_text(
        "<?php\nfunction register_dynamic($slug) {\n    register_post_type($slug, array());\n}\n"
    )

    result = WordPressExtractor().extract(tmp_path)

    [entity] = result.entities
    assert entity.entity_type == "schema"
    assert entity.raw_facts["args"] == []
    assert "dynamic post type" in entity.name


def test_extract_skips_file_with_syntax_error_without_raising(tmp_path):
    (tmp_path / "broken.php").write_text("<?php add_action( {")
    (tmp_path / "ok.php").write_text("<?php add_action('init', 'fine_callback');")

    result = WordPressExtractor().extract(tmp_path)

    hooks = [e for e in result.entities if e.entity_type == "hook"]
    assert [h.name for h in hooks] == ["init"]


def test_extract_raises_on_missing_directory(tmp_path):
    with pytest.raises(WordPressExtractionError):
        WordPressExtractor().extract(tmp_path / "does-not-exist")


def test_extract_raises_when_php_binary_missing(tmp_path):
    (tmp_path / "a.php").write_text("<?php")
    with pytest.raises(WordPressExtractionError):
        WordPressExtractor(php_binary="php-binary-that-does-not-exist").extract(tmp_path)
