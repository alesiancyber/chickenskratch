"""Unit tests for the worker-eval scorer — no model, no network.

The scorer is what turns a worker A/B into a scored answer, so its edge
cases (absent-key-means-unchecked, explicit-null-means-null, exact
counts, forbidden text) get pinned here.
"""

from __future__ import annotations

from harness.worker_evals import score

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
