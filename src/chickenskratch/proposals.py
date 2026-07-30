"""Proposal queue for the review dashboard.

One JSON file per proposal under data/proposals/ — human-skimmable
singles, like bug reports (high-volume append logs stay JSONL). Every
file is validated against config/schemas/proposal.yaml on save: junk
never enters the queue. data/ is authoritative and the only record:
the dashboard and `chickenskratch decide` both read the pending set straight
from here.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path

from chickenskratch_vault import ids, jsonval
from chickenskratch_vault.schemas import load_schemas
from chickenskratch_vault.vault import atomic_write_text

from .bootstrap import ROOT, vault_digest

PROPOSALS_DIR = ROOT / "data" / "proposals"
SCHEMA_VERSION = 1
RETENTION_DAYS = 30  # resolved proposals prune; pending ones never do


def dir_for(vault_path: str) -> Path:
    """The per-vault queue directory — one derivation for the engine and
    the UI server, keyed like every other per-vault state file."""
    return ROOT / "data" / f"proposals-{vault_digest(vault_path)}"


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
    # Atomic: the UI reads this directory from a threadpool thread while a
    # pass writes it, and load_all() SKIPS a file it can't parse — so a
    # torn write didn't corrupt a proposal, it deleted one.
    return atomic_write_text(d / f"{proposal['id']}.json",
                             json.dumps(proposal, indent=2, ensure_ascii=False))


def load_all(proposals_dir: Path | None = None) -> list[dict]:
    d = Path(proposals_dir) if proposals_dir else PROPOSALS_DIR
    if not d.is_dir():
        return []
    schema = load_schemas().get("proposal")
    out = []
    for p in sorted(d.glob("p-*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue  # a corrupt file must not take the queue down
        # Valid JSON in the wrong shape (hand-edited, truncated) must be
        # skipped too: pending()'s data["status"] on a shapeless dict once
        # meant one bad file wedged every pass.
        if not isinstance(data, dict) or (schema and jsonval.validate(data, schema)):
            print(f"skipping malformed proposal file {p.name}", file=sys.stderr)
            continue
        out.append(data)
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
            # stat inside the try: a file deleted mid-prune (concurrent
            # prune, hand cleanup) must not crash the end of a pass
            if data.get("status") != "pending" and p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                removed += 1
        except (json.JSONDecodeError, OSError):
            continue
    return removed
