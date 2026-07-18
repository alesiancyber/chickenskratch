"""Chat persistence: store round-trip, retention pruning, transcript
rendering, and context trimming."""

import os
import time

from tests.test_agent_guarantees import make_agent, response, run

from harness.chats import ChatStore, display_user_text, transcript

MESSAGES = [
    {"role": "system", "content": "system prompt"},
    {"role": "user", "content": "add a task"},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "add_task", "arguments": '{"text": "x"}'}}
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "content": '{"updated": "Tasks/Inbox.md"}'},
    {"role": "assistant", "content": "Added."},
]


def test_store_roundtrip_and_list(tmp_path):
    store = ChatStore(tmp_path)
    cid = store.new_id()
    store.save(cid, "gemma", MESSAGES)

    loaded = store.load(cid)
    assert loaded["messages"] == MESSAGES
    assert loaded["title"] == "add a task"

    listing = store.list()
    assert [c["id"] for c in listing] == [cid]
    assert store.load("nope") is None


def test_prune_respects_retention(tmp_path):
    store = ChatStore(tmp_path, retention_days=3)
    old_id, new_id = store.new_id(), store.new_id()
    store.save(old_id, "m", MESSAGES)
    store.save(new_id, "m", MESSAGES)
    old_mtime = time.time() - 4 * 86400
    os.utime(store._path(old_id), (old_mtime, old_mtime))

    assert store.prune() == 1
    assert store.load(old_id) is None
    assert store.load(new_id) is not None


def test_transcript_rendering():
    entries = transcript(MESSAGES)
    assert entries == [
        {"role": "user", "content": "add a task"},
        {"role": "tool", "name": "add_task", "args": {"text": "x"}},
        {"role": "assistant", "content": "Added."},
    ]


def test_display_user_text_collapses_workflows():
    expanded = (
        'Follow this workflow now.\n\n<workflow name="meeting-capture">\nbody\n</workflow>'
        "\n\nUser input for this workflow: Captures/x.md"
    )
    assert display_user_text(expanded) == "/meeting-capture Captures/x.md"
    assert display_user_text("plain") == "plain"


def test_context_trimming_drops_oldest_turns():
    agent = make_agent([response(content="ok")])
    agent.profile.max_history_chars = 2000
    filler = "x" * 400
    for i in range(4):  # four old turns of ~500 chars each
        agent.messages.append({"role": "user", "content": f"question {i} {filler}"})
        agent.messages.append({"role": "assistant", "content": f"answer {i}"})

    assert run(agent, "newest question") == "ok"

    contents = [m["content"] for m in agent.messages]
    assert agent.messages[0]["role"] == "system"
    assert agent.messages[1]["role"] == "user"  # history restarts at a turn boundary
    assert not any("question 0" in c for c in contents)   # oldest dropped
    assert any("newest question" in c for c in contents)  # current turn kept


def test_trimming_never_drops_the_only_turn():
    agent = make_agent([response(content="ok")])
    agent.profile.max_history_chars = 100
    assert run(agent, "y" * 500) == "ok"
    assert any("y" * 500 == m["content"] for m in agent.messages)
