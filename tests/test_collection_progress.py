

# --- A recycled pid must not read as a live run (P0, 2026-09-14) -----------
#
# 감시의 활동 판정이 배경 프로세스에 속을 수 있다. Pids are reused. A
# collector that died hours ago eventually has its number handed to some
# unrelated background process, and a watcher that only asked "does this pid
# exist" reported the dead run as alive for as long as that process lived.

import os as _os  # noqa: E402
import socket as _socket  # noqa: E402

from rlwrld_worklog.collection_progress import (  # noqa: E402
    _pid_is_alive,
    _process_start_ticks,
)

HOST = _socket.gethostname()


def test_this_process_reads_as_alive() -> None:
    pid = _os.getpid()
    assert _pid_is_alive(pid, HOST, _process_start_ticks(pid)) is True


def test_a_pid_now_held_by_something_that_started_later_is_not_alive() -> None:
    """The number exists; the run does not."""
    pid = _os.getpid()
    older = (_process_start_ticks(pid) or 0) - 1
    assert _pid_is_alive(pid, HOST, older) is False


def test_a_snapshot_from_before_start_times_were_recorded_still_works() -> None:
    """Older snapshots carry no identity. Existence is all they can offer."""
    assert _pid_is_alive(_os.getpid(), HOST, None) is True


def test_another_host_is_unknown_not_dead() -> None:
    assert _pid_is_alive(_os.getpid(), "some-other-host", None) is None


def test_a_pid_that_does_not_exist_is_dead() -> None:
    # Above the usual pid_max, so it cannot be a real process here.
    assert _pid_is_alive(4_000_000, HOST, None) is False


def test_the_start_time_parses_a_process_whose_name_has_a_space() -> None:
    """/proc/<pid>/stat field 2 is parenthesised and can contain spaces.

    Splitting from the left mis-parses those, so the field is counted from
    the closing bracket instead. This is the parse, not a mock of it.
    """
    assert isinstance(_process_start_ticks(_os.getpid()), int)


def test_a_snapshot_records_the_start_time(tmp_path) -> None:
    from rlwrld_worklog.collection_progress import RunProgress

    progress = RunProgress(
        source="slack",
        environment="test",
        run_id="r1",
        started_at="2026-09-14T00:00:00+00:00",
        raw_run_dir=None,
        capture_density="full",
        dry_run=False,
        rule_stamp=None,
        config_root=tmp_path,
    )
    assert progress._state["pid_started_at"] == _process_start_ticks(_os.getpid())
