"""Transcript-shaped dumps (Teams VTT, copied turn headers): machine
scaffolding is stripped at the capture text contract, so the worker,
verify_record's quoted set, and the staleness hash all see one
"Speaker: turn" line per turn — never cue timings. Anything not clearly
transcript-shaped passes through byte-identical."""

from tezcat.capture import CAPTURE_SKELETON, capture_text, normalize_transcript

VTT = """WEBVTT

de60b352-ba63-4bd3/19-0
00:00:03.120 --> 00:00:06.500
<v Rivera, Sam>yeah so we need the firewall logs</v>

de60b352-ba63-4bd3/20-0
00:00:06.900 --> 00:00:09.100
<v Rivera, Sam>before friday honestly</v>

00:00:09.500 --> 00:00:12.000
<v Nadia Kim>I'll send them over tomorrow</v>
"""

TEAMS = """Rivera, Sam   2:34
yeah so we need the firewall logs

Rivera, Sam   2:41
before friday honestly

Nadia Kim   2:47
I'll send them over tomorrow
"""


def test_vtt_becomes_speaker_turns_and_merges_consecutive():
    assert normalize_transcript(VTT).splitlines() == [
        "Rivera, Sam: yeah so we need the firewall logs before friday honestly",
        "Nadia Kim: I'll send them over tomorrow",
    ]


def test_teams_turn_headers_normalize_the_same_way():
    assert normalize_transcript(TEAMS) == normalize_transcript(VTT)


def test_meeting_chat_copy_shape():
    text = ("[10:03 AM] Rivera, Sam\nneed the logs\n\n"
            "[10:04 AM] Rivera, Sam\nbefore friday\n\n"
            "[10:05 AM] Nadia Kim\nsending tomorrow\n")
    assert normalize_transcript(text).splitlines() == [
        "Rivera, Sam: need the logs before friday",
        "Nadia Kim: sending tomorrow",
    ]


def test_plain_dumps_pass_through_byte_identical():
    """The detection gate: an ordinary dump — even one mentioning a
    clock time — is never touched."""
    dump = "buy milk\ncall the vet at 2:30\n\n- [ ] renew domain"
    assert normalize_transcript(dump) == dump


def test_framing_and_trailing_notes_stay_the_users_own_lines():
    """A blank line ends a turn, so text the user typed around the paste
    is never attributed to the last speaker."""
    text = "notes from the ops call:\n\n" + TEAMS + "\nremember: ask about the window\n"
    out = normalize_transcript(text).splitlines()
    assert out[0] == "notes from the ops call:"
    assert out[-1] == "remember: ask about the window"


def test_capture_text_normalizes_and_still_strips_skeleton_and_commands():
    raw = CAPTURE_SKELETON + "\n//azure alert correlation\n" + VTT
    out = capture_text(raw)
    assert "-->" not in out and "WEBVTT" not in out
    assert "//azure" not in out          # commands are never capture text
    assert "# Capture" not in out        # the skeleton stays the surface's
    assert "Nadia Kim: I'll send them over tomorrow" in out
