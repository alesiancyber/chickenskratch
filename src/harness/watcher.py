"""The processor engine: a pipeline, not a process.

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
import hashlib
import json
import os
import re
from pathlib import Path

from obsidian_mcp import markdown as md
from obsidian_mcp import vaultconfig as vc
from obsidian_mcp import workers as wk
from obsidian_mcp.vault import Vault, VaultError
from obsidian_mcp.vaultconfig import get_config

from . import applier, changes, commands, diffhtml, processors, proposals, today
from .bootstrap import ROOT, apply_worker_profile
from .corpus import record_dashboard
from .processors import ProcessorSpec

STATE_PATH = ROOT / "data" / "watcher_state.json"
# n_max (proposals drafted per pass) now lives in VaultConfig — one home,
# user-editable per vault in Spec.md.

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

`worker` picks which models.yaml profile drafts proposals — a profile
NAME, never a URL (endpoints and keys stay in the repo config). `n_max`
caps proposals drafted per pass; `disabled_processors` switches
processors off entirely (the rollback flag). All read fresh each pass:
swapping the worker model is a note edit, no restart.
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
        n_max: int | None = None,
        disabled: tuple[str, ...] | None = None,
        registry: tuple[ProcessorSpec, ...] | None = None,
        grouping_drafter=None,
        capture_extractor=None,
    ):
        self.vault_path = str(vault_path)
        # Per-vault state, queue, and (via with_suffix(".lock")) pass lock:
        # two vaults run from one checkout must not share a fingerprint,
        # serialize each other's passes, or — the 2026-07-23 deploy-script
        # find — auto-stale each other's pending proposals.
        digest = hashlib.sha1(self.vault_path.encode()).hexdigest()[:12]
        self.proposals_dir = (Path(proposals_dir) if proposals_dir is not None
                              else ROOT / "data" / f"proposals-{digest}")
        self.state_path = (Path(state_path) if state_path
                           else STATE_PATH.with_name(f"watcher_state-{digest}.json"))
        # Worker injection points (tests script these; prod uses the
        # bounded run_worker defaults). Processors reach them via `w.`.
        self.drafter = drafter or draft_schema_fix
        self.grouping_drafter = grouping_drafter or draft_grouping
        self.capture_extractor = capture_extractor or wk.extract_tasks
        # Explicit constructor args (tests, CLI flags) pin these knobs;
        # None means the vault's Spec.md decides, re-read every pass.
        self._n_max_override = n_max
        self._disabled_override = disabled
        self._registry_override = registry
        self._configure(get_config(self.vault_path))  # raises on a bad explicit arg
        # hashes of text last handed to the extractors: rejecting a
        # proposal must not re-draft the same text every pass
        self._capture_seen = ""
        self._inbox_seen = ""
        # HEAD sha at scan time, stamped into drafted proposals; set once
        # per pass so processors never re-run git themselves
        self._pass_sha = ""
        # worker status line for Changes.md, probed at most once per pass
        self._pass_status: str | None = None

    def _configure(self, cfg) -> None:
        """Resolve the per-pass knobs: explicit constructor args win,
        otherwise Spec.md (n_max, disabled_processors, surfaces, inboxes).
        Called at construction and again each full pass, so a Spec.md
        edit takes effect without a restart."""
        # The engine's own vault handle carries the exclusions (I4): every
        # watcher-side scan — grouping, commands, the Today surface — must
        # be as blind to Private/ as the MCP server's singleton is. Rebuilt
        # per pass so a Spec.md exclude edit takes effect without restart.
        self.vault = Vault(self.vault_path, exclude=cfg.exclude)
        self.n_max = self._n_max_override if self._n_max_override is not None else cfg.n_max
        disabled = tuple(self._disabled_override if self._disabled_override is not None
                         else cfg.disabled_processors)
        # "inline_command" in the disabled list is the commands kill switch —
        # commands are a pass phase, not a processor, so the name is peeled
        # off before the registry (which would reject it as unknown).
        self.commands_enabled = "inline_command" not in disabled
        disabled = tuple(n for n in disabled if n != "inline_command")
        if self._registry_override is not None:
            self.processors = self._registry_override
        else:
            self.processors = processors.registry(disabled)
        self._by_kind = {p.name: p for p in self.processors}
        self.review_rel = cfg.surfaces.get("review", "Harness/Review.md")
        self.capture_rel = cfg.surfaces.get("capture", "")
        self.changes_rel = cfg.surfaces.get("changes", "")
        self.today_rel = cfg.surfaces.get("today", "")
        self.capture_inbox = cfg.capture_inbox
        self.waiting_inbox = cfg.waiting_inbox

    # ---- shared machinery processors ride on ----

    def write_surface(self, rel: str, content: str, message: str,
                      action: str = "write_surface") -> None:
        """The one code path that writes a Harness surface: locked and
        snapshot-bracketed, so no hook can forget the git guarantee."""
        with self.vault.locked():
            self.vault.snapshot_before(action)
            self.vault.write(rel, content)
            self.vault.snapshot_after(message)

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
            "worker": base.worker,
            "disabled_processors": list(base.disabled_processors),
            "n_max": base.n_max,
            "callout_type": base.callout_type,
            "link_scheme": base.link_scheme,
            "daily_dir": base.daily_dir,
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

    def _save_state(self, fingerprint: dict | None = None) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        state = {
            "fingerprint": fingerprint if fingerprint is not None else self._vault_fingerprint(),
            "capture_seen": self._capture_seen,
            "inbox_seen": self._inbox_seen,
        }
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    # ---- stage 2: execute typed //commands ----

    async def _run_commands(self, summary: dict) -> None:
        """The command phase (commands.py): user ORDERS, not harness
        suggestions — intrinsically trusted, never queued, never budgeted
        by n_max (that bounds LLM drafting cost). Ordered before proposal
        scanning so drafts see the post-command note. Staleness is the
        replace_line anchor itself: the line gone or duplicated means the
        user's edit won and the order lapses."""
        if not self.commands_enabled:
            return
        from obsidian_mcp import server

        cfg = get_config(self.vault_path)
        for item in commands.scan(self.vault, cfg.surface_dirs):
            # a slow search must never queue a human decision
            self._sweep_late_decisions(summary)
            result = await commands.INLINE_COMMANDS[item["command"]](item["args"], self.vault_path)
            if result is None:
                continue  # transient failure: the line stays armed, retried next scan
            try:
                server.replace_line(item["note"], item["line"],
                                    commands.render_callout(item, result, cfg.callout_type))
            except VaultError:
                continue  # the line was edited away mid-flight: the user's edit wins
            summary["commands"] += 1

    # ---- stage 3: draft proposals ----

    async def _draft(self, items: list[tuple[ProcessorSpec, dict]],
                     summary: dict) -> tuple[int, int]:
        """Draft up to n_max proposals; verified-safe drafts from trusted
        processors apply immediately. Returns (drafted, auto_applied)."""
        auto_trusted = set(get_config(self.vault_path).auto_apply)
        drafted = auto = 0
        failed: set[str] = set()  # processors whose draft errored this pass
        for proc, item in items[: self.n_max]:
            if proc.name in failed:
                continue  # its worker already failed once: don't hammer it
            # Decisions never wait behind drafting: a slow worker phase
            # holds the pass lock for minutes, so checked boxes are
            # serviced between calls — latency bounded by ONE draft.
            self._sweep_late_decisions(summary)
            try:
                p = await proc.draft(self, item)
            except VaultError:
                # This processor's worker is down/misbehaving. Others keep
                # drafting — a dead LLM worker must never starve the
                # model-free processors (//commands) behind it in line.
                failed.add(proc.name)
                continue
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
                try:
                    applier.apply(p, self.vault_path)
                except Exception as e:
                    # poison plan: fail the proposal, never the pass (a
                    # pending one would re-verify and re-crash every pass)
                    proposals.resolve(p, "failed", self.proposals_dir)
                    print(f"auto-apply failed for {p['id']}: {type(e).__name__}: {e}")
                    continue
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
        acted = {"approved": 0, "rejected": 0, "stale": 0, "failed": 0}
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
            try:
                applier.apply(p, self.vault_path)
            except Exception as e:
                # A poison plan (e.g. a drafter emitting args the tools
                # reject) must fail ITSELF, never the pass: left pending,
                # the checked box would re-crash every pass forever — the
                # engine wedged exactly this way on 2026-07-23.
                proposals.resolve(p, "failed", self.proposals_dir)
                acted["failed"] += 1
                print(f"apply failed for {p['id']} ({p['title'][:50]}): {type(e).__name__}: {e}")
                continue
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
                # note stem in the heading: several proposals can share a
                # title ("Fix meeting schema issues" × 3 in real use)
                f"## {p['title']} · {Path(p['drafted_against']['note']).stem}",
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
            "commands": 0, "drafted": 0, "auto": 0, "approved": 0, "rejected": 0,
            "stale": 0, "failed": 0, "scanned": False, "locked": False,
        }
        self._pass_status = None  # worker status probed at most once per pass
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
            # Point obsidian_mcp's in-process singleton at THIS vault for the
            # whole pass — scans, verifiers, and applies all resolve through
            # it, and no processor may depend on a sibling having set it.
            applier.use_vault(self.vault_path)
            state = self._load_state()
            self._capture_seen = state.get("capture_seen", "")
            self._inbox_seen = state.get("inbox_seen", "")
            if not decisions_only:
                self._ensure_spec()
                # Re-read the spec each pass: Spec.md edits (worker model,
                # n_max, disabled processors, inboxes) take effect without
                # restarting the daemon. A bad value degrades to the
                # current configuration, never a crashed pass.
                cfg = get_config(self.vault_path)
                try:
                    self._configure(cfg)
                except ValueError as e:
                    print(f"Spec.md processor config ignored: {e}")
                if cfg.worker:
                    apply_worker_profile(cfg.worker)
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
                    self._render_today()
                self._sweep_late_decisions(summary)
                return summary
            if sum(acted.values()):
                self._render()  # visible feedback beats drafting latency
            approved_pre_scan = summary["approved"]
            vs = applier.vault_state(self.vault_path)
            fingerprint = self._vault_fingerprint(vs)
            if state.get("fingerprint") != fingerprint:
                flipped, pending = self._auto_stale(pending, vs)
                summary["stale"] += flipped
                self._pass_sha = vs[0]
                # Commands execute before the proposal scan: drafts see the
                # post-command note, and command writes are scanned in-pass
                # (so they never need the post-scan memo drop below).
                await self._run_commands(summary)
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
                summary["drafted"], summary["auto"] = await self._draft(items, summary)
                summary["scanned"] = True
            self._render()
            self._render_changes()
            self._render_today()
            # The memo must only describe a tree whose changes were all
            # scanned. Applies that landed AFTER the scan — auto-applies,
            # decisions swept between drafting calls — violate that, so the
            # memo is dropped and the next pass rescans. The sweep below is
            # excluded the cheaper way: the fingerprint is captured first.
            post_scan_applies = summary["auto"] + summary["approved"] - approved_pre_scan
            fingerprint = {} if post_scan_applies else self._vault_fingerprint()
            self._sweep_late_decisions(summary)
            self._save_state(fingerprint)
            proposals.prune(self.proposals_dir)
            return summary
        finally:
            lock_file.close()  # releases the flock

    def _sweep_late_decisions(self, summary: dict) -> None:
        """Decisions checked WHILE this pass runs: act now — the fast
        lane's trigger was consumed by the pass lock, and re-baselining
        hides the check from the full loop. Called between drafting calls
        (a slow worker phase must never queue a human) and again before
        the lock releases. The render guard preserved the box; this
        honors it."""
        for _ in range(3):
            pending_ids = {p["id"] for p in proposals.pending(self.proposals_dir)}
            late = [d for d in self._read_decisions() if d[0] in pending_ids]
            if not late:
                return
            acted, _ = self._act(late, proposals.pending(self.proposals_dir))
            for k, v in acted.items():
                summary[k] = summary.get(k, 0) + v
            self._render()
            self._render_changes()
            self._render_today()

    def _worker_status(self) -> str:
        """The Changes.md health line: which worker drafts proposals and
        whether its endpoint answers right now. Probed once per pass (it
        carries a network timeout); the text only changes when the worker
        or its health does, so it never churns the surface."""
        if self._pass_status is None:
            from obsidian_mcp.workers import _worker_model

            from .backends import endpoint_alive_sync

            base = os.environ.get("WORKER_BASE_URL", "http://127.0.0.1:8081/v1")
            up = endpoint_alive_sync(base)
            self._pass_status = (
                f"_Worker: `{_worker_model()}` @ {base} — "
                + ("reachable_" if up else "**UNREACHABLE**_")
            )
        return self._pass_status

    def _render_changes(self) -> None:
        """Phase 10: the audit surface. Rendered every pass, written only
        on change; its own update commits are filtered from the listing,
        so a render can never feed the next one."""
        if not self.changes_rel:
            return
        content = changes.render(
            self.vault, self.changes_rel, status=self._worker_status(),
            surface_rels=set(get_config(self.vault_path).surfaces.values()))
        try:
            if self.vault.read(self.changes_rel) == content:
                return
        except VaultError:
            pass
        self.write_surface(self.changes_rel, content,
                           f"update {self.changes_rel}", action="render_changes")

    def _render_today(self) -> None:
        """The attention surface (today.py): a read-only view, so no trust
        machinery — rendered every pass, written only on change. The 6am
        heartbeat pass covers the date rollover on a quiet vault."""
        if not self.today_rel:
            return
        content = today.render(
            self.vault, get_config(self.vault_path),
            pending_decisions=len(proposals.pending(self.proposals_dir)),
        )
        try:
            if self.vault.read(self.today_rel) == content:
                return
        except VaultError:
            pass
        self.write_surface(self.today_rel, content,
                           f"update {self.today_rel}", action="render_today")


def main() -> None:
    from .bootstrap import load_runtime

    parser = argparse.ArgumentParser(
        prog="harness-watch",
        description="One engine pass: act on Review.md checkboxes, scan, draft, re-render",
    )
    parser.add_argument("-v", "--vault", help="vault path (default: config, or $OBSIDIAN_VAULT_PATH)")
    parser.add_argument("--n-max", type=int, default=None,
                        help="max proposals drafted this pass (default: Spec.md n_max)")
    parser.add_argument(
        "--decisions-only", action="store_true",
        help="fast lane: act on Review.md checkboxes and re-render; no scan/draft")
    parser.add_argument(
        "--disable", action="append", default=[], metavar="NAME",
        help="disable a processor for this pass, overriding Spec.md "
        "disabled_processors (repeatable); names: "
        + ", ".join(p.name for p in processors.REGISTRY)
        + ", inline_command (the //commands phase)",
    )
    args = parser.parse_args()
    _config, vault, _servers = load_runtime(vault=args.vault)
    summary = asyncio.run(
        Watcher(vault, n_max=args.n_max,
                disabled=tuple(args.disable) if args.disable else None)
        .run_once(decisions_only=args.decisions_only)
    )
    if summary["locked"]:
        print("watch pass: skipped — another pass is already running")
        return
    print(
        f"watch pass: {summary['commands']} command(s), drafted {summary['drafted']}, "
        f"auto-applied {summary['auto']}, applied {summary['approved']}, "
        f"rejected {summary['rejected']}, stale {summary['stale']}"
        + (f", FAILED {summary['failed']}" if summary.get("failed") else "")
        + ("" if summary["scanned"] else " (vault unchanged — scan skipped)")
    )


if __name__ == "__main__":
    main()
