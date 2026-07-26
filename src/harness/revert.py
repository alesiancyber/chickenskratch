"""Revert a vault snapshot from the terminal.

Every harness mutation is one git commit, so undo is `git revert` under
the vault lock with manual edits preserved first (vault.py owns that).
Run bare to list recent snapshots, then pass the sha to revert:

    harness-revert                # list recent snapshots
    harness-revert 3f2a91c        # revert that snapshot (a new commit)

A revert is itself a commit, so a wrong revert is revertible the same way.
"""

from __future__ import annotations

import argparse

from obsidian_mcp.vault import Vault

from .bootstrap import load_runtime
from .service import revert as service_revert


def list_snapshots(v: Vault, limit: int) -> None:
    commits = v.history(limit)
    if not commits:
        print("no snapshots yet")
        return
    for c in commits:
        files = ", ".join(c["files"][:3]) + (f" +{len(c['files']) - 3}" if len(c["files"]) > 3 else "")
        print(f"{c['sha'][:10]}  {c['date'][:16]}  {c['subject'][:60]}  [{files}]")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="harness-revert",
        description="Revert one vault snapshot as a new commit; run without a sha to list snapshots",
    )
    parser.add_argument("sha", nargs="?", help="commit sha to revert (from the bare listing)")
    parser.add_argument("-v", "--vault", help="vault path (default: config, or $OBSIDIAN_VAULT_PATH)")
    parser.add_argument("-n", "--limit", type=int, default=15, help="snapshots to list (default 15)")
    args = parser.parse_args()

    _config, vault, _servers = load_runtime(vault=args.vault)
    if not args.sha:
        list_snapshots(Vault(vault), args.limit)
        return
    # service.revert is the one revert path (CLI now, UI undo in Phase 2)
    try:
        subject = service_revert(vault, args.sha)
    except (ValueError, RuntimeError) as e:
        raise SystemExit(str(e)) from e
    print(f"reverted: {subject}")
    print("(as a new snapshot — revertible the same way)")


if __name__ == "__main__":
    main()
