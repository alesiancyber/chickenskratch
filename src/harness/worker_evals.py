"""Worker eval runner: score a worker model's judgment on fixture cases.

The agent suite (evals.py) scores whole conversations against a vault
copy; this scores single worker calls structurally against expected
JSON — the live half of the worker contract. Unit tests already cover
parsing/robustness with a fake client; this measures the model's actual
judgment, so A/B-ing worker candidates is one command per model:

    harness-worker-eval                                # current default
    harness-worker-eval -m qwen3.5:4b-instruct-q4_K_M  # challenger

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

import yaml

from obsidian_mcp import workers

from .bootstrap import ROOT
from .evals import date_subs, fill

CASES_DIR = ROOT / "evals" / "cases" / "workers"
REPORTS_DIR = ROOT / "evals" / "reports" / "workers"


def load_cases() -> list[dict]:
    cases: list[dict] = []
    for f in sorted(CASES_DIR.glob("*.yaml")):
        cases.extend(yaml.safe_load(f.read_text(encoding="utf-8")) or [])
    names = [c["name"] for c in cases]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise SystemExit(f"duplicate case names: {sorted(dupes)}")
    return cases


def score(case: dict, tasks: list[dict], subs: dict[str, str]) -> list[str]:
    """Structural check of one worker reply. Returns failure strings; empty = pass."""
    failures: list[str] = []
    expected = case.get("expect", [])
    remaining = list(tasks)
    for want in expected:
        pattern = fill(want["match"], subs)
        hit = next((t for t in remaining if re.search(pattern, t["text"])), None)
        if hit is None:
            failures.append(f"no returned task matches {pattern!r}")
            continue
        remaining.remove(hit)
        for field in ("due", "priority"):
            if field not in want:
                continue  # absent key = unchecked; explicit null = must be null
            want_val = fill(want[field], subs) if isinstance(want[field], str) else want[field]
            if hit.get(field) != want_val:
                failures.append(f"{pattern!r}: {field}={hit.get(field)!r}, expected {want_val!r}")
    if case.get("exact") and len(tasks) != len(expected):
        extras = [t["text"] for t in remaining]
        failures.append(f"expected exactly {len(expected)} task(s), got {len(tasks)}; extra: {extras!r}")
    for raw in case.get("forbid_text", []):
        pattern = fill(raw, subs)
        for t in tasks:
            if re.search(pattern, t["text"]):
                failures.append(f"forbidden pattern {pattern!r} in task text {t['text']!r}")
    return failures


async def run_case(case: dict, subs: dict[str, str], repeat: int) -> dict:
    runs = []
    for _ in range(repeat):
        start = time.monotonic()
        try:
            tasks = await workers.extract_tasks(fill(case["text"], subs), subs["{today}"])
            failures = score(case, tasks, subs)
        except Exception as e:  # VaultError (junk output, server down) = failed run, not a crash
            tasks, failures = [], [f"run error: {type(e).__name__}: {e}"]
        runs.append({"failures": failures, "tasks": tasks, "seconds": round(time.monotonic() - start, 1)})
    clean = sum(not r["failures"] for r in runs)
    first_bad = next((r for r in runs if r["failures"]), None)
    return {
        "name": case["name"],
        "passed": clean == repeat,
        "clean_runs": clean,
        "runs": repeat,
        "failures": first_bad["failures"] if first_bad else [],
        "seconds": round(sum(r["seconds"] for r in runs), 1),
        "last_tasks": runs[-1]["tasks"],
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


def write_report(model: str, results: list[dict], repeat: int):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", model)
    strict = sum(r["passed"] for r in results)
    clean = sum(r["clean_runs"] for r in results)
    lines = [
        f"# Worker eval — extract_tasks — {model} — {stamp}",
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
        prog="harness-worker-eval", description="Score a worker model against the worker fixture cases"
    )
    parser.add_argument("-m", "--model", help="worker model name (default: WORKER_MODEL env or built-in)")
    parser.add_argument("--base-url", help="OpenAI-compatible endpoint (default: WORKER_BASE_URL or Ollama)")
    parser.add_argument("-k", "--filter", help="only run cases whose name contains this substring")
    parser.add_argument("--repeat", type=int, default=3, help="runs per case (default 3)")
    args = parser.parse_args()

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

    model = workers._worker_model()
    print(f"worker eval: {len(cases)} cases × {args.repeat} runs against {model}")
    results = asyncio.run(run_all(cases, args.repeat))
    strict = sum(r["passed"] for r in results)
    report = write_report(model, results, args.repeat)
    print(f"\n{strict}/{len(results)} cases fully clean — report: {report.relative_to(ROOT)}")
    sys.exit(0 if strict == len(results) else 1)


if __name__ == "__main__":
    main()
