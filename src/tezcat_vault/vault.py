"""Vault filesystem access: path safety and git snapshotting.

The vault is treated as the API surface — every tool reads and writes
markdown files under one root. All paths handed to tools are
vault-relative; anything that resolves outside the root is rejected.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import subprocess
import tempfile
import time
from pathlib import Path

from .vaultconfig import get_config

# Every dot-directory is editor/tool machinery, never notes: .git,
# .obsidian, .trash, Foam's .foam, VSCode's .vscode — enumerating them
# was a losing game (Foam templates under .foam/ were being scanned as
# vault notes).

# repo roots ensure_repo() has already initialized this process (see there)
_ensured_repos: set[str] = set()

# Ceiling on how long a writer waits for the vault lock. A real holder
# owns it for milliseconds (a write plus two git commits), so anything
# past this is a wedged process, not contention — and an unbounded wait
# there deadlocked the whole engine: a pass holds the PASS lock while it
# waits for this one, so every later trigger then no-opped forever.
# Failing loud costs one operation; waiting forever cost the pipeline.
LOCK_TIMEOUT_S = 60.0


class VaultError(Exception):
    """Raised for user-facing vault problems (bad path, missing note)."""


class VaultLocked(VaultError):
    """The vault lock was held past LOCK_TIMEOUT_S — a wedged writer, not
    a bad request. A VaultError subclass so every existing handler still
    catches it, but distinguishable where the difference is visible to a
    user: "another process is stuck" is a 503 to retry, not the 404 that
    "no such note" earns."""


def read_lossy(p: Path) -> str:
    """Tolerant read for READ-ONLY walks (scans, dashboards, search): one
    mis-encoded note yields replacement chars instead of wedging every
    engine pass. Never use on a read-modify-write path — Vault.read stays
    strict there so replacement chars can never be written back."""
    return p.read_text(encoding="utf-8", errors="replace")


def atomic_write_text(path: Path, text: str) -> Path:
    """Write a file so a reader never sees it half-written.

    The one durable-write idiom, shared by everything that persists state
    another process reads concurrently: the proposal queue, the watcher
    memo, the events log, the embedding cache, backend pids, usage
    totals. Five of those grew a plain `write_text` instead, and the
    queue's was load-bearing — the UI reads that directory from a
    threadpool thread while a pass writes it, so a torn file was skipped
    by load_all() and the proposal silently vanished.

    Same temp-file + rename as Vault.write, hoisted out of the class
    because the callers are not vault notes.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return path


class Vault:
    def __init__(self, root: str | Path, chat_id: str = "", exclude: tuple[str, ...] = ()):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise VaultError(f"Vault directory not found: {self.root}")
        # Conversation attribution for harness commits (Chat-Id trailer).
        self.chat_id = chat_id
        # Top-level folders structurally invisible to every tool — reads,
        # searches, and writes all reject them (vault.yaml `exclude`).
        # Matching is case-insensitive: macOS/Windows filesystems are, so
        # "private/x" and "Private/x" are the same folder on disk.
        self._exclude_lower = frozenset(e.lower() for e in exclude)

    # ---- paths ----

    def resolve(self, rel_path: str, must_exist: bool = False) -> Path:
        """Resolve a vault-relative path. Adds .md if no suffix given."""
        rel_path = rel_path.strip().lstrip("/")
        if not rel_path:
            raise VaultError("Empty path")
        p = (self.root / rel_path).resolve()
        if not p.is_relative_to(self.root):
            raise VaultError(f"Path escapes the vault: {rel_path}")
        if p.suffix == "" and not p.is_dir():
            p = p.with_suffix(".md")
        top = p.relative_to(self.root).parts
        if top and top[0].lower() in self._exclude_lower:
            raise VaultError(f"{top[0]}/ is excluded from tezcat access (vault.yaml exclude)")
        if must_exist and not p.exists():
            hint = self._suggest(rel_path)
            raise VaultError(f"Note not found: {rel_path}" + (f". Did you mean: {hint}?" if hint else ""))
        return p

    def rel(self, p: Path) -> str:
        return str(p.relative_to(self.root))

    def _suggest(self, rel_path: str) -> str | None:
        """Cheap fuzzy help: match on basename anywhere in the vault."""
        stem = Path(rel_path).stem.lower()
        matches = [self.rel(p) for p in self.iter_notes() if stem in p.stem.lower()]
        return ", ".join(matches[:3]) if matches else None

    def iter_notes(self, folder: str = "", recursive: bool = True):
        base = self.resolve(folder) if folder else self.root
        if not base.is_dir():
            raise VaultError(f"Not a folder: {folder}")
        pattern = "**/*.md" if recursive else "*.md"
        for p in sorted(base.glob(pattern)):
            parts = p.relative_to(self.root).parts
            if any(part.startswith(".") for part in parts):
                continue  # dot-dirs/files are editor machinery, never notes
            if parts and parts[0].lower() in self._exclude_lower:
                continue
            # glob follows symlinks: a link inside the vault must not smuggle
            # in a target that resolve() would have rejected.
            rp = p.resolve()
            if rp != p:
                if not rp.is_relative_to(self.root):
                    continue
                rtop = rp.relative_to(self.root).parts
                if rtop and rtop[0].lower() in self._exclude_lower:
                    continue
            yield p

    def iter_knowledge_notes(self, surface_dirs, folder: str = ""):
        """(path, rel) for every note that is the user's knowledge rather
        than harness machinery — iter_notes minus the machine-rendered
        surfaces. Every scan that asks "is this a real note?" goes through
        here: the surfaces-aren't-knowledge rule is derived once in
        VaultConfig.surface_dirs and applied once here, so a new surface
        never has to be remembered in four separate walks."""
        for p in self.iter_notes(folder):
            rel = self.rel(p)
            if rel.split("/")[0] in surface_dirs:
                continue
            yield p, rel

    def read(self, rel_path: str) -> str:
        p = self.resolve(rel_path, must_exist=True)
        try:
            return p.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            # VaultError, not UnicodeDecodeError: callers on the mutation
            # path catch VaultError per note/tool, so one mis-encoded file
            # errors cleanly instead of crashing the pass. Reads stay
            # STRICT here — a lossy decode written back would corrupt the
            # note with replacement chars (read-only walks use read_lossy).
            raise VaultError(f"{rel_path} is not valid UTF-8: {e}") from None

    def write(self, rel_path: str, content: str) -> Path:
        # Temp file + rename: an interrupted write (crash, disk full, kill)
        # must never leave a note half-written — the git snapshots only
        # protect content that made it to a commit intact.
        return atomic_write_text(self.resolve(rel_path), content)

    # ---- cross-process lock ----

    @contextlib.contextmanager
    def locked(self, timeout: float = LOCK_TIMEOUT_S):
        """Mutex for a whole snapshot→mutate→snapshot sequence.

        Several processes mutate one vault (MCP tools, capture, the watcher,
        revert); without this, one writer's `git add -A` snapshot can sweep
        another writer's half-finished edit into the wrong commit. The lock
        lives outside the vault so snapshots never pick it up, and flock is
        released automatically if the holder dies.

        Bounded (LOCK_TIMEOUT_S), and polled rather than blocking: a
        wedged holder must cost ONE operation a clean VaultError, not
        deadlock every writer behind it. Note this is not re-entrant —
        each acquire opens its own descriptor, so a nested locked() in
        one process would wait on itself; the timeout at least turns that
        from a hang into a diagnosable error."""
        digest = hashlib.sha1(str(self.root).encode()).hexdigest()[:12]
        lock_path = Path(tempfile.gettempdir()) / f"obsidian-harness-{digest}.lock"
        with open(lock_path, "w") as f:
            deadline = time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise VaultLocked(
                            f"vault lock held by another process for over {timeout:.0f}s "
                            f"({lock_path}) — nothing was changed"
                        ) from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    # ---- git snapshots ----

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True,
        )

    def ensure_repo(self) -> None:
        # A vault nested inside a parent git repo (no .git of its own but
        # tracked by an ancestor) gets its own repo here — snapshots must not
        # land in, or depend on, an unrelated outer repo. A `.git` file
        # (worktree/submodule) counts as a repo and is left alone.
        # Idempotent and called before every git op, so the answer is cached
        # per repo root — an engine pass otherwise pays 10+ `git config`
        # subprocesses re-proving the same thing.
        if str(self.root) in _ensured_repos:
            return
        if not (self.root / ".git").exists():
            self._git("init", "-q")
        if self._git("config", "user.email").returncode != 0:
            self._git("config", "user.email", "harness@local")
            self._git("config", "user.name", "tezcat")
        _ensured_repos.add(str(self.root))

    def commit_if_dirty(self, message: str) -> bool:
        """Commit any pending changes. Returns True if a commit was made.

        A failed commit raises: a snapshot that silently doesn't happen would
        void the every-mutation-is-revertible guarantee. Lock contention
        (another process committing the same repo) is retried briefly.
        """
        self.ensure_repo()
        error = ""
        for _attempt in range(3):
            if not self._git("status", "--porcelain").stdout.strip():
                return False
            r = self._git("add", "-A")
            if r.returncode == 0:
                r = self._git("commit", "-q", "-m", message)
                if r.returncode == 0:
                    return True
            error = (r.stderr or r.stdout).strip() or "unknown git error"
            if "index.lock" not in error:
                break
            # A git process killed mid-commit leaves index.lock behind
            # FOREVER — without this, every later snapshot fails and every
            # engine pass crashes until a human deletes the file. Only a
            # lock older than 5 minutes is removed: a live git holds its
            # lock for milliseconds, so age == abandonment.
            lock = self.root / ".git" / "index.lock"
            with contextlib.suppress(OSError):
                if time.time() - lock.stat().st_mtime > 300:
                    lock.unlink()
            time.sleep(0.2)
        raise VaultError(f"git snapshot failed: {error.splitlines()[0]}")

    def snapshot_before(self, tool: str) -> None:
        """Preserve any manual edits as their own commit before we touch files.
        Raises (aborting the mutation) if the snapshot can't be made."""
        self.commit_if_dirty(f"manual edits (before harness: {tool})")

    def snapshot_after(self, summary: str) -> None:
        message = f"harness: {summary}"
        if self.chat_id:
            # Trailer in the body: invisible to history()'s subject-only
            # display, readable via commit_message() for chat attribution.
            message += f"\n\nChat-Id: {self.chat_id}"
        try:
            self.commit_if_dirty(message)
        except VaultError as e:
            # The mutation itself succeeded; be precise so the model doesn't
            # retry the write. The change will ride in the next snapshot.
            raise VaultError(f"The change was applied, but its git snapshot failed: {e}") from None

    # \x1e separates commits, \x1f fields — bytes no commit subject or path
    # contains, so the parse needs no escaping. Ends with the file list from
    # --name-only.
    _LOG_FORMAT = "%x1e%H%x1f%s%x1f%cI%x1f%(trailers:key=Chat-Id,valueonly=true)"

    def history(self, limit: int = 15) -> list[dict]:
        """Recent commits, newest first: sha, subject, ISO date, the Chat-Id
        trailer, and the files touched — one git subprocess for all of it.

        The ONE git-log parser. The Changes surface had grown a second
        copy of this record-splitting for the sake of the trailer; two
        parsers of the same bespoke format is one too many, so the trailer
        lives here and both callers read the same shape. `subject` because
        that is what %s is — the full body is commit_message()."""
        self.ensure_repo()
        r = self._git("log", f"-{limit}", f"--pretty=format:{self._LOG_FORMAT}", "--name-only")
        if r.returncode != 0:
            return []
        commits = []
        for record in r.stdout.split("\x1e"):
            lines = [ln for ln in record.splitlines() if ln.strip()]
            if not lines or "\x1f" not in lines[0]:
                continue
            sha, subject, date, chat_id = lines[0].split("\x1f")
            commits.append({"sha": sha, "subject": subject, "date": date,
                            "chat_id": chat_id.strip(), "files": lines[1:]})
        return commits

    def commit_message(self, sha: str) -> str:
        """Full commit message body — history() exposes subjects only."""
        self.ensure_repo()
        r = self._git("log", "-1", "--format=%B", sha)
        return r.stdout.strip() if r.returncode == 0 else ""

    def revert(self, sha: str) -> tuple[bool, str]:
        """Revert one commit as a new commit. On conflict, abort cleanly and
        report — the vault is never left in a half-reverted state."""
        self.ensure_repo()
        with self.locked():
            try:
                self.commit_if_dirty("manual edits (before revert)")
            except VaultError as e:
                return False, str(e)
            r = self._git("revert", "--no-edit", sha)
            if r.returncode != 0:
                self._git("revert", "--abort")
                detail = (r.stderr or r.stdout).strip().splitlines()
                return False, detail[0] if detail else "revert failed"
            return True, ""


_vault: Vault | None = None


def get_vault() -> Vault:
    global _vault
    if _vault is None:
        root = os.environ.get("TEZCAT_VAULT_PATH")
        if not root:
            raise VaultError("TEZCAT_VAULT_PATH is not set")
        _vault = Vault(
            root,
            chat_id=os.environ.get("HARNESS_CHAT_ID", ""),
            exclude=get_config(root).exclude,
        )
    return _vault
