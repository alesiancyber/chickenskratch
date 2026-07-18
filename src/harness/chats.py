"""Conversation persistence: JSON files under data/chats/, pruned after
a few days (user decision: the vault is the archive, chats are scratch).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import uuid
from pathlib import Path

from .agent import NUDGE_TEXT
from .bootstrap import ROOT

CHATS_DIR = ROOT / "data" / "chats"
RETENTION_DAYS = 3

_WORKFLOW_RE = re.compile(r'\AFollow this workflow now\.\n\n<workflow name="([\w-]+)">', re.S)
_WORKFLOW_INPUT_RE = re.compile(r"User input for this workflow: (.*)\Z", re.S)


def display_user_text(content: str) -> str:
    """Collapse an expanded slash-command back to its short form for display."""
    m = _WORKFLOW_RE.match(content)
    if not m:
        return content
    user_input = _WORKFLOW_INPUT_RE.search(content)
    return f"/{m.group(1)}" + (f" {user_input.group(1).strip()}" if user_input else "")


def transcript(messages: list[dict]) -> list[dict]:
    """Render agent history as UI-friendly entries (user/assistant/tool)."""
    out = []
    for m in messages[1:]:  # skip system prompt
        if m["role"] == "user":
            if m["content"] == NUDGE_TEXT:
                continue
            out.append({"role": "user", "content": display_user_text(m["content"])})
        elif m["role"] == "assistant":
            for tc in m.get("tool_calls") or []:
                try:
                    args = json.loads(tc["function"]["arguments"] or "{}")
                except (json.JSONDecodeError, KeyError):
                    args = {}
                out.append({"role": "tool", "name": tc["function"]["name"], "args": args})
            if m["content"]:
                out.append({"role": "assistant", "content": m["content"]})
    return out


class ChatStore:
    def __init__(self, root: Path = CHATS_DIR, retention_days: int = RETENTION_DAYS):
        self.root = root
        self.retention_days = retention_days
        self.root.mkdir(parents=True, exist_ok=True)

    def new_id(self) -> str:
        return dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]

    def _path(self, chat_id: str) -> Path:
        safe = re.sub(r"[^\w-]", "", chat_id)
        return self.root / f"{safe}.json"

    def save(self, chat_id: str, model: str, messages: list[dict]) -> None:
        title = next(
            (display_user_text(m["content"]) for m in messages if m["role"] == "user"),
            "(untitled)",
        )
        data = {
            "id": chat_id,
            "model": model,
            "title": title[:60],
            "updated": dt.datetime.now().isoformat(timespec="seconds"),
            "messages": messages,
        }
        self._path(chat_id).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def load(self, chat_id: str) -> dict | None:
        p = self._path(chat_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def list(self) -> list[dict]:
        self.prune()
        chats = []
        for p in self.root.glob("*.json"):
            data = self.load(p.stem)
            if data:
                chats.append({k: data[k] for k in ("id", "model", "title", "updated")})
        return sorted(chats, key=lambda c: c["updated"], reverse=True)

    def prune(self) -> int:
        """Delete chats older than the retention window. Returns count removed."""
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        for p in self.root.glob("*.json"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                removed += 1
        return removed
