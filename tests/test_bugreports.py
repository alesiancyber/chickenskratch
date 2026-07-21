"""Bug reports: a saved conversation is a complete, readable example."""

from harness.bugreports import format_transcript, list_reports, save_report

MESSAGES = [
    {"role": "system", "content": "you are an assistant"},
    {"role": "user", "content": "how many open tasks?"},
    {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "list_tasks", "arguments": '{"status": "open"}'}}
    ]},
    {"role": "tool", "content": '{"total": 8}'},
    {"role": "assistant", "content": "You have 8 open tasks."},
]


def test_format_transcript_includes_tools_and_results_but_not_system():
    md = format_transcript(MESSAGES)
    assert "you are an assistant" not in md          # system prompt dropped
    assert "how many open tasks?" in md
    assert "list_tasks" in md and '"status": "open"' in md   # tool call + args
    assert '"total": 8' in md                        # tool result kept
    assert "You have 8 open tasks." in md


def test_save_report_writes_readable_file(tmp_path):
    path = save_report(
        description="counts are wrong",
        model="qwen3.5-9b",
        messages=MESSAGES,
        stats={"input_tokens": 120, "output_tokens": 15, "estimated": True},
        servers={"obsidian": {"state": "connected"}, "files": {"state": "disabled"}},
        reports_dir=tmp_path,
    )
    assert path.exists() and path.suffix == ".md"
    text = path.read_text()
    assert "# Bug report: counts are wrong" in text
    assert "qwen3.5-9b" in text
    assert "120 in / 15 out (estimated)" in text
    assert "obsidian" in text and "files" not in text.split("## Conversation")[0]  # only connected
    assert "You have 8 open tasks." in text

    listed = list_reports(tmp_path)
    assert len(listed) == 1 and listed[0]["title"] == "Bug report: counts are wrong"


def test_save_report_survives_empty_conversation(tmp_path):
    path = save_report(description="", model="", messages=[], reports_dir=tmp_path)
    assert path.exists()
    assert "(no description)" in path.read_text()
