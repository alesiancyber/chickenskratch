"""The processor engine: a pipeline, not a process (DESIGN-processors.md,
DESIGN-review-dashboard.md §1).

One pass = act on Review.md checkboxes → (if the vault moved) run every
registered processor's deterministic `scan`, draft up to N proposals via
their bounded `draft` calls → re-render Harness/Review.md → memoize the
vault's git state so an unchanged vault makes the next pass free. No
daemon yet: the web server triggers a pass when a harness surface
changes, and `uv run harness-watch` runs one by hand or cron.

The engine knows no processor by name — registry order encodes priority
(capture first: active user input never starves), and per-processor
behavior rides on ProcessorSpec hooks (ensure / on_applied /
render_diff). Passes are serialized by an exclusive flock so concurrent
triggers (web loop + CLI + future daemon) can never double-apply.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import re
from pathlib import Path

from obsidian_mcp import markdown as md
from obsidian_mcp import vaultconfig as vc
from obsidian_mcp import workers as wk
from obsidian_mcp.vault import Vault, VaultError
from obsidian_mcp.vaultconfig import get_config

from . import applier, changes, diffhtml, processors, proposals
from .bootstrap import ROOT
from .corpus import record_dashboard
from .processors import ProcessorSpec

STATE_PATH = ROOT / "data" / "watcher_state.json"
N_MAX = 5  # proposals drafted per pass: a big backlog drains over days

DECISION_RE = re.compile(r"^- \[[xX]\] (approve|reject) (p-[0-9a-f]{12})\s*$", re.M)

SPEC_BODY = """\
# Vault Spec

The properties above ARE this vault's configuration — the folder→type
map, the capture/waiting inboxes, and the model-context exclusions.
Edit them here (Obsidian's properties panel works); the engine reads
this note at the start of every pass, no restart needed. Unlike
everything else in Harness/, this note is yours: seeded once from the
repo defaults, never regenerated.

Folders are workspaces, not a taxonomy: the map supplies the default
type for untyped notes, and a note's own frontmatter always wins. When
a new top-level folder appears in the vault, a proposal to map it shows
up in [[Review]].

auto_apply lists the processors trusted to act without a checkbox —
honored only for mechanical transformations that pass a deterministic
fidelity check (your words only, existing lines byte-identical); any
doubt falls back to [[Review]]. Judgment calls always queue.
"""


async def draft_schema_fix(item: dict) -> dict:
    """Default schema drafter: one bounded worker call -> {title, summary, apply}."""
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


async def draft_grouping(item: dict) -> dict:
    """Default grouping drafter: the model only ever chooses a partition."""
    return await wk.run_worker(
        wk.WORKERS["grouping"],
        tasks="\n".join(f"{t.block_id}: {t.text}" for t in item["tasks"]),
    )


class Watcher:
    def __init__(
        self,
        vault_path: str,
        proposals_dir: Path | None = None,
        state_path: Path | None = None,
        drafter=None,
        n_max: int = N_MAX,
        disabled: tuple[str, ...] = (),
        registry: tuple[ProcessorSpec, ...] | None = None,
        grouping_drafter=None,
        capture_extractor=None,
    ):
        self.vault_path = str(vault_path)
        self.vault = Vault(self.vault_path)  # stateless handle, built once
        self.proposals_dir = proposals_dir
        self.state_path = Path(state_path) if state_path else STATE_PATH
        self.n_max = n_max
        # Worker injection points (tests script these; prod uses the
        # bounded run_worker defaults). Processors reach them via `w.`.
        self.drafter = drafter or draft_schema_fix
        self.grouping_drafter = grouping_drafter or draft_grouping
        self.capture_extractor = capture_extractor or wk.extract_tasks
        # The registry: explicit list wins (tests, future custom sets);
        # otherwise the default set minus `disabled` (the rollback flag).
        self.processors = registry if registry is not None else processors.registry(disabled)
        self._by_kind = {p.name: p for p in self.processors}
        cfg = get_config(self.vault_path)
        self.review_rel = cfg.surfaces.get("review", "Harness/Review.md")
        self.capture_rel = cfg.surfaces.get("capture", "")
        self.changes_rel = cfg.surfaces.get("changes", "")
        self.capture_inbox = cfg.capture_inbox
        self.waiting_inbox = cfg.waiting_inbox
        # hashes of text last handed to the extractors: rejecting a
        # proposal must not re-draft the same text every pass
        self._capture_seen = ""
        self._inbox_seen = ""
        # HEAD sha at scan time, stamped into drafted proposals; set once
        # per pass so processors never re-run git themselves
        self._pass_sha = ""

    # ---- shared machinery processors ride on ----

    def write_surface(self, rel: str, content: str, message: str,
                      action: str = "write_surface") -> None:
        """The one code path that writes a Harness surface: locked and
        snapshot-bracketed, so no hook can forget the git guarantee."""
        with self.vault.locked():
            self.vault.snapshot_before(action)
            self.vault.write(rel, content)
            self.vault.snapshot_after(message)

    def trigger_paths(self) -> list[Path]:
        """Files a serving client polls between passes: every configured
        surface (checkbox decisions and capture input live there) plus the
        vault's own Spec.md, so a spec edit takes effect promptly. The
        daemon covers whole-vault change detection."""
        root = Path(self.vault_path)
        paths = [root / rel for rel in get_config(self.vault_path).surfaces.values()]
        return paths + [root / vc.SPEC_REL]

    def _ensure_spec(self) -> None:
        """Seed Harness/Spec.md from the repo defaults on a vault that
        lacks one. Written exactly once — from then on the note is the
        user's, and get_config() reads it fresh each pass."""
        if self.vault.resolve(vc.SPEC_REL).exists():
            return
        base = get_config(self.vault_path)
        fm = {
            "folders": dict(base.folder_types),
            "capture_inbox": base.capture_inbox,
            "waiting_inbox": base.waiting_inbox,
            "exclude": list(base.exclude),
            "auto_apply": list(base.auto_apply),
            "fidelity": base.fidelity,
            "diff_style": base.diff_style,
        }
        self.write_surface(vc.SPEC_REL, md.join_frontmatter(fm, SPEC_BODY),
                           f"seed {vc.SPEC_REL}", action="seed_vault_spec")

    # ---- git memo ----

    def _vault_fingerprint(self, state: tuple[str, str] | None = None) -> dict:
        sha, dirty = state if state is not None else applier.vault_state(self.vault_path)
        return {"head": sha, "dirty": proposals.content_hash(dirty)}

    def _load_state(self) -> dict:
        try:
            saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return saved if "fingerprint" in saved else {}

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "fingerprint": self._vault_fingerprint(),
            "capture_seen": self._capture_seen,
            "inbox_seen": self._inbox_seen,
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    # ---- stage 2: draft ----

    async def _draft(self, items: list[tuple[ProcessorSpec, dict]]) -> tuple[int, int]:
        """Draft up to n_max proposals; verified-safe drafts from trusted
        processors apply immediately. Returns (drafted, auto_applied)."""
        auto_trusted = set(get_config(self.vault_path).auto_apply)
        drafted = auto = 0
        for proc, item in items[: self.n_max]:
            try:
                p = await proc.draft(self, item)
            except VaultError:
                break  # worker down/misbehaving: stop drafting, pass still completes
            if p is None:
                continue
            try:
                proposals.save(p, self.proposals_dir)
            except ValueError:
                continue  # drafter emitted an invalid plan: never enters the queue
            drafted += 1
            # Auto-apply: only processors the vault's Spec.md trusts, only
            # with a verifier, only when it passes, only against an unmoved
            # note. Any failure means the proposal simply keeps its
            # checkbox — verification degrades to review, never blocks.
            if proc.name in auto_trusted and proc.verify:
                if proc.verify(self, item, p) or applier.is_stale(p, self.vault_path):
                    continue
                applier.apply(p, self.vault_path)
                # No corpus row: auto-applies are not human labels.
                proposals.resolve(p, "auto", self.proposals_dir)
                auto += 1
                if proc.on_applied:
                    proc.on_applied(self, p)
        return drafted, auto

    # ---- decisions from the vault surface ----

    def _read_decisions(self) -> list[tuple[str, str]]:
        try:
            text = self.vault.read(self.review_rel)
        except VaultError:
            return []
        return [(m.group(2), m.group(1)) for m in DECISION_RE.finditer(text)]

    def _act(self, decisions: list[tuple[str, str]], pending: list[dict]) -> tuple[dict, list[dict]]:
        """Act on checked boxes. Returns (counts, proposals still pending)."""
        acted = {"approved": 0, "rejected": 0, "stale": 0}
        resolved: set[str] = set()
        by_id = {p["id"]: p for p in pending}
        for pid, decision in decisions:
            p = by_id.get(pid)
            if p is None or pid in resolved:
                continue  # already resolved, or a stray id — never act twice
            resolved.add(pid)
            if decision == "reject":
                proposals.resolve(p, "rejected", self.proposals_dir)
                self._record(p, "rejected")
                acted["rejected"] += 1
                continue
            # staleness re-checks fresh state per approval: each apply
            # mutates the vault, so nothing here is hoistable
            reason = applier.is_stale(p, self.vault_path)
            if reason:
                proposals.resolve(p, "stale", self.proposals_dir)
                acted["stale"] += 1
                continue
            applier.apply(p, self.vault_path)
            proposals.resolve(p, "approved", self.proposals_dir)
            self._record(p, "approved")
            acted["approved"] += 1
            spec = self._by_kind.get(p["kind"])
            if spec and spec.on_applied:
                spec.on_applied(self, p)
        return acted, [p for p in pending if p["id"] not in resolved]

    @staticmethod
    def _record(p: dict, decision: str) -> None:
        try:
            record_dashboard(p, decision)
        except OSError:
            pass  # labels are telemetry; never fail the pass over them

    # ---- queue hygiene ----

    def _auto_stale(self, pending: list[dict], state: tuple[str, str]) -> tuple[int, list[dict]]:
        """Pending proposals whose target note changed (fixed by hand or in
        chat) flip to stale instead of lingering with a wrong diff. The
        vault doesn't move during this loop, so one precomputed state
        serves every check. Returns (flipped, survivors)."""
        flipped, keep = 0, []
        for p in pending:
            if applier.is_stale(p, self.vault_path, state):
                proposals.resolve(p, "stale", self.proposals_dir)
                flipped += 1
            else:
                keep.append(p)
        return flipped, keep

    # ---- render ----

    def _render(self) -> bool:
        """Rewrite Review.md from the pending set. Deterministic (no
        timestamps), written only on change, committed as its own snapshot
        so user edits are never swept into a harness commit."""
        v = self.vault
        rows = proposals.pending(self.proposals_dir)
        lines = [
            "# Review",
            "",
            f"_{len(rows)} pending proposal(s). Check a box; the harness acts on its next pass "
            "and this note re-renders. Nothing is ever applied without a check._",
        ]
        for p in sorted(rows, key=lambda r: r["created"]):
            lines += [
                "",
                "---",
                "",
                f"## {p['title']}",
                "",
                f"`{p['drafted_against']['note']}` — {p['summary']}",
                "",
                *[f"- {i}" for i in p["issues"]],
            ]
            spec = self._by_kind.get(p["kind"])
            if spec is None or spec.render_diff:
                lines += self._diff_block(p)
            lines += [
                "",
                f"- [ ] approve {p['id']}",
                f"- [ ] reject {p['id']}",
            ]
        content = "\n".join(lines) + "\n"
        try:
            current = v.read(self.review_rel)
        except VaultError:
            current = ""
        if current == content:
            return False
        # A checked box on a still-pending proposal is an unacted decision
        # (checked mid-pass, after _act ran). Never overwrite it — the next
        # pass acts on it, then rendering resumes.
        pending_ids = {p["id"] for p in rows}
        if any(m.group(2) in pending_ids for m in DECISION_RE.finditer(current)):
            return False
        self.write_surface(self.review_rel, content,
                           f"update {self.review_rel}", action="render_review")
        return True

    def _diff_block(self, p: dict) -> list[str]:
        """A proposal's diff for Review.md. `diff_style: split` (Spec.md)
        renders a GitHub-style side-by-side HTML table — Obsidian's
        reading view renders embedded HTML, and Review.md is a machine
        view, so HTML is fair game here. `unified` keeps the classic
        ```diff fence."""
        try:
            note_text = self.vault.read(p["drafted_against"]["note"])
        except VaultError:
            return ["", "```diff", "# (target note unreadable — proposal will go stale)", "```"]
        if get_config(self.vault_path).diff_style == "split":
            after, skipped = applier.simulate_for_note(note_text, p)
            table = diffhtml.split_diff(note_text, after)
            out = [""]
            if table:
                out.append(table)
            out += [f"- not previewed: `{s}`" for s in skipped]
            if len(out) > 1:
                return out
            # nothing previewable at all: fall through to unified's notes
        return ["", "```diff", applier.preview(note_text, p), "```"]

    async def run_once(self, decisions_only: bool = False) -> dict:
        """One engine pass. `decisions_only` is the fast lane a checkbox
        trigger uses: act on decisions, re-render, done — no scanning or
        drafting, and the git memo is left untouched so the next full
        pass still scans whatever the apply changed."""
        summary = {
            "drafted": 0, "auto": 0, "approved": 0, "rejected": 0, "stale": 0,
            "scanned": False, "locked": False,
        }
        lock_path = self.state_path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Another trigger's pass is running; its pass covers this tick.
            lock_file.close()
            summary["locked"] = True
            return summary
        try:
            state = self._load_state()
            self._capture_seen = state.get("capture_seen", "")
            self._inbox_seen = state.get("inbox_seen", "")
            if not decisions_only:
                self._ensure_spec()
                # Re-read the spec each pass: Spec.md edits (inboxes,
                # exclusions) take effect without restarting the daemon.
                cfg = get_config(self.vault_path)
                self.capture_inbox = cfg.capture_inbox
                self.waiting_inbox = cfg.waiting_inbox
                for proc in self.processors:
                    if proc.ensure:
                        proc.ensure(self)
            # One queue read and one git-state read serve the whole pass:
            # _act works through the pending list, and everything after it
            # (auto-stale, coverage, sha stamping) reuses the same context.
            pending = proposals.pending(self.proposals_dir)
            acted, pending = self._act(self._read_decisions(), pending)
            summary.update(acted)
            if decisions_only:
                if sum(acted.values()):
                    self._render()
                    self._render_changes()
                return summary
            if sum(acted.values()):
                self._render()  # visible feedback beats drafting latency
            vs = applier.vault_state(self.vault_path)
            fingerprint = self._vault_fingerprint(vs)
            if state.get("fingerprint") != fingerprint:
                flipped, pending = self._auto_stale(pending, vs)
                summary["stale"] += flipped
                self._pass_sha = vs[0]
                # One pending proposal per note is a queue invariant, so it
                # is enforced here — not re-implemented in every scan. The
                # seen set also dedupes WITHIN the pass: when two
                # processors both want a note, registry priority wins.
                seen_notes = {p["drafted_against"]["note"] for p in pending}
                # Registry order is priority order: capture-class first, so
                # the user actively feeding the pipeline is never starved
                # out of n_max by a maintenance backlog.
                items = []
                for proc in self.processors:
                    for item in proc.scan(self):
                        if item["note"] not in seen_notes:
                            seen_notes.add(item["note"])
                            items.append((proc, item))
                summary["drafted"], summary["auto"] = await self._draft(items)
                summary["scanned"] = True
            self._render()
            self._render_changes()
            self._save_state()
            proposals.prune(self.proposals_dir)
            return summary
        finally:
            lock_file.close()  # releases the flock

    def _render_changes(self) -> None:
        """Phase 10: the audit surface. Rendered every pass, written only
        on change; its own update commits are filtered from the listing,
        so a render can never feed the next one."""
        if not self.changes_rel:
            return
        content = changes.render(self.vault, self.changes_rel)
        try:
            if self.vault.read(self.changes_rel) == content:
                return
        except VaultError:
            pass
        self.write_surface(self.changes_rel, content,
                           f"update {self.changes_rel}", action="render_changes")


def main() -> None:
    from .bootstrap import load_runtime

    parser = argparse.ArgumentParser(
        prog="harness-watch",
        description="One engine pass: act on Review.md checkboxes, scan, draft, re-render",
    )
    parser.add_argument("-v", "--vault", help="vault path (default: config, or $OBSIDIAN_VAULT_PATH)")
    parser.add_argument("--n-max", type=int, default=N_MAX, help="max proposals drafted this pass")
    parser.add_argument(
        "--decisions-only", action="store_true",
        help="fast lane: act on Review.md checkboxes and re-render; no scan/draft")
    parser.add_argument(
        "--disable", action="append", default=[], metavar="NAME",
        help="disable a processor for this pass (repeatable); names: "
        + ", ".join(p.name for p in processors.REGISTRY),
    )
    args = parser.parse_args()
    _config, vault, _servers = load_runtime(vault=args.vault)
    summary = asyncio.run(
        Watcher(vault, n_max=args.n_max, disabled=tuple(args.disable))
        .run_once(decisions_only=args.decisions_only)
    )
    if summary["locked"]:
        print("watch pass: skipped — another pass is already running")
        return
    print(
        f"watch pass: drafted {summary['drafted']}, auto-applied {summary['auto']}, "
        f"applied {summary['approved']}, rejected {summary['rejected']}, "
        f"stale {summary['stale']}"
        + ("" if summary["scanned"] else " (vault unchanged — scan skipped)")
    )


if __name__ == "__main__":
    main()
