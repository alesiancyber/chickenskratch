"""Unit tests for the worker-eval scorer — no model, no network.

The scorer is what turns a worker A/B into a scored answer, so its edge
cases (absent-key-means-unchecked, explicit-null-means-null, exact
counts, forbidden text) get pinned here.
"""

from __future__ import annotations

from chickenskratch.worker_evals import score, score_grouping

SUBS = {"{today}": "2026-07-20", "{tomorrow}": "2026-07-21", "{plus14}": "2026-08-03"}


def task(text, due=None, priority=None):
    return {"text": text, "due": due, "priority": priority}


def test_pass_with_all_field_checks():
    case = {"expect": [{"match": "(?i)cert", "due": "{today}", "priority": "highest"}], "exact": True}
    assert score(case, [task("Renew the mail server cert", "2026-07-20", "highest")], SUBS) == []


def test_missing_expected_task_fails():
    case = {"expect": [{"match": "(?i)pricing"}]}
    assert score(case, [], SUBS)


def test_field_mismatch_names_the_field():
    case = {"expect": [{"match": "cert", "due": "{today}"}]}
    failures = score(case, [task("renew cert")], SUBS)
    assert any("due" in f for f in failures)


def test_exact_count_flags_hallucinated_extras():
    case = {"expect": [], "exact": True}
    assert score(case, [task("made-up task")], SUBS)


def test_explicit_null_due_is_enforced():
    case = {"expect": [{"match": "onboarding", "due": None}]}
    assert score(case, [task("update onboarding doc", due="2026-07-25")], SUBS)


def test_absent_field_is_not_checked():
    case = {"expect": [{"match": "onboarding"}]}
    assert score(case, [task("update onboarding doc", due="2026-07-25", priority="low")], SUBS) == []


def test_forbidden_text_fails():
    case = {"expect": [{"match": "(?i)cert"}], "forbid_text": ["(?i)urgent"]}
    assert score(case, [task("URGENT renew cert")], SUBS)


def test_two_expectations_cannot_match_one_task():
    case = {"expect": [{"match": "(?i)report"}, {"match": "(?i)report"}]}
    assert score(case, [task("submit expense report")], SUBS)


# ---- grouping scorer ----

GCASE = {
    "tasks": [{"id": "t-1", "text": "a"}, {"id": "t-2", "text": "b"},
              {"id": "t-3", "text": "c"}, {"id": "t-4", "text": "d"}],
    "must_group": [["t-1", "t-2"]],
    "must_separate": [["t-1", "t-3"]],
}


def groups(*member_lists):
    return {"groups": [{"name": f"G{i}", "member_ids": list(m)}
                       for i, m in enumerate(member_lists)]}


def test_grouping_pass():
    assert score_grouping(GCASE, groups(["t-1", "t-2"], ["t-3", "t-4"])) == []


def test_grouping_partition_integrity_always_enforced():
    assert score_grouping(GCASE, groups(["t-1", "t-2"], ["t-3"]))          # dropped t-4
    assert score_grouping(GCASE, groups(["t-1", "t-2"], ["t-3", "t-4", "t-9"]))  # invented


def test_grouping_must_group_violation_fails():
    assert score_grouping(GCASE, groups(["t-1", "t-3"], ["t-2", "t-4"]))


def test_grouping_must_separate_violation_fails():
    assert score_grouping(GCASE, groups(["t-1", "t-2", "t-3"], ["t-4"]))


def test_grouping_names_are_never_scored():
    reply = {"groups": [{"name": "anything at all", "member_ids": ["t-1", "t-2"]},
                        {"name": "whatever", "member_ids": ["t-3", "t-4"]}]}
    assert score_grouping(GCASE, reply) == []


# ---- schema_fix scoring: the production verifier is the judge ----

SCHEMA_CASE = {
    "worker": "schema_fix",
    "note": "Meetings/2026-07-15 Sync.md",
    "entity": "meeting",
    "new_id": "m-0123456789ab",
    "issues": ["(root): 'id' is a required property"],
    "content": (
        "---\ndate: '2026-07-15'\ntype: meeting\nattendees: [Sam]\n---\n"
        "# Sync\n\nRio demoed the dashboard.\n"
    ),
    "after_contains": ["id: m-0123456789ab"],
}


def _fix(*steps):
    return {"apply": list(steps)}


def test_schema_fix_pass():
    from chickenskratch.worker_evals import score_schema_fix

    reply = _fix({"tool": "update_frontmatter",
                  "args": {"path": SCHEMA_CASE["note"], "key": "id", "value": "m-0123456789ab"}})
    assert score_schema_fix(SCHEMA_CASE, reply) == []


def test_schema_fix_reworded_prose_fails():
    from chickenskratch.worker_evals import score_schema_fix

    reply = _fix(
        {"tool": "update_frontmatter",
         "args": {"path": SCHEMA_CASE["note"], "key": "id", "value": "m-0123456789ab"}},
        {"tool": "replace_section",
         "args": {"path": SCHEMA_CASE["note"], "heading": "Sync",
                  "content": "Rio demonstrated the dashboard."}},  # reword = never ours
    )
    failures = score_schema_fix(SCHEMA_CASE, reply)
    assert any("altered" in f or "reworded" in f for f in failures)


def test_schema_fix_wrong_id_fails_after_contains():
    from chickenskratch.worker_evals import score_schema_fix

    reply = _fix({"tool": "update_frontmatter",
                  "args": {"path": SCHEMA_CASE["note"], "key": "id", "value": "m-999999999999"}})
    failures = score_schema_fix(SCHEMA_CASE, reply)
    assert any("lacks required pattern" in f for f in failures)


def test_schema_fix_incomplete_fix_fails_conformance():
    from chickenskratch.worker_evals import score_schema_fix

    # touches nothing the schema needs: result still nonconforming
    reply = _fix({"tool": "update_frontmatter",
                  "args": {"path": SCHEMA_CASE["note"], "key": "tags", "value": "[x]"}})
    failures = score_schema_fix(SCHEMA_CASE, reply)
    assert any("nonconforming" in f for f in failures)


def test_expected_task_binds_to_best_matching_candidate():
    """Two returned tasks can match one pattern (a defensible extra that
    shares words); the expectation must bind to the one whose fields
    check out, not blindly the first regex hit."""
    case = {"expect": [{"match": "(?i)review", "due": "{today}", "owed_by": "Sam"}]}
    tasks = [
        {"text": "Do not deploy until review is complete", "due": None, "owed_by": None},
        {"text": "Schedule the review", "due": "2026-07-20", "owed_by": "Sam"},
    ]
    assert score(case, tasks, SUBS) == []


# ---- record scorer (pure half) ----


def _record_data(kind="capture", title="", attendees=(), summary="", tasks=()):
    return {"record": {"kind": kind, "title": title,
                       "attendees": list(attendees), "summary": summary},
            "tasks": [dict(t) for t in tasks]}


def test_score_record_pins_kind_and_filing():
    from chickenskratch.worker_evals import score_record
    data = _record_data(kind="capture")
    step = {"tool": "append_to_note", "args": {}}
    case = {"kind": "meeting", "files_as": "note"}
    failures = score_record(case, data, step, [], SUBS)
    assert any("kind=" in f for f in failures)
    assert any("filed as daily-append" in f for f in failures)


def test_score_record_gate_prose_only_counts_when_clean_demanded():
    from chickenskratch.worker_evals import score_record
    data = _record_data()
    step = {"tool": "append_to_note", "args": {}}
    verify = ["summary prose no check can vouch for: 'a bold claim'"]
    assert score_record({}, data, step, verify, SUBS) == []
    assert score_record({"gate": "clean"}, data, step, verify, SUBS)


def test_score_record_hard_gate_failures_always_count():
    from chickenskratch.worker_evals import score_record
    data = _record_data()
    step = {"tool": "append_to_note", "args": {}}
    verify = ["verbatim line missing from the record: '> the dump line'"]
    assert any("gate:" in f for f in score_record({}, data, step, verify, SUBS))


def test_score_record_attendee_and_summary_pins():
    from chickenskratch.worker_evals import score_record
    data = _record_data(kind="meeting", attendees=["Priya"], summary="legacy auth stays off")
    step = {"tool": "create_note", "args": {}}
    case = {"attendees_include": ["Marcus"], "attendees_forbid": ["Priya"],
            "summary_matches": ["biweekly"], "summary_forbid": ["legacy auth"]}
    failures = score_record(case, data, step, [], SUBS)
    assert len(failures) == 4


def test_score_record_owed_by_name_case_is_spelling_not_judgment():
    case = {"expect": [{"match": "dpa", "owed_by": "Ravi"}]}
    assert score(case, [{"text": "send the dpa draft", "owed_by": "ravi"}], SUBS) == []


# ---- answer scorer ----


PACK = {"Ops/Journal.md": "# Journal\n\nthe cert expired on the backup target\n",
        "Ops/Other.md": "# Other\n\nnothing relevant here\n"}


def test_score_answer_happy_path_with_verified_quote():
    from chickenskratch.worker_evals import score_answer
    reply = {"answer": "The cert expired.", "sources": ["Ops/Journal.md"],
             "quotes": {"Ops/Journal.md": "the cert expired on the backup target"}}
    case = {"answer_matches": ["cert expired"], "must_cite": ["Ops/Journal.md"],
            "must_not_cite": ["Ops/Other.md"], "min_verified_quotes": 1}
    assert score_answer(case, reply, PACK) == []


def test_score_answer_invented_source_is_a_failure():
    from chickenskratch.worker_evals import score_answer
    reply = {"answer": "x", "sources": ["Ops/Imagined.md"]}
    assert any("not in the pack" in f for f in score_answer({}, reply, PACK))


def test_score_answer_md_suffix_is_normalized_like_production():
    from chickenskratch.worker_evals import score_answer
    reply = {"answer": "cert expired", "sources": ["Ops/Journal"]}
    case = {"must_cite": ["Ops/Journal.md"]}
    assert score_answer(case, reply, PACK) == []


def test_score_answer_paraphrased_quote_never_verifies():
    from chickenskratch.worker_evals import score_answer
    reply = {"answer": "cert expired", "sources": ["Ops/Journal.md"],
             "quotes": {"Ops/Journal.md": "the certificate had expired"}}
    case = {"min_verified_quotes": 1}
    assert any("survive verification" in f for f in score_answer(case, reply, PACK))


def test_score_answer_refusal_shape():
    from chickenskratch.worker_evals import score_answer
    ok = {"answer": "The notes don't say.", "sources": []}
    case = {"expect_no_sources": True}
    assert score_answer(case, ok, PACK) == []
    assert score_answer(case, {"answer": "x", "sources": ["Ops/Journal.md"]}, PACK)


# ---- plan scorer ----


PLAN_CASE = {"tasks": [{"id": "t-aaaaaaaaaaa1", "text": "a"},
                       {"id": "t-aaaaaaaaaaa2", "text": "b"},
                       {"id": "t-aaaaaaaaaaa3", "text": "c"},
                       {"id": "t-aaaaaaaaaaa4", "text": "d"}]}


def test_score_plan_id_discipline():
    from chickenskratch.worker_evals import score_plan
    reply = {"picks": [{"id": "t-invented0001"}, {"id": "t-aaaaaaaaaaa1"},
                       {"id": "t-aaaaaaaaaaa1"}]}
    failures = score_plan(PLAN_CASE, reply)
    assert any("invented" in f for f in failures)
    assert any("repeated" in f for f in failures)


def test_score_plan_over_max_picks_fails():
    from chickenskratch.worker_evals import score_plan
    reply = {"picks": [{"id": f"t-aaaaaaaaaaa{i}"} for i in (1, 2, 3, 4)]}
    assert any("max_picks" in f for f in score_plan(PLAN_CASE, reply))


def test_score_plan_judgment_pins():
    from chickenskratch.worker_evals import score_plan
    reply = {"picks": [{"id": "t-aaaaaaaaaaa2"}]}
    case = {**PLAN_CASE, "must_pick": ["t-aaaaaaaaaaa1"],
            "must_not_pick": ["t-aaaaaaaaaaa2"], "max_expected": 0}
    failures = score_plan(case, reply)
    assert len(failures) == 3
    assert score_plan({**PLAN_CASE, "expect_empty": True}, {"picks": []}) == []


# ---- the record path end to end (fake worker, real shaping + gate) ----


def test_record_case_runs_the_production_gate(monkeypatch):
    import asyncio

    from chickenskratch import worker_evals as we
    from chickenskratch.bootstrap import ROOT
    from chickenskratch.evals import date_subs
    from chickenskratch_vault import workers

    monkeypatch.setenv("VAULT_CONFIG_PATH", str(ROOT / "config" / "vault.yaml"))

    async def fake_extract(text, today, stats=None):
        if stats is not None:
            stats["attempts"] = 1
        return {"record": {"kind": "capture", "title": "", "attendees": [], "summary": ""},
                "tasks": [{"text": "renew the cert", "due": None, "scheduled": None,
                           "priority": None, "owed_by": None}]}

    monkeypatch.setattr(workers, "extract_record", fake_extract)
    case = {"name": "wired", "worker": "extract_record", "text": "renew the cert",
            "kind": "capture", "files_as": "daily-append", "gate": "clean",
            "summary_empty": True, "expect": [{"match": "cert"}], "exact": True}
    reply, failures = asyncio.run(we._run_worker_once(case, date_subs(), {}))
    assert failures == []

    async def sloppy_extract(text, today, stats=None):
        return {"record": {"kind": "capture", "title": "", "attendees": [],
                           "summary": "an invented editorial summary"},
                "tasks": [{"text": "renew the cert", "due": None, "scheduled": None,
                           "priority": None, "owed_by": None}]}

    monkeypatch.setattr(workers, "extract_record", sloppy_extract)
    reply, failures = asyncio.run(we._run_worker_once(case, date_subs(), {}))
    # gate: clean means the invented prose is the failure — the auto-file bar
    assert any("clean demanded" in f for f in failures)


# ---- the case files themselves: every case parses, names a known
# worker, and every regex compiles — a broken fixture must fail in CI,
# not silently skip during a bakeoff ----


def test_case_files_lint():
    import re as _re

    from chickenskratch.worker_evals import DEFAULT_BUDGET_S, load_cases

    known = set(DEFAULT_BUDGET_S)
    cases = load_cases()
    assert len(cases) >= 90, f"expected the fattened case set, found {len(cases)}"
    seen_workers = set()
    for c in cases:
        worker = c.get("worker", "extract_tasks")
        assert worker in known, f"{c['name']}: unknown worker {worker!r}"
        seen_workers.add(worker)
        for want in c.get("expect") or []:
            if isinstance(want, dict) and "match" in want:
                _re.compile(want["match"])
        for key in ("forbid_text", "answer_matches", "answer_forbid",
                    "summary_matches", "summary_forbid"):
            for pat in c.get(key) or []:
                _re.compile(pat)
        if "title_matches" in c:
            _re.compile(c["title_matches"])
        if worker == "answer":
            assert c.get("question") and c.get("notes"), f"{c['name']}: answer case shape"
            rels = {n["rel"] for n in c["notes"]}
            for rel in list(c.get("must_cite") or []) + list(c.get("must_not_cite") or []):
                assert rel in rels, f"{c['name']}: {rel!r} not in the case's own pack"
        if worker == "plan_today":
            ids = [t["id"] for t in c["tasks"]]
            for i in list(c.get("must_pick") or []) + list(c.get("must_not_pick") or []):
                assert i in ids, f"{c['name']}: pin {i!r} not in the pool"
    # every worker the engine invokes by name has judgment coverage
    assert seen_workers == known, f"workers without cases: {sorted(known - seen_workers)}"
