"""Harness/Changes.md — the audit surface (DEVPLAN B13, Phase 10).

Verified auto-apply means the harness edits the vault without a
checkbox; this surface answers "what changed while I wasn't looking"
without leaving Obsidian. It is a rendered view of the vault's own git
log — every row is one snapshot commit, so the web UI's ↺ Changes revert
always applies. Deliberately no revert checkbox (explicitly-cut list):
destructive actions don't ride text files yet.
"""

from __future__ import annotations

from obsidian_mcp.vault import Vault

HEADER = """\
# Changes

_Recent vault snapshots, newest first — everything the harness (or you,
between passes) committed. Every row is one git commit; revert from the
web UI's ↺ Changes. Rows made inside a chat turn carry its chat id._
"""


def recent_commits(v: Vault, limit: int) -> list[dict]:
    """Newest-first commits with subject, ISO time, files, and the
    Chat-Id trailer — one git subprocess for everything."""
    v.ensure_repo()
    r = v._git(
        "log", f"-{limit}",
        "--pretty=format:%x1e%H%x1f%s%x1f%cI%x1f%(trailers:key=Chat-Id,valueonly=true)",
        "--name-only",
    )
    if r.returncode != 0:
        return []
    commits = []
    for record in r.stdout.split("\x1e"):
        lines = record.splitlines()
        if not lines or "\x1f" not in lines[0]:
            continue
        _sha, subject, date, chat_id = lines[0].split("\x1f")
        commits.append({
            "subject": subject,
            "date": date,
            "chat_id": chat_id.strip(),
            "files": [ln for ln in lines[1:] if ln.strip()],
        })
    return commits


def render(v: Vault, changes_rel: str, limit: int = 20) -> str:
    """Deterministic markdown for the surface: absolute times (relative
    ones would churn the note every pass), the surface's own update
    commits filtered out so the render can never feed itself."""
    rows = []
    for c in recent_commits(v, limit + 10):  # headroom for filtered rows
        if c["files"] == [changes_rel]:
            continue  # this surface's own renders are not news
        when = c["date"][:16].replace("T", " ")
        files = ", ".join(f"`{f}`" for f in c["files"][:3])
        more = f" +{len(c['files']) - 3} more" if len(c["files"]) > 3 else ""
        chat = f" · chat `{c['chat_id'][:12]}`" if c["chat_id"] else ""
        rows.append(f"- **{when}** {c['subject']} — {files}{more}{chat}")
        if len(rows) >= limit:
            break
    return HEADER + "\n" + "\n".join(rows) + "\n"
