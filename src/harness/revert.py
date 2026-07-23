"""Revert a vault snapshot from the terminal — the web UI's ↺ Changes,
CLI-ized (the UI retirement, 2026-07-23).

Every harness mutation is one git commit, so undo is `git revert` under
the vault lock with manual edits preserved first (vault.py owns that).
Run bare to list recent snapshots, then pass the sha to revert:

    harness-revert                # list recent snapshots
    harness-revert 3f2a91c        # revert that snapshot (a new commit)

A revert is itself a commit, so a wrong revert is revertible the same way.
"""

from __future__ import annotations

import argparse
import re

from obsidian_mcp.vault import Vault

from .bootstrap import load_runtime


def list_snapshots(v: Vault, limit: int) -> None:
    commits = v.history(limit)
    if not commits:
        print("no snapshots yet")
        return
    for c in commits:
        files = ", ".join(c["files"][:3]) + (f" +{len(c['files']) - 3}" if len(c["files"]) > 3 else "")
        print(f"{c['sha'][:10]}  {c['date'][:16]}  {c['message'][:60]}  [{files}]")


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
    v = Vault(vault)
    if not args.sha:
        list_snapshots(v, args.limit)
        return
    sha = args.sha.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,40}", sha):
        raise SystemExit(f"not a commit sha: {args.sha!r}")
    subject = v.commit_message(sha).splitlines()
    if not subject:
        raise SystemExit(f"no such commit in {vault}: {sha}")
    print(f"reverting: {subject[0]}")
    ok, error = v.revert(sha)
    if not ok:
        raise SystemExit(f"revert failed: {error}")
    print("reverted (as a new snapshot — revertible the same way)")


if __name__ == "__main__":
    main()
