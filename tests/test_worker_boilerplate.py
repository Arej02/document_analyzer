from document_analyzer.worker.boilerplate import FilteredFile, filter_boilerplate, log_filtered_files

from fakes import FakeClient


def test_filter_keeps_repo_owned_files(tmp_path):
    app_file = tmp_path / "app.php"
    app_file.write_text("<?php")

    kept, filtered = filter_boilerplate(tmp_path, [app_file])

    assert kept == [app_file]
    assert filtered == []


def test_filter_excludes_vendor_directory(tmp_path):
    (tmp_path / "vendor" / "acme").mkdir(parents=True)
    vendored = tmp_path / "vendor" / "acme" / "lib.php"
    vendored.write_text("<?php")

    kept, filtered = filter_boilerplate(tmp_path, [vendored])

    assert kept == []
    assert filtered == [FilteredFile(file_path="vendor/acme/lib.php", reason="composer_vendor_directory")]


def test_filter_excludes_wordpress_core_directories(tmp_path):
    (tmp_path / "wp-includes").mkdir()
    (tmp_path / "wp-admin").mkdir()
    core_file = tmp_path / "wp-includes" / "functions.php"
    core_file.write_text("<?php")
    admin_file = tmp_path / "wp-admin" / "admin.php"
    admin_file.write_text("<?php")

    kept, filtered = filter_boilerplate(tmp_path, [core_file, admin_file])

    assert kept == []
    reasons = {f.file_path: f.reason for f in filtered}
    assert reasons == {
        "wp-includes/functions.php": "wordpress_core",
        "wp-admin/admin.php": "wordpress_core",
    }


def test_filter_excludes_node_modules_and_git_directories(tmp_path):
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / ".git").mkdir()
    npm_file = tmp_path / "node_modules" / "pkg" / "index.js"
    npm_file.write_text("")
    git_file = tmp_path / ".git" / "config"
    git_file.write_text("")

    kept, filtered = filter_boilerplate(tmp_path, [npm_file, git_file])

    assert kept == []
    reasons = {f.file_path: f.reason for f in filtered}
    assert reasons == {
        "node_modules/pkg/index.js": "node_modules_directory",
        ".git/config": "vcs_directory",
    }


def test_filter_only_matches_directory_components_not_filename(tmp_path):
    tricky = tmp_path / "vendor.php"
    tricky.write_text("<?php")

    kept, filtered = filter_boilerplate(tmp_path, [tricky])

    assert kept == [tricky]
    assert filtered == []


def test_filter_nested_boilerplate_directory_still_matches(tmp_path):
    (tmp_path / "plugins" / "acme" / "vendor").mkdir(parents=True)
    nested = tmp_path / "plugins" / "acme" / "vendor" / "lib.php"
    nested.write_text("<?php")

    kept, filtered = filter_boilerplate(tmp_path, [nested])

    assert kept == []
    assert filtered[0].reason == "composer_vendor_directory"


def test_log_filtered_files_writes_rows_scoped_to_org_and_job():
    client = FakeClient(org_id="org-123")
    filtered = [
        FilteredFile(file_path="vendor/lib.php", reason="composer_vendor_directory"),
        FilteredFile(file_path="wp-includes/functions.php", reason="wordpress_core"),
    ]

    log_filtered_files(client, job_id="job-1", org_id="org-123", filtered=filtered)

    rows = client.table("filtered_files").rows
    assert len(rows) == 2
    assert all(r["org_id"] == "org-123" and r["job_id"] == "job-1" for r in rows)
    assert {r["file_path"] for r in rows} == {"vendor/lib.php", "wp-includes/functions.php"}


def test_log_filtered_files_no_filtered_files_is_a_no_op():
    client = FakeClient(org_id="org-123")

    log_filtered_files(client, job_id="job-1", org_id="org-123", filtered=[])

    assert client.table("filtered_files").rows == []
