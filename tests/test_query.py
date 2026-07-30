"""//ask — the query layer's first verb: deterministic retrieval packs
the notes, one bounded worker call answers from them with sources, and
a partial pack says so. Fakes stand in for the worker, as everywhere."""

import asyncio

import pytest

from chickenskratch import commands, query
from chickenskratch_vault import workers as wk


@pytest.fixture()
def qvault(vault):
    (vault / "Projects").mkdir(exist_ok=True)
    (vault / "Projects/Harness Review.md").write_text(
        "# Harness Review\n\n## Failure modes\n\n"
        "We identified three failure modes: the wedged vault lock, the\n"
        "stale memo, and the silent dead worker.\n")
    (vault / "Projects/Unrelated.md").write_text("# Unrelated\n\nnothing here\n")
    return vault


def scripted_worker(monkeypatch, reply):
    calls = []

    async def fake(spec, **inputs):
        assert spec.name == "answer"
        calls.append(inputs)
        return reply

    monkeypatch.setattr(wk, "run_worker", fake)
    return calls


def test_gather_ranks_the_note_that_answers(qvault):
    picked, matched = query.gather(str(qvault), "how many failure modes did we identify?")
    assert picked[0][0] == "Projects/Harness Review.md"
    assert matched >= 1
    # question stopwords carry no signal — "did we identify" alone
    # must not rank Unrelated.md above the real hit
    assert all(rel != "Projects/Unrelated.md" for rel, _ in picked[:1])


def test_gather_falls_back_to_recency_when_nothing_matches(qvault):
    picked, _matched = query.gather(str(qvault), "zzz qqq xxyzzy")
    assert picked  # never an empty pack on a non-empty vault


def test_ask_renders_answer_with_validated_sources(qvault, monkeypatch):
    calls = scripted_worker(monkeypatch, {
        "answer": "Three: the wedged lock, the stale memo, the dead worker.",
        "sources": ["Projects/Harness Review.md", "Invented/Nope.md"],
    })
    out = asyncio.run(query.ask(str(qvault), "how many failure modes did we identify?"))
    assert "Three:" in out
    assert "[[Projects/Harness Review]]" in out   # cited, linked, .md dropped
    assert "Nope" not in out                      # invented source: dropped, not linked
    # the pack the worker saw is headed by the note that answers
    assert "=== Projects/Harness Review.md ===" in calls[0]["notes"]


def test_ask_says_when_the_pack_is_partial(qvault, monkeypatch):
    scripted_worker(monkeypatch, {"answer": "An answer.", "sources": []})
    monkeypatch.setattr(query, "MAX_NOTES", 1)
    out = asyncio.run(query.ask(str(qvault), "note"))  # matches several notes
    assert "narrow the question" in out


def test_ask_is_a_registered_command_with_the_transient_contract(qvault, monkeypatch):
    """//ask parses like any command, and a down worker returns None —
    the line stays armed in a note, the canvas says try again."""
    from chickenskratch_vault.vault import VaultError

    assert commands.parse("//ask how many failure modes?") == \
        ("ask", "how many failure modes?")
    assert commands.is_command_line("//ask anything")  # never capture text

    async def down(vault_path, question):
        raise VaultError("answer worker unavailable: 429")

    monkeypatch.setattr(query, "ask", down)
    result = asyncio.run(commands.INLINE_COMMANDS["ask"]("q", str(qvault)))
    assert result is None


def test_in_note_ask_writes_the_answer_back_as_a_callout(qvault, make_watcher, monkeypatch):
    """The whole loop in a note: //ask line in, callout with the answer
    and its [[source]] out, no queue artifacts — a command, not a
    proposal."""
    from chickenskratch_vault.vault import Vault

    Vault(str(qvault)).commit_if_dirty("baseline")

    async def canned(vault_path, question):
        return "Three failure modes.\n\n— [[Projects/Harness Review]]"

    monkeypatch.setattr(query, "ask", canned)
    note = qvault / "Projects/Harness Review.md"
    note.write_text(note.read_text() + "\n//ask how many failure modes?\n")
    w = make_watcher(qvault, registry=())
    assert asyncio.run(w.run_once())["commands"] == 1
    text = note.read_text()
    assert "> [!NOTE] //ask how many failure modes?" in text
    assert "> Three failure modes." in text
    assert "//ask how many" not in text.splitlines()[0]  # replaced in place


def test_ask_quotes_ride_as_link_targets_only_when_verbatim(qvault, monkeypatch):
    """A verified quote becomes the link's #target (clicking lands on the
    supporting line); the visible label stays the note name."""
    scripted_worker(monkeypatch, {
        "answer": "Three.",
        "sources": ["Projects/Harness Review.md"],
        "quotes": {"Projects/Harness Review.md": "the wedged vault lock"},
    })
    out = asyncio.run(query.ask(str(qvault), "how many failure modes did we identify?"))
    assert "[[Projects/Harness Review#the wedged vault lock|Harness Review]]" in out


def test_ask_drops_a_quote_that_is_not_verbatim(qvault, monkeypatch):
    """A paraphrase (or a quote for an uncited note) must degrade to the
    plain note link — never a jump that lands nowhere."""
    scripted_worker(monkeypatch, {
        "answer": "Three.",
        "sources": ["Projects/Harness Review.md"],
        "quotes": {"Projects/Harness Review.md": "a paraphrase that appears nowhere",
                   "Projects/Unrelated.md": "nothing here"},
    })
    out = asyncio.run(query.ask(str(qvault), "how many failure modes did we identify?"))
    assert "[[Projects/Harness Review]]" in out
    assert "paraphrase" not in out
    assert "Unrelated" not in out
