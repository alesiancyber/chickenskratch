"""Revertible writes for repo-side config (`config/*.yaml`).

Every other write the UI makes lands in the vault, where `Vault.locked()`
+ `snapshot_before/after` make it one git commit you can revert from the
dashboard. `config/` is not the vault: it holds endpoints, keys-by-name,
MCP server commands and entity schemas, and nothing there is covered by
that guarantee. A console that edits it without one would be the first
UI write you cannot take back — and the blast radius is every pass, not
one note.

So this module supplies the same shape by other means: validate, keep
the old bytes, then write. Backups go to `data/config-history/`, not to
the repo's git, on purpose — a config edit is not a source change, and
interleaving the two would make `git log` a worse record of both.

The contract each write keeps:

- validate FIRST, against the real loader, and refuse on failure. A
  models.yaml that doesn't parse breaks every pass and the chat pane
  with it, and the process that would tell you so is the one you just
  broke.
- snapshot the current bytes before replacing them, so "undo that" is
  always available and never depends on the new content being readable.
- write atomically, because a torn config file is indistinguishable
  from a malformed one at the next startup.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from chickenskratch_vault.vault import atomic_write_text

from .bootstrap import ROOT

HISTORY_DIR = ROOT / "data" / "config-history"
# Keep the file small and the list readable. Config edits are rare and
# the useful undo is nearly always the most recent one.
KEEP_PER_FILE = 20


class ConfigError(ValueError):
    """A rejected config write. The message is shown to the user, so it
    names what is wrong rather than where it was raised."""


@dataclass(frozen=True)
class Backup:
    stamp: str          # 2026-07-28T14-02-11-337, unique per write
    path: Path

    @property
    def when(self) -> str:
        """Readable, without the milliseconds that only exist to keep two
        writes in the same second apart."""
        date, _, clock = self.stamp.partition("T")
        return f"{date} {':'.join(clock.split('-')[:3])}"


def _slug(path: Path) -> str:
    """A flat, collision-free backup name.

    `config/models.yaml` -> `models.yaml`, `config/schemas/task.yaml` ->
    `schemas__task.yaml`. A path OUTSIDE the repo keeps just its name —
    this must never raise: the caller is mid-write, and a backup naming
    problem is not a reason to refuse a valid config change.
    """
    resolved = path.resolve()
    base = (ROOT / "config").resolve()
    rel = resolved.relative_to(base) if resolved.is_relative_to(base) else Path(resolved.name)
    return str(rel).replace("/", "__")


def history(path: Path) -> list[Backup]:
    """Backups for one config file, newest first."""
    if not HISTORY_DIR.is_dir():
        return []
    suffix = "_" + _slug(path)
    out = [Backup(stamp=p.name[: -len(suffix)], path=p)
           for p in HISTORY_DIR.glob(f"*{suffix}") if p.name.endswith(suffix)]
    return sorted(out, key=lambda b: b.stamp, reverse=True)


def removed(dir_path: Path) -> list[str]:
    """File names under dir_path that no longer exist but still have
    backups — what remove() left restorable. Without this the console's
    delete toast ("its backups remain restorable") would be a promise
    with no surface behind it: rows render from live files, so a deleted
    file would vanish along with its only path to restore().

    Membership is decided by round-trip, not prefix parsing: a backup
    belongs to dir_path exactly when its slug is what _slug would mint
    for that name in that dir — the same rule history() lives by."""
    if not HISTORY_DIR.is_dir():
        return []
    names = set()
    for p in HISTORY_DIR.glob("*.yaml"):
        _, sep, slug = p.name.partition("_")   # stamps never contain "_"
        name = slug.rsplit("__", 1)[-1]
        if sep and name and slug == _slug(dir_path / name):
            names.add(name)
    return sorted(n for n in names if not (dir_path / n).is_file())


def _snapshot(path: Path) -> None:
    """Keep the current bytes. A file that doesn't exist yet has nothing
    to keep — and restoring to "absent" is not something the console
    offers, so there is no empty marker to write."""
    if not path.is_file():
        return
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    # Milliseconds, and then a counter if even those collide. A restore
    # snapshots the bytes it is about to replace and usually lands in the
    # same second as the save it is undoing — at whole-second resolution
    # that write silently overwrote the very backup it was recovering.
    slug = _slug(path)
    base = dt.datetime.now().strftime("%Y-%m-%dT%H-%M-%S-%f")[:-3]
    stamp, n = base, 0
    while (HISTORY_DIR / f"{stamp}_{slug}").exists():
        n += 1
        stamp = f"{base}-{n}"
    shutil.copy2(path, HISTORY_DIR / f"{stamp}_{slug}")
    for old in history(path)[KEEP_PER_FILE:]:
        old.path.unlink(missing_ok=True)


def write(path: Path, text: str, validate=None) -> None:
    """Validate, back up, replace. `validate` takes the NEW text and
    raises ConfigError with a human-readable reason.

    Validation runs before the backup for a reason that is easy to get
    backwards: a rejected write must leave no trace at all, or the
    history fills with snapshots of a file nothing ever changed.
    """
    if validate is not None:
        validate(text)
    _snapshot(path)
    atomic_write_text(path, text)


def remove(path: Path) -> None:
    """Delete a config file, snapshotting the bytes first so the deletion
    is undoable — restore() writes a backup back whether or not the file
    still exists, so "undo the delete" is the same gesture as any undo."""
    if not path.is_file():
        raise ConfigError(f"{path.name} does not exist")
    _snapshot(path)
    path.unlink()


def restore(path: Path, stamp: str) -> None:
    """Put a backup back. The CURRENT bytes are snapshotted first, so a
    restore is itself undoable — otherwise the one action whose whole
    purpose is recovering from a mistake would be the one you cannot
    recover from."""
    if not re.fullmatch(r"[0-9T:_-]{1,32}", stamp):
        raise ConfigError("bad backup id")
    match = [b for b in history(path) if b.stamp == stamp]
    if not match:
        raise ConfigError(f"no backup {stamp!r} for {path.name}")
    text = match[0].path.read_text(encoding="utf-8")
    _snapshot(path)
    atomic_write_text(path, text)
