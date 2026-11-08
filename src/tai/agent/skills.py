"""Agent Skills: folders with a SKILL.md (YAML frontmatter `name`, `description` + instructions).

Shared with Claude Code: project `.claude/skills/`, then `~/.claude/skills/`, then
`~/.config/tai/skills/`; the first skill with a given name wins. Only names and
descriptions go into the system prompt; the model loads a skill's full
instructions on demand with the `skill` tool (progressive disclosure).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tai.agent.tools import Tool, ToolContext, ToolError

MAX_DESCRIPTION = 400  # characters of a description shown in the system prompt


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path  # the SKILL.md

    @property
    def folder(self) -> Path:
        return self.path.parent


def skill_dirs(root: Path) -> list[Path]:
    home = Path.home()
    return [root / ".claude" / "skills", home / ".claude" / "skills", home / ".config" / "tai" / "skills"]


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Minimal YAML frontmatter: `key: value` lines, quoted values, `>`/`|` blocks and
    indented continuation lines. Returns (fields, body)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    fields: dict[str, str] = {}
    key = None
    for line in text[3:end].splitlines():
        if line[:1] in (" ", "\t") and key:
            fields[key] = (fields[key] + " " + line.strip()).strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            fields[key] = "" if value in (">", "|", ">-", "|-") else value.strip("'\"")
    return fields, text[end + 4:].lstrip("\n")


def _load(skill_md: Path) -> Skill | None:
    try:
        fields, _ = parse_frontmatter(skill_md.read_text(errors="replace"))
    except OSError:
        return None
    name = fields.get("name") or skill_md.parent.name
    return Skill(name, fields.get("description", ""), skill_md)


def discover(root: Path) -> list[Skill]:
    found: dict[str, Skill] = {}
    for base in skill_dirs(root):
        if not base.is_dir():
            continue
        for skill_md in sorted(base.glob("*/SKILL.md")):
            skill = _load(skill_md)
            if skill and skill.name not in found:
                found[skill.name] = skill
    return sorted(found.values(), key=lambda s: s.name)


def prompt_section(skills: list[Skill]) -> str:
    if not skills:
        return ""
    lines = [f"- {s.name}: {s.description[:MAX_DESCRIPTION]}" for s in skills]
    return ("\n# Skills\n\nSkills are packaged instructions for specific tasks. When a task matches a skill's "
            "description, call the `skill` tool with its name first and follow what it returns. Files it "
            "mentions are relative to the skill's folder.\n\n" + "\n".join(lines) + "\n")


def skill_tool(skills: list[Skill]) -> Tool:
    by_name = {s.name: s for s in skills}

    def run(args: dict, ctx: ToolContext) -> str:
        skill = by_name.get(args["name"])
        if skill is None:
            raise ToolError(f"Unknown skill '{args['name']}'. Available: {', '.join(by_name)}")
        _, body = parse_frontmatter(skill.path.read_text(errors="replace"))
        return f"# Skill: {skill.name}\nSkill folder: {skill.folder}\n\n{body}"

    return Tool("skill", "Load the full instructions of a skill listed under Skills in the system prompt.",
                {"name": {"type": "string", "enum": sorted(by_name)}}, run, required=["name"])
