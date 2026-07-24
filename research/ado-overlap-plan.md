# ADO cross-team overlap detection — V1 plan

*2026-07-24. Non-canon research doc: the build happens in its own repo on
the work machine; this is the plan to carry there. Tezcat lends its
patterns (detect-is-code / one bounded judge call / links-not-summaries /
eval-gated models), not its code.*

**The problem.** Multiple teams' ADO boards accumulate work items that
describe the same or overlapping work in different vocabularies. Nobody
reads other teams' backlogs. V1 surfaces cross-team overlap as a ranked,
read-only report a lead can act on in under a minute per row.

**The one-sentence architecture.** Deterministic code pulls and pairs
candidates (embeddings/lexical similarity, cross-team only); an LLM
judges exactly one pair per bounded call with a schema-validated verdict;
the output is ticket links with one-line evidence, never model prose.

---

## Hard constraints (work machine)

- **Models only via the LiteLLM proxy** (`litellm.prod.cit.bvnt.co`,
  `LITELLM_API_KEY` in a gitignored `.env`). Assume no local model
  hosting; verify what the proxy serves before designing around it.
- **ADO via PAT**, Work Items (Read) scope only — never wider, never
  committed. `ADO_ORG_URL` + `ADO_PAT` env vars.
- **Work data stays put**: cache dirs gitignored, nothing leaves the
  machine except through the mandated proxy.
- **Zero writes to ADO in V1.** A report is a view; wrongness costs a
  glance. All consent machinery (tezcat's proposals) is deliberately
  out of scope until something mutates.

## Phase 0 — recon (half a day, decides two forks)

1. `GET {proxy}/v1/models` — is there an **embedding model**? This picks
   the blocking path (Phase 2 A vs B).
2. Mint the PAT; smoke-test: list projects (`_apis/projects`), list
   teams per project, count open items per project via WIQL. Write
   `scope.json`; pick the 2–4 projects for the pilot.
3. Confirm **team identity**: ADO teams are area-path-backed — check
   each team's area path config (`_apis/work/teamsettings/teamfieldvalues`).
   If teams actually live in tags or a custom field, the mapping module
   in Phase 1 keys on that instead. This definition IS what "cross-team"
   means; get it right first.

## Phase 1 — pull (deterministic, fully testable offline)

- WIQL for open items of configurable types (User Story, PBI, Task, Bug,
  Feature), then batch-fetch fields 200 ids/call: id, title, description,
  acceptance criteria, area path, iteration, state, type, tags, assigned
  to, created/changed dates, url, **relations** (needed in Phase 2).
- WIQL caps at 20k results — shard by project/area path if needed.
  Respect `Retry-After` on 429s.
- Normalize: ADO stores Description/AC as HTML — strip to text. Build
  one judged text per item: `title + description + AC`, truncated to a
  budget.
- One `teams.py` module derives `team` per item (area-path map with the
  Phase-0 fallback), unit-tested — every later stage trusts it.
- Cache `data/items.json` + snapshot timestamp; idempotent re-pull.
- Tests run against canned ADO JSON fixtures, no network — same rule as
  tezcat's suite.

## Phase 2 — block (candidate pairs, still no judging)

- **Path A (proxy has embeddings)**: embed each item's text once, cache
  vectors keyed by `(id, changed_date)` so only changed items re-embed.
  Cosine top-k (k≈10) per item; numpy brute force is fine at ≤50k items
  — no vector DB in V1.
- **Path B (no embeddings)**: TF-IDF cosine (scikit-learn) or BM25.
  Semantically weaker but zero model dependency — good enough to
  bootstrap the label set and the pipeline; swap A in later behind the
  same interface.
- Keep only pairs that **cross team boundaries** (same-team dupes are
  the team's own hygiene; cross-team is the signal), above threshold τ
  (start ≈0.75 for embeddings, tune in Phase 5).
- **Exclude already-linked pairs**: if ADO relations already connect the
  items (parent/child/related/duplicate), the org already knows — noise.
- Dedup (a,b)/(b,a). Emit `data/candidates.json` with scores + a count
  summary — sanity-check volumes here before spending a single judge call.

## Phase 3 — judge (the one bounded LLM call)

- Port tezcat's `run_worker` shape: prompt template, jsonschema on the
  reply, temperature 0.1, bounded max_tokens (mind the reasoning-model
  lessons: `choices=[]` guard, token headroom), one retry, fail loud.
- Verdict schema:
  ```json
  {"verdict": "duplicate | overlapping | coordinate | unrelated",
   "evidence": "one sentence citing the concrete shared scope",
   "confidence": 0.0}
  ```
  Definitions pinned in the prompt: *duplicate* = same deliverable;
  *overlapping* = same component/scope, partially shared work;
  *coordinate* = different work that should be sequenced/shared
  (dependency, shared interface); *unrelated* = blocking false positive.
- Judge in descending similarity order under a per-run budget (the n_max
  lesson — cost stays bounded, backlog drains over runs).
- Cache verdicts keyed by `(id_a, changed_a, id_b, changed_b)` — never
  re-judge an unchanged pair.

## Phase 4 — report

- Ranked markdown (or HTML) grouped by verdict — `duplicate` first, then
  `overlapping`, then `coordinate`; **capped rows per section** (one
  screen or it isn't a dashboard). Each row:
  `[AB#123 Title](url) ↔ [CD#456 Title](url) · TeamA / TeamB · states ·
  evidence sentence · sim 0.83`.
- Footer: snapshot date, items scanned, pairs judged, thresholds, and
  (once Phase 5 exists) measured precision — set the reader's trust
  calibration explicitly.
- Local file output. Publishing to an ADO wiki is a WRITE — V2.
- Framing note with social teeth: lead with `coordinate` language, not
  accusation. "These teams should talk" lands; "you're duplicating" gets
  the report ignored after its first false positive.

## Phase 5 — eval + calibration (the credibility gate)

- Hand-label 50–100 candidate pairs (`evals/labels.csv`: id_a, id_b,
  human verdict). Cheapest right after Phase 2 — label straight from the
  candidate list while eyeballing it.
- `evals/scoring.py`: precision/recall per verdict class for the judge;
  sweep τ for the blocker (recall@k against labeled positives).
- **Exit bar for V1**: duplicate+overlapping precision ≥ ~80% on the
  label set. Below that, tune prompt/τ before showing anyone.
- Every model/prompt/threshold change re-scores against the labels —
  swaps are measured, never vibes (tezcat principle 6, verbatim).

## Repo sketch

```
ado-overlap/
  src/adolap/
    config.py   # env + config.yaml loading; secrets env-only
    pull.py     # WIQL + batch fetch + HTML strip + cache
    teams.py    # the team-identity mapping (unit-tested, single home)
    block.py    # embed-or-lexical candidates behind one interface
    judge.py    # run_worker-shaped bounded call + verdict cache
    report.py   # deterministic render from caches
    cli.py      # pull / block / judge / report / all
  config/config.yaml   # org, projects, item types, k, τ, budgets, model ids
  data/                # gitignored: items, vectors, candidates, verdicts
  evals/labels.csv  evals/scoring.py
  tests/               # canned-fixture tests, no network, no model
```

## Order of work, day one

recon script → pull ONE project pair → Path-B blocking → eyeball the
candidate list (this alone may already be the "oh no" moment that proves
value) → label 30 pairs → then embeddings and the judge.

## Explicitly out until a design decision says otherwise

- Any ADO write (comments, links, tags) — that's where tezcat's
  proposal/consent machinery gets ported, not before.
- Scheduled runs, Slack/email digests.
- Same-team dedup, cross-org comparison, historical (closed-item) mining.
- Agent/MCP-driven interactive querying — batch pipeline first; an MCP
  layer over the caches is a V2 nicety.

## Known risks

- **Proxy lacks embeddings** → Path B bootstrap; check first (Phase 0).
- **False-positive social cost** → precision-first exit bar, coordinate
  framing, capped report.
- **Team mapping is dirty** in most orgs (stale area paths, shared
  areas) → it's a single tested module; expect to iterate it.
- **Scale surprises** (an org with 100k open items) → shard the pull,
  raise τ, cap k; the architecture doesn't change.
