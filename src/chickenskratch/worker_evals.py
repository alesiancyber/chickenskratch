"""Worker eval runner: score a worker model's judgment on fixture cases.

The agent suite (evals.py) scores whole conversations against a vault
copy; this scores single worker calls structurally against expected
JSON — the live half of the worker contract. Unit tests already cover
parsing/robustness with a fake client; this measures the model's actual
judgment, so A/B-ing worker candidates is one command per model:

    chickenskratch-worker-eval                                # current default
    chickenskratch-worker-eval -m qwen3.5:4b-instruct-q4_K_M  # challenger

Each case runs --repeat times (default 3): small models are stochastic,
and a worker that's right two runs out of three is a different animal
from one that's right every time. Reports land in evals/reports/workers/
(kept out of evals/reports/ so the /evals dashboard only ever parses
agent-suite reports).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import re
import sys
import time

from chickenskratch_vault import workers

from . import evals, grouping
from .bootstrap import ROOT
from .evals import date_subs, fill

CASES_DIR = ROOT / "evals" / "cases" / "workers"
REPORTS_DIR = ROOT / "evals" / "reports" / "workers"

# Per-run wall-time budgets (seconds, mean across repeats), by worker.
# Usability is judgment AND speed: a model that is right in 90s per
# extract_tasks call fails a pass that fans out a dozen calls, and no
# accuracy score should be able to hide that. Generous on purpose —
# these catch "unusable", not "not snappy" — and a case may override
# with its own `budget_s` (the long-input cases do).
DEFAULT_BUDGET_S = {
    "extract_tasks": 30.0,
    "extract_record": 120.0,
    "answer": 60.0,
    "schema_fix": 60.0,
    "overlap": 30.0,
    "grouping": 30.0,
    "plan_today": 30.0,
}


def load_cases() -> list[dict]:
    return evals.load_cases(CASES_DIR)


def score(case: dict, tasks: list[dict], subs: dict[str, str]) -> list[str]:
    """Structural check of one worker reply. Returns failure strings; empty = pass."""
    failures: list[str] = []
    expected = case.get("expect", [])
    remaining = list(tasks)
    for want in expected:
        pattern = fill(want["match"], subs)

        def mismatches(t: dict, want: dict = want, pattern: str = pattern) -> list[str]:
            out = []
            for field in ("due", "scheduled", "priority", "owed_by"):
                if field not in want:
                    continue  # absent key = unchecked; explicit null = must be null
                want_val = fill(want[field], subs) if isinstance(want[field], str) else want[field]
                got = t.get(field)
                # owed_by is a NAME — its capitalization follows the dump's
                # ("ravi pinged me" vs a speaker label "Ravi:"), which is
                # spelling, not judgment. Dates and priorities stay exact.
                if field == "owed_by" and isinstance(got, str) and isinstance(want_val, str):
                    got, want_val = got.lower(), want_val.lower()
                if got != want_val:
                    out.append(f"{pattern!r}: {field}={t.get(field)!r}, expected {want[field]!r}")
            return out

        candidates = [t for t in remaining if re.search(pattern, t["text"])]
        if not candidates:
            failures.append(f"no returned task matches {pattern!r}")
            continue
        # Several returned tasks can match one pattern (a defensible extra
        # sharing words with the expected task): credit the candidate whose
        # fields check out, not blindly the first regex hit.
        best = min(candidates, key=lambda t: len(mismatches(t)))
        remaining.remove(best)
        failures += mismatches(best)
    if case.get("exact") and len(tasks) != len(expected):
        extras = [t["text"] for t in remaining]
        failures.append(f"expected exactly {len(expected)} task(s), got {len(tasks)}; extra: {extras!r}")
    for raw in case.get("forbid_text", []):
        pattern = fill(raw, subs)
        for t in tasks:
            if re.search(pattern, t["text"]):
                failures.append(f"forbidden pattern {pattern!r} in task text {t['text']!r}")
    return failures


def score_schema_fix(case: dict, reply: dict) -> list[str]:
    """Structural check of one schema_fix reply, scored by the SAME
    deterministic verifier that gates production auto-apply — so the eval
    answers exactly "would this model's fix have applied without a
    checkbox": only additive structure, every user line byte-identical,
    result fully conformant. Cases may pin specifics with
    after_contains / after_not_contains regexes evaluated against the
    simulated post-fix note."""
    from . import applier
    from .processors import verify_schema_fix

    item = {"note": case["note"], "content": case["content"]}
    proposal = {"apply": reply.get("apply", [])}
    failures = list(verify_schema_fix(None, item, proposal))
    after = applier.simulate(case["content"], proposal, note=case["note"])
    if after is not None:
        for pat in case.get("after_contains", []):
            if not re.search(pat, after):
                failures.append(f"simulated note lacks required pattern {pat!r}")
        for pat in case.get("after_not_contains", []):
            if re.search(pat, after):
                failures.append(f"simulated note matches forbidden pattern {pat!r}")
    return failures


def score_grouping(case: dict, reply: dict) -> list[str]:
    """Structural check of one grouping reply. Partition integrity is
    always enforced; cases then pin the judgment with `must_group` (id
    lists that must share a group) and `must_separate` (id pairs that
    must not) — group names are the model's choice and never scored."""
    ids = [t["id"] for t in case["tasks"]]
    groups = reply.get("groups", [])
    failures = grouping.validate_partition(groups, ids)
    member_of = {mid: str(g.get("name", "")) for g in groups for mid in g.get("member_ids", [])}
    for id_list in case.get("must_group", []):
        names = {member_of.get(i) for i in id_list}
        if len(names) > 1:
            failures.append(f"{id_list} should share a group; got {sorted(n or '?' for n in names)}")
    for a, b in case.get("must_separate", []):
        if member_of.get(a) is not None and member_of.get(a) == member_of.get(b):
            failures.append(f"{a} and {b} must not share a group (both in {member_of[a]!r})")
    return failures


def score_overlap(case: dict, reply: dict) -> list[str]:
    """Structural check of one overlap reply. Every submitted pair must
    be answered with a valid verdict; cases pin judgment with `expect`:
    {a, b, verdict} rows (and optionally `keep`). Ids never invented."""
    submitted = {frozenset((p["a"], p["b"])) for p in case["pairs"]}
    failures: list[str] = []
    answered: dict[frozenset, dict] = {}
    for p in reply.get("pairs", []):
        key = frozenset((str(p.get("a")), str(p.get("b"))))
        if key not in submitted:
            failures.append(f"invented or mangled pair: {sorted(key)}")
            continue
        answered[key] = p
    for key in submitted - set(answered):
        failures.append(f"unanswered pair: {sorted(key)}")
    for want in case.get("expect", []):
        key = frozenset((want["a"], want["b"]))
        got = answered.get(key)
        if got is None:
            continue  # already reported as unanswered
        if got.get("verdict") != want["verdict"]:
            failures.append(f"{sorted(key)}: verdict={got.get('verdict')!r}, "
                            f"expected {want['verdict']!r}")
        if "keep" in want and got.get("keep") != want["keep"]:
            failures.append(f"{sorted(key)}: keep={got.get('keep')!r}, "
                            f"expected {want['keep']!r}")
    return failures


def score_record(case: dict, data: dict, step: dict,
                 verify_failures: list[str], subs: dict[str, str]) -> list[str]:
    """Structural check of one extract_record decomposition AND its gate.

    The reply is shaped into a real record plan by the production code
    (_note_record/_capture_record) and judged by the production gate
    (verify_record) — same pattern as schema_fix, so the eval answers
    "would this model's record have survived review". Gate failures
    split two ways: fidelity/attendee/verbatim failures ALWAYS count
    (they mean the record queues with warnings, or worse, fabricates);
    summary-prose flags count only when the case demands `gate: clean`
    (a bare task-list dump must auto-file — any prose the model invents
    for it breaks exactly that).

    Case pins: kind, files_as (note | daily-append), attendees_include /
    attendees_forbid, title_matches, summary_matches / summary_forbid,
    plus the extract_tasks task pins (expect / exact / forbid_text)."""
    failures: list[str] = []
    record = data["record"]
    if "kind" in case and record.get("kind") != case["kind"]:
        failures.append(f"kind={record.get('kind')!r}, expected {case['kind']!r}")
    if "files_as" in case:
        got = "note" if step["tool"] == "create_note" else "daily-append"
        if got != case["files_as"]:
            failures.append(f"filed as {got}, expected {case['files_as']}")
    attendees = [a.lower() for a in record.get("attendees") or []]
    for name in case.get("attendees_include", []):
        if not any(name.lower() in a for a in attendees):
            failures.append(f"attendee {name!r} missing from {record.get('attendees')!r}")
    for name in case.get("attendees_forbid", []):
        if any(name.lower() in a for a in attendees):
            failures.append(f"forbidden attendee {name!r} in {record.get('attendees')!r}")
    if "title_matches" in case and not re.search(
            case["title_matches"], record.get("title") or "", re.I):
        failures.append(f"title {record.get('title')!r} lacks {case['title_matches']!r}")
    summary = record.get("summary") or ""
    for pat in case.get("summary_matches", []):
        if not re.search(fill(pat, subs), summary, re.I):
            failures.append(f"summary lacks required pattern {pat!r}")
    for pat in case.get("summary_forbid", []):
        if re.search(fill(pat, subs), summary, re.I):
            failures.append(f"summary matches forbidden pattern {pat!r}")
    if case.get("summary_empty") and summary.strip():
        failures.append(f"expected no summary, got {summary[:80]!r}")
    failures += score(case, data["tasks"], subs)
    prose = [f for f in verify_failures if f.startswith("summary prose")]
    hard = [f for f in verify_failures if not f.startswith("summary prose")]
    failures += [f"gate: {f}" for f in hard]
    if case.get("gate") == "clean":
        failures += [f"gate (clean demanded): {f}" for f in prose]
    return failures


def score_answer(case: dict, reply: dict, packed: dict[str, str]) -> list[str]:
    """Structural check of one answer reply against its own pack.

    Citation discipline is scored harder here than production enforces:
    query.ask silently DROPS an invented source (correct for the user —
    a link that opens the wrong note teaches them not to click), but in
    an eval an invented source is the fabrication signal itself. Quote
    verification runs through the real gate (query._verified_quotes),
    so `min_verified_quotes` measures how many of the model's citations
    actually earn a jump link — the difference between //ask with
    receipts and //ask with bare note names."""
    from .query import _verified_quotes

    failures: list[str] = []
    answer = str(reply.get("answer") or "")
    for pat in case.get("answer_matches", []):
        if not re.search(pat, answer, re.I):
            failures.append(f"answer lacks required pattern {pat!r}")
    for pat in case.get("answer_forbid", []):
        if re.search(pat, answer, re.I):
            failures.append(f"answer matches forbidden pattern {pat!r}")
    offered = set(packed)
    sources: list[str] = []
    for s in reply.get("sources") or []:
        s = str(s).strip()
        rel = s if s in offered else (s + ".md" if s + ".md" in offered else None)
        if rel is None:
            failures.append(f"cited source not in the pack: {s!r}")
        elif rel not in sources:
            sources.append(rel)
    if case.get("expect_no_sources") and sources:
        failures.append(f"expected no sources (notes don't answer it), got {sources!r}")
    for rel in case.get("must_cite", []):
        rel_md = rel if rel.endswith(".md") else rel + ".md"
        if rel not in sources and rel_md not in sources:
            failures.append(f"missing required source {rel!r} (cited: {sources!r})")
    for rel in case.get("must_not_cite", []):
        rel_md = rel if rel.endswith(".md") else rel + ".md"
        if rel in sources or rel_md in sources:
            failures.append(f"cited irrelevant source {rel!r}")
    want_quotes = case.get("min_verified_quotes", 0)
    if want_quotes:
        verified = _verified_quotes(reply.get("quotes"), sources, packed)
        if len(verified) < want_quotes:
            failures.append(
                f"only {len(verified)} quote(s) survive verification "
                f"(need {want_quotes}); offered: {reply.get('quotes')!r}")
    return failures


def score_plan(case: dict, reply: dict) -> list[str]:
    """Structural check of one plan_today reply. Id discipline is always
    enforced (never invented, never repeated, never over max_picks);
    cases pin the judgment with must_pick / must_not_pick /
    expect_empty / max_expected — the committed-day restraint rule the
    prompt encodes is exactly what small models pad through."""
    from .processors import MAX_PLAN_PICKS

    known = {t["id"] for t in case["tasks"]}
    failures: list[str] = []
    picks = reply.get("picks") or []
    ids = [str(p.get("id")) for p in picks]
    for i in ids:
        if i not in known:
            failures.append(f"invented id {i!r}")
    if len(set(ids)) != len(ids):
        failures.append(f"repeated id(s) in picks: {ids!r}")
    if len(ids) > MAX_PLAN_PICKS:
        failures.append(f"{len(ids)} picks exceeds max_picks {MAX_PLAN_PICKS}")
    if case.get("expect_empty") and ids:
        failures.append(f"expected no picks, got {ids!r}")
    if "max_expected" in case and len(ids) > case["max_expected"]:
        failures.append(f"{len(ids)} picks; case allows at most {case['max_expected']}")
    for i in case.get("must_pick", []):
        if i not in ids:
            failures.append(f"expected pick {i!r} missing (picked: {ids!r})")
    for i in case.get("must_not_pick", []):
        if i in ids:
            failures.append(f"picked {i!r}, which the case forbids")
    return failures


# The record cases shape replies through the production drafting helpers,
# which need a Watcher-shaped object over a real (empty) vault dir — the
# path decisions (meetings_dir, capture archive) come from get_config
# exactly as in production. One temp dir per process, lazily.
_record_watcher = None


def _get_record_watcher():
    global _record_watcher
    if _record_watcher is None:
        import tempfile
        from types import SimpleNamespace

        from chickenskratch_vault.vault import Vault

        d = tempfile.mkdtemp(prefix="worker-eval-vault-")
        _record_watcher = SimpleNamespace(vault_path=d, vault=Vault(d))
    return _record_watcher


async def _run_record_case(case: dict, subs: dict[str, str],
                           stats: dict) -> tuple[object, list[str]]:
    """One extract_record call, shaped and gated by the production code
    path: extract_record -> owed-name prefixing -> record shaping
    (_note_record/_capture_record per the schema files' kinds) ->
    verify_record. Everything after the worker call is deterministic,
    so failures here are the model's."""
    import datetime as _dt

    from chickenskratch_vault import schemas as sc

    from . import processors as pr

    text = fill(case["text"], subs)
    data = await workers.extract_record(text, subs["{today}"], stats=stats)
    record, tasks = data["record"], data["tasks"]
    tasks = pr._name_prefix_owed(record, tasks, text)
    own, owed = pr._split_owed(tasks)
    w = _get_record_watcher()
    now = _dt.datetime.now()
    kinds = sc.record_kinds()
    rk = kinds.get(record["kind"]) or kinds.get(sc.default_record_kind())
    if rk is not None and rk.files_as == "note":
        step, _rel = pr._note_record(w, rk, record, own, owed, text, now)
    else:
        step, _rel = pr._capture_record(w, record, own, owed, text, now)
    verify = pr.verify_record(w, {"text": text}, {"apply": [step]})
    data = {**data, "tasks": tasks}
    return data, score_record(case, data, step, verify, subs)


async def _run_worker_once(case: dict, subs: dict[str, str],
                           stats: dict) -> tuple[object, list[str]]:
    """Dispatch one call to the case's worker (default extract_tasks) and
    score its reply. Returns (reply, failures); `stats` collects call
    telemetry (attempts) from run_worker."""
    if case.get("worker") == "overlap":
        lines = []
        for n, p in enumerate(case["pairs"], 1):
            lines.append(f"{n}. a: {p['a']} — \"{p['a_text']}\" (in {p.get('a_note', 'Tasks/Inbox.md')})\n"
                         f"   b: {p['b']} — \"{p['b_text']}\" (in {p.get('b_note', 'Tasks/Inbox.md')})")
        reply = await workers.run_worker(
            workers.get_worker("overlap"), stats=stats, pairs="\n".join(lines))
        return reply, score_overlap(case, reply)
    if case.get("worker") == "grouping":
        reply = await workers.run_worker(
            workers.get_worker("grouping"), stats=stats,
            tasks="\n".join(f"{t['id']}: {t['text']}" for t in case["tasks"]),
        )
        return reply, score_grouping(case, reply)
    if case.get("worker") == "schema_fix":
        reply = await workers.run_worker(
            workers.get_worker("schema_fix"), stats=stats,
            note=case["note"],
            entity=case["entity"],
            issues="\n".join(f"- {i}" for i in case["issues"]),
            content=case["content"],
            new_id=case.get("new_id", "n-0123456789ab"),
            extra_ids=", ".join(case.get("extra_ids", [])),
        )
        return reply, score_schema_fix(case, reply)
    if case.get("worker") == "extract_record":
        return await _run_record_case(case, subs, stats)
    if case.get("worker") == "answer":
        packed = {n["rel"]: fill(n["text"], subs) for n in case["notes"]}
        pack = "\n\n".join(f"=== {rel} ===\n{text}" for rel, text in packed.items())
        reply = await workers.run_worker(
            workers.get_worker("answer"), stats=stats,
            question=case["question"], notes=pack)
        return reply, score_answer(case, reply, packed)
    if case.get("worker") == "plan_today":
        # Through the production drafter, so the prompt lines a model is
        # judged on are the lines production sends — no eval-side copy.
        from types import SimpleNamespace

        from .watcher import draft_plan_today

        item = {
            "today": subs["{today}"],
            "committed": case.get("committed", 0),
            "tasks": [(SimpleNamespace(
                block_id=t["id"], text=t["text"],
                created=fill(str(t.get("created") or ""), subs) or None,
                priority=t.get("priority")),
                t.get("note", "Tasks/Inbox.md")) for t in case["tasks"]],
        }
        reply = await draft_plan_today(item, stats=stats)
        return reply, score_plan(case, reply)
    tasks = await workers.extract_tasks(fill(case["text"], subs),
                                        subs["{today}"], stats=stats)
    return tasks, score(case, tasks, subs)


async def run_case(case: dict, subs: dict[str, str], repeat: int) -> dict:
    runs = []
    for _ in range(repeat):
        start = time.monotonic()
        stats: dict = {}
        try:
            reply, failures = await _run_worker_once(case, subs, stats)
        except Exception as e:  # VaultError (junk output, server down) = failed run, not a crash
            reply, failures = None, [f"run error: {type(e).__name__}: {e}"]
        runs.append({"failures": failures, "reply": reply,
                     "attempts": stats.get("attempts", 1),
                     "seconds": round(time.monotonic() - start, 1)})
    clean = sum(not r["failures"] for r in runs)
    first_bad = next((r for r in runs if r["failures"]), None)
    failures = first_bad["failures"] if first_bad else []
    # Retries: extra attempts run_worker's silent retry spent recovering
    # junk output. A case can pass 3/3 while retrying 3/3 — that model is
    # half as fast and one bad sample from failing, and the report says so.
    retries = sum(max(0, r["attempts"] - 1) for r in runs)
    # The time budget judges the mean run, so one cold-start outlier
    # doesn't fail a fast model, but a consistently slow one can't hide.
    mean_s = sum(r["seconds"] for r in runs) / max(1, len(runs))
    budget = case.get("budget_s") or DEFAULT_BUDGET_S.get(
        case.get("worker", "extract_tasks"))
    over_budget = bool(budget) and mean_s > budget
    if over_budget:
        failures = failures + [
            f"over time budget: mean {mean_s:.1f}s/run > {budget:.0f}s"]
    return {
        "name": case["name"],
        "passed": clean == repeat and not over_budget,
        "clean_runs": clean,
        "runs": repeat,
        "retries": retries,
        "failures": failures,
        "seconds": round(sum(r["seconds"] for r in runs), 1),
        "mean_seconds": round(mean_s, 1),
        "budget_s": budget,
        "last_reply": runs[-1]["reply"],
    }


async def run_all(cases: list[dict], repeat: int) -> list[dict]:
    subs = date_subs()
    results = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['name']} ... ", end="", flush=True)
        result = await run_case(case, subs, repeat)
        note = "" if result["passed"] else f"  ({result['failures'][0][:80]})"
        retry_note = f" {result['retries']} retr{'y' if result['retries'] == 1 else 'ies'}" \
            if result["retries"] else ""
        print(f"{result['clean_runs']}/{repeat} clean  [{result['seconds']}s{retry_note}]{note}")
        results.append(result)
    return results


def write_report(model: str, results: list[dict], repeat: int, adapt: str = ""):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    # seconds in the stamp: two runs in the same minute (quick -k spot
    # checks) must never overwrite each other's reports
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    # adapt tier in the name and title: hedged and loose runs of the same
    # model are different animals and must never read as one series
    label = f"{model} (adapt={adapt})" if adapt else model
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{model}-{adapt}" if adapt else model)
    strict = sum(r["passed"] for r in results)
    clean = sum(r["clean_runs"] for r in results)
    retries = sum(r.get("retries", 0) for r in results)
    total_s = sum(r["seconds"] for r in results)
    slowest = sorted(results, key=lambda r: -r.get("mean_seconds", 0))[:3]
    lines = [
        f"# Worker eval — {label} — {stamp}",
        "",
        f"**{strict}/{len(results)} cases passed all {repeat} runs** "
        f"— {clean}/{repeat * len(results)} runs clean "
        f"— {retries} retr{'y' if retries == 1 else 'ies'} "
        f"— {total_s / 60:.1f} min total",
        "",
        "Slowest (mean s/run): " + ", ".join(
            f"{r['name']} ({r.get('mean_seconds', 0)}s)" for r in slowest),
        "",
        "| case | clean runs | retries | mean s/run | first failure |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        note = r["failures"][0].replace("|", "\\|")[:120] if r["failures"] else ""
        lines.append(
            f"| {r['name']} | {r['clean_runs']}/{r['runs']} | {r.get('retries', 0)} "
            f"| {r.get('mean_seconds', 0)}s | {note} |")
    md = REPORTS_DIR / f"{stamp}-{safe}.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (REPORTS_DIR / f"{stamp}-{safe}.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return md


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="chickenskratch-worker-eval", description="Score a worker model against the worker fixture cases"
    )
    parser.add_argument("-m", "--model", help="worker model name (default: WORKER_MODEL env or built-in)")
    parser.add_argument("--base-url", help="OpenAI-compatible endpoint (default: WORKER_BASE_URL or Ollama)")
    parser.add_argument("-k", "--filter", help="only run cases whose name contains this substring")
    parser.add_argument("--repeat", type=int, default=3, help="runs per case (default 3)")
    args = parser.parse_args()

    from .bootstrap import DEFAULT_MODELS, load_env_file, resolve_worker_env
    from .profiles import load_config

    # Same worker resolution as every other entry point: .env fills gaps,
    # the models.yaml `worker:` profile fills the rest, flags below win.
    load_env_file()
    resolve_worker_env(load_config(DEFAULT_MODELS))
    if args.model:
        os.environ["WORKER_MODEL"] = args.model
    if args.base_url:
        os.environ["WORKER_BASE_URL"] = args.base_url
    workers.reset_client()  # drop any cached client so env overrides take effect

    cases = load_cases()
    if args.filter:
        cases = [c for c in cases if args.filter in c["name"]]
    if not cases:
        raise SystemExit("no cases matched")

    model = workers.worker_model()
    adapt = workers.worker_adapt()
    print(f"worker eval: {len(cases)} cases × {args.repeat} runs "
          f"against {model} (adapt={adapt})")
    results = asyncio.run(run_all(cases, args.repeat))
    strict = sum(r["passed"] for r in results)
    report = write_report(model, results, args.repeat, adapt=adapt)
    print(f"\n{strict}/{len(results)} cases fully clean — report: {report.relative_to(ROOT)}")
    sys.exit(0 if strict == len(results) else 1)


if __name__ == "__main__":
    main()
