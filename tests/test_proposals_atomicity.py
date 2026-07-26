"""Durable writes: the queue, the memo, and the events log are read by
one process while another writes them, so a half-written file is a real
state — and load_all SKIPS what it can't parse, which turned a torn
write into a silently deleted proposal."""

import json
import threading

from harness import proposals


def test_a_concurrent_reader_never_sees_a_half_written_proposal(tmp_path):
    d = tmp_path / "q"
    big = "x" * 200_000  # large enough that a non-atomic write is visibly torn
    p = proposals.new_proposal(
        kind="capture", note="Home.md", vault_sha="", note_content="c",
        title="t", summary=big, issues=[],
        apply=[{"tool": "add_task", "args": {"path": "Tasks/Inbox.md", "text": "t"}}])
    proposals.save(p, d)
    stop = threading.Event()
    torn: list[str] = []

    def rewrite():
        while not stop.is_set():
            proposals.save({**p, "summary": big}, d)

    writer = threading.Thread(target=rewrite, daemon=True)
    writer.start()
    try:
        for _ in range(400):
            for f in d.glob("p-*.json"):
                try:
                    json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    torn.append(str(e))
    finally:
        stop.set()
        writer.join(timeout=5)
    assert not torn, f"reader saw {len(torn)} torn file(s): {torn[:1]}"
