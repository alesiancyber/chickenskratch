# The B70 serving box — connection card and operating rules

*The local inference box chickenskratch's workers run on, as configured
2026-08-02. Companion to [GUIDE.md](GUIDE.md) §8–9 (profiles, bakeoff).
Update this file when the box's config changes — it is the one place
this is written down.*

## Connection (OpenAI-compatible, any client)

```
Base URL:  http://10.0.0.85:8081/v1     (llama-swap, direct)
API key:   any non-empty string — nothing checks it
Env style: OPENAI_BASE_URL=http://10.0.0.85:8081/v1  OPENAI_API_KEY=sk-local
```

Sanity check / see what's loaded:

```sh
curl -s http://10.0.0.85:8081/v1/models | python3 -m json.tool
```

Open WebUI (chat UI for humans) is separate, on
`http://10.0.0.85:8080`. Its API-key path is broken in this build
(0.10.2 doesn't honor `ENABLE_API_KEY` with persistent config off) —
programs use 8081, people use 8080, and that split is deliberate.

## Model roster (all no-think by default, per standing rule)

| model | class | t/s (tg) | bakeoff | character |
|---|---|---|---|---|
| `qwen3.6-35b-a3b` | MoE | 82 | **90/112** | **the worker default** — best speed×judgment |
| `gemma-4-26b-a4b` | MoE | 94 | 80/111 | fast; weaker schema_fix |
| `qwen3-4b` | dense | 120 | 64/111 | speed demon; grouping collapses |
| `gemma-4-31b` | dense | 24 | 92/111 | best judgment, 4× slower |
| `qwen3.6-27b` | dense | 23 | 91/111 | near-31B judgment, best answer scores |

`gemma-4-26b-a4b-think` exists for cloud-style reasoning comparison;
everything else launches with `--reasoning-budget 0`.

## The rules this box taught us (each learned the hard way)

1. **Single-tenant GPU.** llama-swap holds ONE model. Naming a
   different model evicts the resident one (~sub-minute reload from
   NVMe). Never benchmark while chatting; never chat while the engine
   is mid-pass on a different model.
2. **No auth on 8081, LAN-reachable.** The deliberate trade for
   skipping Open WebUI's broken key path. Anything on the network can
   use the GPU. Reconsider if untrusted devices join the LAN.
3. **No-think under normal names, always.** Reasoning burn ate entire
   token budgets on long inputs (19 hard-fails in one eval run) and 6×
   the wall clock, for zero judgment gain here. New models get
   `--reasoning-budget 0` from day one.
4. **Bench through the serving path, not beside it.** llama-bench
   validated flash-attention settings the real server couldn't live
   with (it doesn't allocate serving-sized KV). Flags get A/B'd
   through llama-server, judged by `chickenskratch-worker-eval`.
5. **Cold swaps are budgeted, not feared.** Worker profiles carry
   `worker_timeout_s: 300`; the first call after a model switch pays
   the load, everything after runs 1–5s.

## The harness side (for context, changes live in git)

- Profiles `qwen35-moe`, `gemma4-moe`, `qwen3-4b` in
  `config/models.yaml`; `worker: qwen35-moe` is the global default.
  Per-vault override: Spec.md `worker:` key.
- Worker replies are grammar-enforced (`response_format: json_schema`)
  with automatic fallback for endpoints that don't support it.
- //ask packs are sized for ~32K serving context (80K chars); the
  eval's 47K-char monster case guards that ceiling.

## Pending

- [ ] `--cache-reuse 256` on the llama-swap entries (measured absent
      2026-08-02: repeat prompts re-evaluate every token; ~1s/call on
      the table). Verify by re-running the two-call probe — evaluated
      prompt tokens should collapse on the second call.
- [ ] An embedding model served on the box, then point the harness's
      `EMBED_BASE_URL` at it (overlap's near-match candidate index).
- [ ] schema_fix hedge session — the 35B's one weak lane (10/15 vs the
      dense models' 12/15).
