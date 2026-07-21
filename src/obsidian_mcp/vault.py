"""Vault filesystem access: path safety and git snapshotting.

The vault is treated as the API surface — every tool reads and writes
markdown files under one root. All paths handed to tools are
vault-relative; anything that resolves outside the root is rejected.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

IGNORED_DIRS = {".git", ".obsidian", ".trash"}


class VaultError(Exception):
    """Raised for user-facing vault problems (bad path, missing note)."""


class Vault:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise VaultError(f"Vault directory not found: {self.root}")

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
            if not any(part in IGNORED_DIRS for part in p.relative_to(self.root).parts):
                yield p

    def read(self, rel_path: str) -> str:
        return self.resolve(rel_path, must_exist=True).read_text(encoding="utf-8")

    def write(self, rel_path: str, content: str) -> Path:
        p = self.resolve(rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    # ---- git snapshots ----

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True, text=True,
        )

    def ensure_repo(self) -> None:
        if not (self.root / ".git").exists():
            self._git("init", "-q")
        if self._git("config", "user.email").returncode != 0:
            self._git("config", "user.email", "harness@local")
            self._git("config", "user.name", "harness")

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
        try:
            self.commit_if_dirty(f"harness: {summary}")
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

    def revert(self, sha: str) -> tuple[bool, str]:
        """Revert one commit as a new commit. On conflict, abort cleanly and
        report — the vault is never left in a half-reverted state."""
        self.ensure_repo()
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
        _vault = Vault(root)
    return _vault
