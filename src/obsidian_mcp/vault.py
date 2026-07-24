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


class VaultError(Exception):
    """Raised for user-facing vault problems (bad path, missing note)."""


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
            raise VaultError(f"{top[0]}/ is excluded from harness access (vault.yaml exclude)")
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

    def read(self, rel_path: str) -> str:
        return self.resolve(rel_path, must_exist=True).read_text(encoding="utf-8")

    def write(self, rel_path: str, content: str) -> Path:
        p = self.resolve(rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Temp file + rename: an interrupted write (crash, disk full, kill)
        # must never leave a note half-written — the git snapshots only
        # protect content that made it to a commit intact.
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, p)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        return p

    # ---- cross-process lock ----

    @contextlib.contextmanager
    def locked(self):
        """Mutex for a whole snapshot→mutate→snapshot sequence.

        Several processes mutate one vault (MCP tools, capture, the watcher,
        revert); without this, one writer's `git add -A` snapshot can sweep
        another writer's half-finished edit into the wrong commit. The lock
        lives outside the vault so snapshots never pick it up, and flock is
        released automatically if the holder dies."""
        digest = hashlib.sha1(str(self.root).encode()).hexdigest()[:12]
        lock_path = Path(tempfile.gettempdir()) / f"obsidian-harness-{digest}.lock"
        with open(lock_path, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
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
            self._git("config", "user.name", "harness")
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

    def history(self, limit: int = 15) -> list[dict]:
        """Recent commits, newest first: sha, message, ISO date, files touched."""
        self.ensure_repo()
        r = self._git(
            "log", f"-{limit}", "--pretty=format:%x1e%H%x1f%s%x1f%cI", "--name-only"
        )
        if r.returncode != 0:
            return []
        commits = []
        for record in r.stdout.split("\x1e"):
            lines = [ln for ln in record.splitlines() if ln.strip()]
            if not lines or "\x1f" not in lines[0]:
                continue
            sha, message, date = lines[0].split("\x1f")
            commits.append({"sha": sha, "message": message, "date": date, "files": lines[1:]})
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
        root = os.environ.get("OBSIDIAN_VAULT_PATH")
        if not root:
            raise VaultError("OBSIDIAN_VAULT_PATH is not set")
        _vault = Vault(
            root,
            chat_id=os.environ.get("HARNESS_CHAT_ID", ""),
            exclude=get_config(root).exclude,
        )
    return _vault
