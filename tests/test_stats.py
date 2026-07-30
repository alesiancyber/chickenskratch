"""Usage stats: persistent per-model totals."""

from chickenskratch.stats import record_run, totals


def test_record_and_totals_accumulate(tmp_path):
    p = tmp_path / "stats.json"
    record_run("m1", {"input_tokens": 100, "output_tokens": 10, "tool_calls": 2}, path=p)
    record_run("m1", {"input_tokens": 50, "output_tokens": 5, "tool_calls": 1, "estimated": True}, path=p)
    record_run("m2", {"input_tokens": 1, "output_tokens": 1, "tool_calls": 0}, path=p)

    t = totals(p)
    assert t["m1"]["runs"] == 2
    assert t["m1"]["input_tokens"] == 150
    assert t["m1"]["output_tokens"] == 15
    assert t["m1"]["tool_calls"] == 3
    assert t["m1"]["estimated"] is True  # one estimated run taints the total
    assert "last_used" in t["m1"]
    assert t["m2"]["runs"] == 1


def test_corrupt_stats_file_resets_cleanly(tmp_path):
    p = tmp_path / "stats.json"
    p.write_text("not json at all")
    assert totals(p) == {}
    record_run("m", {"input_tokens": 5, "output_tokens": 1, "tool_calls": 0}, path=p)
    assert totals(p)["m"]["runs"] == 1
