"""Corpus capture: every human judgment lands as a self-contained JSONL
row, and empty trajectories never do."""

import json

from chickenskratch import corpus

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


def test_empty_messages_no_op_unless_self_contained(tmp_path):
    """No trajectory + no payload = nothing to train on. A revert row IS
    its payload (sha + message identify the disavowed action), so it
    lands even with the scratch transcript gone."""
    assert corpus.record_confirm("c", "m", [], "t", {}, approved=True, corpus_dir=tmp_path) is None
    assert corpus.record_revert("", "", [], "a1b2c3d", "harness: add task", corpus_dir=tmp_path)
    (row,) = read_rows(tmp_path)
    assert row["label"]["source"] == "revert" and row["label"]["sha"] == "a1b2c3d"


def test_month_file_accumulates_and_nested_dir_created(tmp_path):
    nested = tmp_path / "deep" / "corpus"
    corpus.record_confirm("c", "m", MSGS, "t", {}, approved=True, corpus_dir=nested)
    corpus.record_revert("c", "m", MSGS, "a1b2c3d", "msg", corpus_dir=nested)
    rows = read_rows(nested)
    assert len(rows) == 2 and all(r["schema_version"] == 1 for r in rows)


def test_capture_failure_never_reaches_the_caller(tmp_path, monkeypatch, capsys):
    """The swallow lives in _append now, once, instead of at each call
    site — and it covers serialization failures too, which the old
    per-caller `except OSError` did not."""
    from chickenskratch import corpus

    # unserializable payload: used to raise TypeError straight through
    class Exotic:
        def __repr__(self):
            return "<exotic>"

    p = corpus.record_dashboard({"id": "p-1", "x": Exotic()}, "approved",
                                corpus_dir=tmp_path / "c")
    assert p is not None and "<exotic>" in p.read_text()

    # and a genuinely unwritable directory is reported, not raised
    monkeypatch.setattr(corpus.Path, "mkdir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only fs")))
    assert corpus.record_dashboard({"id": "p-2"}, "approved",
                                   corpus_dir=tmp_path / "nope") is None
    assert "corpus row dropped" in capsys.readouterr().err
