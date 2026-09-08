"""Loader tests.

The default path uses a recording fake DB-API cursor so the SQL shape, the
parameters, and the head-priority behaviour are covered without a server.

Set WORKLOG_TEST_DATABASE_URL to a throwaway database to additionally run the
real round trip. Never point it at the operational database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from conftest_ledger import CHANNEL, WORKSPACE, build_notion_tree, build_slack_tree, slack_message  # noqa: E402

from rlwrld_worklog.ledger import load as load_module  # noqa: E402
from rlwrld_worklog.ledger.convert import convert_source  # noqa: E402
from rlwrld_worklog.ledger.load import LEGACY_ORIGIN_PRIORITY, LoadResult, load_source  # noqa: E402


class FakeCursor:
    """Records every statement. Head upserts report a configurable rowcount."""

    def __init__(self, recorder: dict, head_rowcount: int = 1) -> None:
        self.recorder = recorder
        self.rowcount = 0
        self._head_rowcount = head_rowcount
        self._next_fetch = None

    def execute(self, statement, params=None):
        text = " ".join(str(statement).split())
        self.recorder["statements"].append((text, params))
        if "INSERT INTO ledger_load_runs" in text:
            self._next_fetch = ("00000000-0000-4000-8000-000000000001",)
            self.rowcount = 1
        elif "SELECT id FROM ledger_batches" in text:
            self._next_fetch = None
            self.rowcount = 0
        elif "INSERT INTO ledger_batches" in text:
            self._next_fetch = ("00000000-0000-4000-8000-000000000002",)
            self.rowcount = 1
        elif "INSERT INTO source_object_heads" in text:
            self.rowcount = self._head_rowcount
        else:
            self.rowcount = 1

    def fetchone(self):
        return self._next_fetch

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeConnection:
    def __init__(self, recorder: dict, head_rowcount: int = 1) -> None:
        self.recorder = recorder
        self.autocommit = False
        self._head_rowcount = head_rowcount

    def cursor(self):
        return FakeCursor(self.recorder, self._head_rowcount)

    def commit(self):
        self.recorder["committed"] = True

    def rollback(self):
        self.recorder["rolled_back"] = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def install_fake(monkeypatch, recorder: dict, head_rowcount: int = 1):
    class FakeJsonb:
        def __init__(self, value):
            self.value = value

    fake_psycopg = type(
        "FakePsycopg",
        (),
        {"connect": staticmethod(lambda url: FakeConnection(recorder, head_rowcount))},
    )
    fake_json_module = type("FakeJsonModule", (), {"Jsonb": FakeJsonb})
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg.types", type("T", (), {}))
    monkeypatch.setitem(sys.modules, "psycopg.types.json", fake_json_module)


def prepared(tmp_path: Path, source: str = "slack") -> Path:
    legacy = tmp_path / "legacy"
    if source == "slack":
        build_slack_tree(legacy)
    else:
        build_notion_tree(legacy)
    out = tmp_path / "out"
    convert_source(
        legacy_root=legacy, out_root=out, source=source, salvage_comments=source == "notion"
    )
    return out


def test_dry_run_rolls_back(tmp_path, monkeypatch):
    out = prepared(tmp_path)
    recorder = {"statements": []}
    install_fake(monkeypatch, recorder)
    result = load_source(
        database_url="postgresql://fake", ledger_root=out, source="slack", dry_run=True
    )
    assert recorder.get("rolled_back") is True
    assert recorder.get("committed") is None
    assert result.ledger_records == 5
    assert result.timeline_events == 5
    assert result.run_id is None


def test_apply_commits(tmp_path, monkeypatch):
    out = prepared(tmp_path)
    recorder = {"statements": []}
    install_fake(monkeypatch, recorder)
    result = load_source(
        database_url="postgresql://fake", ledger_root=out, source="slack", dry_run=False
    )
    assert recorder.get("committed") is True
    assert recorder.get("rolled_back") is None
    assert result.run_id is not None


def test_legacy_rows_carry_legacy_origin_and_priority(tmp_path, monkeypatch):
    out = prepared(tmp_path)
    recorder = {"statements": []}
    install_fake(monkeypatch, recorder)
    load_source(database_url="postgresql://fake", ledger_root=out, source="slack", dry_run=True)
    heads = [
        params
        for text, params in recorder["statements"]
        if "INSERT INTO source_object_heads" in text
    ]
    assert heads
    for params in heads:
        assert params[4] == "legacy"
        assert params[5] == LEGACY_ORIGIN_PRIORITY
        assert params[5] < load_module.LIVE_ORIGIN_PRIORITY


def test_thread_store_is_a_lower_priority_legacy_supplement():
    assert load_module._origin_for(
        {"capture_profile": "legacy-slack-thread-store/v1"}
    ) == ("legacy", load_module.LEGACY_THREAD_STORE_PRIORITY)
    assert load_module.LEGACY_THREAD_STORE_PRIORITY < LEGACY_ORIGIN_PRIORITY
    assert load_module._origin_for({"capture_profile": "legacy-slack-slim12/v1"}) == (
        "legacy",
        LEGACY_ORIGIN_PRIORITY,
    )


def test_live_capture_profiles_outrank_every_legacy_profile():
    """Rule 5: a legacy re-run must never be able to demote a live head."""
    for profile in (
        "live-slack-web-api/v1",
        "live-slack-search/v1",
        "live-notion-page/v1",
        "live-google-calendar-events/v1",
    ):
        assert load_module._origin_for({"capture_profile": profile}) == (
            "live",
            load_module.LIVE_ORIGIN_PRIORITY,
        )
    assert load_module.LIVE_ORIGIN_PRIORITY > LEGACY_ORIGIN_PRIORITY


def test_head_not_advanced_is_counted_not_forced(tmp_path, monkeypatch):
    out = prepared(tmp_path)
    recorder = {"statements": []}
    # rowcount 0 means the WHERE clause refused to demote a live head.
    install_fake(monkeypatch, recorder, head_rowcount=0)
    result = load_source(
        database_url="postgresql://fake", ledger_root=out, source="slack", dry_run=True
    )
    assert result.heads_advanced == 0
    assert result.heads_not_advanced == 5


def test_every_ledger_row_carries_provenance(tmp_path, monkeypatch):
    out = prepared(tmp_path)
    recorder = {"statements": []}
    install_fake(monkeypatch, recorder)
    load_source(database_url="postgresql://fake", ledger_root=out, source="slack", dry_run=True)
    rows = [
        params
        for text, params in recorder["statements"]
        if "INSERT INTO ledger_records" in text
    ]
    assert len(rows) == 5
    for params in rows:
        assert params["source_file"]
        assert params["source_file_sha256"].startswith("sha256:")
        assert params["record_pointer"]
        assert params["observation_role"] == "historical_observation"


def test_timeline_payload_points_at_the_ledger_instead_of_copying_it(tmp_path, monkeypatch):
    out = prepared(tmp_path)
    recorder = {"statements": []}
    install_fake(monkeypatch, recorder)
    load_source(database_url="postgresql://fake", ledger_root=out, source="slack", dry_run=True)
    events = [
        params
        for text, params in recorder["statements"]
        if "INSERT INTO timeline_events" in text
    ]
    assert events
    for params in events:
        payload = params["payload"].value
        assert payload["ledger_id"] == params["event_id"]
        assert "text" not in payload
        assert payload["provenance"]["source_file"]
        # the loader asserts no classification of its own
        assert params["classifications"] == ["unclassified"]


def test_notion_blocks_stay_out_of_the_timeline(tmp_path, monkeypatch):
    out = prepared(tmp_path, source="notion")
    recorder = {"statements": []}
    install_fake(monkeypatch, recorder)
    result = load_source(
        database_url="postgresql://fake", ledger_root=out, source="notion", dry_run=True
    )
    # 3 pages + 2 blocks + 1 comment stored; only pages and the comment projected
    assert result.ledger_records == 6
    assert result.timeline_events == 4
    assert result.skipped_not_projected == 2
    assert result.extracted_text == 1


def test_missing_ledger_directory_reports_instead_of_raising(tmp_path, monkeypatch):
    recorder = {"statements": []}
    install_fake(monkeypatch, recorder)
    result = load_source(
        database_url="postgresql://fake", ledger_root=tmp_path / "nothing", source="slack"
    )
    assert isinstance(result, LoadResult)
    assert result.errors and "no ledger files" in result.errors[0]


@pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database to run the real round trip",
)
def test_real_database_round_trip(tmp_path):
    import psycopg

    from rlwrld_worklog.ledger.load import apply_migrations
    from rlwrld_worklog.ledger.verify import verify_ledger

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    legacy = build_slack_tree(tmp_path / "legacy")
    out = tmp_path / "out"
    convert_source(legacy_root=legacy, out_root=out, source="slack")

    # A genuinely empty throwaway database has none of the service baseline
    # tables referenced by 0002.  Install the baseline first, exactly as a
    # fresh Compose database does, so this test exercises the documented
    # deployment order instead of depending on pre-existing objects.
    baseline = Path("sql/schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(url) as connection:
        connection.execute(baseline)
        connection.commit()

    plan = apply_migrations(
        database_url=url, migrations_dir=Path("sql/migrations"), dry_run=False
    )
    assert not plan["checksum_mismatch"]

    dry = load_source(database_url=url, ledger_root=out, source="slack", dry_run=True)
    assert dry.ledger_records == 5

    applied = load_source(database_url=url, ledger_root=out, source="slack", dry_run=False)
    assert applied.ledger_records == 5

    # A fresh live observation of the same Slack object must take the head.
    # The workspace-qualified key is the contract shared by both collectors.
    from rlwrld_worklog.normalizers import normalize_slack
    from rlwrld_worklog.storage import write_events

    live_record = slack_message("1777000000.000100")
    live_record.update({"channel": CHANNEL, "team_id": WORKSPACE})
    live_event = normalize_slack(live_record, self_user_id="U0SELF")
    assert live_event.external_id == f"{WORKSPACE}:{CHANNEL}:1777000000.000100"
    write_events(url, [live_event], origin="live")

    # re-running must converge, not duplicate
    again = load_source(
        database_url=url, ledger_root=out, source="slack", dry_run=False, skip_unchanged=False
    )
    assert again.ledger_records == 5
    with psycopg.connect(url) as connection:
        head = connection.execute(
            """
            SELECT origin, origin_priority FROM source_object_heads
            WHERE source = 'slack' AND object_type = 'message' AND external_id = %s
            """,
            (live_event.external_id,),
        ).fetchone()
    assert head == ("live", 100)

    report = verify_ledger(
        ledger_root=out, source="slack", legacy_root=legacy, database_url=url
    )
    assert report.ok, report.failures
    assert report.database["ledger_records"] == 5
    assert report.database["rows_without_provenance"] == 0


@pytest.mark.skipif(
    not os.environ.get("WORKLOG_TEST_DATABASE_URL"),
    reason="set WORKLOG_TEST_DATABASE_URL to a throwaway database to run the real round trip",
)
def test_real_database_notion_round_trip(tmp_path):
    import psycopg

    from rlwrld_worklog.ledger.load import apply_migrations
    from rlwrld_worklog.ledger.verify import verify_ledger

    url = os.environ["WORKLOG_TEST_DATABASE_URL"]
    legacy = build_notion_tree(tmp_path / "legacy")
    out = tmp_path / "out"
    convert_source(legacy_root=legacy, out_root=out, source="notion", salvage_comments=True)

    baseline = Path("sql/schema.sql").read_text(encoding="utf-8")
    with psycopg.connect(url) as connection:
        connection.execute(baseline)
        connection.commit()
    plan = apply_migrations(database_url=url, migrations_dir=Path("sql/migrations"), dry_run=False)
    assert not plan["checksum_mismatch"]

    dry = load_source(database_url=url, ledger_root=out, source="notion", dry_run=True)
    assert dry.ledger_records == 6
    assert dry.extracted_text == 1
    applied = load_source(database_url=url, ledger_root=out, source="notion", dry_run=False)
    assert applied.ledger_records == 6
    again = load_source(
        database_url=url, ledger_root=out, source="notion", dry_run=False, skip_unchanged=False
    )
    assert again.ledger_records == 6

    report = verify_ledger(
        ledger_root=out, source="notion", legacy_root=legacy, database_url=url
    )
    assert report.ok, report.failures
    assert report.database["ledger_records"] == 6
    assert report.database["extracted_text"] == 1


# --------------------------------------------------- live capture loading


def _live_slack_ledger(tmp_path: Path) -> Path:
    """A real live capture run, converted to ledger JSONL on disk."""
    sys.path.insert(0, str(Path(__file__).parent))
    from test_slack_collector import CHANNEL, DM, FakeSlack, message, ts
    from test_slack_collector import collect as collect_slack

    from rlwrld_worklog.ledger.live import convert_live_run

    archive_root = tmp_path / "archive"
    _, run = collect_slack(archive_root, FakeSlack(history={CHANNEL: [[message(ts(-100))]], DM: [[]]}))
    out = tmp_path / "staging"
    convert_live_run(
        archive_root=archive_root,
        manifest_path=run.manifest_path,
        out_root=out,
        source="slack",
    )
    return out


def _params_for(recorder: dict, statement_fragment: str) -> list:
    return [
        params
        for text, params in recorder["statements"]
        if statement_fragment in text
    ]


def test_live_records_load_as_live_origin_at_the_highest_priority(tmp_path, monkeypatch):
    out = _live_slack_ledger(tmp_path)
    recorder = {"statements": []}
    install_fake(monkeypatch, recorder)

    result = load_source(
        database_url="postgresql://fake", ledger_root=out, source="slack", dry_run=False
    )

    assert result.ledger_records > 0
    observations = _params_for(recorder, "INSERT INTO source_object_observations")
    assert observations, "activity records must reach the observation table"
    for params in observations:
        assert params["origin"] == "live"
        assert params["origin_priority"] == load_module.LIVE_ORIGIN_PRIORITY
    heads = _params_for(recorder, "INSERT INTO source_object_heads")
    assert all(head[4] == "live" and head[5] == load_module.LIVE_ORIGIN_PRIORITY for head in heads)


def test_live_dimension_records_stay_out_of_the_timeline(tmp_path, monkeypatch):
    out = _live_slack_ledger(tmp_path)
    recorder = {"statements": []}
    install_fake(monkeypatch, recorder)

    result = load_source(
        database_url="postgresql://fake", ledger_root=out, source="slack", dry_run=False
    )

    projected = {
        params["entity_type"]
        for params in _params_for(recorder, "INSERT INTO ledger_records")
    }
    assert {"conversation", "user", "usergroup"} <= projected, "dimensions reach ledger_records"
    timeline = _params_for(recorder, "INSERT INTO timeline_events")
    assert {params["event_type"] for params in timeline} == {"message"}
    assert result.skipped_not_projected >= 3


def test_a_live_ledger_file_still_gets_an_observation_date(tmp_path, monkeypatch):
    """A live file is named by run id, so the date comes from the rows."""
    out = _live_slack_ledger(tmp_path)
    recorder = {"statements": []}
    install_fake(monkeypatch, recorder)

    load_source(database_url="postgresql://fake", ledger_root=out, source="slack", dry_run=False)

    batch = _params_for(recorder, "INSERT INTO ledger_batches")[0]
    assert batch[1] is not None, "observation_date must not be null for a live batch"


def test_reloading_the_same_live_file_is_skipped_as_unchanged(tmp_path, monkeypatch):
    out = _live_slack_ledger(tmp_path)
    recorder = {"statements": []}

    class SeenCursor(FakeCursor):
        def execute(self, statement, params=None):
            super().execute(statement, params)
            if "SELECT id FROM ledger_batches" in " ".join(str(statement).split()):
                self._next_fetch = ("00000000-0000-4000-8000-000000000009",)

    class SeenConnection(FakeConnection):
        def cursor(self):
            return SeenCursor(self.recorder, self._head_rowcount)

    fake_psycopg = type(
        "FakePsycopg", (), {"connect": staticmethod(lambda url: SeenConnection(recorder))}
    )
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(
        sys.modules,
        "psycopg.types.json",
        type("FakeJsonModule", (), {"Jsonb": lambda value: value}),
    )

    result = load_source(
        database_url="postgresql://fake", ledger_root=out, source="slack", dry_run=False
    )

    assert result.batches_skipped_unchanged == 1
    assert result.ledger_records == 0


def test_an_empty_migrations_directory_is_refused_not_reported_as_migrated(
    tmp_path,
) -> None:
    """`pending: []` must mean "nothing left", never "I looked nowhere".

    On 2026-09-08 a container was asked to migrate with a relative
    --migrations-dir. The image did not carry sql/, `Path.glob` on the missing
    directory returned nothing, and the command reported the database fully
    migrated. Twice, to a person watching the output both times.
    """
    from rlwrld_worklog.ledger.load import apply_migrations

    for directory in (tmp_path / "not-there", tmp_path / "empty"):
        (tmp_path / "empty").mkdir(exist_ok=True)
        with pytest.raises(FileNotFoundError, match="refusing to report"):
            apply_migrations(
                database_url="postgresql://unreachable/nowhere",
                migrations_dir=directory,
                dry_run=True,
            )


def test_the_image_carries_the_migrations_it_may_be_asked_to_apply(  ) -> None:
    """A container run is the only way to reach the database from the host today."""
    from pathlib import Path as _Path

    dockerfile = (_Path(__file__).resolve().parents[1] / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY sql ./sql" in dockerfile
