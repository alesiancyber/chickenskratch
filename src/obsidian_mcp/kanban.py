"""Obsidian Kanban plugin board format.

A board is a markdown file with `kanban-plugin: board` frontmatter,
one `## Heading` per column, `- [ ]` / `- [x]` lines as cards, and an
optional `%% kanban:settings ... %%` footer that must be preserved.
The harness owns format correctness so models never hand-edit boards.

Round-trip safety: only top-level checkbox lines are treated as cards.
Everything else — indented sub-items, card descriptions, archive
sections, any prose — is carried verbatim (attached to the preceding
card or column) and written back on render, so a board rewrite never
loses content the harness doesn't model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .markdown import join_frontmatter, split_frontmatter
from .tasks import TASK_RE

_SETTINGS_RE = re.compile(r"\n*%%\s*kanban:settings.*\Z", re.S)
_COLUMN_RE = re.compile(r"^##\s+(.*?)\s*$")


@dataclass
class Column:
    name: str
    complete_marker: bool = False   # the plugin marks its "done" column with **Complete**
    cards: list[dict] = field(default_factory=list)
    preamble: list[str] = field(default_factory=list)  # verbatim lines before the first card


@dataclass
class Board:
    frontmatter: dict
    columns: list[Column]
    settings: str  # raw settings footer, verbatim
    preamble: list[str] = field(default_factory=list)  # verbatim lines before the first column

    def column(self, name: str) -> Column:
        target = name.strip().lower()
        for col in self.columns:
            if col.name.lower() == target:
                return col
        names = ", ".join(c.name for c in self.columns)
        raise ValueError(f"Column not found: {name!r}. Board columns: {names}")

    def find_card(self, text: str) -> tuple[Column, dict]:
        """Match a card by exact text, falling back to unique substring."""
        needle = text.strip().lower()
        exact = [(c, card) for c in self.columns for card in c.cards if card["text"].lower() == needle]
        if len(exact) == 1:
            return exact[0]
        partial = [(c, card) for c in self.columns for card in c.cards if needle in card["text"].lower()]
        if len(partial) == 1:
            return partial[0]
        if not exact and not partial:
            raise ValueError(f"No card matching {text!r} on this board")
        matches = ", ".join(repr(card["text"]) for _, card in (exact or partial)[:5])
        raise ValueError(f"Card text {text!r} is ambiguous; matches: {matches}")

    def to_dict(self) -> dict:
        return {
            "columns": [
                {
                    "name": c.name,
                    "cards": [
                        {
                            "text": card["text"],
                            "done": card["done"],
                            **({"details": card["extra"]} if card.get("extra") else {}),
                        }
                        for card in c.cards
                    ],
                }
                for c in self.columns
            ]
        }


def parse_board(content: str) -> Board:
    fm, body = split_frontmatter(content)
    settings = ""
    m = _SETTINGS_RE.search(body)
    if m:
        settings = m.group(0).strip("\n")
        body = body[: m.start()]

    board_preamble: list[str] = []
    columns: list[Column] = []
    current_card: dict | None = None
    for line in body.splitlines():
        heading = _COLUMN_RE.match(line)
        if heading:
            columns.append(Column(name=heading.group(1)))
            current_card = None
            continue
        if not line.strip():
            continue
        if not columns:
            board_preamble.append(line)
            continue
        col = columns[-1]
        task = TASK_RE.match(line)
        if task and not task.group("indent"):
            current_card = {"text": task.group("text").strip(), "done": task.group("mark").lower() == "x"}
            col.cards.append(current_card)
        elif line.strip() == "**Complete**":
            col.complete_marker = True
        elif current_card is not None:
            # sub-items, descriptions, indented tasks: they belong to this
            # card and move with it, byte-for-byte
            current_card.setdefault("extra", []).append(line)
        else:
            col.preamble.append(line)
    return Board(
        frontmatter=fm or {"kanban-plugin": "board"},
        columns=columns,
        settings=settings,
        preamble=board_preamble,
    )


def render_board(board: Board) -> str:
    chunks = []
    if board.preamble:
        chunks.append("\n".join(board.preamble))
    for col in board.columns:
        lines = [f"## {col.name}", ""]
        if col.complete_marker:
            lines.append("**Complete**")
        lines.extend(col.preamble)
        for card in col.cards:
            mark = "x" if card["done"] else " "
            lines.append(f"- [{mark}] {card['text']}")
            lines.extend(card.get("extra", []))
        chunks.append("\n".join(lines))
    body = "\n\n".join(chunks) + "\n"
    if board.settings:
        body += "\n\n" + board.settings + "\n"
    return join_frontmatter(board.frontmatter, "\n" + body)
