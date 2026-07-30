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
"""

from __future__ import annotations

import re

from tezcat_vault import workers as wk
from tezcat_vault.vault import Vault, read_lossy
from tezcat_vault.vaultconfig import get_config

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

MAX_NOTES = 8          # notes per pack — sources stay skimmable
NOTE_CHAR_CAP = 8000   # per note: a transcript record must not eat the pack
PACK_CHAR_BUDGET = 40000  # total; bounded even on a huge-context worker


def _terms(question: str) -> list[str]:
    return [w for w in _WORD_RE.findall(question.lower())
            if w not in _STOP and len(w) >= 3]


def gather(vault_path: str, question: str) -> tuple[list[tuple[str, str]], int]:
    """(the packed notes as (rel, text), how many notes matched at all).

    Keyword scoring, title hits weighted over body hits. When nothing
    matches — or the question has no content words — the fallback is
    the most recently modified notes: recency is the best deterministic
    guess at "what we were just talking about", and the worker's own
    rule ("say when the notes don't answer it") covers the miss."""
    cfg = get_config(vault_path)
    v = Vault(vault_path, exclude=cfg.exclude)
    terms = _terms(question)
    rows = []  # (score, mtime, rel, text)
    for p, rel in v.iter_knowledge_notes(cfg.surface_dirs):
        text = read_lossy(p)
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
    picked, matched = gather(vault_path, question)
    if not picked:
        return "_no notes to search yet_"
    notes = "\n\n".join(f"=== {rel} ===\n{text}" for rel, text in picked)
    data = await wk.run_worker(wk.WORKERS["answer"], question=question, notes=notes)
    offered = {rel for rel, _ in picked}
    sources: list[str] = []
    for s in data.get("sources") or []:
        s = str(s).strip()
        rel = s if s in offered else (s + ".md" if s + ".md" in offered else None)
        if rel and rel not in sources:
            sources.append(rel)
    quotes = _verified_quotes(data.get("quotes"), sources, dict(picked))
    return _render(str(data["answer"]), sources, len(picked), matched, quotes)
