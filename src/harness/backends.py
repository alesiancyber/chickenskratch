"""Local model-backend process control: start/stop from the UI.

Backends are declared in models.yaml under `backends:` (name, base_url,
launch command). Two safety rules the whole module is built around:

- Start only ever executes the command from config — nothing from the
  request reaches a shell.
- Stop only ever signals a process this harness started. Started pids
  persist in data/backends.json and are re-adopted across harness
  restarts (after checking the pid is alive and still runs the same
  command); a backend that's reachable but launched elsewhere is
  "external" and never touched.

Processes are spawned in their own session, so exiting the harness
leaves models running — closing a CLI must not unload a model.
Output goes to data/logs/<name>.log.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import httpx

from .bootstrap import ROOT
from .profiles import BackendSpec

STATE_PATH = ROOT / "data" / "backends.json"
LOGS_DIR = ROOT / "data" / "logs"
STOP_GRACE_S = 5.0


async def endpoint_alive(base_url: str, timeout: float = 1.5) -> bool:
    """The one probe for "is an OpenAI-compatible server answering?":
    GET {base}/models. Every reachability check goes through here so the
    contract (path, error handling) exists once."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            await http.get(base_url.rstrip("/") + "/models")
        return True
    except httpx.HTTPError:
        return False


def endpoint_alive_sync(base_url: str, timeout: float = 1.5) -> bool:
    """Sync twin of endpoint_alive for non-async callers (the engine's
    Changes.md status line). Same contract, same path."""
    try:
        httpx.get(base_url.rstrip("/") + "/models", timeout=timeout)
        return True
    except httpx.HTTPError:
        return False


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    # A zombie answers signal 0 but is dead for our purposes — it just
    # hasn't been reaped by its parent yet.
    state = subprocess.run(["ps", "-p", str(pid), "-o", "state="],
                           capture_output=True, text=True).stdout.strip()
    return bool(state) and not state.startswith("Z")


def _pid_command(pid: int) -> str:
    r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return r.stdout.strip()


class BackendManager:
    def __init__(self, backends: dict[str, BackendSpec], state_path: Path | None = None):
        self.backends = backends
        self.state_path = state_path or STATE_PATH
        self._procs: dict[str, subprocess.Popen] = {}
        self._state = self._load_state()

    def _load_state(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")

    def managed_pid(self, name: str) -> int | None:
        """The pid we own for this backend, or None. Validates liveness, and
        for adopted pids (previous harness run) that the command still looks
        like ours — a recycled pid must never become killable."""
        proc = self._procs.get(name)
        if proc is not None:
            if proc.poll() is None:
                return proc.pid
            self._procs.pop(name, None)  # reap the zombie
        entry = self._state.get(name)
        if not entry:
            return None
        pid = int(entry.get("pid", 0))
        spec = self.backends.get(name)
        anchor = os.path.basename(spec.command[0]) if spec and spec.command else ""
        if pid > 0 and _pid_alive(pid) and anchor and anchor in _pid_command(pid):
            return pid
        # stale entry: process died while we weren't looking
        self._state.pop(name, None)
        self._save_state()
        return None

    def start(self, name: str) -> tuple[bool, str]:
        spec = self.backends.get(name)
        if spec is None:
            return False, f"unknown backend {name!r}"
        if not spec.command:
            return False, f"backend {name!r} has no command configured"
        if self.managed_pid(name) is not None:
            return True, "already running"
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log = open(LOGS_DIR / f"{name}.log", "ab")
        try:
            proc = subprocess.Popen(
                spec.command,
                stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                start_new_session=True,  # survives the harness process; killpg reaches children
                env={**os.environ, **spec.env},
            )
        except OSError as e:
            return False, f"failed to launch: {e}"
        finally:
            log.close()
        self._procs[name] = proc
        self._state[name] = {"pid": proc.pid, "command": spec.command,
                             "started": time.strftime("%Y-%m-%dT%H:%M:%S")}
        self._save_state()
        return True, f"started (pid {proc.pid}); models load in the background"

    def stop(self, name: str) -> tuple[bool, str]:
        pid = self.managed_pid(name)
        if pid is None:
            return False, ("not started by the harness — stop it wherever it was "
                           "launched (terminal, launchd, menu bar)")
        try:
            os.killpg(pid, signal.SIGTERM)  # session leader: pgid == pid
        except (ProcessLookupError, PermissionError):
            pass
        deadline = time.monotonic() + STOP_GRACE_S
        while time.monotonic() < deadline and _pid_alive(pid):
            proc = self._procs.get(name)
            if proc is not None:
                proc.poll()  # reap so _pid_alive sees the death
            time.sleep(0.15)
        if _pid_alive(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self._procs.pop(name, None)
        self._state.pop(name, None)
        self._save_state()
        return True, "stopped"


def main() -> None:
    """CLI for the declared local model backends — the web UI's ⚡ Servers
    panel, terminal-ized (the UI retirement, 2026-07-23).

        harness-backends              # list backends + reachability
        harness-backends start mlx-worker
        harness-backends stop mlx-worker
    """
    import argparse
    import asyncio

    from .bootstrap import DEFAULT_MODELS, load_env_file
    from .profiles import load_config

    parser = argparse.ArgumentParser(
        prog="harness-backends",
        description="List, start, or stop the model backends declared in models.yaml",
    )
    parser.add_argument("action", nargs="?", choices=["list", "start", "stop"], default="list")
    parser.add_argument("name", nargs="?", help="backend name (required for start/stop)")
    args = parser.parse_args()

    load_env_file()
    config = load_config(DEFAULT_MODELS)
    mgr = BackendManager(config.backends)

    if args.action == "list":
        if not config.backends:
            print("no backends declared in models.yaml")
            return
        for name, spec in config.backends.items():
            up = asyncio.run(endpoint_alive(spec.url))
            pid = mgr.managed_pid(name)
            owner = f"managed (pid {pid})" if pid else ("external" if up else "down")
            print(f"{name:20} {spec.url:32} {'reachable' if up else 'unreachable':12} {owner}")
        return
    if not args.name:
        raise SystemExit(f"{args.action} needs a backend name; declared: {', '.join(config.backends)}")
    ok, message = mgr.start(args.name) if args.action == "start" else mgr.stop(args.name)
    print(message)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
