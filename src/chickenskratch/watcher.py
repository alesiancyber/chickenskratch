"""The processor engine: a pipeline, not a process.

One pass = act on decisions handed in by a human verb → run every
registered processor's deterministic `scan` → draft up to N proposals
via their bounded `draft` calls. `uv run chickenskratch-watch` runs one by hand
or cron; chickenskratchd runs one when the vault settles.

Every pass scans. Skipping the scan on an unchanged vault was a second
change-detector behind a daemon that already fires only on change, and
it cost far more than the walk it saved: a pass that gave up early
memoized "this tree is handled" and the input behind it stayed
invisible. Repetition is bounded where it is expensive instead — the
per-processor seen-hashes below stop redundant LLM drafts.

Consent arrives through `decisions` from a human verb (the dashboard
button, `chickenskratch decide`), never by parsing a note back: the vault is
storage, not an input device. The queue in `data/` is authoritative;
both consent channels converge on `_act`. Capture.md is the one surface
the engine writes — the canvas's write-ahead log.

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

from chickenskratch_vault import markdown as md
from chickenskratch_vault import vaultconfig as vc
from chickenskratch_vault import workers as wk
from chickenskratch_vault.vault import Vault, VaultError, atomic_write_text, read_lossy
from chickenskratch_vault.vaultconfig import get_config

from . import applier, changes, commands, events, processors, proposals
from .bootstrap import ROOT, apply_worker_profile, vault_digest
from .corpus import record_dashboard
from .processors import ProcessorSpec

STATE_PATH = ROOT / "data" / "watcher_state.json"
# n_max (proposals drafted per pass) lives in VaultConfig — one home,
# user-editable per vault in Spec.md.

# How long a HUMAN-initiated pass waits for the pass lock before giving
# up. Timer-driven passes never wait (their next tick covers the skip);
# a click has no next tick, so it queues instead. Generous, because the
# holder may be mid-draft: a decision is worth waiting a minute for, and
# LockBusy is a clean 409 rather than a silently dropped verdict.
DECISION_LOCK_WAIT_S = 90.0
# Draft attempts against ONE input before an unusable plan parks. Small on
# purpose: the drafters own the proposal's shape and the model only fills
# the fields, so a rejected plan usually fails identically next pass — the
# budget is there to catch the flake, not to grind at a poison input.
DRAFT_PLAN_ATTEMPTS = 3


class LockBusy(RuntimeError):
    """Another pass held the pass lock for the whole wait. Raised only for
    callers that asked to wait (the UI's verbs); the timer lanes get
    summary["locked"] instead, because skipping is correct for them."""


SPEC_BODY = """\
# Vault Spec

The properties above ARE this vault's configuration — the folder→type
map, where records live (meetings_dir, and the dated capture notes),
and the model-context exclusions. Edit them here in any editor; the
engine reads this note at the start of every pass, no restart needed. Unlike everything else in Harness/,
this note is yours: seeded once from the repo defaults, never
regenerated.

Folders are workspaces, not a taxonomy: the map supplies the default
type for untyped notes, and a note's own frontmatter always wins. When
a new top-level folder appears in the vault, a proposal to map it
shows up on the dashboard.

`policies` is the consent map — one level per processor, so nothing
here can conflict with anything else:
- `off`: the processor doesn't run;
- `ask`: every proposal queues for your verdict;
- `verified`: applies when its deterministic check passes (your words
  only, verbatim complete); no check, or a failing one, queues;
- `full`: applies regardless — objections are recorded on the proposal,
  and the folded verbatim / changes feed is your audit trail.
Every apply is still one revertible snapshot; `chickenskratch-revert` (or the
dashboard's undo) is always one step, and a revert is the strongest
training signal you can give. The console's autonomy presets just
pre-select these levels — the map is the only stored truth.

`worker` picks which models.yaml profile drafts proposals — a profile
NAME, never a URL (endpoints and keys stay in the repo config). `n_max`
caps proposals drafted per pass; `disabled_processors` switches
processors off entirely (the rollback flag). All read fresh each pass:
swapping the worker model is a note edit, no restart.
"""


def spec_seed(vault_path: str) -> str:
    """The Spec.md a vault starts with: every console-managed key written
    out explicitly, over the prose that explains them.

    One home, because two callers seed it — the engine's first pass, and
    the console when you configure a vault that has never had a pass. A
    second copy would drift, and the drift would be a key that silently
    stops appearing in new vaults."""
    base = get_config(vault_path)
    fm = {
        "folders": dict(base.folder_types),
        "done_archive": base.done_archive,
        "exclude": list(base.exclude),
        "capture_archive": base.capture_archive,
        # The consent map, every processor named at its resolved level —
        # a new vault starts with its whole posture legible in one key.
        "policies": {p.name: base.policy_for(p.name) for p in processors.REGISTRY},
        "fidelity": base.fidelity,
        "worker": base.worker,
        # Kept for the //commands kill switch ("inline_command") — the
        # command phase is a pass stage, not a processor with a policy.
        "disabled_processors": list(base.disabled_processors),
        "n_max": base.n_max,
        "daily_dir": base.daily_dir,
        "meetings_dir": base.meetings_dir,
    }
    return md.join_frontmatter(fm, SPEC_BODY)


async def draft_schema_fix(item: dict) -> dict:
    """Default schema drafter: one bounded worker call -> {title, summary, apply}."""
    data = await wk.run_worker(
        wk.get_worker("schema_fix"),
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
        wk.get_worker("grouping"),
        tasks="\n".join(f"{t.block_id}: {t.text}" for t in item["tasks"]),
    )


async def embed_pool(vault_path: str, items: dict[str, str]) -> dict[str, list[float]]:
    """Default embedder: the per-vault Index (content-hash cached, so an
    unchanged pool costs zero network). Returns {key: normalized vec}."""
    from . import embeddings as em

    index = em.Index(vault_path)
    await index.update(items)
    return {k: index.rows[k]["vec"] for k in items if k in index.rows}


async def draft_overlap_worker(item: dict, pairs: list[tuple]) -> dict:
    """Default overlap judge: ONE batched worker call for every candidate
    pair this pass (draft_overlap validates the verdicts)."""
    lines = []
    for n, (a, b) in enumerate(pairs, 1):
        (ta, ra), (tb, rb) = a, b

        def describe(t, rel):
            extras = ", ".join(x for x in (
                f"due {t.due}" if t.due else "",
                f"planned {t.scheduled}" if t.scheduled else "",
                t.priority or "") if x)
            return f"{t.block_id} — \"{t.text}\" (in {rel}{', ' + extras if extras else ''})"

        lines.append(f"{n}. a: {describe(ta, ra)}\n   b: {describe(tb, rb)}")
    return await wk.run_worker(wk.get_worker("overlap"), pairs="\n".join(lines))


async def draft_plan_today(item: dict, stats: dict | None = None) -> dict:
    """Default plan drafter: one bounded worker call over the undated
    pool; the reply only ever picks ids (draft_plan validates them).
    The pool spans notes, so each line names the record it lives in —
    "three action items from the Azure meeting, 12 days untouched" is a
    better prompt than three bare texts. The worker eval calls this
    same drafter (stats rides through), so the benchmark can never
    drift from the prompt lines production sends."""
    lines = "\n".join(
        f"{t.block_id}: {t.text} — created {t.created or 'unknown'}, "
        f"priority {t.priority or 'none'}, in {rel}"
        for t, rel in item["tasks"])
    return await wk.run_worker(
        wk.get_worker("plan_today"), stats=stats, today=item["today"],
        max_picks=processors.MAX_PLAN_PICKS,
        committed=item["committed"], tasks=lines)


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
        record_worker=None,
        plan_drafter=None,
        overlap_drafter=None,
        embedder=None,
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
        # serialize each other's passes, or auto-stale each other's
        # pending proposals.
        digest = vault_digest(self.vault_path)
        self.proposals_dir = (Path(proposals_dir) if proposals_dir is not None
                              else proposals.dir_for(self.vault_path))
        self.state_path = (Path(state_path) if state_path
                           else STATE_PATH.with_name(f"watcher_state-{digest}.json"))
        # Worker injection points (tests script these; prod uses the
        # bounded run_worker defaults). Processors reach them via `w.`.
        self.drafter = drafter or draft_schema_fix
        self.grouping_drafter = grouping_drafter or draft_grouping
        self.record_worker = record_worker or wk.extract_record
        self.plan_drafter = plan_drafter or draft_plan_today
        self.overlap_drafter = overlap_drafter or draft_overlap_worker
        self.embedder = embedder or embed_pool
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
        # `processor:content-hash` -> failed draft attempts against that
        # exact input. An entry at the budget ceiling IS the parking —
        # _draft skips its item until the text changes. See _invalid_plan.
        self.draft_attempts: dict[str, int] = {}
        # HEAD sha at scan time, stamped into drafted proposals; set once
        # per pass so processors never re-run git themselves
        self._pass_sha = ""
        # The pass's one content walk (rel -> lossy text), built lazily by
        # pass_notes() and reset per scanned pass. See pass_notes().
        self._pass_notes: dict[str, str] | None = None

    def _configure(self, cfg) -> None:
        """Resolve the per-pass knobs: explicit constructor args win,
        otherwise Spec.md (n_max, disabled_processors, surfaces, inboxes).
        Called at construction and again each full pass, so a Spec.md
        edit takes effect without a restart.

        Spec.md junk never raises — unknown names are reported and
        ignored per key, so a stale Spec configures everything it CAN
        name (raising used to brick every Watcher construction over one
        retired processor name). Only explicit constructor args (the
        test/CLI overrides) still fail loud, via processors.registry."""
        if self._disabled_override is not None:
            # Explicit constructor/CLI names pin the disabled set outright.
            disabled = tuple(self._disabled_override)
            commands_enabled = "inline_command" not in disabled
            registry_disabled = tuple(n for n in disabled if n != "inline_command")
        else:
            # A typo'd (or retired) name in either consent key is REPORTED
            # and ignored, per key: a policy silently gating nothing is the
            # checkbox lie this model exists to remove, so it must be said
            # out loud every pass — but raising here once bricked every
            # Watcher construction (decide, ingest, the pass) over one
            # stale name in a Spec seeded by an older registry. The valid
            # keys still apply; the console refuses to SAVE an unknown
            # name, so this print is the legacy-Spec path only. Known =
            # the default registry plus anything injected — a test running
            # a subset must not make the vault's ordinary Spec read as
            # typos.
            names = {p.name for p in processors.REGISTRY} | {"inline_command"}
            if self._registry_override is not None:
                names |= {p.name for p in self._registry_override}
            unknown = (set(cfg.disabled_processors) | set(cfg.policies)) - names
            if unknown:
                print(f"Spec.md names unknown processor(s) {sorted(unknown)} — ignored")
            # A processor is off when its resolved level says so (the
            # disabled_processors kill switch outranks the policies map —
            # see policy_for). "inline_command" is a pass phase, not a
            # processor, so its kill switch is read directly.
            registry_disabled = tuple(
                p.name for p in processors.REGISTRY
                if cfg.policy_for(p.name) == "off")
            commands_enabled = not (
                "inline_command" in cfg.disabled_processors
                or cfg.policies.get("inline_command") == "off")
        registry = (self._registry_override if self._registry_override is not None
                    else processors.registry(registry_disabled))
        # The engine's own vault handle carries the exclusions (I4): every
        # watcher-side scan — grouping, commands, the Today surface — must
        # be as blind to Private/ as the MCP server's singleton is. Rebuilt
        # per pass so a Spec.md exclude edit takes effect without restart.
        self.vault = Vault(self.vault_path, exclude=cfg.exclude)
        self.n_max = self._n_max_override if self._n_max_override is not None else cfg.n_max
        self.commands_enabled = commands_enabled
        self.processors = registry
        # Hooks are looked up across the FULL registry, not the enabled
        # one: a proposal drafted before its processor was disabled in
        # Spec.md still needs its on_applied (reset_capture) when a human
        # approves it later — disabling stops new scans, not the
        # bookkeeping of already-drafted work.
        hook_source = (self._registry_override if self._registry_override is not None
                       else processors.REGISTRY)
        self._by_kind = {p.name: p for p in hook_source}
        # Capture.md is the only surface left: the canvas's write-ahead
        # log, written before extraction so a dump survives it failing.
        self.capture_rel = cfg.surfaces.get("capture", "")

    # ---- shared machinery processors ride on ----

    def pass_notes(self) -> dict[str, str]:
        """The pass's ONE content walk: every knowledge note as
        {rel: lossy text}, built on first use and shared by the command
        scan, schema validation, and grouping — three consumers, one
        read of the vault. Holding the texts trades memory (bounded by
        the vault's markdown) for two full re-walks per pass.

        Lossy reads only (I6: one mis-encoded note must not wedge the
        pass) — any consumer about to WRITE against a note re-reads it
        strictly itself, so replacement chars can never be written back.
        The command phase refreshes entries it rewrites; nothing else
        writes between cache build and the scans."""
        if self._pass_notes is None:
            surface_dirs = get_config(self.vault_path).surface_dirs
            self._pass_notes = {
                rel: read_lossy(p)
                for p, rel in self.vault.iter_knowledge_notes(surface_dirs)
            }
        return self._pass_notes

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
        self.write_surface(vc.SPEC_REL, spec_seed(self.vault_path),
                           f"seed {vc.SPEC_REL}", action="seed_vault_spec")

    # ---- pass state: what has already been drafted ----
    #
    # Every pass scans. There is no vault memo: the daemon already fires
    # only when its own tree fingerprint moves, so a second change-detector
    # here skipped the scan mainly on passes where nothing had changed
    # anyway — and it bought ~20-70ms on an idle pass in exchange for a
    # whole class of failure where a pass that gave up early memoized
    # "this tree is handled" and stranded the input behind it until an
    # unrelated edit moved the fingerprint. The seen-hashes below already
    # prevent the expensive repetition (redundant LLM drafts); the memo
    # only ever saved the walk.

    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    @staticmethod
    def _seen_from_state(state: dict) -> dict[str, str]:
        """The seen-hash map: processor name -> hash of the text last
        handed to its extractor."""
        return {k: str(v) for k, v in (state.get("seen") or {}).items() if v}

    @staticmethod
    def _attempts_from_state(state: dict) -> dict[str, int]:
        """Retry budgets, keyed `processor:content-hash`. An entry at the
        ceiling is a PARKED input and persists (it is the parking itself —
        see _invalid_plan); live retries drop on success. Absent in state
        files written before the budget existed."""
        return {str(k): int(v) for k, v in (state.get("draft_attempts") or {}).items()
                if str(v).lstrip("-").isdigit()}

    def _save_state(self) -> None:
        # Parked slots persist (the entry IS the parking — see
        # _invalid_plan), bounded so state.json never grows a scrapheap of
        # dead hashes: oldest fall off, which merely un-parks an input
        # nobody has re-drafted in ages.
        atomic_write_text(self.state_path, json.dumps({
            "seen": self.seen,
            "draft_attempts": dict(list(self.draft_attempts.items())[-64:]),
        }))

    @staticmethod
    def _draft_slot(proc: ProcessorSpec, item: dict) -> str:
        """The retry-budget key for one work item: the processor plus a
        hash of the exact input handed to its drafter. Content-keyed so
        EVERY lane gets a real budget — the deterministic scans
        (schema_fix, grouping) re-find their items each pass and stamp no
        seen-hash to hold a parking on — and so an edit to the text is a
        new slot, a fresh budget, a fresh try."""
        basis = str(item.get("text") or item.get("content") or "")
        return f"{proc.name}:{proposals.content_hash(item['note'] + chr(0) + basis)}"

    def _invalid_plan(self, summary: dict, proc, error: Exception, slot: str) -> None:
        """The drafter answered, and nothing can enter the queue — either
        because the plan fails the proposal schema, or because the drafter
        itself rejected the worker's output (processors.UnusablePlan).
        Both are the same event to a user, and both need this budget.

        Visibly identical to a dead worker (zero drafts), and it used to
        be swallowed by a bare `continue` with the drafter's seen-hash
        ALREADY stamped — so the text became unreachable by any later
        pass while every surface said "nothing extractable". It wasn't:
        extraction ran, and only the plan was malformed.

        So it re-arms like a worker failure — but on a BUDGET. A down
        worker recovers when you restart it; the drafters build the
        proposal shape themselves and the model only fills the fields, so
        a plan rejected for this input tends to be rejected the same way
        every time, and an unbounded re-arm is one LLM call per pass
        forever. The budget rides `slot` (processor + input hash, from
        _draft_slot), and the slot entry ITSELF parks the input once
        spent: _draft skips any item sitting at the ceiling, which holds
        for every lane — including the deterministic scans that re-find
        their items each pass and have no seen-hash to hold a parking on.
        The seen-hash lanes (capture) also get their hash dropped while
        retrying, so the next pass redrafts. Editing the text moves the
        hash: new slot, fresh budget."""
        detail = f"invalid plan: {error}"
        attempts = self.draft_attempts.get(slot, 0) + 1
        self.draft_attempts[slot] = attempts
        retrying = attempts < DRAFT_PLAN_ATTEMPTS
        if retrying:
            self.seen.pop(proc.name, None)   # re-arm the seen-hash lanes
        # `retrying` is load-bearing on the surfaces: "the engine retries on
        # its next pass" is the right thing to say exactly while it's true.
        record = {"processor": proc.name, "error": detail, "retrying": retrying}
        summary.setdefault("worker_errors", []).append(record)
        events.emit(self.vault_path, "worker_error", record)
        print(f"invalid plan from {proc.name} "
              f"({'retrying' if retrying else 'parked'}): {error}")

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
        from chickenskratch_vault import server

        for item in commands.scan(self.pass_notes()):
            result = await commands.INLINE_COMMANDS[item["command"]](item["args"], self.vault_path)
            if result is None:
                continue  # transient failure: the line stays armed, retried next scan
            try:
                server.replace_line(item["note"], item["line"],
                                    commands.render_callout(item, result))
            except VaultError:
                continue  # the line was edited away mid-flight: the user's edit wins
            # The cache entry is now stale by our own hand: refresh it so
            # the proposal scans behind us see the post-command note.
            self.pass_notes()[item["note"]] = read_lossy(self.vault.resolve(item["note"]))
            summary["commands"] += 1

    # ---- stage 3: draft proposals ----

    async def _draft(self, items: list[tuple[ProcessorSpec, dict]],
                     summary: dict) -> tuple[int, int]:
        """Draft up to n_max proposals; drafts whose processor policy and
        verifier allow it apply immediately. Returns (drafted, auto_applied)."""
        cfg = get_config(self.vault_path)
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
            slot = self._draft_slot(proc, item)
            if self.draft_attempts.get(slot, 0) >= DRAFT_PLAN_ATTEMPTS:
                continue  # parked: this exact input already returned N bad plans
            attempts += 1
            # The activity signal: which processor is thinking, about what.
            # Emission is telemetry (events.emit swallows OSError) — the
            # indicator is a courtesy, never a stage a pass can fail on.
            events.emit(self.vault_path, "draft_started",
                        {"processor": proc.name, "note": item["note"]})
            try:
                p = await proc.draft(self, item)
            except VaultError as e:
                # This processor's worker is down/misbehaving. Others keep
                # drafting — a dead LLM worker must never starve the
                # model-free processors (//commands) behind it in line.
                # RECORDED, never swallowed: a dead worker and a note with
                # nothing in it produce the same zero drafts, and reporting
                # the second when the first happened tells the user their
                # dump was empty. It wasn't — it re-arms (the seen-hash is
                # only stamped past a successful draft) and the next pass
                # retries it, which is what the surfaces must say.
                failed.add(proc.name)
                # `retrying` is always true here: the seen-hash is stamped
                # past a successful draft, so a worker that never answered
                # left the input armed, and a down worker recovers the
                # moment it comes back. The budgeted case is _invalid_plan.
                record = {"processor": proc.name, "error": str(e), "retrying": True}
                summary.setdefault("worker_errors", []).append(record)
                # Also on the stream: typing straight into Capture.md is a
                # first-class input, and that path has no request to answer
                # — without this, a dead worker is invisible on every
                # surface until you notice tasks never appearing.
                events.emit(self.vault_path, "worker_error", record)
                continue
            except processors.UnusablePlan as e:
                # Extraction RAN and came back broken. Visibly identical to
                # a quiet vault, which is why it needs the same budget as a
                # schema-invalid plan: without one, a lane that re-finds its
                # items deterministically (schema_fix, grouping) pays a
                # fresh LLM call for the same rejected output on every
                # pass, forever, reporting drafted=0 and no error.
                self._invalid_plan(summary, proc, e, slot)
                continue
            if p is None:
                # A correct, cheap "nothing to propose" — the judge saw
                # only distinct pairs, the partition gained nothing. It
                # costs the input its budget on purpose.
                self.draft_attempts.pop(slot, None)
                continue
            try:
                proposals.save(p, self.proposals_dir)
            except ValueError as e:
                # An invalid plan never enters the queue — but it is a
                # draft-stage failure, not a quiet vault. See _invalid_plan.
                self._invalid_plan(summary, proc, e, slot)
                continue
            # A plan this processor CAN build retires any budget this
            # input was burning down.
            self.draft_attempts.pop(slot, None)
            drafted += 1
            # Auto-apply is the processor's POLICY level (Spec.md
            # `policies`, via policy_for), only
            # against an unmoved note, and never when the drafter itself
            # flagged something a human must see (needs_review). The
            # levels:
            # - ask: every proposal queues;
            # - verified: its deterministic verifier must pass —
            #   verification degrades to review, never blocks — and a
            #   processor without one (the judgment lanes) queues;
            # - full (the standing, human-written grant; I2 amendment):
            #   applies regardless — a failing or absent verifier's
            #   objections are recorded on the proposal, so the audit
            #   trail names exactly what nobody vouched for.
            level = cfg.policy_for(proc.name)
            if level in ("verified", "full") and not p.get("needs_review"):
                failures: list[str] = []
                if proc.verify is not None:
                    try:
                        failures = proc.verify(self, item, p)
                    except Exception as e:
                        # A verifier that RAISES is a verifier that didn't
                        # vouch, so it means what a verifier that returns
                        # failures means. It must never take the pass down
                        # with it.
                        failures = [f"verifier raised {type(e).__name__}: {e}"]
                elif level != "full":
                    continue  # judgment queues under `verified`
                if failures and level == "full":
                    p["verifier_failures"] = failures  # applied, on the record
                    failures = []
                if failures or applier.is_stale(p, self.vault_path):
                    continue
                head = self.vault.history(1)
                before_sha = head[0]["sha"] if head else ""
                try:
                    applier.apply(p, self.vault_path)
                except Exception as e:
                    # poison plan: fail the proposal, never the pass (a
                    # pending one would re-verify and re-crash every pass)
                    proposals.resolve(p, "failed", self.proposals_dir)
                    print(f"auto-apply failed for {p['id']}: {type(e).__name__}: {e}")
                    continue
                # An autonomous apply must be as easy to undo as it was to
                # make: its own harness commits ride the resolved file, so
                # the dashboard's audit row reverts THIS decision without
                # hunting the git log. (Manual-edit sweep commits are the
                # user's work and never included — changes.py owns that
                # rule for this path and the human-verdict one both.)
                p["undo"] = changes.harness_commits_since(self.vault, before_sha)
                # No corpus row: auto-applies are not human labels.
                proposals.resolve(p, "auto", self.proposals_dir)
                auto += 1
                self._post_apply(proc, p)
        return drafted, auto

    def _act(self, decisions: list[tuple[str, str]], pending: list[dict]) -> tuple[dict, list[dict]]:
        """Act on checked boxes. Returns (counts, proposals still pending).

        `acted["undo"]` carries the shas an approve just wrote, so the
        dashboard's toast can offer undo without re-deriving the range
        from a HEAD it read before the pass lock — a wait of up to
        DECISION_LOCK_WAIT_S during which a concurrent pass's own
        auto-applies would land inside the window and be swept into the
        user's undo."""
        acted: dict = {"approved": 0, "rejected": 0, "stale": 0, "failed": 0,
                       "undo": []}
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
            head = self.vault.history(1)
            before_sha = head[0]["sha"] if head else ""
            try:
                applier.apply(p, self.vault_path)
            except Exception as e:
                # A poison plan (e.g. a drafter emitting args the tools
                # reject) must fail ITSELF, never the pass: left pending,
                # the approved proposal would re-crash every pass forever.
                proposals.resolve(p, "failed", self.proposals_dir)
                acted["failed"] += 1
                print(f"apply failed for {p['id']} ({p['title'][:50]}): {type(e).__name__}: {e}")
                continue
            # Same derivation as the autonomous path, taken at the same
            # moment relative to the apply: bracketing THIS plan's writes
            # is the only way the range can mean "undo this decision".
            p["undo"] = changes.harness_commits_since(self.vault, before_sha)
            acted["undo"] += p["undo"]
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
        extraction would silently vanish. Dropping the entry by kind
        covers every extractor at once — a processor that keeps no
        seen-hash simply has nothing to drop."""
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
        """One engine pass. `decisions_only` is the fast lane: act on
        decisions, done — no scanning or drafting.

        `decisions` injects explicit (pid, verdict) pairs — the human
        verbs' channel (service.decide, the dashboard button, `chickenskratch
        decide`). Every channel converges on _act under the pass lock,
        so two front doors can never double-apply (_act resolves each
        pid once).

        `lock_wait` seconds to wait for the pass lock, and it selects the
        contention contract. The default 0 is the timer lane: skip and
        report summary["locked"], because the running pass covers this
        tick. Any positive wait is a human verb, and losing the lock after
        waiting is exceptional — LockBusy, not a summary of a pass that
        quietly did nothing. Anything carrying `decisions` MUST wait:
        those verdicts exist nowhere else, so a `locked` return would
        silently discard a human's decision."""
        summary = {
            "commands": 0, "drafted": 0, "auto": 0, "approved": 0, "rejected": 0,
            # `failed` counts applies that threw; `worker_errors` carries the
            # DRAFT-side failures (a down or rate-limited LLM worker), because
            # "drafted 0" alone can't tell "nothing to extract" from "the
            # worker never answered" — and the surfaces must not guess.
            "stale": 0, "failed": 0, "worker_errors": [],
            # Shas this pass's APPROVALS wrote, for the dashboard's undo
            # toast (_act fills it; see harness_commits_since).
            "undo": [],
            # False only for a decisions-only pass: every full pass scans.
            "scanned": False, "locked": False, "cancelled": False,
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
            # Point chickenskratch_vault's in-process singleton at THIS vault for the
            # whole pass — scans, verifiers, and applies all resolve through
            # it, and no processor may depend on a sibling having set it.
            applier.use_vault(self.vault_path)
            state = self._load_state()
            self.seen = self._seen_from_state(state)
            self.draft_attempts = self._attempts_from_state(state)
            if not decisions_only:
                # Opens the activity indicator; pass_complete closes it.
                events.emit(self.vault_path, "pass_started", {})
                self._ensure_spec()
                # Re-read the spec each pass: Spec.md edits (worker model,
                # n_max, disabled processors, inboxes) take effect without
                # restarting the daemon. A bad value degrades to the
                # current configuration, never a crashed pass.
                cfg = get_config(self.vault_path)
                # Never raises for Spec.md content (unknown names are
                # reported and ignored inside); the override branch was
                # validated at construction.
                self._configure(cfg)
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
                self._save_state()
                self._emit_pass(summary)
                return summary
            vs = applier.vault_state(self.vault_path)
            # One content walk per pass: reset so pass_notes() rebuilds
            # against the tree THIS pass reads.
            self._pass_notes = None
            flipped, pending = self._auto_stale(pending, vs)
            summary["stale"] += flipped
            self._pass_sha = vs[0]
            # Commands execute before the proposal scan and refresh
            # their notes in the pass cache, so drafts see the
            # post-command note.
            await self._run_commands(summary)
            # One pending proposal per note is a queue invariant, so it
            # is enforced here — not re-implemented in every scan. The
            # seen set also dedupes WITHIN the pass: when two
            # processors both want a note, registry priority wins.
            # Cross-note processors (claims_note=False) stand outside
            # the invariant in both directions: their anchor note is
            # bookkeeping, and letting it claim a real record note
            # would starve that note's own processors behind a
            # proposal that barely concerns it.
            seen_notes = {p["drafted_against"]["note"] for p in pending
                          if getattr(self._by_kind.get(p["kind"]), "claims_note", True)}
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
                    # enforce for their stages. Nothing to re-arm: the
                    # next pass scans again regardless.
                    print(f"scan failed for {proc.name}: {type(e).__name__}: {e}")
                    continue
                for item in found:
                    if not proc.claims_note:
                        items.append((proc, item))
                    elif item["note"] not in seen_notes:
                        seen_notes.add(item["note"])
                        items.append((proc, item))
            summary["drafted"], summary["auto"] = await self._draft(items, summary)
            summary["scanned"] = True
            summary["cancelled"] = bool(self.should_cancel())
            self._save_state()
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
        prog="chickenskratch-watch",
        description="One engine pass: scan the vault, draft proposals, auto-apply what verifies",
    )
    parser.add_argument("-v", "--vault", help="vault path (default: config, or $CHICKENSKRATCH_VAULT_PATH)")
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
    w = Watcher(vault, n_max=args.n_max,
                disabled=tuple(args.disable) if args.disable else None)
    summary = asyncio.run(w.run_once())
    if summary["locked"]:
        print("watch pass: skipped — another pass is already running")
        return
    print(
        f"watch pass: {summary['commands']} command(s), drafted {summary['drafted']}, "
        f"auto-applied {summary['auto']}, applied {summary['approved']}, "
        f"rejected {summary['rejected']}, stale {summary['stale']}"
        + (f", FAILED {summary['failed']}" if summary.get("failed") else "")
    )
    for err in summary.get("worker_errors", []):
        print(f"  ⚠ {err['processor']}: {err['error']}"
              + (" — retries next pass" if err.get("retrying", True) else " — parked"))


if __name__ == "__main__":
    main()
