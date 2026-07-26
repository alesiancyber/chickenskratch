"""The processor engine: a pipeline, not a process.

One pass = act on decisions handed in by a human verb → (if the vault
moved) run every registered processor's deterministic `scan`, draft up
to N proposals via their bounded `draft` calls → memoize the vault's git
state so an unchanged vault makes the next pass free. `uv run
harness-watch` runs one by hand or cron; tezcatd runs one when the vault
settles.

The markdown surfaces this engine used to render — Review.md, Today.md,
Changes.md — were retired 2026-07-26 (research/surface-subtraction-plan.md).
Consent arrives through `decisions` from a human verb (the dashboard
button, `harness decide`), never by parsing a note back: the vault is
storage again, not an input device. What survives is the queue in
`data/` (authoritative all along) and `_act`, which both channels always
shared. Capture.md stays — it is the canvas's write-ahead log, not a
compat surface.

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
import time
from pathlib import Path

from obsidian_mcp import markdown as md
from obsidian_mcp import vaultconfig as vc
from obsidian_mcp import workers as wk
from obsidian_mcp.vault import Vault, VaultError, atomic_write_text
from obsidian_mcp.vaultconfig import get_config

from . import applier, commands, events, processors, proposals
from .bootstrap import ROOT, apply_worker_profile, vault_digest
from .corpus import record_dashboard
from .processors import ProcessorSpec

STATE_PATH = ROOT / "data" / "watcher_state.json"
# n_max (proposals drafted per pass) now lives in VaultConfig — one home,
# user-editable per vault in Spec.md.

# How long a HUMAN-initiated pass waits for the pass lock before giving
# up. Timer-driven passes never wait (their next tick covers the skip);
# a click has no next tick, so it queues instead. Generous, because the
# holder may be mid-draft: a decision is worth waiting a minute for, and
# LockBusy is a clean 409 rather than a silently dropped verdict.
DECISION_LOCK_WAIT_S = 90.0

class LockBusy(RuntimeError):
    """Another pass held the pass lock for the whole wait. Raised only for
    callers that asked to wait (the UI's verbs); the timer lanes get
    summary["locked"] instead, because skipping is correct for them."""


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
        should_cancel=None,
    ):
        self.vault_path = str(vault_path)
        # Cooperative stop (cancel.token), polled between draft calls and
        # between processors — points where the vault is already
        # consistent. Never mid-plan: a half-applied proposal would leave
        # the queue disagreeing with the vault.
        self.should_cancel = should_cancel or (lambda: False)
        # Per-vault state, queue, and (via with_suffix(".lock")) pass lock:
        # two vaults run from one checkout must not share a fingerprint,
        # serialize each other's passes, or — the 2026-07-23 deploy-script
        # find — auto-stale each other's pending proposals.
        digest = vault_digest(self.vault_path)
        self.proposals_dir = (Path(proposals_dir) if proposals_dir is not None
                              else proposals.dir_for(self.vault_path))
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
        # processor name -> hash of the text last handed to its extractor:
        # rejecting a proposal must not re-draft the same text every pass.
        # Keyed by name so a new extractor needs no engine-side field —
        # scan/draft own their entry, and _notify_stale re-arms it.
        self.seen: dict[str, str] = {}
        # HEAD sha at scan time, stamped into drafted proposals; set once
        # per pass so processors never re-run git themselves
        self._pass_sha = ""

    def _configure(self, cfg) -> None:
        """Resolve the per-pass knobs: explicit constructor args win,
        otherwise Spec.md (n_max, disabled_processors, surfaces, inboxes).
        Called at construction and again each full pass, so a Spec.md
        edit takes effect without a restart.

        Everything that can reject a value is resolved BEFORE anything is
        assigned. The caller's contract is that a bad Spec.md degrades to
        the current configuration — assigning as we went made that a lie:
        an unknown processor name raised only after the new vault handle,
        n_max, and surfaces had already landed, leaving the pass running
        half of each config."""
        disabled = tuple(self._disabled_override if self._disabled_override is not None
                         else cfg.disabled_processors)
        # "inline_command" in the disabled list is the commands kill switch —
        # commands are a pass phase, not a processor, so the name is peeled
        # off before the registry (which would reject it as unknown).
        commands_enabled = "inline_command" not in disabled
        registry = (self._registry_override if self._registry_override is not None
                    else processors.registry(  # raises on an unknown name
                        tuple(n for n in disabled if n != "inline_command")))
        # The engine's own vault handle carries the exclusions (I4): every
        # watcher-side scan — grouping, commands, the Today surface — must
        # be as blind to Private/ as the MCP server's singleton is. Rebuilt
        # per pass so a Spec.md exclude edit takes effect without restart.
        self.vault = Vault(self.vault_path, exclude=cfg.exclude)
        self.n_max = self._n_max_override if self._n_max_override is not None else cfg.n_max
        self.commands_enabled = commands_enabled
        self.processors = registry
        self._by_kind = {p.name: p for p in self.processors}
        # Capture.md is the only surface left: the canvas's write-ahead
        # log, written before extraction so a dump survives it failing.
        self.capture_rel = cfg.surfaces.get("capture", "")
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
            "done_archive": base.done_archive,
            "exclude": list(base.exclude),
            "auto_apply": list(base.auto_apply),
            "fidelity": base.fidelity,
            "worker": base.worker,
            "disabled_processors": list(base.disabled_processors),
            "n_max": base.n_max,
            "daily_dir": base.daily_dir,
            "meetings_dir": base.meetings_dir,
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

    @staticmethod
    def _seen_from_state(state: dict) -> dict[str, str]:
        """The seen-hash map, tolerating state files written before it was
        one dict: a missed migration would re-draft every extractor's last
        text once on upgrade."""
        seen = {k: str(v) for k, v in (state.get("seen") or {}).items() if v}
        for legacy, name in (("capture_seen", "capture"), ("inbox_seen", "inbox_extract")):
            if state.get(legacy) and name not in seen:
                seen[name] = str(state[legacy])
        return seen

    def _save_state(self, fingerprint: dict | None = None) -> None:
        state = {
            "fingerprint": fingerprint if fingerprint is not None else self._vault_fingerprint(),
            "seen": self.seen,
        }
        atomic_write_text(self.state_path, json.dumps(state))

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
            result = await commands.INLINE_COMMANDS[item["command"]](item["args"], self.vault_path)
            if result is None:
                continue  # transient failure: the line stays armed, retried next scan
            try:
                server.replace_line(item["note"], item["line"],
                                    commands.render_callout(item, result))
            except VaultError:
                continue  # the line was edited away mid-flight: the user's edit wins
            summary["commands"] += 1

    # ---- stage 3: draft proposals ----

    async def _draft(self, items: list[tuple[ProcessorSpec, dict]],
                     summary: dict) -> tuple[int, int]:
        """Draft up to n_max proposals; verified-safe drafts from trusted
        processors apply immediately. Returns (drafted, auto_applied)."""
        auto_trusted = set(get_config(self.vault_path).auto_apply)
        drafted = auto = attempts = 0
        failed: set[str] = set()  # processors whose draft errored this pass
        for proc, item in items:
            # n_max bounds draft CALLS (the LLM cost), so a failed
            # processor's skipped items must not consume budget that a
            # healthy processor behind it could use.
            if attempts >= self.n_max or self.should_cancel():
                break
            if proc.name in failed:
                continue  # its worker already failed once: don't hammer it
            attempts += 1
            # The activity signal: which processor is thinking, about what.
            # Emission is telemetry (events.emit swallows OSError) — the
            # indicator is a courtesy, never a stage a pass can fail on.
            events.emit(self.vault_path, "draft_started",
                        {"processor": proc.name, "note": item["note"]})
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
            # note — and never when the drafter itself flagged something a
            # human must see (needs_review). Any failure means the proposal
            # simply keeps its checkbox — verification degrades to review,
            # never blocks.
            if proc.name in auto_trusted and proc.verify and not p.get("needs_review"):
                try:
                    failures = proc.verify(self, item, p)
                except Exception as e:
                    # A verifier that RAISES is a verifier that didn't
                    # vouch, so it means what a verifier that returns
                    # failures means: keep the checkbox. It must never
                    # take the pass down with it — the whole point of
                    # this gate is that verification degrades to review.
                    failures = [f"verifier raised {type(e).__name__}: {e}"]
                if failures or applier.is_stale(p, self.vault_path):
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
                self._post_apply(proc, p)
        return drafted, auto

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
                self._notify_stale(p)
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
            self._post_apply(self._by_kind.get(p["kind"]), p)
        return acted, [p for p in pending if p["id"] not in resolved]

    def _post_apply(self, spec, p: dict) -> None:
        """Run a processor's on_applied hook. Guarded, because by the time
        it runs the change is ALREADY written, committed, and resolved in
        the queue: letting the hook raise would abandon a correct apply
        with an exception, and the caller would tell the user it failed.
        (reset_capture writes a surface, so a wedged vault lock reaches
        here — VaultLocked is exactly the case this must survive.)"""
        if spec is None or spec.on_applied is None:
            return
        try:
            spec.on_applied(self, p)
        except Exception as e:
            print(f"post-apply hook for {spec.name} failed "
                  f"(the change itself landed): {type(e).__name__}: {e}")

    @staticmethod
    def _record(p: dict, decision: str) -> None:
        record_dashboard(p, decision)  # swallows its own failures (corpus.py)

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
                self._notify_stale(p)
                flipped += 1
            else:
                keep.append(p)
        return flipped, keep

    def _notify_stale(self, p: dict) -> None:
        """A stale proposal voided its draft, so its processor must re-arm:
        the seen-hash would otherwise suppress the re-draft and the
        extraction would silently vanish (the 2026-07-24 capture bug).
        Dropping the entry by kind covers every extractor at once — a
        processor that keeps no seen-hash simply has nothing to drop."""
        self.seen.pop(p["kind"], None)

    async def _acquire_pass_lock(self, wait: float):
        """The pass lock, or None if another pass holds it.

        `wait` is the whole difference between the two lanes. A
        timer-driven pass passes 0: it must never queue behind a
        long-drafting pass, because that pass covers its tick anyway. A
        human verb passes DECISION_LOCK_WAIT_S: nothing else will ever
        carry that click, so it waits — polled with `await` so the caller
        (and, before the engine thread existed, the whole server) keeps
        running while it does."""
        lock_path = self.state_path.with_suffix(".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = open(lock_path, "w")
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lock_file
            except OSError:
                if time.monotonic() >= deadline:
                    lock_file.close()
                    return None
                await asyncio.sleep(0.1)

    async def run_once(self, decisions_only: bool = False,
                       decisions: list[tuple[str, str]] | None = None,
                       lock_wait: float = 0.0) -> dict:
        """One engine pass. `decisions_only` is the fast lane a checkbox
        trigger uses: act on decisions, re-render, done — no scanning or
        drafting, and the git memo is left untouched so the next full
        pass still scans whatever the apply changed.

        `decisions` injects explicit (pid, verdict) pairs ahead of the
        Review.md checkbox scan — the UI button's channel (service.decide).
        Both converge on _act under the pass lock, so a click can never
        race a checkbox or double-apply (_act resolves each pid once).

        `lock_wait` seconds to wait for the pass lock, and it selects the
        contention contract. The default 0 is the timer lane: skip and
        report summary["locked"], because the running pass covers this
        tick. Any positive wait is a human verb, and losing the lock after
        waiting is exceptional — LockBusy, not a summary of a pass that
        quietly did nothing. Anything carrying `decisions` MUST wait:
        those verdicts exist nowhere else, so a `locked` return discarded
        a human's decision, which is exactly what it used to do."""
        summary = {
            "commands": 0, "drafted": 0, "auto": 0, "approved": 0, "rejected": 0,
            "stale": 0, "failed": 0, "scanned": False, "locked": False,
            "cancelled": False,
        }
        lock_file = await self._acquire_pass_lock(lock_wait)
        if lock_file is None:
            if lock_wait > 0:
                raise LockBusy(
                    f"the engine has been busy with another pass for {lock_wait:.0f}s — "
                    "nothing was applied; try again in a moment")
            # Another trigger's pass is running; its pass covers this tick.
            summary["locked"] = True
            return summary
        try:
            # Point obsidian_mcp's in-process singleton at THIS vault for the
            # whole pass — scans, verifiers, and applies all resolve through
            # it, and no processor may depend on a sibling having set it.
            applier.use_vault(self.vault_path)
            state = self._load_state()
            self.seen = self._seen_from_state(state)
            if not decisions_only:
                # Opens the activity indicator; pass_complete closes it.
                events.emit(self.vault_path, "pass_started", {})
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
                        try:
                            proc.ensure(self)
                        except VaultError as e:
                            # Creating a surface is setup, not the work:
                            # a processor that can't scaffold skips this
                            # pass rather than cancelling everyone else's.
                            print(f"ensure failed for {proc.name}: {e}")
            # One queue read and one git-state read serve the whole pass:
            # _act works through the pending list, and everything after it
            # (auto-stale, coverage, sha stamping) reuses the same context.
            pending = proposals.pending(self.proposals_dir)
            acted, pending = self._act(list(decisions or []), pending)
            summary.update(acted)
            if decisions_only:
                # Persist any stale-triggered re-arm (seen-hashes) — losing it
                # here would resurrect the suppression on the next full pass.
                # The memo passes through untouched, so that pass still scans.
                self._save_state(state.get("fingerprint") or {})
                self._emit_pass(summary)
                return summary
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
                    if self.should_cancel():
                        break
                    try:
                        found = proc.scan(self)
                    except Exception as e:
                        # A poison scan (a note deleted mid-walk, an
                        # unparseable edge) fails ITS processor, never the
                        # pass — the same contract _draft and _act already
                        # enforce for their stages.
                        print(f"scan failed for {proc.name}: {type(e).__name__}: {e}")
                        continue
                    for item in found:
                        if item["note"] not in seen_notes:
                            seen_notes.add(item["note"])
                            items.append((proc, item))
                summary["drafted"], summary["auto"] = await self._draft(items, summary)
                summary["scanned"] = True
            summary["cancelled"] = bool(self.should_cancel())
            # The memo must only describe a tree whose changes were all
            # scanned. Applies that landed AFTER the scan (auto-applies)
            # violate that, so the memo is dropped and the next pass
            # rescans.
            post_scan_applies = summary["auto"] + summary["approved"] - approved_pre_scan
            fingerprint = {} if post_scan_applies else self._vault_fingerprint()
            self._save_state(fingerprint)
            proposals.prune(self.proposals_dir)
            self._emit_pass(summary)
            return summary
        finally:
            lock_file.close()  # releases the flock

    def _emit_pass(self, summary: dict) -> None:
        """One pass_complete event per completed pass — the UI's SSE
        source. Emission is telemetry (events.emit swallows OSError) —
        the queue in data/ is the record."""
        events.emit(self.vault_path, "pass_complete", {
            "summary": summary,
            "pending": len(proposals.pending(self.proposals_dir)),
        })


def main() -> None:
    from .bootstrap import load_runtime

    parser = argparse.ArgumentParser(
        prog="harness-watch",
        description="One engine pass: scan the vault, draft proposals, auto-apply what verifies",
    )
    parser.add_argument("-v", "--vault", help="vault path (default: config, or $OBSIDIAN_VAULT_PATH)")
    parser.add_argument("--n-max", type=int, default=None,
                        help="max proposals drafted this pass (default: Spec.md n_max)")
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
        .run_once()
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
