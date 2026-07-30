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
