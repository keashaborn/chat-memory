from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SOURCE = (ROOT / "app.py").read_text(encoding="utf-8")
MIGRATION = (
    ROOT / "ops/sql/20260724_thread_title_provenance.sql"
).read_text(encoding="utf-8")


def test_rename_contract_distinguishes_automatic_and_manual_sources():
    assert 'Literal["automatic", "manual"]' in APP_SOURCE
    assert 'title_source: Literal["automatic", "manual"] = "manual"' in APP_SOURCE


def test_automatic_title_cannot_replace_manual_title():
    assert "AND title_source='automatic'" in APP_SOURCE
    assert '"skipped": "manual_title_preserved"' in APP_SOURCE


def test_manual_rename_sets_manual_provenance():
    assert "SET title=$1, title_source='manual', updated_at=now()" in APP_SOURCE


def test_migration_preserves_existing_titles_and_defaults_new_titles_to_automatic():
    assert "SET title_source = 'manual'" in MIGRATION
    assert "ALTER COLUMN title_source SET DEFAULT 'automatic'" in MIGRATION
    assert "ALTER COLUMN title_source SET NOT NULL" in MIGRATION
    assert "title_source IN ('automatic', 'manual')" in MIGRATION
