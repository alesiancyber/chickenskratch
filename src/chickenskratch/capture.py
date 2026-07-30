"""The capture surface's text contract: skeleton in, user text out.

Its own module because both sides of the capture pipeline need it from
opposite sides of a dependency edge — processors drafts against this
text, and applier's staleness check must normalize a capture proposal's
note exactly the same way (hashing the raw note would let a
whitespace-only autosave void an approval). A leaf module both can
import beats a lazy import buried in a function.
"""

from __future__ import annotations

import re

from .commands import is_command_line

CAPTURE_SKELETON = """\
# Capture

_Type or paste anything here — brain dumps, meeting scraps, forwarded
messages. On its next pass the harness files it as a record: tasks
extracted and formatted, your original text kept verbatim beneath. A
plain task list files itself once verified; anything with a rewrite
asks on the dashboard first (unless its policy in Spec.md is full)._
"""


# ---- transcript normalization ----
#
# Teams (and any VTT/SRT) transcripts are machine-shaped: cue timings,
# <v Speaker> voice tags, per-turn "Name   2:34" headers. Dumped raw,
# the worker reads — and the record quotes — hundreds of lines of
# timestamp scaffolding, and the fidelity verifier tries to trace tasks
# to fragments of cue text. Normalized, a transcript is one
# "Speaker: everything they said in that turn" line per turn — the
# shape the extraction prompt, the verbatim callout, and the
# sequence-matcher all want.
#
# It lives HERE, inside capture_text, so every consumer (the drafter,
# verify_record's quoted set, is_stale's hash) sees the same text by
# construction. Timestamps and cue ids are dropped; WORDS never are —
# and the raw paste survives in Capture.md's git history (and in Teams
# itself), so phase 0's guarantee holds where it matters. Detection is
# deliberately conservative: unless the text is clearly
# transcript-shaped, it passes through byte-identical, and a shape this
# doesn't recognize degrades to today's behavior (raw text in, raw
# text quoted), never to loss.

# VTT/SRT cue timing: "00:00:03.120 --> 00:00:06.500" (hours optional,
# SRT uses a comma).
_TIMING_RE = re.compile(
    r"^\s*(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{1,3}\s+-->\s+(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{1,3}")
_VOICE_RE = re.compile(r"^<v\s+([^>]+)>\s*(.*?)\s*(?:</v>)?\s*$")
# Teams turn headers: "Rivera, Sam   2:34" (two+ spaces before the
# clock, clock ends the line — a prose "call the vet at 2:30" has one
# space and never matches) and the meeting-chat copy shape
# "[10:03 AM] Rivera, Sam".
_TURN_HEADER_RE = re.compile(r"^(?P<name>\S.{0,80}?)\s{2,}\d{1,2}:\d{2}(?::\d{2})?\s*$")
_CHAT_HEADER_RE = re.compile(
    r"^\[\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AP]M)?\]\s+(?P<name>\S.*)$", re.I)
_VTT_NOISE = ("WEBVTT", "NOTE", "STYLE", "REGION")


def _speaker_of(line: str) -> tuple[str | None, str | None]:
    """(speaker, inline words) when the line opens a turn, else (None,
    None). Only voice tags carry words on the header line itself."""
    m = _VOICE_RE.match(line)
    if m:
        return m.group(1).strip(), m.group(2)
    m = _TURN_HEADER_RE.match(line) or _CHAT_HEADER_RE.match(line)
    if m:
        return m.group("name").strip(), None
    return None, None


def normalize_transcript(text: str) -> str:
    """Transcript-shaped text as one "Speaker: turn" line per turn,
    consecutive same-speaker turns merged; anything else unchanged.

    Lines outside any turn (the user's own framing above the paste, a
    note typed after it) pass through verbatim — a blank line ends a
    turn, so trailing text is never attributed to the last speaker."""
    lines = text.splitlines()
    timings = sum(1 for ln in lines if _TIMING_RE.match(ln))
    headers = sum(1 for ln in lines if _speaker_of(ln)[0] is not None)
    if timings < 2 and headers < 3:
        return text  # not transcript-shaped: never touched

    turns: list[list] = []  # [speaker | None, [parts]] — None never merges

    def say(name: str, part: str) -> None:
        part = part.strip()
        if not part:
            return
        if turns and turns[-1][0] == name:
            turns[-1][1].append(part)
        else:
            turns.append([name, [part]])

    speaker: str | None = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s:
            speaker = None  # a blank ends the turn (Teams and VTT both)
            continue
        if (_TIMING_RE.match(ln) or s.isdigit() or s.split()[0] in _VTT_NOISE
                or (i + 1 < len(lines) and _TIMING_RE.match(lines[i + 1]))):
            continue  # cue scaffolding: timings, SRT counters, cue ids
        name, said = _speaker_of(ln)
        if name is not None:
            speaker = name
            if said:  # <v Name>words</v> carries words on the header line
                say(speaker, said)
            continue
        if speaker is not None:
            say(speaker, s)  # continuation of the current turn
        else:
            turns.append([None, [ln]])  # the user's own text, verbatim
    return "\n".join(
        parts[0] if name is None else f"{name}: {' '.join(parts)}"
        for name, parts in turns)


def capture_text(raw: str) -> str:
    """What the user typed: the note minus the skeleton's own lines.
    Known //command lines are never capture
    text — a command typed here by habit must not be extracted into a
    junk task (commands execute in knowledge notes; surfaces are
    excluded from the command scan). A transcript-shaped dump (Teams
    VTT, copied turn headers) is normalized to speaker-turn lines
    first — see normalize_transcript."""
    raw = normalize_transcript(raw)
    skeleton = {ln for ln in CAPTURE_SKELETON.splitlines() if ln.strip()}
    return "\n".join(
        ln for ln in raw.splitlines()
        if ln.strip() and ln not in skeleton and not is_command_line(ln)
    ).strip()
