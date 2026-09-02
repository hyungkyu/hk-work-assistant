"""Static checks on sql/migrations.

No PostgreSQL server is available in this environment, so these checks cover
the failure modes that would otherwise only surface at apply time: a foreign
key pointing at a table nobody creates, a migration that is not idempotent,
and accidental data in a schema file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASELINE = REPO / "sql" / "schema.sql"
MIGRATIONS = REPO / "sql" / "migrations"

CREATE_TABLE_RE = re.compile(r"CREATE TABLE(?: IF NOT EXISTS)?\s+([a-z_][a-z0-9_]*)", re.I)
CREATE_VIEW_RE = re.compile(r"CREATE(?: OR REPLACE)? VIEW\s+([a-z_][a-z0-9_]*)", re.I)
REFERENCES_RE = re.compile(r"REFERENCES\s+([a-z_][a-z0-9_]*)\s*\(", re.I)
ALTER_TABLE_RE = re.compile(r"ALTER TABLE\s+([a-z_][a-z0-9_]*)", re.I)


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS.glob("*.sql"))


def test_migrations_exist_and_are_ordered():
    files = migration_files()
    assert files, "no migrations found"
    numbers = [int(path.stem.split("_", 1)[0]) for path in files]
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers), "duplicate migration numbers"


def test_every_foreign_key_target_is_created_somewhere():
    defined: set[str] = set()
    for path in [BASELINE, *migration_files()]:
        defined.update(name.lower() for name in CREATE_TABLE_RE.findall(path.read_text()))
    missing: list[str] = []
    for path in migration_files():
        for target in REFERENCES_RE.findall(path.read_text()):
            if target.lower() not in defined:
                missing.append(f"{path.name} -> {target}")
    assert not missing, f"foreign keys with no target table: {missing}"


def test_altered_tables_exist_in_the_baseline():
    baseline_tables = {name.lower() for name in CREATE_TABLE_RE.findall(BASELINE.read_text())}
    migration_tables: set[str] = set()
    for path in migration_files():
        migration_tables.update(name.lower() for name in CREATE_TABLE_RE.findall(path.read_text()))
    for path in migration_files():
        for table in ALTER_TABLE_RE.findall(path.read_text()):
            assert table.lower() in baseline_tables | migration_tables, (
                f"{path.name} alters unknown table {table}"
            )


def test_new_tables_are_created_idempotently():
    for path in migration_files():
        text = path.read_text()
        for match in re.finditer(r"CREATE TABLE(\s+IF NOT EXISTS)?\s+([a-z_][a-z0-9_]*)", text, re.I):
            assert match.group(1), f"{path.name}: CREATE TABLE {match.group(2)} is not idempotent"
        for match in re.finditer(r"CREATE(\s+UNIQUE)? INDEX(\s+IF NOT EXISTS)?", text, re.I):
            assert match.group(2), f"{path.name}: an index is created without IF NOT EXISTS"


def test_constraint_changes_drop_before_adding():
    """A bare ADD CONSTRAINT fails on re-run; each one needs a DROP first."""
    for path in migration_files():
        text = path.read_text()
        added = re.findall(r"ADD CONSTRAINT\s+([a-z_][a-z0-9_]*)", text, re.I)
        dropped = re.findall(r"DROP CONSTRAINT(?:\s+IF EXISTS)?\s+([a-z_][a-z0-9_]*)", text, re.I)
        for name in added:
            assert name in dropped, f"{path.name}: ADD CONSTRAINT {name} without a preceding DROP"


def test_migrations_contain_no_data():
    for path in migration_files():
        text = path.read_text()
        inserts = [
            line
            for line in text.splitlines()
            if re.match(r"\s*INSERT\s+INTO", line, re.I)
        ]
        assert not inserts, f"{path.name} contains INSERT statements: {inserts}"


def test_ledger_tables_are_defined():
    text = "\n".join(path.read_text() for path in migration_files())
    tables = {name.lower() for name in CREATE_TABLE_RE.findall(text)}
    for required in (
        "schema_migrations",
        "ledger_records",
        "ledger_extracted_text",
        "ledger_batches",
        "ledger_load_runs",
        "derived_attribution",
        "roster_identity_link",
        "computed_metrics",
    ):
        assert required in tables, f"missing table {required}"


def test_timeline_events_accepts_notion_after_migration():
    baseline = BASELINE.read_text()
    timeline_block = baseline.split("CREATE TABLE IF NOT EXISTS timeline_events")[1].split(");")[0]
    assert "'notion'" not in timeline_block, "baseline unexpectedly already allows notion"
    migration_text = "\n".join(path.read_text() for path in migration_files())
    assert "ADD CONSTRAINT" in migration_text
    assert "timeline_events_source_check" in migration_text
    fixed = migration_text.split("timeline_events_source_check")[-1]
    assert "'notion'" in fixed


@pytest.mark.parametrize("column", ["source_file", "source_file_sha256", "record_pointer"])
def test_provenance_columns_are_not_null(column):
    """Principle 8: every stored value must be traceable to its legacy file."""
    text = (MIGRATIONS / "0002_ledger_v1.sql").read_text()
    ledger_block = text.split("CREATE TABLE IF NOT EXISTS ledger_records")[1].split(");")[0]
    line = next(line for line in ledger_block.splitlines() if line.strip().startswith(column))
    assert "NOT NULL" in line, f"{column} must be NOT NULL"
