"""Proposal queue for the review dashboard.

One JSON file per proposal under data/proposals/ — human-skimmable
singles, like bug reports (high-volume append logs stay JSONL). Every
file is validated against config/schemas/proposal.yaml on save: junk
never enters the queue. data/ stays authoritative; Harness/Review.md is
a rendered view of the pending set.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import time
from pathlib import Path

from obsidian_mcp import ids, jsonval
from obsidian_mcp.schemas import load_schemas

from .bootstrap import ROOT

PROPOSALS_DIR = ROOT / "data" / "proposals"
SCHEMA_VERSION = 1
RETENTION_DAYS = 30  # resolved proposals prune; pending ones never do


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def new_proposal(
    kind: str,
    note: str,
    vault_sha: str,
    note_content: str,
    title: str,
    summary: str,
    issues: list[str],
    apply: list[dict],
    entity_ids: list[str] | None = None,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": ids.stamp_id("p"),
        "kind": kind,
        "status": "pending",
        "created": dt.datetime.now().isoformat(timespec="seconds"),
        "drafted_against": {
            "vault_sha": vault_sha,
            "note": note,
            "content_hash": content_hash(note_content),
            **({"entity_ids": entity_ids} if entity_ids else {}),
        },
        "title": title,
        "summary": summary,
        "issues": issues,
        "apply": apply,
        "resolved": None,
    }


def save(proposal: dict, proposals_dir: Path | None = None) -> Path:
    schema = load_schemas().get("proposal")
    issues = jsonval.validate(proposal, schema) if schema else []
    if issues:
        raise ValueError(f"invalid proposal: {'; '.join(issues[:3])}")
    d = Path(proposals_dir) if proposals_dir else PROPOSALS_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{proposal['id']}.json"
    path.write_text(json.dumps(proposal, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_all(proposals_dir: Path | None = None) -> list[dict]:
    d = Path(proposals_dir) if proposals_dir else PROPOSALS_DIR
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("p-*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue  # a corrupt file must not take the queue down
    return out


def pending(proposals_dir: Path | None = None) -> list[dict]:
    return [p for p in load_all(proposals_dir) if p["status"] == "pending"]


def resolve(proposal: dict, decision: str, proposals_dir: Path | None = None) -> dict:
    """Mark approved/rejected/stale and persist. Returns the updated dict."""
    proposal = {
        **proposal,
        "status": decision,
        "resolved": {"ts": dt.datetime.now().isoformat(timespec="seconds"), "decision": decision},
    }
    save(proposal, proposals_dir)
    return proposal


def prune(proposals_dir: Path | None = None, retention_days: int = RETENTION_DAYS) -> int:
    """Delete resolved proposal files older than the retention window."""
    d = Path(proposals_dir) if proposals_dir else PROPOSALS_DIR
    if not d.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for p in d.glob("p-*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("status") != "pending" and p.stat().st_mtime < cutoff:
            p.unlink(missing_ok=True)
            removed += 1
    return removed
