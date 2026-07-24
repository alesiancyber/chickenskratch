"""Corpus capture: every human judgment lands as a self-contained JSONL
row, and empty trajectories never do."""

import json

from harness import corpus

MSGS = [
    {"role": "system", "content": "s"},
    {"role": "user", "content": "add a task"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
]


def read_rows(corpus_dir):
    files = list(corpus_dir.glob("*.jsonl"))
    assert len(files) <= 1
    if not files:
        return []
    return [json.loads(ln) for ln in files[0].read_text().splitlines()]


def test_confirm_approved_round_trips(tmp_path):
    path = corpus.record_confirm(
        "chat-1", "fake-model", MSGS, "add_task", {"text": "x"}, approved=True,
        corpus_dir=tmp_path,
    )
    assert path is not None and path.suffix == ".jsonl"
    (row,) = read_rows(tmp_path)
    assert row["schema_version"] == 1
    assert row["chat_id"] == "chat-1" and row["model"] == "fake-model"
    assert row["messages"] == MSGS
    assert row["label"]["source"] == "confirm"
    assert row["label"]["polarity"] == "positive"
    assert row["label"]["approved"] is True and row["label"]["timed_out"] is False


def test_confirm_denied_and_timeout(tmp_path):
    corpus.record_confirm("c", "m", MSGS, "add_task", {}, approved=False, corpus_dir=tmp_path)
    corpus.record_confirm("c", "m", MSGS, "add_task", {}, approved=False, timed_out=True,
                          corpus_dir=tmp_path)
    denied, timed = read_rows(tmp_path)
    assert denied["label"]["polarity"] == "negative" and denied["label"]["timed_out"] is False
    assert timed["label"]["timed_out"] is True


def test_bug_report_row(tmp_path):
    corpus.record_bug_report("c", "m", MSGS, "the counts were wrong",
                             "data/bug_reports/x.md", corpus_dir=tmp_path)
    (row,) = read_rows(tmp_path)
    assert row["label"]["source"] == "bug_report" and row["label"]["polarity"] == "negative"
    assert row["label"]["description"] == "the counts were wrong"
    assert row["label"]["report_path"] == "data/bug_reports/x.md"


def test_revert_row(tmp_path):
    corpus.record_revert("c", "m", MSGS, "a1b2c3d",
                         "harness: append to Tasks/Inbox.md\n\nChat-Id: c",
                         corpus_dir=tmp_path)
    (row,) = read_rows(tmp_path)
    assert row["label"]["source"] == "revert" and row["label"]["sha"] == "a1b2c3d"
    assert row["label"]["polarity"] == "negative"


def test_dashboard_row_needs_no_trajectory(tmp_path):
    proposal = {"id": "p-1", "kind": "schema_fix", "apply": []}
    corpus.record_dashboard(proposal, "approved", corpus_dir=tmp_path)
    corpus.record_dashboard(proposal, "rejected", corpus_dir=tmp_path)
    approved, rejected = read_rows(tmp_path)
    assert approved["label"]["polarity"] == "positive"
    assert approved["label"]["proposal"] == proposal
    assert rejected["label"]["polarity"] == "negative"


def test_empty_messages_no_op(tmp_path):
    assert corpus.record_confirm("c", "m", [], "t", {}, approved=True, corpus_dir=tmp_path) is None
    assert corpus.record_bug_report("c", "m", [], "d", "p", corpus_dir=tmp_path) is None
    assert read_rows(tmp_path) == []


def test_month_file_accumulates_and_nested_dir_created(tmp_path):
    nested = tmp_path / "deep" / "corpus"
    corpus.record_confirm("c", "m", MSGS, "t", {}, approved=True, corpus_dir=nested)
    corpus.record_bug_report("c", "m", MSGS, "d", "p", corpus_dir=nested)
    rows = read_rows(nested)
    assert len(rows) == 2 and all(r["schema_version"] == 1 for r in rows)
