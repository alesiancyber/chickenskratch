"""Skills loader and slash-command expansion."""

from harness.skills import expand_slash, load_skills, skills_prompt_block


def test_load_skills():
    skills = load_skills()
    names = [s.name for s in skills]
    assert "meeting-capture" in names
    assert "weekly-review" in names
    for s in skills:
        assert s.description, f"{s.name} has no description"
        assert s.body, f"{s.name} has no body"


def test_prompt_block_is_compact():
    skills = load_skills()
    block = skills_prompt_block(skills)
    assert "meeting-capture:" in block
    # level 1 must stay cheap: descriptions only, never bodies
    assert "Organize, don't embellish" not in block


def test_expand_slash():
    skills = load_skills()
    expanded = expand_slash("/meeting-capture Meetings/2026-07-17 Standup.md", skills)
    assert expanded is not None
    assert "Organize, don't embellish" in expanded
    assert "Meetings/2026-07-17 Standup.md" in expanded

    assert expand_slash("/not-a-skill hello", skills) is None
    assert expand_slash("plain message", skills) is None


def test_unknown_slash_gets_deterministic_reply():
    from harness.skills import unknown_slash

    skills = load_skills()
    reply = unknown_slash("/reserch azure stuff", skills)
    assert reply is not None
    assert "/research" in reply  # lists real commands

    assert unknown_slash("/meeting-capture Meetings/x.md", skills) is None  # valid: no reply
    assert unknown_slash("/Users/b4ndit/Desktop/file.md", skills) is None   # path, not command
    assert unknown_slash("plain message", skills) is None

    # zero skills installed: still deterministic, never improvised by the model
    reply = unknown_slash("/anything", [])
    assert reply is not None and "/help" in reply
