> [!IMPORTANT] RESEARCH — non-operational
> Restored 2026-07-23 from `DESIGN-mixed-models.md` (deleted in the docs
> consolidation, `5bf7488`) because the routing/cascade/serving research
> stays relevant to the local-model endgame: a ~27B dense worker on the
> b70 (32 GB), chosen by eval bakeoff. This document is a **what-if**:
> it is not canon, prescribes nothing, and is not maintained against the
> code. [PHILOSOPHY.md](../PHILOSOPHY.md) governs operational reality.
> Known divergences since writing: the harness runs a single worker
> profile (Spec.md `worker:`, temporarily Gemini for test iteration); no
> router, cascade, votes, or classify_intent exists; the worker eval
> suite (69 cases, verifier-scored) is the selection gate §11 sketches.

# Design: mixed-model routing, workers, and cascade

*Status: proposed — routing/cascade/classifier remain unapproved and
unbuilt. **Exception, built 2026-07-22 (DEVPLAN Phase 3):** the §7.1
worker-contract subset shipped as the `WorkerSpec` registry in
`src/obsidian_mcp/workers.py` — jsonschema-validated output, per-spec
timeout/temperature, one retry, fail-loud on dropped items (the §7.3
`due: null` someday policy is enforced and evaled). `votes:`,
`classify_intent`, and `verify_extraction` are NOT built. · Hardware
assumption: the b70 (32 GB @ ~600 GB/s) is on the network and serving
models; the M5 MacBook (16 GB) keeps running the harness and remains a
fully functional fallback on its own.*

The literature this design draws on: Anthropic's
[Building Effective Agents](https://www.anthropic.com/research/building-effective-agents)
(workflow patterns, workflow-vs-agent line),
[RouteLLM](https://arxiv.org/abs/2406.18665) (routing transfers across model
pairs; preference data is the fuel), FrugalGPT/HybridLLM (cascade: cheap
first, escalate on signal), and [BFCL](https://gorilla.cs.berkeley.edu/leaderboard.html)
(screen tool-calling ability before downloading anything).
Mixture-of-Agents-style parallel layering is explicitly a non-goal (§13).

---

## 1. Goals

1. **Latency down, capability up.** Route each request to the cheapest model
   that passes evals for that request class; put a 27B-class orchestrator at
   the top of the local stack now that the b70 can hold one.
2. **Nothing brittle.** Every new component is optional, has a deterministic
   fallback, degrades to today's behavior when absent, and can be rolled back
   by deleting one config block.
3. **Data flywheel.** Every routed request logs enough to (a) grow the eval
   suite from real failures and (b) make a learned router *possible later*
   without committing to one now.

## 2. Non-goals

- A learned/neural router in v1 (23 eval cases and a few hundred logged
  requests can validate heuristics, not train models — see §12 P4 gate).
- Mixture-of-Agents parallel layering (benchmarked on chat quality, not tool
  calling; costs N resident models; revisit only if the b70 proves roomy).
- Cloud as a default rung. The cloud escape hatch stays exactly that — an
  explicit profile, never silently reached.
- Multi-user, multi-vault, or concurrent-conversation serving.

## 3. Design principles (extends the README's three rules)

4. **Routing may be aggressive because writes are guarded.** confirm_writes,
   the loop guard, and git snapshots bound the blast radius of any misroute.
   A wrong route costs latency, never data. This is the load-bearing insight;
   if a change weakens a write guarantee to make routing easier, the change
   is wrong.
5. **Fail upward.** Any uncertainty — classifier timeout, low confidence,
   unknown intent, worker unreachable — resolves to the *strongest* resident
   model, never the weakest. Misrouting up wastes tokens; misrouting down
   produces the plausible-but-wrong failures that are hardest to catch.
6. **Every tier is optional.** b70 offline ⇒ M5-only mode, identical to
   today. Worker model missing ⇒ deterministic fallback or orchestrator does
   the job itself. No component may sit on the critical path without a
   non-LLM fallback.
7. **Workers interpret, they never mutate.** (Already the `extract_tasks`
   contract — promoted to a hard rule for all workers.) All writes flow
   through the orchestrator's normal gated tools.
8. **Shadow before live.** Every routing behavior ships first in a mode that
   logs what it *would* do without doing it, and is promoted only after the
   log says it's right.

## 4. Hardware & memory budget

b70: 32 GB, ~600 GB/s. Decode is bandwidth-bound: tok/s ≈ BW ÷ weight-bytes
× ~0.65 efficiency. All numbers are Q4_K_M estimates to be verified on the
box in P3 — treat them as planning figures, not facts.

| Model class        | Weights | KV @16k | Est. decode  | Role                    |
|--------------------|---------|---------|--------------|-------------------------|
| 27B-class          | ~16.5 GB| ~2.0 GB | ~22–28 tok/s | orchestrator (top rung) |
| 9B                 | ~5.7 GB | ~1.0 GB | ~60–75 tok/s | optional mid rung       |
| 4B                 | ~2.6 GB | ~0.5 GB | ~120–150     | workers                 |
| 2B (e2b)           | ~1.6 GB | ~0.3 GB | ~180–220     | classifier / extraction |

### Residency plan A — recommended start

| Resident on b70                  | Memory        |
|----------------------------------|---------------|
| OS + serving stack               | ~2.5 GB       |
| 27B orchestrator + KV            | ~18.5 GB      |
| 2–4B worker (classifier+extract) | ~3.0 GB       |
| **Total / headroom**             | **~24 / 8 GB**|

Two rungs on the box: worker tier and 27B. No mid rung — at 600 GB/s the 27B
is fast enough (~25 tok/s) that a 9B mid rung must *prove* it earns its
7 GB (§12 P3 go/no-go). Headroom has two good uses, in priority order:
**longer orchestrator context** (KV at 32k ≈ double the 16k figure) and a
**second worker model from a different family** (~2 GB, e.g. gemma-e2b
beside a qwen-4B) for cross-family voting (§7.2). Both together still
clear ~4 GB of slack; a same-model second copy buys nothing (§7.2).

### Residency plan B — only if evals justify a mid rung

27B + 9B + 2B ≈ 29 GB. Fits, but one context-length bump away from eviction
thrash. Adopt only with pinning configured (§10) and after Plan A data shows
a request class where 9B quality is equal and its latency win matters.

### M5 (16 GB) — degraded mode, always functional

gemma4-12b-mlx (current default) + optionally the 2B worker under Ollama.
When the b70 is unreachable this **is** the system, and it is exactly
today's system. CI/tests never require the b70.

## 5. Model tiers

- **Tier 0 — code, no model.** Date arithmetic, kanban/task formatting,
  pagination, citation appending, git snapshots. The design's cheapest win
  remains moving work *into* this tier; every tier-0 conversion is one less
  thing any model can get wrong.
- **Tier 1 — workers (2–4B).** Narrow generative subtasks, read-only,
  structured JSON out (§7). Initial set: `extract_tasks` (exists),
  `classify_intent` (new), `verify_extraction` (new, voting checker).
- **Tier 2 — routed responders.** Profiles the router may pick per request:
  the M5 12B today; optionally the 9B for read-only classes (Plan B only).
- **Tier 3 — orchestrator / top local rung.** 27B-class on the b70. Default
  for anything ambiguous, multi-step, or write-bearing. Candidate checkpoint
  chosen by the §11 screening procedure, not by assumption.
- **Tier 4 — cloud escape hatch (optional, off by default).** Existing
  roadmap item; becomes the rung above 27B. Reached only by explicit user
  action or an escalation the user confirms — never silently (vault text
  leaves the machine; that is a user decision every time).

## 6. Router

### 6.1 Intent taxonomy (v1 — deliberately coarse)

| Intent        | Examples                                  | v1 route      |
|---------------|-------------------------------------------|---------------|
| `read_query`  | "what's due today?", "find notes on X"    | routable down |
| `single_write`| "add a task to renew the domain friday"   | orchestrator  |
| `multi_step`  | "plan my week from the meeting notes"     | orchestrator  |
| `generative`  | capture structuring, drafting, summarizing| orchestrator  |
| `skill`       | `/meeting-capture`, `use_skill`           | skill decides (§6.5) |
| `ambiguous`   | anything the classifier isn't sure about  | orchestrator  |

Only `read_query` is ever routed below the orchestrator in v1. Writes are
*never* routed down until per-class eval evidence exists (P2+), and even
then confirm_writes stays on for down-routed writes regardless of profile
defaults.

### 6.2 Classification

Primary: the `classify_intent` worker (2B, JSON out, temperature 0,
**1.5 s timeout**). Fallback, in order:

1. Worker timeout/error/malformed JSON → deterministic keyword heuristic
   (interrogatives and "what's/show/find/list" → `read_query`; imperative
   verbs from the tool vocabulary → `single_write`; else `ambiguous`).
2. Heuristic unsure → `ambiguous` → orchestrator.

The classifier output includes `confidence: high|low`; `low` maps to
`ambiguous`. The classifier never sees tool schemas or vault content — just
the user message and the last 2 turns (reference resolution: "mark *that*
one done" must not look read-only).

### 6.3 Routing mechanics

- Routing decision happens **per user message**, in the harness
  (`src/harness/agent.py` session setup / `web.py` message entry), *before*
  the responder session is built.
- **Escalation ratchet:** once a conversation escalates to a stronger
  profile, later messages in that conversation start at that rung. No
  automatic downgrades mid-conversation; a new conversation resets. This
  kills route ping-pong and the "it got dumber mid-chat" failure.
- Mid-conversation profile switches reuse the existing
  **rebuild-and-resume** path (already proven for crash recovery and
  toolset changes) — transcript replays into the new profile's session.
  No new conversation-state machinery.
- The chat UI shows the active rung the same way the model picker does
  today; routing is never invisible.

### 6.4 Config surface (models.yaml — additive, delete = rollback)

```yaml
routing:
  enabled: true            # false ⇒ exactly today's behavior
  shadow: true             # log decisions, don't act on them (P0)
  classifier_worker: classify_intent
  default: b70-27b         # the fail-upward target
  routes:
    read_query: qwen3.5-9b # only routable class in v1
  escalate_to: [b70-27b]   # cascade rungs above the routed profile, in order
```

No routing keys inside individual model profiles: which model handles what
is global policy, not a model quirk (rule 2 stays intact).

### 6.5 Skills and routing

A skill may declare `model: <profile>` in its SKILL.md frontmatter (e.g.
meeting-capture pinning the structuring pass to the 27B). Skill declaration
beats router; router beats profile default; explicit `-m` / model picker
beats everything.

## 7. Worker framework (generalizing `src/obsidian_mcp/workers.py`)

### 7.1 Contract (all workers, enforced by the framework not by convention)

1. **Read-only.** No vault imports beyond error types; no tool access.
2. **Structured out.** Each worker declares a JSON schema; output is
   validated against it (`jsonschema`), not just `json.loads`-ed. Schema
   failure after one retry ⇒ `VaultError`, same surface as today.
3. **Bounded.** Per-worker timeout (default 10 s; classifier 1.5 s), one
   retry, temperature ≤ 0.2 (extraction) or 0 (classification).
4. **Non-blocking.** Every call site must have a stated fallback: caller
   does the job itself (orchestrator), deterministic heuristic (router), or
   the feature degrades visibly (capture works without extraction — it
   already does).
5. **Isolated config.** `WORKER_BASE_URL` / `WORKER_MODEL` stay, plus
   per-worker overrides `WORKER_<NAME>_MODEL` for split placement (e.g.
   classifier on b70, extractor local during a b70 outage).

### 7.2 Registry

```python
@dataclass(frozen=True)
class WorkerSpec:
    name: str            # "extract_tasks"
    prompt: str          # template with declared placeholders
    schema: dict         # jsonschema for the reply
    timeout_s: float
    temperature: float
    votes: int = 1       # >1 ⇒ run N times, merge/flag disagreements

WORKERS: dict[str, WorkerSpec] = {...}
```

`votes: 2` for extraction-class workers runs the prompt twice (temperature
0 and 0.2) and returns the union with disagreements flagged
`confidence: "low"` — directly addresses the observed dropped-"someday"-item
failure: an item one pass drops and the other keeps surfaces as a
low-confidence suggestion instead of vanishing. Cost at 2B speeds:
sub-second. Voting is per-spec, not global.

### 7.3 Initial worker set

| Worker              | Model | Purpose                                        |
|---------------------|-------|------------------------------------------------|
| `extract_tasks`     | qwen3.5-4B MLX (A/B'd 2026-07-20: 7/8 vs gemma-e2b's 6/8, ~7x faster; disjoint error sets) | exists; gains schema validation + votes: 2 |
| `classify_intent`   | 2–4B  | §6.2; new; temperature 0, tiny prompt          |
| `verify_extraction` | gemma-e2b (cross-family voter — its misses are disjoint from qwen's inventions) | P4; critique pass over another worker's output |

Policy fix rolled into the extraction schema: undated/"someday" items are
**returned with `due: null`** — dropping them is scored as a failure in the
worker's eval cases. (Resolves the open question from the prototype commit;
suggestions are free to reject, silently missing ones are not.)

### 7.4 Worker evals

Workers get their own fixture cases under `evals/cases/workers/*.yaml`:
input text + expected JSON (scored structurally, same philosophy as the
main suite — never prose). Regression case #1 is the someday-item drop.
CI keeps using scripted fake clients (the existing FakeOpenAI pattern);
live judgment is scored by `uv run harness-worker-eval -m <model>`
(repeat runs per case — a worker that's right 2 of 3 runs is a different
animal from one that's right every time). Reports land in
`evals/reports/workers/`. A/B-ing worker candidates is one command per
model.

Note on residency: worker *roles* are prompts, not models — one resident
worker model serves classify + extract + verify sequentially. A second
resident worker model buys nothing for throughput (one conversation at a
time); what it buys is **cross-family voting diversity**: two different
models disagreeing on an extraction is a far stronger low-confidence
signal than one model resampled at two temperatures. `votes: 2` should
therefore support a cross-model mode when two worker models are
resident.

## 8. Cascade & escalation

### 8.1 Triggers (all already-observable harness events — no new detectors)

| Signal                                  | Already exists as             |
|-----------------------------------------|-------------------------------|
| loop guard fires                        | duplicate-write guard         |
| tool-call validation fails / unknown tool| MCP call error path          |
| empty-reply recovery fires              | empty-reply guarantee         |
| malformed/unparseable tool arguments    | agent loop error path         |
| `max_iterations` hit without an answer  | agent loop                    |
| context trim discarded turns mid-task   | context trimming              |

Any trigger ⇒ escalate to the next rung in `escalate_to`, replay via
rebuild-and-resume, ratchet (§6.3). **One escalation per user message**;
if the top rung also trips, the failure surfaces to the user as it does
today — no retry loops, no silent cloud hop.

### 8.2 What escalation is *not*

- Not a quality judgment on a successful-looking answer. Plausible-but-wrong
  from a down-routed model is real but rare in v1 by construction: only
  `read_query` routes down, where wrongness is visible (the user is looking
  at the answer against their own vault) and mutation-free. The checker
  worker (P4) is the eventual answer; shrinking the down-routed surface is
  the v1 answer.
- Not a downgrade mechanism. There is no de-escalation logic at all.

## 9. Observability — the outcome log

`data/routing_log.jsonl`, one line per user message when routing is enabled
(including shadow mode):

```json
{
  "ts": "...", "conv": "...", "msg": 4,
  "intent": {"label": "read_query", "confidence": "high", "source": "worker"},
  "route": {"profile": "qwen3.5-9b", "reason": "routes.read_query",
             "shadow_only": false},
  "escalation": {"to": "b70-27b", "trigger": "loop_guard"},
  "signals": {"loop_guard": 1, "tool_errors": 0, "empty_reply": 0,
               "iterations": 3, "denied_writes": 0},
  "usage": {"in_tok": 1800, "out_tok": 240, "latency_ms": 4100},
  "user_signal": "none"
}
```

- **No message text, no vault text** by default (`routing.log_text: true`
  exists for debugging sessions only). The log must be safe to hand to
  Claude Code alongside a bug report.
- `user_signal` is filled in retroactively by cheap proxies the UI already
  has: a ↺ revert of the resulting commit ⇒ `revert`; an immediate re-ask ⇒
  `retry`; else `none`. Weak labels, but RouteLLM's core result is that
  weak preference data routes well.
- The stats page gains a per-rung table (requests, escalation rate, p50
  latency) from this log. Evals stay excluded, as today.
- This file is the P4 training set if a learned router ever happens, and
  the source of new eval cases meanwhile (a logged escalation = a candidate
  regression case).

## 10. Serving stack on the b70

**v1: Ollama.** Reasons over vLLM/llama.cpp for this phase: multi-model
serving with per-model pinning in one process, and the harness's adapter
already speaks to it. Settings that are part of the design, not optional:

- `OLLAMA_MAX_LOADED_MODELS=2` (Plan A: 27B + worker) — hard cap so a stray
  request can't evict the orchestrator.
- `keep_alive=-1` for both resident models (pin; no idle unload, no
  first-token cold start of a 16 GB load).
- `OLLAMA_CONTEXT_LENGTH` set explicitly, and `context_tokens` in each b70
  profile set to match — the ctx% meter must not lie (existing rule).
- `OLLAMA_NUM_PARALLEL=1` — one conversation at a time is a design choice.

The harness needs zero code for this: b70 profiles are `base_url` entries
(the commented `homelab-70b` example in models.yaml, made real). The ⚡
Servers panel shows remote backends as "external" — correct and sufficient;
remote start/stop over SSH is explicitly out of scope (a failure mode
factory: half-dead remote processes the harness thinks it owns).

If per-token throughput or scheduling on the b70 ever becomes the
bottleneck, vLLM/llama.cpp swap in behind the same `base_url` — rule 2
means no harness change.

## 11. Model selection procedure (per candidate, before it earns a profile)

### 11.1 Reading the BFCL board for tezcat

(Column analysis from the 2026-07 board snapshot.) The columns are not
equally relevant, and reading them wrong picks the wrong model:

- **FC rows only.** The FC/Prompt split is enormous (Opus-4.5: 77.5 FC vs
  33.5 Prompt) — models must be judged in native function-calling mode,
  which is how the harness drives them. A family that only appears in
  Prompt mode (gemma-3 era) carries a confounded, weak prior.
- **Multi-Turn is the column that predicts our failures.** It maps to the
  observed regression classes (multi-turn reference resolution, duplicate
  writes). Single-turn AST does *not* discriminate — see §11.2.
- **Irrelevance > Relevance.** Irrelevance detection ("correctly makes no
  call") maps to the spurious-mutation failure class — the invisible,
  dangerous one. Low Relevance (missed a needed call) is a *visible*
  failure the user immediately re-prompts. Prefer irrelevance-heavy
  models; it's the same fail-visible principle as rule 5.
- **The Latency column is a thinking-mode detector.** Qwen3-32B posts the
  best open sub-35B Multi-Turn (47.9) at a 170 s mean latency — thinking
  mode, unusable interactively at any local speed. Candidates must be
  instruct/non-thinking variants, or evaled with thinking disabled.
- **Format-Sensitivity Max Delta is a fragility score** (gemma-3-12b:
  67.5 — its accuracy swings wildly with prompt phrasing). High deltas in
  a family are a warning for any small-model rung, and more reason
  model-specific phrasing stays in profiles (rule 2).
- **Ignore:** Cost (API pricing — meaningless locally) and the Web
  Search/Memory agentic columns (long-horizon scaffolding the harness
  provides deterministically; xLAM-2-32b proves the columns are
  independent: 69.5 Multi-Turn, 25.5/20.9 agentic).

### 11.2 The single-turn plateau (why the worker tier can stay tiny)

The board's strongest structural finding for this design: **single-turn
structured tool calling is near-saturated down to ~4B, while multi-turn
collapses with size.** Qwen3-4B-Instruct scores 87.9/76.4 (non-live/live
AST) vs Opus-4.5's 88.6/79.8 — within noise — but 22.1 Multi-Turn vs
Opus's 68.4. Consequence: workers (single-shot JSON extraction — exactly
single-turn-AST-shaped) lose nothing by being 2–4B, and orchestrator
selection should be decided almost entirely on multi-turn behavior. The
tier split in §5 is this table in architectural form.

### 11.3 When the candidate isn't listed — the normal case

The board lags local releases: as of 2026-07 it has no gemma4 and no
qwen3.5. So BFCL screening is a *weak prior from the previous family
generation*, never a gate:

- Family priors can badly understate: gemma-3-12b's board row is dismal
  (30.4 overall, Prompt-only, Max Delta 67.5), yet gemma4-12b — with
  native FC and this harness's scaffolding — scores 22/23 on our suite.
  Board lag + scaffolding effects are why the suite, not the board, is
  the acceptance test.
- When a candidate is unlisted: shortlist from its family's best FC row,
  then go straight to step 2.

### 11.4 Procedure

1. **Shortlist** via §11.1–11.3. No download before this step.
2. **Pull + profile entry + full eval run** (`uv run harness-eval -m <p>`).
   The 23-case suite plus worker cases is the acceptance test; BFCL only
   shortlists.
3. **Record the report** in `evals/reports/` (the phi4-14b "fails
   immediately, no tool support" report is the model for how cheap a fast
   *no* should be).
4. A model that will route *down* must additionally beat the incumbent on
   the specific intent class it's being routed for — not just globally.

### 11.5 Current 27B-class shortlist (priors only — step 2 decides)

| Candidate                  | Family prior from the board                    |
|----------------------------|------------------------------------------------|
| qwen3.5-32B instruct-class | Best open multi-turn lineage (Qwen3-32B 47.9) — but that score was earned in thinking mode at 170 s; must eval non-thinking. Q4 ≈ 19 GB: squeezes §4 headroom. |
| gemma4-27B-class           | Weak confounded prior (gemma-3 was Prompt-only, fragile) — but gemma4-12b's 22/23 on our suite is the stronger, direct evidence for the family. Q4 ≈ 16.5 GB: fits Plan A cleanly. |
| xLAM-2-32b-class FC tune (wildcard) | Board-best sub-35B Multi-Turn (69.5, above Sonnet-4.5) — but near-zero agentic columns and cc-by-nc license; risk that narrow FC tuning degrades prose answers ("general tools keep capable models uncaged"). Eval before dismissing or adopting. |

## 12. Rollout phases — each with a go/no-go gate and a one-line rollback

**P0 — instrument + classify in shadow.**
Build: outcome log; `classify_intent` worker; `routing.shadow: true`.
Behavior change: none.
Gate: over ≥50 hand-labeled real requests, classifier agreement ≥90% and
p95 classification latency ≤1.5 s (else fix taxonomy or prompt, stay in P0).
Rollback: `routing.enabled: false`.

**P1 — route `read_query` live.**
Build: flip `shadow: false`; routes map with the single entry.
Gate: routed-mode eval run (`harness-eval --routed`) scores ≥ the
orchestrator-only baseline on read cases, with lower mean latency; one week
of routing_log shows escalation rate on down-routed messages <15%.
Rollback: delete `routes:` block.

**P2 — cascade/escalation live.**
Build: §8 triggers wired to rebuild-and-resume; ratchet; regression eval
cases for each trigger (scripted fake model forces the trigger, assert
escalation happens exactly once).
Gate: all trigger cases pass; no double-escalation observed in logs.
Rollback: `escalate_to: []`.

**P3 — b70 27B orchestrator + re-baseline.**
Build: winning candidate from §11 as `b70-27b` profile; residency Plan A;
throughput/latency measured on the box (replaces §4 estimates).
Gate: 27B ≥ gemma4-12b's 22/23 on the suite; b70-offline drill passes —
pull the network cable, next message flows through M5 default with router
auto-disabled by the health check, zero errors surfaced beyond a rung-change
notice.
Decision point: Plan B mid-rung only if P1 logs show a class where 9B
matches 27B quality and the latency delta matters in practice.

**P4 — optional, each independently gated.**
- `verify_extraction` checker + `votes: 2` promoted from extraction to
  other workers.
- Cloud rung: profile + explicit user opt-in per §5.
- Learned router: only when routing_log has ≥2,000 labeled (non-`none`
  user_signal or eval-labeled) rows *and* the heuristic router's measured
  misroute rate is the dominant remaining cost. Neither is expected soon;
  RouteLLM says the transfer works when we get there.

## 13. Failure modes → designed response

| Failure                            | Response (all pre-wired, none manual)       |
|------------------------------------|---------------------------------------------|
| b70 unreachable / powered down     | Health check at message time fails ⇒ router disabled, M5 default profile, UI shows rung change. M5 mode **is** today's system. |
| Worker down / not pulled           | Classifier ⇒ keyword heuristic ⇒ `ambiguous` ⇒ fail upward. Extraction ⇒ VaultError suggestion "unavailable", capture still saved raw (already true). |
| Classifier misroutes a write as read| Down-routed model still runs with confirm_writes forced on; worst case is a y/N prompt from a weaker model — deniable, snapshotted, revertible. |
| Plausible-but-wrong from down-route | v1: surface area limited to read-only queries (§8.2). P4: checker worker. |
| Escalation ping-pong               | Ratchet + one-escalation-per-message cap.    |
| VRAM eviction thrash on b70        | `OLLAMA_MAX_LOADED_MODELS` cap + `keep_alive=-1` + Plan A headroom; Plan B requires re-verification. |
| Ollama silent context truncation   | Explicit `OLLAMA_CONTEXT_LENGTH` + matching `context_tokens` (existing rule, now load-bearing). |
| Worker returns junk JSON           | jsonschema validation + 1 retry + VaultError (existing surface). |
| Mid-conversation switch loses state| Only rebuild-and-resume is used — a path with existing tests and production use. No second mechanism. |
| Routing log grows unbounded        | JSONL, pruned like chats (N days), stats aggregated before prune. |
| Eval-suite overfitting             | BFCL as an external cross-check at selection time; routing_log failures feed *new* cases continuously. |

## 14. Open questions (tracked, not blocking P0–P2)

1. Which 27B-class checkpoint — shortlist and priors in §11.5; eval runs
   (§11.4 step 2) decide.
2. Does any request class ever justify Plan B's mid rung, or does 27B @
   ~25 tok/s make "route down for latency" moot outside `read_query`?
3. Intent taxonomy stability — if P0 shadow labels disagree with hand
   labels mainly at class *boundaries*, merge classes rather than tuning
   prompts (coarser = more robust).
4. Should `single_write` ever route down? Only revisit after P2, with
   per-class eval evidence and confirm_writes forced on.

---

*Summary of the safety story in one line: the router can be wrong, the
workers can be down, the b70 can be off — and every one of those lands on
today's already-tested behavior, because routing only ever chooses **which
model speaks**, never **what is allowed to touch the vault**.*
