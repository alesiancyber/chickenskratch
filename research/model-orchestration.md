> [!IMPORTANT] RESEARCH — non-operational
> A what-if document, maintained against the current architecture
> (rewritten 2026-07-23 from the pre-pages-era `DESIGN-mixed-models.md`;
> the original is in git history). It prescribes nothing and nothing in
> it is built unless it says so; [PHILOSOPHY.md](../PHILOSOPHY.md)
> governs operational reality. Its purpose: when the b70 arrives and the
> bakeoff starts, this is the hypothesis sheet.

# Research: model orchestration for the local endgame

The question this document explores: once a ~27B dense model runs
locally on the b70 (32 GB, ~600 GB/s), how should the harness spend it —
and how much of the work can stay on much smaller, faster models
without giving up the auto-apply trust the verifiers earn?

Literature this draws on: Anthropic's
[Building Effective Agents](https://www.anthropic.com/research/building-effective-agents)
(workflow patterns), [RouteLLM](https://arxiv.org/abs/2406.18665)
(routing transfers across model pairs; weak preference data is enough
fuel), FrugalGPT/HybridLLM (cascade: cheap first, escalate on signal),
[BFCL](https://gorilla.cs.berkeley.edu/leaderboard.html) (screen
tool-calling before downloading). Mixture-of-Agents parallel layering
remains a non-goal.

---

## 1. The baseline (what exists, 2026-07-23)

- **All production LLM work is single-shot worker calls.** Three
  workers (`extract_tasks`, `schema_fix`, `grouping` —
  `src/obsidian_mcp/workers.py`): one bounded call, jsonschema-validated
  reply, one retry, fail-loud. One global worker model, chosen by
  Spec.md `worker:` (a models.yaml profile name; temporarily Gemini for
  iteration speed).
- **Two classes of change.** Commands (`//azure`) are user-ordered and
  currently model-free; proposals are harness-inferred, and the
  deterministic **verifiers** (extraction fidelity, additive-shape) gate
  checkbox-free application.
- **The eval suite is the selection gate.** 69 worker cases × repeats,
  scored by the production verifiers (a passing schema_fix case means
  "would have auto-applied"), plus paraphrase clusters against
  phrasing luck. The 22-case agent suite covers the frozen CLI chat.
- **Chat is a frozen CLI escape hatch.** No message stream to route; no
  orchestrator loop in production.
- **Corpus** captures every human judgment (Review.md decisions,
  reverts) as self-contained JSONL — the preference data a learned
  placement policy would train on.

The architectural consequence that reshapes everything below: **the
old router problem dissolved.** There is no ambiguous chat message to
classify — every LLM invocation already knows its task class, because a
processor or command invoked it by name. "Routing" reduces to
**placement policy**: which model serves which worker, and when to
escalate. `classify_intent` is dead; nothing here resurrects it.

## 2. Goals

1. **Latency down, capability up, locally.** The cheapest model that
   passes the eval suite *for that worker* does the job; the 27B is the
   strongest local rung, not the default for everything.
2. **Nothing brittle.** Every mechanism is optional, has a
   deterministic fallback, degrades to today's behavior when absent,
   and rolls back by deleting one config key.
3. **Data flywheel.** Eval reports + corpus labels + auto-apply/queue
   rates make placement decisions measurable, and a learned policy
   *possible later* without committing to one now.

## 3. Design principles

1. **Placement may be aggressive because writes are guarded.** The
   verifier/queue layer — not the model — decides what touches the
   vault. A wrong placement costs latency or a queued (rather than
   auto-applied) proposal, never data. If a change weakens a write
   guarantee to make placement easier, the change is wrong.
2. **Fail upward.** Worker junk output, schema failure, verifier
   failure — all resolve toward the *strongest* resident model or the
   Review queue, never silently downward.
3. **Every rung is optional.** b70 offline ⇒ M5-only or cloud-profile
   mode, identical to today. No component sits on the critical path
   without a non-LLM fallback (the engine already survives a dead
   worker: that processor skips, commands and other processors
   continue).
4. **Workers interpret, they never mutate.** All writes flow through
   the gated vault tools and the two-class contracts.
5. **Shadow before live.** Placement behaviors ship first in a mode
   that logs what they *would* do; promoted only after the log agrees.

## 4. Hardware & memory budget (planning figures, verify on the box)

b70: 32 GB, ~600 GB/s. Decode is bandwidth-bound: tok/s ≈ BW ÷
weight-bytes × ~0.65. Q4_K_M estimates:

| Model class | Weights | KV @16k | Est. decode  | Role                     |
|-------------|---------|---------|--------------|--------------------------|
| 27B-class   | ~16.5 GB| ~2.0 GB | ~22–28 tok/s | top rung (hard workers, chat, LLM commands) |
| 9B          | ~5.7 GB | ~1.0 GB | ~60–75 tok/s | mid rung — must earn its 7 GB |
| 4B          | ~2.6 GB | ~0.5 GB | ~120–150     | cheap worker rung        |
| 2B (e2b)    | ~1.6 GB | ~0.3 GB | ~180–220     | cross-family voter       |

**Residency plan A (recommended start):** 27B + one 2–4B worker ≈ 24 GB,
~8 GB headroom. Best uses of headroom, in order: longer 27B context
(KV at 32k ≈ double), then a second small model *from a different
family* (~2 GB) for cross-family voting (§7). A same-family second copy
buys nothing. **Plan B** (add a 9B mid rung, ≈29 GB) only if bakeoff
data shows a worker where 9B matches 27B quality *and* the latency
delta matters — at ~25 tok/s for single-shot worker calls of a few
hundred output tokens, it plausibly never does.

**M5 (16 GB) — degraded mode, always functional.** The harness host
runs its own small models under Ollama/MLX when the b70 is
unreachable. CI/tests never require the b70.

## 5. Tiers (as they exist after the two-class refactor)

- **Tier 0 — code, no model.** Still the cheapest win and it keeps
  growing: `//azure` is an entire user-facing feature at tier 0, and
  the verifiers moved "is this fix safe?" from model judgment to code.
  Every tier-0 conversion is one less thing any model can get wrong.
- **Tier 1 — cheap workers (2–4B).** Single-shot JSON extraction —
  exactly the regime where small models plateau near frontier (§9.2).
- **Tier 2 — the 27B.** The strongest local rung: hard workers
  (schema_fix on messy notes), escalation target, CLI chat, future
  LLM-backed `//commands` (`//summarize`) — which write directly (the
  user watches the landing spot), making per-command model choice a
  quality knob, not a safety knob.
- **Tier 3 — cloud (explicit, never silent).** Today's temporary
  Gemini arrangement, kept afterward as an opt-in profile. Vault text
  leaving the machine is a user decision every time (PHILOSOPHY I4).

## 6. Placement policy (the successor to "the router")

### 6.1 Per-worker model assignment — the first unbuilt step

Today `worker:` is one global choice. The research question is whether
the split brain wins:

```yaml
# Spec.md frontmatter (hypothetical)
worker: b70-27b              # default rung
workers:
  extract_tasks: b70-4b      # single-shot extraction: small-model plateau
  grouping: b70-4b
  schema_fix: b70-27b        # hardest judgment, verifier-gated anyway
```

Implementation cost is small (the env plumbing already supports
per-call overrides in spirit; `WORKER_<NAME>_MODEL` was the old sketch)
— but it ships only when the bakeoff shows a per-worker quality gap
worth managing. If the 27B wins everything at acceptable latency, one
global `worker:` stays and this section dies happily.

### 6.2 What killed the classifier

The old design spent a 2B model + taxonomy + shadow phase classifying
chat intents. The two-class refactor made the invoker the classifier:
capture knows it's extraction, the schema scanner knows it's a fix,
`//azure` knows it's a search. **Placement needs a table, not a
model.** (If chat ever un-freezes, this section is where intent
classification would return — with the same fail-upward rules.)

## 7. Cascade — verifier-triggered escalation

The original cascade needed invented signals (loop guards, iteration
caps). The current architecture provides a better one for free: **the
deterministic verifier is a ground-truth quality oracle for exactly the
cases where auto-apply matters.**

Hypothetical flow, per proposal draft:

1. Cheap worker (4B) drafts.
2. Schema-invalid reply or verifier failure → **one** escalation:
   re-draft on the 27B.
3. 27B draft passes → auto-apply as normal. Fails again → the Review
   queue, exactly as today.

Properties worth noting:

- Escalation triggers are already-computed events — no new detectors,
  no model-judging-model.
- The failure mode of a too-small model becomes *latency + queue rate*,
  never bad writes: misplacement is visible in `Changes.md` cadence and
  the auto-apply ratio, not in vault damage.
- The eval suite measures the cascade directly: run the worker suite
  per rung; the cases the 4B fails but the 27B passes are precisely the
  escalation traffic, and their frequency prices the cascade.
- One escalation per item, no de-escalation logic, ever.

For **commands**, cascade is simpler still: a transient executor
failure already re-arms the line; an LLM-backed command could retry
once on the top rung before re-arming.

## 8. Voting & cross-family checking (unbuilt, still promising)

The verifiers catch structural violations but not judgment errors
*within* the allowed envelope (a defensible-but-wrong due date; a
plausible owed_by). `votes: 2` — run the prompt at two temperatures, or
better, on **two models from different families** (qwen's inventions
and gemma's omissions were observed to be disjoint) — turns
disagreement into a `confidence: low` flag on the proposal line in
Review.md. Cost at small-model speeds: sub-second. Residency plan A's
headroom exists partly for this. Gate: only worth building if corpus
data shows rejected proposals clustering in the judgment-error class
the verifiers can't see.

## 9. Model selection (the bakeoff — procedure now mostly real)

### 9.1 Reading BFCL as a shortlist prior

FC rows only (the FC/Prompt split is enormous); **Irrelevance >
Relevance** (irrelevance detection maps to the spurious-action failure
class — the invisible, dangerous one); the Latency column is a
thinking-mode detector (a great Multi-Turn score at 170 s mean latency
is a thinking model — eval non-thinking variants or disable thinking);
Format-Sensitivity Max Delta is a fragility score for small rungs.
Ignore Cost and the agentic columns. The board lags local releases, so
family priors shortlist — **the worker suite is the acceptance test.**

### 9.2 The single-turn plateau — now the load-bearing finding

BFCL's structural result: single-turn structured calling is
near-saturated down to ~4B (Qwen3-4B within noise of Opus-class on
single-turn AST) while multi-turn collapses with size. The two-class
refactor quietly moved **all production LLM work into the single-turn
regime** — every worker call is one-shot. Consequences:

- The cheap rung loses almost nothing by being 4B *for the regime it
  operates in* — which is exactly why per-worker placement (§6.1) and
  the verifier cascade (§7) are plausible at all.
- Multi-turn ability now matters only for the frozen CLI chat and any
  future conversational surface — it should be weighed accordingly in
  27B selection, not dominate it.
- The user's observation that some models are exceptional one-shot
  instruction followers is this plateau, seen from the terminal.

### 9.3 Procedure (per candidate)

1. Shortlist via §9.1 family priors. No download before this step.
2. Profile entry in models.yaml → `uv run harness-worker-eval -m <model>`
   (69 cases × 3 repeats; verifier-scored; watch *cluster-level*
   failures — a model failing a whole paraphrase cluster has a
   systematic hole where auto-apply would trust it). Add
   `harness-eval -m` if the candidate will also serve chat.
3. Record the report in `evals/reports/` — a fast, cheap *no* is a
   good outcome.
4. A candidate for the *cheap* rung must beat the incumbent on the
   specific workers it would serve, not globally.

### 9.4 27B-class shortlist (priors only; step 2 decides)

| Candidate | Prior |
|---|---|
| qwen3.5-32B instruct-class | Best open multi-turn lineage, but earned in thinking mode — must eval non-thinking. Q4 ≈ 19 GB squeezes headroom. |
| gemma4-27B-class | Weak board prior (gemma-3 era was Prompt-only/fragile) but gemma4-12b's strong run on this suite is direct family evidence. Q4 ≈ 16.5 GB fits Plan A cleanly. |
| xLAM-2-32b-class FC tune (wildcard) | Board-best sub-35B multi-turn; near-zero agentic columns irrelevant here; cc-by-nc license and possible prose degradation are the risks. |

## 10. Serving stack on the b70

**v1: Ollama** (multi-model residency + pinning in one process; the
harness already speaks OpenAI-compatible to anything). Settings that
are part of the design: `OLLAMA_MAX_LOADED_MODELS=2`, `keep_alive=-1`
for residents (no cold starts of a 16 GB load), explicit
`OLLAMA_CONTEXT_LENGTH` matched by the profile's `context_tokens`,
`OLLAMA_NUM_PARALLEL=1`. The harness needs zero code: b70 profiles are
`base_url` entries in models.yaml, and Spec.md `worker:` flips the
whole engine to the box with one note edit. `harness-backends` shows
remote endpoints as "external" — correct; remote start/stop over SSH
stays out of scope (half-dead remote processes the harness thinks it
owns are a failure-mode factory). vLLM/llama.cpp swap in behind the
same `base_url` if throughput ever demands it.

## 11. Observability

Most of the old "routing log" design is now covered by things that
exist: eval reports (per-model, per-case), corpus rows (human
judgments on proposals), `Changes.md` (what auto-applied), and the
worker health line. The remaining gap, worth building *with* §6.1/§7
and not before: a per-draft outcome record — worker, model, rung,
verifier result, escalated?, final disposition (auto/queued/approved/
rejected/stale), latency. JSONL beside the corpus, no note text.
That file is the §12 R4 training set, and meanwhile every
"cheap-rung fail, top-rung pass" row is a candidate eval case.

## 12. Rollout — each step gated and reversible from today's baseline

- **R0 — the bakeoff (machinery exists; waiting on the b70).**
  Run §9.3 across the 25–32B field; pick the top rung; flip Spec.md
  `worker:`. Gate: ≥ incumbent on the worker suite. Rollback: edit the
  note back.
- **R1 — per-worker placement (§6.1).** Only if R0 reports show a
  per-worker gap. Gate: split assignment ≥ single-model baseline on
  every worker's cases. Rollback: delete the `workers:` map.
- **R2 — verifier cascade (§7).** Only if R1 leaves a cheap rung in
  place. Gate: scripted-failure tests show exactly one escalation;
  auto-apply rate ≥ single-27B baseline; added p50 latency acceptable.
  Rollback: one config flag.
- **R3 — cross-family voting (§8).** Gated on corpus evidence of
  judgment-error rejections. Rollback: `votes: 1`.
- **R4 — learned placement.** Only when the §11 log has thousands of
  labeled rows *and* measured misplacement is the dominant remaining
  cost. RouteLLM says the transfer works if that day comes.

## 13. Failure modes → designed response

| Failure | Response |
|---|---|
| b70 unreachable | Spec.md `worker:` names an unreachable profile ⇒ drafting for LLM processors skips (engine already isolates per-processor failure; commands and decisions unaffected); flip the note to a local/M5 profile. A health-check auto-fallback profile is a candidate refinement. |
| Cheap rung emits junk | jsonschema + retry (exists) ⇒ escalate (R2) or skip; never a bad write. |
| Verifier failure storm from a too-small model | Visible as queue growth / auto-apply rate drop in Changes.md; fix is a placement edit in Spec.md. |
| VRAM eviction thrash | `MAX_LOADED_MODELS` cap + `keep_alive=-1` + Plan A headroom. |
| Silent context truncation | Explicit server context length + matching `context_tokens` (existing rule). |
| Eval-suite overfitting to one model | Paraphrase clusters (exist); judge failed cases against written policy, not against the model; BFCL as an external cross-check at shortlist time. |

## 14. Open questions

1. Which 27B checkpoint (§9.4 — the bakeoff decides).
2. Does per-worker placement (§6.1) ever beat one good 27B at ~25
   tok/s for calls this short? (Honest possibility: no — in which case
   this document's answer is "one model, chosen well, verifier-gated,"
   and that is a fine answer.)
3. Where do rejected proposals cluster — structural (verifier-visible)
   or judgment (voting territory, §8)? Corpus data answers this.
4. Should a future LLM-backed `//command` get a per-command model
   knob, or always the top rung? (Commands write directly; quality is
   the only axis.)

---

*The safety story in one line, stronger than the original's: placement
only ever chooses **which model drafts** — the verifiers, the queue,
and the two-class contracts decide **what touches the vault**, and
they don't know or care which model was talking.*
