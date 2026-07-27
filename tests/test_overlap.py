"""The overlap processor: duplicates and links across notes.

Boundary in code, similarity ranks inside it, one batched judge call
decides — and every verdict queues. Fakes stand in for both models: the
embedder returns canned vectors, the judge scripted verdicts."""

import asyncio

import pytest

from tezcat import processors, proposals
from tezcat.processors import _linked_line


@pytest.fixture()
def vault(gitvault):
    (gitvault / "Harness").mkdir(exist_ok=True)
    (gitvault / "Harness/Spec.md").write_text("---\nauto_apply: []\n---\n\n# Vault Spec\n")
    (gitvault / "Tasks/Inbox.md").write_text(
        "## Tasks\n\n"
        "- [ ] renew the domain registration ^t-inbox0000001\n"
        "- [ ] chase marcus for firewall logs ^t-inbox0000002\n")
    (gitvault / "Meetings").mkdir(exist_ok=True)
    (gitvault / "Meetings/2026-07-20 Ops Sync.md").write_text(
        "---\ntype: meeting\nid: m-aaaaaaaaaaaa\ndate: 2026-07-20\nattendees: []\n---\n"
        "# Ops Sync\n\n## Action items\n\n"
        "- [ ] Renew the domain registration! ^t-meet00000001\n")
    (gitvault / "Tasks/Waiting.md").write_text(
        "# Waiting\n\n- [ ] Marcus: send the firewall logs over ^t-wait00000001\n")
    return gitvault


def run(w):
    return asyncio.run(w.run_once())


def fake_embedder(similar: dict[str, list[float]] | None = None):
    """Canned vectors; unknown ids get the zero vector (cosine 0)."""
    vecs = similar or {}

    async def embedder(vault_path, items):
        return {k: vecs.get(k, [0.0, 0.0, 0.0]) for k in items}

    return embedder


def fake_judge(verdicts):
    calls = []

    async def judge(item, pairs):
        calls.append([(a[0].block_id, b[0].block_id) for a, b in pairs])
        return {"pairs": verdicts}

    judge.calls = calls
    return judge


def test_exact_duplicate_is_found_and_merged_without_a_judge(vault, make_watcher):
    """Normalized-identical text is a duplicate no model needs to vouch
    for: paired in the scan, drafted deterministically, and approving
    removes exactly one copy — the other line stays byte-identical."""
    judge = fake_judge([])
    w = make_watcher(vault, registry=(processors.OVERLAP,),
                     overlap_drafter=judge, embedder=fake_embedder())
    assert run(w)["drafted"] == 1
    assert judge.calls == []  # no worker call for an exact pair

    (p,) = proposals.pending(w.proposals_dir)
    assert p["kind"] == "overlap" and p["pairs"]
    assert p["apply"][0]["tool"] == "replace_line"
    assert "duplicate" in p["issues"][0]

    assert asyncio.run(w.run_once(decisions=[(p["id"], "approve")],
                                  lock_wait=5))["approved"] == 1
    both = ((vault / "Tasks/Inbox.md").read_text()
            + (vault / "Meetings/2026-07-20 Ops Sync.md").read_text())
    assert both.lower().count("renew the domain registration") == 1


def test_fuzzy_pair_rides_the_embedder_and_the_judge(vault, make_watcher):
    """A near-match crosses notes only when the vectors say so; the judge
    says related → a ⇄ block link lands on the inbox copy, before its
    ^ref, and the other line is untouched."""
    sim = {"t-inbox0000002": [1.0, 0.0, 0.0], "t-wait00000001": [1.0, 0.0, 0.0]}
    judge = fake_judge([{"a": "t-inbox0000002", "b": "t-wait00000001",
                         "verdict": "related", "keep": None}])
    # remove the exact-dup distraction so this test isolates the fuzzy lane
    (vault / "Meetings/2026-07-20 Ops Sync.md").write_text(
        "---\ntype: meeting\nid: m-aaaaaaaaaaaa\ndate: 2026-07-20\nattendees: []\n---\n"
        "# Ops Sync\n\n## Action items\n")
    w = make_watcher(vault, registry=(processors.OVERLAP,),
                     overlap_drafter=judge, embedder=fake_embedder(sim))
    assert run(w)["drafted"] == 1
    assert judge.calls == [[("t-inbox0000002", "t-wait00000001")]]

    (p,) = proposals.pending(w.proposals_dir)
    assert asyncio.run(w.run_once(decisions=[(p["id"], "approve")],
                                  lock_wait=5))["approved"] == 1
    inbox = (vault / "Tasks/Inbox.md").read_text()
    assert ("- [ ] chase marcus for firewall logs "
            "⇄ [[Tasks/Waiting#^t-wait00000001]] ^t-inbox0000002") in inbox
    assert "⇄" not in (vault / "Tasks/Waiting.md").read_text()
    # a linked pair is settled: the next pool change never re-pairs it
    (vault / "Tasks/Inbox.md").write_text(inbox + "- [ ] brand new thing ^t-new000000001\n")
    run(make_watcher(vault, registry=(processors.OVERLAP,),
                     overlap_drafter=judge, embedder=fake_embedder(sim)))
    assert len(judge.calls) == 1


def test_rejected_pairs_are_remembered_not_renagged(vault, make_watcher):
    sim = {"t-inbox0000002": [1.0, 0.0, 0.0], "t-wait00000001": [1.0, 0.0, 0.0]}
    judge = fake_judge([{"a": "t-inbox0000002", "b": "t-wait00000001",
                         "verdict": "duplicate", "keep": "t-wait00000001"}])
    w = make_watcher(vault, registry=(processors.OVERLAP,),
                     overlap_drafter=judge, embedder=fake_embedder(sim))
    run(w)
    for p in proposals.pending(w.proposals_dir):
        asyncio.run(w.run_once(decisions=[(p["id"], "reject")], lock_wait=5))
    # the pool changes (re-arming the seen hash) — but decided pairs stay decided
    inbox = vault / "Tasks/Inbox.md"
    inbox.write_text(inbox.read_text() + "- [ ] unrelated new task ^t-new000000001\n")
    w2 = make_watcher(vault, registry=(processors.OVERLAP,),
                      overlap_drafter=judge, embedder=fake_embedder(sim))
    run(w2)
    for call in judge.calls[1:]:
        assert ("t-inbox0000002", "t-wait00000001") not in call
    assert proposals.pending(w2.proposals_dir) == []


def test_embedder_down_degrades_to_exact_only(vault, make_watcher):
    async def broken(vault_path, items):
        raise RuntimeError("connection refused")

    judge = fake_judge([])
    w = make_watcher(vault, registry=(processors.OVERLAP,),
                     overlap_drafter=judge, embedder=broken)
    summary = run(w)
    assert summary["drafted"] == 1  # the exact duplicate still made it
    assert judge.calls == []


def test_unanswered_pair_kills_the_draft(vault, make_watcher):
    """Silently-missing pairs are failures (grouping's rule): a judge
    that answers half the batch produces nothing, not half a proposal."""
    sim = {"t-inbox0000002": [1.0, 0.0, 0.0], "t-wait00000001": [1.0, 0.0, 0.0]}
    (vault / "Meetings/2026-07-20 Ops Sync.md").write_text(
        "---\ntype: meeting\nid: m-aaaaaaaaaaaa\ndate: 2026-07-20\nattendees: []\n---\n# Ops Sync\n")
    judge = fake_judge([])  # answers nothing
    w = make_watcher(vault, registry=(processors.OVERLAP,),
                     overlap_drafter=judge, embedder=fake_embedder(sim))
    assert run(w)["drafted"] == 0
    assert proposals.pending(w.proposals_dir) == []


def test_linked_line_keeps_the_ref_last():
    raw = "- [ ] chase the logs 📅 2099-01-01 ^t-aaaaaaaaaaaa"
    out = _linked_line(raw, "Tasks/Waiting.md", "t-bbbbbbbbbbbb")
    assert out == ("- [ ] chase the logs 📅 2099-01-01 "
                   "⇄ [[Tasks/Waiting#^t-bbbbbbbbbbbb]] ^t-aaaaaaaaaaaa")
