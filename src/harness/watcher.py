"""The watcher: a pipeline, not a process (DESIGN-review-dashboard.md §1).

One pass = act on Review.md checkboxes → (if the vault moved) scan for
nonconforming entities and draft up to N proposals → re-render
Harness/Review.md → memoize the vault's git state so an unchanged vault
makes the next pass free. No daemon: the web server triggers a pass when
Review.md changes, and `uv run harness-watch` runs one by hand or cron.

Stage 1 (scan) is deterministic code — the same validate_schema function
the MCP tool exposes. Stage 2 (draft) is a bounded worker call that
writes only to the proposal queue; the vault is touched exclusively at
approval time, through the gated tools, via the applier.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path

from obsidian_mcp import ids
from obsidian_mcp import workers as wk
from obsidian_mcp.vault import Vault, VaultError
from obsidian_mcp.vaultconfig import get_config

from . import applier, proposals
from .bootstrap import ROOT
from .corpus import record_dashboard

STATE_PATH = ROOT / "data" / "watcher_state.json"
N_MAX = 5  # proposals drafted per pass: a big backlog drains over days

DECISION_RE = re.compile(r"^- \[[xX]\] (approve|reject) (p-[0-9a-f]{12})\s*$", re.M)

# entity type -> id prefix for the note-level watermark the drafter stamps
ID_PREFIX = {"meeting": "m", "research": "r", "kanban": "b"}


async def draft_schema_fix(item: dict) -> dict:
    """Default drafter: one bounded worker call -> {title, summary, apply}."""
    data = await wk.run_worker(
        wk.WORKERS["schema_fix"],
        note=item["note"],
        entity=item["entity"],
        issues="\n".join(f"- {i}" for i in item["issues"]),
        content=item["content"],
        new_id=item["new_id"],
        extra_ids=", ".join(item["extra_ids"]),
    )
    return data


class Watcher:
    def __init__(
        self,
        vault_path: str,
        proposals_dir: Path | None = None,
        state_path: Path | None = None,
        drafter=None,
        n_max: int = N_MAX,
    ):
        self.vault_path = str(vault_path)
        self.proposals_dir = proposals_dir
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self.drafter = drafter or draft_schema_fix
        self.n_max = n_max
        cfg = get_config()
        self.review_rel = cfg.surfaces.get("review", "Harness/Review.md")

    # ---- git memo ----

    def _vault_fingerprint(self) -> dict:
        sha, clean = applier.vault_state(self.vault_path)
        v = Vault(self.vault_path)
        dirty = v._git("status", "--porcelain").stdout if not clean else ""
        return {"head": sha, "dirty": proposals.content_hash(dirty)}

    def _state_changed(self, fingerprint: dict) -> bool:
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return True
        return saved != fingerprint

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._vault_fingerprint()), encoding="utf-8")

    # ---- stage 1: scan ----

    def _scan(self) -> list[dict]:
        """Deterministic work items for notes not already covered by a
        pending proposal. Harness/ and Private/ are excluded by the
        validate_schema implementation itself."""
        applier.use_vault(self.vault_path)
        from obsidian_mcp import server

        covered = {p["drafted_against"]["note"] for p in proposals.pending(self.proposals_dir)}
        v = Vault(self.vault_path)
        sha, _clean = applier.vault_state(self.vault_path)
        items = []
        for row in server.validate_schema(limit=10_000)["nonconforming"]:
            if row["note"] in covered:
                continue
            items.append({
                "note": row["note"],
                "entity": row["entity"],
                "issues": row.get("issues", [])
                + [f"line {t['line']}: {i}" for t in row.get("tasks", []) for i in t["issues"]],
                "content": v.read(row["note"]),
                "vault_sha": sha,
                "new_id": ids.stamp_id(ID_PREFIX.get(row["entity"], "n")),
                "extra_ids": [ids.stamp_id("t") for _ in range(len(row.get("tasks", [])))],
            })
        return items

    # ---- stage 2: draft ----

    async def _draft(self, items: list[dict]) -> int:
        drafted = 0
        for item in items[: self.n_max]:
            try:
                reply = await self.drafter(item)
            except VaultError:
                break  # worker down/misbehaving: stop drafting, pass still completes
            p = proposals.new_proposal(
                kind="schema_fix",
                note=item["note"],
                vault_sha=item["vault_sha"],
                note_content=item["content"],
                title=reply["title"],
                summary=reply["summary"],
                issues=item["issues"],
                apply=reply["apply"],
            )
            try:
                proposals.save(p, self.proposals_dir)
            except ValueError:
                continue  # drafter emitted an invalid plan: never enters the queue
            drafted += 1
        return drafted

    # ---- decisions from the vault surface ----

    def _read_decisions(self) -> list[tuple[str, str]]:
        v = Vault(self.vault_path)
        try:
            text = v.read(self.review_rel)
        except VaultError:
            return []
        return [(m.group(2), m.group(1)) for m in DECISION_RE.finditer(text)]

    def _act(self, decisions: list[tuple[str, str]]) -> dict:
        acted = {"approved": 0, "rejected": 0, "stale": 0}
        by_id = {p["id"]: p for p in proposals.pending(self.proposals_dir)}
        for pid, decision in decisions:
            p = by_id.get(pid)
            if p is None:
                continue  # already resolved, or a stray id — never act twice
            if decision == "reject":
                proposals.resolve(p, "rejected", self.proposals_dir)
                self._record(p, "rejected")
                acted["rejected"] += 1
                continue
            reason = applier.is_stale(p, self.vault_path)
            if reason:
                proposals.resolve(p, "stale", self.proposals_dir)
                acted["stale"] += 1
                continue
            applier.apply(p, self.vault_path)
            proposals.resolve(p, "approved", self.proposals_dir)
            self._record(p, "approved")
            acted["approved"] += 1
        return acted

    @staticmethod
    def _record(p: dict, decision: str) -> None:
        try:
            record_dashboard(p, decision)
        except OSError:
            pass  # labels are telemetry; never fail the pass over them

    # ---- queue hygiene ----

    def _auto_stale(self) -> int:
        """Pending proposals whose target note changed (fixed by hand or in
        chat) flip to stale instead of lingering with a wrong diff."""
        flipped = 0
        for p in proposals.pending(self.proposals_dir):
            if applier.is_stale(p, self.vault_path):
                proposals.resolve(p, "stale", self.proposals_dir)
                flipped += 1
        return flipped

    # ---- render ----

    def _render(self) -> bool:
        """Rewrite Review.md from the pending set. Deterministic (no
        timestamps), written only on change, committed as its own snapshot
        so user edits are never swept into a harness commit."""
        v = Vault(self.vault_path)
        rows = proposals.pending(self.proposals_dir)
        lines = [
            "# Review",
            "",
            f"_{len(rows)} pending proposal(s). Check a box; the harness acts on its next pass "
            "and this note re-renders. Nothing is ever applied without a check._",
        ]
        for p in sorted(rows, key=lambda r: r["created"]):
            try:
                note_text = v.read(p["drafted_against"]["note"])
                diff = applier.preview(note_text, p)
            except VaultError:
                diff = "# (target note unreadable — proposal will go stale)"
            lines += [
                "",
                "---",
                "",
                f"## {p['title']}",
                "",
                f"`{p['drafted_against']['note']}` — {p['summary']}",
                "",
                *[f"- {i}" for i in p["issues"]],
                "",
                "```diff",
                diff,
                "```",
                "",
                f"- [ ] approve {p['id']}",
                f"- [ ] reject {p['id']}",
            ]
        content = "\n".join(lines) + "\n"
        try:
            if v.read(self.review_rel) == content:
                return False
        except VaultError:
            pass
        v.snapshot_before("render_review")
        v.write(self.review_rel, content)
        v.snapshot_after(f"update {self.review_rel}")
        return True

    # ---- one pass ----

    async def run_once(self) -> dict:
        summary = {"drafted": 0, "approved": 0, "rejected": 0, "stale": 0, "scanned": False}
        acted = self._act(self._read_decisions())
        summary.update(acted)
        fingerprint = self._vault_fingerprint()
        if self._state_changed(fingerprint):
            summary["stale"] += self._auto_stale()
            summary["drafted"] = await self._draft(self._scan())
            summary["scanned"] = True
        self._render()
        self._save_state()
        proposals.prune(self.proposals_dir)
        return summary


def main() -> None:
    from .bootstrap import load_runtime

    parser = argparse.ArgumentParser(
        prog="harness-watch",
        description="One watcher pass: act on Review.md checkboxes, scan, draft, re-render",
    )
    parser.add_argument("-v", "--vault", help="vault path (default: config, or $OBSIDIAN_VAULT_PATH)")
    parser.add_argument("--n-max", type=int, default=N_MAX, help="max proposals drafted this pass")
    args = parser.parse_args()
    _config, vault, _servers = load_runtime(vault=args.vault)
    summary = asyncio.run(Watcher(vault, n_max=args.n_max).run_once())
    print(
        f"watch pass: drafted {summary['drafted']}, applied {summary['approved']}, "
        f"rejected {summary['rejected']}, stale {summary['stale']}"
        + ("" if summary["scanned"] else " (vault unchanged — scan skipped)")
    )


if __name__ == "__main__":
    main()
