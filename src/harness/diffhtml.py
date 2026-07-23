"""GitHub-style split diffs for Review.md (diff_style: split).

Review.md is a machine-rendered view, and Obsidian renders embedded HTML
in reading mode — so proposals can show a real side-by-side diff: line
numbers, row coloring, word-level marks, long unchanged runs collapsed.
Styled via Obsidian's CSS variables (--font-monospace) so it follows the
user's theme; colors are low-alpha GitHub hues that read in light and
dark. Emitted as one HTML block with no blank lines (a blank line would
end the markdown HTML block mid-table).
"""

from __future__ import annotations

import difflib
import html
import re

_TOKEN_RE = re.compile(r"\S+|\s+")

# GitHub's hues at alphas that work on both themes
_ROW_ADD = "background:rgba(63,185,80,0.15);"
_ROW_DEL = "background:rgba(248,81,73,0.15);"
_WORD_ADD = "background:rgba(63,185,80,0.38); border-radius:2px;"
_WORD_DEL = "background:rgba(248,81,73,0.38); border-radius:2px;"

_TABLE = ('<table style="width:100%; border-collapse:collapse; '
          'font-family:var(--font-monospace); font-size:0.82em; line-height:1.5;">')
_NUM = ('<td style="width:2.6em; min-width:2.6em; text-align:right; padding:0 6px; '
        'opacity:0.45; user-select:none; vertical-align:top;">{}</td>')
_CELL = ('<td style="width:50%; padding:0 8px; white-space:pre-wrap; '
         'word-break:break-word; vertical-align:top; {}">{}</td>')

CONTEXT = 3  # visible lines on each side of a change; longer equal runs collapse


def _mark_words(old: str, new: str) -> tuple[str, str]:
    """Word-level highlight for a paired changed line, GitHub-style."""
    a, b = _TOKEN_RE.findall(old), _TOKEN_RE.findall(new)
    left, right = [], []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        old_part, new_part = html.escape("".join(a[i1:i2])), html.escape("".join(b[j1:j2]))
        if tag == "equal":
            left.append(old_part)
            right.append(new_part)
        else:
            if old_part:
                left.append(f'<span style="{_WORD_DEL}">{old_part}</span>')
            if new_part:
                right.append(f'<span style="{_WORD_ADD}">{new_part}</span>')
    return "".join(left), "".join(right)


def _row(ln: str | int, lhtml: str, lstyle: str, rn: str | int, rhtml: str, rstyle: str) -> str:
    return ("<tr>" + _NUM.format(ln) + _CELL.format(lstyle, lhtml)
            + _NUM.format(rn) + _CELL.format(rstyle, rhtml) + "</tr>")


def _skip_row(n: int) -> str:
    return ('<tr><td colspan="4" style="text-align:center; opacity:0.5; '
            f'padding:2px 0;">⋯ {n} unchanged line(s)</td></tr>')


def split_diff(before: str, after: str) -> str:
    """The side-by-side table for two versions of a note, as one HTML
    block (single string, no blank lines)."""
    if before == after:
        return ""
    a, b = before.splitlines(), after.splitlines()
    opcodes = difflib.SequenceMatcher(None, a, b).get_opcodes()
    rows: list[str] = []
    for k, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            # context lines hug the changes; long unchanged runs collapse
            run = i2 - i1
            head = CONTEXT if k > 0 else 0
            tail = CONTEXT if k < len(opcodes) - 1 else 0
            if run <= head + tail + 1:
                show = set(range(run))
            else:
                show = set(range(head)) | set(range(run - tail, run))
            skip_emitted = False
            for off in range(run):
                if off in show:
                    text = html.escape(a[i1 + off])
                    rows.append(_row(i1 + off + 1, text, "", j1 + off + 1, text, ""))
                elif not skip_emitted:
                    rows.append(_skip_row(run - len(show)))
                    skip_emitted = True
        elif tag == "replace":
            n = max(i2 - i1, j2 - j1)
            for off in range(n):
                has_l, has_r = i1 + off < i2, j1 + off < j2
                if has_l and has_r:
                    lh, rh = _mark_words(a[i1 + off], b[j1 + off])
                else:
                    lh = html.escape(a[i1 + off]) if has_l else ""
                    rh = html.escape(b[j1 + off]) if has_r else ""
                rows.append(_row(
                    i1 + off + 1 if has_l else "", lh, _ROW_DEL if has_l else "",
                    j1 + off + 1 if has_r else "", rh, _ROW_ADD if has_r else "",
                ))
        elif tag == "delete":
            for off in range(i2 - i1):
                rows.append(_row(i1 + off + 1, html.escape(a[i1 + off]), _ROW_DEL, "", "", ""))
        elif tag == "insert":
            for off in range(j2 - j1):
                rows.append(_row("", "", "", j1 + off + 1, html.escape(b[j1 + off]), _ROW_ADD))
    if not rows:
        return ""
    return "\n".join([_TABLE, *rows, "</table>"])
