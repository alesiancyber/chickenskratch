"""Eval runner: measure a model profile against fixture tasks.

Each case gets a pristine copy of evals/vault (date placeholders like
{tomorrow} resolved at copy time), runs its turns through the real Agent
loop, and is scored by checking the resulting files and structures —
never by trusting the model's prose. Run when swapping models, editing
the system prompt, or changing tools:   tezcat-eval -m qwen3-27b
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import anyio
import yaml

from tezcat_vault import kanban as kb
from tezcat_vault import tasks as tk

from .agent import Agent
from .bootstrap import DEFAULT_MCP, DEFAULT_MODELS, ROOT
from .mcp_client import load_mcp_config
from .profiles import load_config
from .skills import load_skills

EVALS = ROOT / "evals"
DEFAULT_TIMEOUT = 300


# --------------- fixtures ---------------

def date_subs() -> dict[str, str]:
    today = dt.date.today()

    def day(n: int) -> str:
        return (today + dt.timedelta(days=n)).isoformat()

    this_sunday = tk.week_end(today)
    return {
        "{today}": day(0), "{tomorrow}": day(1), "{yesterday}": day(-1),
        "{plus3}": day(3), "{plus5}": day(5), "{plus7}": day(7), "{plus14}": day(14),
        # the ⏳ week anchors: "this week" plans stamp the week's Sunday
        # (tasks.week_end — the same Sunday every other surface uses)
        "{this_sunday}": this_sunday.isoformat(),
        "{next_sunday}": (this_sunday + dt.timedelta(days=7)).isoformat(),
    }


def fill(text: str, subs: dict[str, str]) -> str:
    for key, value in subs.items():
        text = text.replace(key, value)
    return text


def copy_fixture(src: Path, dst: Path, subs: dict[str, str]) -> None:
    for p in src.rglob("*"):
        if not p.is_file():
            continue
        target = dst / p.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            target.write_text(fill(p.read_text(encoding="utf-8"), subs), encoding="utf-8")
        except UnicodeDecodeError:
            # binary attachment (image, PDF): copy as-is, no placeholders
            shutil.copy2(p, target)


def snapshot(vault: Path) -> dict[str, str]:
    return {
        str(p.relative_to(vault)): hashlib.sha1(p.read_bytes()).hexdigest()
        for p in vault.rglob("*")
        if p.is_file() and ".git" not in p.parts
    }


# --------------- checks ---------------

def _read(vault: Path, rel: str) -> str:
    p = vault / rel
    if not p.exists():
        raise FileNotFoundError(f"file missing: {rel}")
    return p.read_text(encoding="utf-8")


def check_file_contains(spec, vault, answer, before, subs):
    content = _read(vault, fill(spec["path"], subs))
    pattern = fill(spec["pattern"], subs)
    n = len(re.findall(pattern, content)) if spec.get("regex") else content.count(pattern)
    want = spec.get("count")
    if want is not None:
        return n == want, f"expected exactly {want} of {pattern!r}, found {n}"
    return n >= 1, f"pattern not found: {pattern!r}"


def check_file_not_contains(spec, vault, answer, before, subs):
    content = _read(vault, fill(spec["path"], subs))
    pattern = fill(spec["pattern"], subs)
    hit = re.search(pattern, content) if spec.get("regex") else (pattern in content)
    return not hit, f"pattern should be absent but was found: {pattern!r}"


def check_file_exists(spec, vault, answer, before, subs):
    should = spec.get("should", True)
    exists = (vault / fill(spec["path"], subs)).exists()
    return exists == should, f"exists={exists}, expected {should}: {fill(spec['path'], subs)}"


def check_answer_matches(spec, vault, answer, before, subs):
    pattern = fill(spec["pattern"], subs)
    ok = re.search(pattern, answer, re.S) is not None
    return ok, f"final answer did not match {pattern!r}: {answer[:200]!r}"


def check_answer_not_matches(spec, vault, answer, before, subs):
    pattern = fill(spec["pattern"], subs)
    ok = re.search(pattern, answer, re.S) is None
    return ok, f"final answer should not match {pattern!r}: {answer[:200]!r}"


def check_card_in_column(spec, vault, answer, before, subs):
    board = kb.parse_board(_read(vault, fill(spec["board"], subs)))
    should = spec.get("should", True)
    column = next((c for c in board.columns if c.name.lower() == spec["column"].lower()), None)
    if column is None:
        return not should, f"column {spec['column']!r} not found"
    card = fill(spec["card"], subs).lower()
    present = any(card in c["text"].lower() for c in column.cards)
    return present == should, f"card {spec['card']!r} in {spec['column']!r}: {present}, expected {should}"


def check_task_done(spec, vault, answer, before, subs):
    content = _read(vault, fill(spec["path"], subs))
    needle = fill(spec["text"], subs).lower()
    matches = [t for t in tk.iter_tasks(content) if needle in t.text.lower()]
    if not matches:
        return False, f"no task matching {spec['text']!r}"
    want = spec.get("done", True)
    ok = all(t.done == want for t in matches)
    return ok, f"task done={matches[0].done}, expected {want}"


def check_vault_unchanged(spec, vault, answer, before, subs):
    after = snapshot(vault)
    changed = sorted(set(before.items()) ^ set(after.items()))
    return not changed, f"vault changed: {sorted({k for k, _ in changed})}"


CHECKS = {
    "file_contains": check_file_contains,
    "file_not_contains": check_file_not_contains,
    "file_exists": check_file_exists,
    "answer_matches": check_answer_matches,
    "answer_not_matches": check_answer_not_matches,
    "card_in_column": check_card_in_column,
    "task_done": check_task_done,
    "vault_unchanged": check_vault_unchanged,
}


def run_checks(case, vault, answer, before, subs) -> list[str]:
    failures = []
    for check in case.get("checks", []):
        ((kind, spec),) = check.items()
        fn = CHECKS.get(kind)
        if fn is None:
            failures.append(f"unknown check type: {kind}")
            continue
        try:
            ok, detail = fn(spec if isinstance(spec, dict) else {}, vault, answer, before, subs)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        if not ok:
            failures.append(f"{kind}: {detail}")
    return failures


# --------------- runner ---------------

async def run_case(case: dict, profile, subs: dict[str, str]) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="tezcat-eval-"))
    vault = tmp / "vault"
    copy_fixture(EVALS / "vault", vault, subs)

    servers = ["vault"] + (["ms-learn"] if "network" in case.get("tags", []) else [])
    prof = dataclasses.replace(profile, servers=servers, confirm_writes=False)
    confirm = None
    if case.get("confirm") == "deny":
        prof = dataclasses.replace(prof, confirm_writes=True)

        async def confirm(name, args):
            return False

    specs = load_mcp_config(DEFAULT_MCP, str(vault))
    calls: list[str] = []
    before = snapshot(vault)
    answer, error = "", None
    start = time.monotonic()
    try:
        async with Agent(
            prof, specs, log=lambda n, a: calls.append(n), confirm=confirm, skills=load_skills()
        ) as agent:
            for turn in case.get("turns", []):
                # anyio.fail_after, not asyncio.wait_for: agent.run drives
                # MCP sessions (anyio cancel scopes), and wait_for's
                # cancellation escapes as a bare CancelledError that would
                # kill the whole eval run instead of failing this case
                # (same class as mcp_client's connect timeout).
                with anyio.fail_after(case.get("timeout", DEFAULT_TIMEOUT)):
                    answer = await agent.run(fill(turn, subs))
    except TimeoutError:
        error = f"timeout after {case.get('timeout', DEFAULT_TIMEOUT)}s"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
    elapsed = time.monotonic() - start

    failures = [f"run error: {error}"] if error else run_checks(case, vault, answer, before, subs)
    shutil.rmtree(tmp, ignore_errors=True)
    return {
        "name": case["name"],
        "passed": not failures,
        "failures": failures,
        "seconds": round(elapsed, 1),
        "tool_calls": len(calls),
        "tools_used": calls,
        "answer": answer[:500],
    }


def load_cases(cases_dir: Path) -> list[dict]:
    cases = []
    for f in sorted(cases_dir.glob("*.yaml")):
        cases.extend(yaml.safe_load(f.read_text(encoding="utf-8")) or [])
    names = [c["name"] for c in cases]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise SystemExit(f"duplicate case names: {sorted(dupes)}")
    return cases


def write_report(profile_name: str, results: list[dict]) -> Path:
    # seconds in the stamp: two runs in the same minute must never
    # overwrite each other's reports
    stamp = dt.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    passed = sum(r["passed"] for r in results)
    total_time = sum(r["seconds"] for r in results)
    lines = [
        f"# Eval report — {profile_name} — {stamp}",
        "",
        f"**{passed}/{len(results)} passed** in {total_time / 60:.1f} min",
        "",
        "| case | result | time | tool calls | notes |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        note = "" if r["passed"] else r["failures"][0].replace("|", "\\|")[:120]
        lines.append(
            f"| {r['name']} | {'✅' if r['passed'] else '❌'} | {r['seconds']}s | {r['tool_calls']} | {note} |"
        )
    # Build names explicitly: profile names may contain dots (qwen3.5-9b),
    # which with_suffix would clobber.
    base = EVALS / "reports"
    md = base / f"{stamp}-{profile_name}.md"
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    (base / f"{stamp}-{profile_name}.json").write_text(
        json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return md


async def run_all(cases: list[dict], profile) -> list[dict]:
    subs = date_subs()
    results = []
    for i, case in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {case['name']} ... ", end="", flush=True)
        result = await run_case(case, profile, subs)
        status = "PASS" if result["passed"] else f"FAIL ({result['failures'][0][:80]})"
        print(f"{status}  [{result['seconds']}s, {result['tool_calls']} tools]")
        results.append(result)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(prog="tezcat-eval", description="Run the eval suite against a model profile")
    parser.add_argument("-m", "--model", help="profile name from models.yaml (default: config default)")
    parser.add_argument("-k", "--filter", help="only run cases whose name contains this substring")
    parser.add_argument("--include-network", action="store_true", help="include cases tagged 'network'")
    args = parser.parse_args()

    config = load_config(DEFAULT_MODELS)
    try:
        profile = config.profile(args.model)
    except KeyError as e:
        raise SystemExit(e.args[0]) from None
    cases = load_cases(EVALS / "cases")
    if args.filter:
        cases = [c for c in cases if args.filter in c["name"]]
    if not args.include_network:
        cases = [c for c in cases if "network" not in c.get("tags", [])]
    if not cases:
        raise SystemExit("no cases matched")

    print(f"eval: {len(cases)} cases against {profile.name} ({profile.model})")
    results = asyncio.run(run_all(cases, profile))
    passed = sum(r["passed"] for r in results)
    report = write_report(profile.name, results)
    print(f"\n{passed}/{len(results)} passed — report: {report.relative_to(ROOT)}")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
