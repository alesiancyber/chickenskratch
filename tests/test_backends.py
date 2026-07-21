"""Backend process control: only-ours stop safety, restart adoption."""

import sys
import time

from harness.backends import BackendManager, _pid_alive
from harness.profiles import BackendSpec


def spec(name="dummy", command=None):
    return BackendSpec(
        name=name,
        url="http://127.0.0.1:59999/v1",
        command=command or [sys.executable, "-c", "import time; time.sleep(60)"],
    )


def test_start_stop_lifecycle(tmp_path):
    mgr = BackendManager({"dummy": spec()}, state_path=tmp_path / "state.json")
    ok, msg = mgr.start("dummy")
    assert ok, msg
    pid = mgr.managed_pid("dummy")
    assert pid and _pid_alive(pid)

    ok, msg = mgr.start("dummy")
    assert ok and "already" in msg  # idempotent, no second process

    ok, msg = mgr.stop("dummy")
    assert ok, msg
    time.sleep(0.1)
    assert mgr.managed_pid("dummy") is None
    assert not _pid_alive(pid)


def test_adoption_across_restarts(tmp_path):
    state = tmp_path / "state.json"
    first = BackendManager({"dummy": spec()}, state_path=state)
    first.start("dummy")
    pid = first.managed_pid("dummy")

    # new manager instance = harness-web restarted; pid comes from the file
    second = BackendManager({"dummy": spec()}, state_path=state)
    assert second.managed_pid("dummy") == pid
    ok, _ = second.stop("dummy")
    assert ok
    assert not _pid_alive(pid)


def test_never_stops_what_it_did_not_start(tmp_path):
    mgr = BackendManager({"dummy": spec()}, state_path=tmp_path / "state.json")
    ok, msg = mgr.stop("dummy")
    assert not ok
    assert "not started by the harness" in msg


def test_recycled_pid_is_not_killable(tmp_path):
    state = tmp_path / "state.json"
    mgr = BackendManager({"dummy": spec()}, state_path=state)
    mgr.start("dummy")
    mgr.stop("dummy")
    # forge a state entry pointing at a live pid that is NOT our command
    state.write_text('{"dummy": {"pid": 1, "command": ["x"]}}')  # pid 1 = launchd
    fresh = BackendManager({"dummy": spec()}, state_path=state)
    assert fresh.managed_pid("dummy") is None  # command mismatch -> not ours
    ok, _ = fresh.stop("dummy")
    assert not ok


def test_unknown_and_commandless_backends(tmp_path):
    mgr = BackendManager({"nocmd": BackendSpec(name="nocmd", url="http://x/v1", command=[])},
                         state_path=tmp_path / "state.json")
    ok, msg = mgr.start("nope")
    assert not ok and "unknown" in msg
    ok, msg = mgr.start("nocmd")
    assert not ok and "no command" in msg
