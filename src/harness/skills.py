"""Skills: the workflow layer, following the Agent Skills open standard.

A skill is a folder under skills/ with a SKILL.md (YAML frontmatter
name + description, markdown body of instructions). Progressive
disclosure keeps context cheap: only name + description ride in the
system prompt; the body loads when invoked — by the model via the
use_skill tool, or by the user via a /slash command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from obsidian_mcp.markdown import split_frontmatter

SKILLS_DIR = Path(__file__).resolve().parents[2] / "skills"
_SLASH_RE = re.compile(r"^/([\w-]+)\s*(.*)\Z", re.S)


@dataclass
class Skill:
    name: str
    description: str
    body: str


def load_skills(root: Path | None = None) -> list[Skill]:
    root = root or SKILLS_DIR
    if not root.is_dir():
        return []
    skills = []
    for f in sorted(root.glob("*/SKILL.md")):
        fm, body = split_frontmatter(f.read_text(encoding="utf-8"))
        if not fm:
            continue
        skills.append(
            Skill(
                name=str(fm.get("name") or f.parent.name),
                description=str(fm.get("description") or "").strip(),
                body=body.strip(),
            )
        )
    return skills


def skills_prompt_block(skills: list[Skill]) -> str:
    """Level 1: one line per skill for the system prompt."""
    if not skills:
        return ""
    lines = "\n".join(f"- {s.name}: {s.description}" for s in skills)
    return (
        "\n\nWorkflow skills are available. When a request matches one, call the "
        f"use_skill tool first and follow the instructions it returns:\n{lines}"
    )


def render_skill(skill: Skill, user_input: str = "") -> str:
    """Level 2: the full skill body, framed for the model to follow now."""
    text = f'Follow this workflow now.\n\n<workflow name="{skill.name}">\n{skill.body}\n</workflow>'
    if user_input:
        text += f"\n\nUser input for this workflow: {user_input}"
    return text


def expand_slash(message: str, skills: list[Skill]) -> str | None:
    """`/skill-name rest...` -> rendered skill + input, or None if no match."""
    m = _SLASH_RE.match(message.strip())
    if not m:
        return None
    skill = next((s for s in skills if s.name == m.group(1)), None)
    if skill is None:
        return None
    return render_skill(skill, m.group(2).strip())
