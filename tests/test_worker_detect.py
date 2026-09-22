from document_analyzer.worker.detect import detect_language


def test_detects_plain_php(tmp_path):
    (tmp_path / "index.php").write_text("<?php echo 'hi';")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "helpers.php").write_text("<?php function helpers() {}")

    profile = detect_language(tmp_path)

    assert profile.framework == "plain_php"
    assert profile.wordpress_structure is None
    assert profile.php_file_count == 2


def test_detects_empty_repo_as_plain_php(tmp_path):
    profile = detect_language(tmp_path)

    assert profile.framework == "plain_php"
    assert profile.wordpress_structure is None
    assert profile.php_file_count == 0


def test_detects_wordpress_plugin_at_root(tmp_path):
    (tmp_path / "my-plugin.php").write_text(
        "<?php\n/**\n * Plugin Name: My Plugin\n * Version: 1.0\n */\n"
    )

    profile = detect_language(tmp_path)

    assert profile.framework == "wordpress"
    assert profile.wordpress_structure == "plugin"


def test_detects_wordpress_plugin_one_level_deep(tmp_path):
    plugin_dir = tmp_path / "my-plugin"
    plugin_dir.mkdir()
    (plugin_dir / "my-plugin.php").write_text(
        "<?php\n/**\n * Plugin Name: My Plugin\n */\n"
    )

    profile = detect_language(tmp_path)

    assert profile.framework == "wordpress"
    assert profile.wordpress_structure == "plugin"


def test_does_not_detect_plugin_header_beyond_scan_depth(tmp_path):
    deep_dir = tmp_path / "a" / "b" / "c"
    deep_dir.mkdir(parents=True)
    (deep_dir / "main.php").write_text("<?php\n/**\n * Plugin Name: Too Deep\n */\n")

    profile = detect_language(tmp_path)

    assert profile.framework == "plain_php"
    assert profile.wordpress_structure is None


def test_detects_wordpress_theme_via_style_css(tmp_path):
    (tmp_path / "style.css").write_text("/*\nTheme Name: My Theme\nVersion: 1.0\n*/\n")
    (tmp_path / "index.php").write_text("<?php get_header();")

    profile = detect_language(tmp_path)

    assert profile.framework == "wordpress"
    assert profile.wordpress_structure == "theme"


def test_style_css_without_theme_header_is_not_a_theme(tmp_path):
    (tmp_path / "style.css").write_text("body { color: red; }")
    (tmp_path / "index.php").write_text("<?php echo 'hi';")

    profile = detect_language(tmp_path)

    assert profile.framework == "plain_php"
    assert profile.wordpress_structure is None


def test_detects_full_wordpress_site_via_core_marker_file(tmp_path):
    (tmp_path / "wp-config.php").write_text("<?php // config")
    (tmp_path / "index.php").write_text("<?php // front controller")

    profile = detect_language(tmp_path)

    assert profile.framework == "wordpress"
    assert profile.wordpress_structure == "site"


def test_detects_full_wordpress_site_via_core_dir(tmp_path):
    (tmp_path / "wp-includes").mkdir()
    (tmp_path / "wp-includes" / "version.php").write_text("<?php // version")

    profile = detect_language(tmp_path)

    assert profile.framework == "wordpress"
    assert profile.wordpress_structure == "site"


def test_site_marker_takes_priority_over_plugin_header(tmp_path):
    (tmp_path / "wp-config.php").write_text("<?php // config")
    (tmp_path / "my-plugin.php").write_text("<?php\n/**\n * Plugin Name: Bundled Plugin\n */\n")

    profile = detect_language(tmp_path)

    assert profile.wordpress_structure == "site"
