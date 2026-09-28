import pytest

from document_analyzer import setup_db


def test_discovers_repo_migrations_in_version_order():
    migrations = setup_db.discover_migrations()

    versions = [m.version for m in migrations]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert all(m.path.suffix == ".sql" for m in migrations)
    assert migrations[0].version == "20260918000000"
    assert migrations[0].name == "create_agent1_tables"


def test_rejects_file_without_numeric_version(tmp_path):
    (tmp_path / "20260101000000_ok.sql").write_text("select 1;")
    (tmp_path / "notes.sql").write_text("select 1;")

    with pytest.raises(ValueError, match="numeric version"):
        setup_db.discover_migrations(tmp_path)


def test_rejects_duplicate_versions(tmp_path):
    (tmp_path / "20260101000000_a.sql").write_text("select 1;")
    (tmp_path / "20260101000000_b.sql").write_text("select 1;")

    with pytest.raises(ValueError, match="share a version"):
        setup_db.discover_migrations(tmp_path)


def test_pending_keeps_order_and_skips_applied(tmp_path):
    for name in ("20260101000000_a", "20260102000000_b", "20260103000000_c"):
        (tmp_path / f"{name}.sql").write_text("select 1;")
    migrations = setup_db.discover_migrations(tmp_path)

    assert setup_db.pending(migrations, set()) == migrations
    assert [m.name for m in setup_db.pending(migrations, {"20260102000000"})] == ["a", "c"]
    assert setup_db.pending(migrations, {m.version for m in migrations}) == []


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql://postgres.ref:pw@aws-0-us-east-1.pooler.supabase.com:6543/postgres", True),
        ("postgresql://postgres.ref:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres", False),
        ("postgresql://postgres:pw@localhost/postgres", False),
    ],
)
def test_detects_transaction_pooler(dsn, expected):
    assert setup_db.is_transaction_pooler(dsn) is expected
