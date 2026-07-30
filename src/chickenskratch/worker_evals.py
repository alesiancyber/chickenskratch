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
                if t.get(field) != want_val:
                    out.append(f"{pattern!r}: {field}={t.get(field)!r}, expected {want_val!r}")
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


async def _run_worker_once(case: dict, subs: dict[str, str]) -> tuple[object, list[str]]:
    """Dispatch one call to the case's worker (default extract_tasks) and
    score its reply. Returns (reply, failures)."""
    if case.get("worker") == "overlap":
        lines = []
        for n, p in enumerate(case["pairs"], 1):
            lines.append(f"{n}. a: {p['a']} — \"{p['a_text']}\" (in {p.get('a_note', 'Tasks/Inbox.md')})\n"
                         f"   b: {p['b']} — \"{p['b_text']}\" (in {p.get('b_note', 'Tasks/Inbox.md')})")
        reply = await workers.run_worker(
            workers.WORKERS["overlap"], pairs="\n".join(lines))
        return reply, score_overlap(case, reply)
    if case.get("worker") == "grouping":
        reply = await workers.run_worker(
            workers.WORKERS["grouping"],
            tasks="\n".join(f"{t['id']}: {t['text']}" for t in case["tasks"]),
        )
        return reply, score_grouping(case, reply)
    if case.get("worker") == "schema_fix":
        reply = await workers.run_worker(
            workers.WORKERS["schema_fix"],
            note=case["note"],
            entity=case["entity"],
            issues="\n".join(f"- {i}" for i in case["issues"]),
            content=case["content"],
            new_id=case.get("new_id", "n-0123456789ab"),
            extra_ids=", ".join(case.get("extra_ids", [])),
        )
        return reply, score_schema_fix(case, reply)
    tasks = await workers.extract_tasks(fill(case["text"], subs), subs["{today}"])
    return tasks, score(case, tasks, subs)


async def run_case(case: dict, subs: dict[str, str], repeat: int) -> dict:
    runs = []
    for _ in range(repeat):
        start = time.monotonic()
        try:
            reply, failures = await _run_worker_once(case, subs)
        except Exception as e:  # VaultError (junk output, server down) = failed run, not a crash
            reply, failures = None, [f"run error: {type(e).__name__}: {e}"]
        runs.append({"failures": failures, "reply": reply, "seconds": round(time.monotonic() - start, 1)})
    clean = sum(not r["failures"] for r in runs)
    first_bad = next((r for r in runs if r["failures"]), None)
    return {
        "name": case["name"],
        "passed": clean == repeat,
        "clean_runs": clean,
        "runs": repeat,
        "failures": first_bad["failures"] if first_bad else [],
        "seconds": round(sum(r["seconds"] for r in runs), 1),
        "last_reply": runs[-1]["reply"],
    }


async def run_all(cases: list[dict], repeat: int) -> list[dict]:
    subs = date_subs()
    results = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['name']} ... ", end="", flush=True)
        result = await run_case(case, subs, repeat)
        note = "" if result["passed"] else f"  ({result['failures'][0][:80]})"
        print(f"{result['clean_runs']}/{repeat} clean  [{result['seconds']}s]{note}")
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
    lines = [
        f"# Worker eval — {label} — {stamp}",
        "",
        f"**{strict}/{len(results)} cases passed all {repeat} runs** "
        f"— {clean}/{repeat * len(results)} runs clean",
        "",
        "| case | clean runs | time | first failure |",
        "|---|---|---|---|",
    ]
    for r in results:
        note = r["failures"][0].replace("|", "\\|")[:120] if r["failures"] else ""
        lines.append(f"| {r['name']} | {r['clean_runs']}/{r['runs']} | {r['seconds']}s | {note} |")
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
    workers._client = None  # drop any cached client so env overrides take effect

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
