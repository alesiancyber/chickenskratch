"""//ask — one-shot questions answered from the vault's own notes.

The query layer's first verb (research/dumps-become-records.md rev 2:
"compile the query; don't retrieve with the model"): retrieval is
deterministic code — keyword scoring over the knowledge walk, bounded
by a char budget — and the model makes ONE bounded call that answers
from the packed notes and says which ones it drew on. No agent loop:
the chat pane stays the escape hatch for multi-hop work; this is "spit
it back at me, with sources".

Honesty rule carried over from the plan: when more notes matched than
the pack could hold, the answer says so — a partial answer must never
present as a complete one. And the walk is iter_knowledge_notes on a
Vault built WITH the exclusions, so Private/ is as invisible to a
question as to everything else (I4).

Provenance rule (2026-07-30): the context is PRE-AI text only — the
user's verbatim dumps and hand-typed notes, never model prose (see
user_text). An answer sourced from an earlier model's summary would
compound that summary's errors invisibly; the deterministic views
(task counts, due lists) stay factual regardless because they read
structure, not prose.
"""

from __future__ import annotations

import asyncio
import re

from chickenskratch_vault import workers as wk
from chickenskratch_vault.vault import Vault, read_lossy
from chickenskratch_vault.vaultconfig import get_config

# Question words carry no signal about WHICH note answers; scoring on
# them would rank every note that says "the" or "did".
_STOP = {
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or",
    "we", "i", "my", "our", "you", "your", "it", "its", "this", "that",
    "how", "many", "much", "what", "which", "who", "when", "where", "why",
    "is", "are", "was", "were", "do", "does", "did", "have", "has", "had",
    "with", "about", "from", "any", "all", "there", "me", "us",
}
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'-]+")

# Pack bounds, sized for a ~32K-token serving context (the B70's tuned
# llama-swap configs; raised 2026-08-02 from the 8K/40K caps that fit
# 16GB-Mac local models). ~80K chars ≈ 20-27K tokens of notes leaves
# room for the prompt's rules and the reply. The worker-eval's
# answer-monster-pack case exercises this ceiling — if a serving config
# can't take it, the eval says so before //ask does.
MAX_NOTES = 10         # notes per pack — sources stay skimmable
NOTE_CHAR_CAP = 48000  # per note: even a half-year journal rides whole
PACK_CHAR_BUDGET = 80000  # total; bounded even on a huge-context worker

# The two machine-written callout shapes, matched by their canonical
# titles: the record pipeline's verbatim fold (processors._verbatim_callout
# writes `> [!NOTE]- Verbatim — HH:MM`) and a command's answer written
# back into a note (commands.render_callout writes `> [!NOTE] //ask …`).
_VERBATIM_RE = re.compile(r"^> ?\[!NOTE\]- Verbatim\s*(—.*)?$")
_CMD_CALLOUT_RE = re.compile(r"^> ?\[!\w+\]-?\s*//")
_QUOTE_PREFIX_RE = re.compile(r"^\s*>\s?")


def _terms(question: str) -> list[str]:
    return [w for w in _WORD_RE.findall(question.lower())
            if w not in _STOP and len(w) >= 3]


def user_text(text: str) -> str:
    """The note reduced to what the USER wrote — //ask's context rule.

    //ask must never answer from model prose: an answer built on an
    earlier model's summary compounds that summary's errors, invisibly.
    So the context is pre-AI text only, and the vault's own grammar
    says which is which:

    - A note holding Verbatim callouts is a pipeline record — the
      callout bodies are the user's own dump, machine-folded; the
      prose and tasks around them are model output. Only the bodies
      ride (records always carry their verbatim: verify_record fails
      one that doesn't, so this never empties a real record).
    - A note without one was never drafted by the pipeline: the user's
      hand-typed text, packed whole — minus any //command answer
      callouts written back into it, which are model output wherever
      they sit.

    Deterministic views (task counts, due lists) are unaffected: they
    read structure, not prose, and stay correct even on AI-touched
    notes. Every packed line — the callout header included — is the raw
    note's own line minus its quote prefix, so ANY verified quote is a
    substring of a real line and the source link's jump lands. A
    prettier synthetic header here would be quotable text that exists
    nowhere in the note."""
    lines = text.splitlines()
    verbatim: list[str] = []
    kept: list[str] = []
    i = 0
    while i < len(lines):
        if _VERBATIM_RE.match(lines[i]):
            body = [_QUOTE_PREFIX_RE.sub("", lines[i], count=1)]
            i += 1
            while i < len(lines) and _QUOTE_PREFIX_RE.match(lines[i]):
                body.append(_QUOTE_PREFIX_RE.sub("", lines[i], count=1))
                i += 1
            verbatim.append("\n".join(body))
            continue
        if _CMD_CALLOUT_RE.match(lines[i]):
            i += 1
            while i < len(lines) and _QUOTE_PREFIX_RE.match(lines[i]):
                i += 1
            continue
        kept.append(lines[i])
        i += 1
    if verbatim:
        return "\n\n".join(verbatim)
    return "\n".join(kept)


def gather(vault_path: str, question: str) -> tuple[list[tuple[str, str]], int]:
    """(the packed notes as (rel, text), how many notes matched at all).

    Keyword scoring, title hits weighted over body hits. When nothing
    matches — or the question has no content words — the fallback is
    the most recently modified notes: recency is the best deterministic
    guess at "what we were just talking about", and the worker's own
    rule ("say when the notes don't answer it") covers the miss.

    Every note is reduced to user_text FIRST — scoring included, so a
    note can never rank on model-summary words its pack won't contain."""
    cfg = get_config(vault_path)
    v = Vault(vault_path, exclude=cfg.exclude)
    terms = _terms(question)
    rows = []  # (score, mtime, rel, text)
    for p, rel in v.iter_knowledge_notes(cfg.surface_dirs):
        text = user_text(read_lossy(p))
        if not text.strip():
            continue  # nothing the user wrote: nothing to answer from
        low = text.lower()
        stem = rel.rsplit("/", 1)[-1].lower()
        score = sum(3 * stem.count(t) + low.count(t) for t in terms)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        rows.append((score, mtime, rel, text))
    matched = [r for r in rows if r[0] > 0]
    if matched:
        matched.sort(key=lambda r: (-r[0], r[2]))
    else:
        matched = sorted(rows, key=lambda r: -r[1])  # recency fallback
    picked, spent = [], 0
    for _score, _mtime, rel, text in matched[:MAX_NOTES]:
        clip = text[:NOTE_CHAR_CAP]
        if spent + len(clip) > PACK_CHAR_BUDGET and picked:
            break
        picked.append((rel, clip))
        spent += len(clip)
    return picked, len(matched)


QUOTE_MAX_CHARS = 160


def _link(rel: str, quote: str | None) -> str:
    """One source as a wikilink. A validated quote rides as the link's
    #target — the editor's reveal finds that text and marks its line, so
    clicking a source lands on the passage that backs the answer, not
    the top of the note. The note NAME is the visible label either way
    (the target machinery folds away in rendering)."""
    base = rel.removesuffix(".md")
    if not quote:
        return f"[[{base}]]"
    return f"[[{base}#{quote}|{base.rsplit('/', 1)[-1]}]]"


def _render(answer: str, sources: list[str], packed: int, matched: int,
            quotes: dict[str, str] | None = None) -> str:
    lines = [answer.strip()]
    if sources:
        lines += ["", "— " + " · ".join(_link(r, (quotes or {}).get(r)) for r in sources)]
    if matched > packed:
        lines += ["", f"_(answered from the top {packed} of {matched} matching "
                      "notes — narrow the question to reach the rest)_"]
    return "\n".join(lines)


def _verified_quotes(raw, sources: list[str], packed: dict[str, str]) -> dict[str, str]:
    """The deterministic gate between a model's 'supporting quote' and a
    link target: verbatim (exact substring of the packed note) or it
    doesn't ride. A paraphrase degrades to a plain note link rather than
    a jump that lands nowhere — same rule as invented sources. The
    charset check keeps a quote from breaking the [[target#frag|alias]]
    grammar it travels in."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        key = str(key).strip()
        rel = key if key in packed else key + ".md" if key + ".md" in packed else None
        quote = str(value or "").strip()
        if (rel is None or rel not in sources or not quote
                or len(quote) > QUOTE_MAX_CHARS
                or any(c in quote for c in "]|#\n")
                or quote not in packed[rel]):
            continue
        out[rel] = quote
    return out


async def ask(vault_path: str, question: str) -> str:
    """The answer as markdown: the model's reply plus [[links]] to the
    notes it cited. Raises VaultError when the worker is down or answers
    junk — the command layer's transient contract (retry, never lose the
    question). Cited paths are validated against the pack: an invented
    source is dropped rather than linked, because a link that opens the
    wrong note teaches the user not to click them."""
    question = question.strip()
    # Off the loop. gather() is the one blocking stretch in this module —
    # a full walk of the knowledge notes, a read and a regex pass per
    # note, scored — and //ask is reached from two callers that run on
    # the HTTP loop (the editor's ⌘↩ via /api/command, and the canvas's
    # command split in /api/ingest). Measured at ~0.56ms per note, so a
    # real vault freezes the server, SSE included, for the length of the
    # walk. Hoisted here rather than at each caller: this is a property
    # of the retrieval, and a third caller must not have to rediscover it.
    picked, matched = await asyncio.to_thread(gather, vault_path, question)
    if not picked:
        return "_no notes to search yet_"
    notes = "\n\n".join(f"=== {rel} ===\n{text}" for rel, text in picked)
    data = await wk.run_worker(wk.get_worker("answer"), question=question, notes=notes)
    offered = {rel for rel, _ in picked}
    sources: list[str] = []
    for s in data.get("sources") or []:
        s = str(s).strip()
        rel = s if s in offered else (s + ".md" if s + ".md" in offered else None)
        if rel and rel not in sources:
            sources.append(rel)
    quotes = _verified_quotes(data.get("quotes"), sources, dict(picked))
    return _render(str(data["answer"]), sources, len(picked), matched, quotes)
