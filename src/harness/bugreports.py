"""Save a conversation as a self-contained bug report for later review.

The user hits "Report issue" mid-chat; this writes a markdown file under
data/bug_reports/ capturing their note plus the full transcript — user
turns, assistant replies, and every tool call *with its result*. Each
file is a ready-made example to hand to Claude Code, no reconstruction
needed.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from .bootstrap import ROOT

REPORTS_DIR = ROOT / "data" / "bug_reports"


def _fmt_args(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw or "{}"), ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        return raw or "{}"


def format_transcript(messages: list[dict]) -> str:
    """Render raw agent messages to readable markdown, tool results included
    (unlike the UI transcript, which hides them). The system prompt is dropped."""
    out: list[str] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            continue
        if role == "user":
            out.append(f"### 🧑 User\n\n{m.get('content', '').strip()}")
        elif role == "assistant":
            if m.get("content"):
                out.append(f"### 🤖 Assistant\n\n{m['content'].strip()}")
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function", {})
                out.append(f"**→ tool call** `{fn.get('name')}` `{_fmt_args(fn.get('arguments', ''))}`")
        elif role == "tool":
            body = (m.get("content") or "").strip()
            if len(body) > 2000:
                body = body[:2000] + "\n…(truncated)"
            out.append(f"**← tool result**\n\n```\n{body}\n```")
    return "\n\n".join(out)


def save_report(
    description: str,
    model: str,
    messages: list[dict],
    stats: dict | None = None,
    servers: dict | None = None,
    reports_dir: Path | None = None,
) -> Path:
    reports_dir = reports_dir or REPORTS_DIR
    reports_dir.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now()
    slug = re.sub(r"[^\w-]+", "-", (description or "issue").lower()).strip("-")[:40] or "issue"
    path = reports_dir / f"{now.strftime('%Y%m%d-%H%M%S')}-{slug}.md"

    head = [
        f"# Bug report: {description.strip() or '(no description)'}",
        "",
        f"- **When:** {now.isoformat(timespec='seconds')}",
        f"- **Model:** {model or '(unknown)'}",
    ]
    if stats:
        head.append(
            f"- **Tokens:** {stats.get('input_tokens', 0)} in / {stats.get('output_tokens', 0)} out"
            + (" (estimated)" if stats.get("estimated") else "")
        )
    if servers:
        alive = ", ".join(n for n, s in servers.items() if (s or {}).get("state") == "connected")
        head.append(f"- **Servers:** {alive or '(none connected)'}")
    head += ["", "---", "", "## Conversation", ""]

    path.write_text("\n".join(head) + "\n" + format_transcript(messages) + "\n", encoding="utf-8")
    return path


def list_reports(reports_dir: Path | None = None) -> list[dict]:
    reports_dir = reports_dir or REPORTS_DIR
    if not reports_dir.is_dir():
        return []
    out = []
    for p in sorted(reports_dir.glob("*.md"), reverse=True):
        first = p.read_text(encoding="utf-8").splitlines()[:1]
        title = first[0].lstrip("# ").strip() if first else p.stem
        out.append({"file": p.name, "title": title, "updated": dt.datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds")})
    return out
