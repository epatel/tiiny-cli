from pathlib import Path

import pytest

from tai.agent import skills as S
from tai.agent.tools import ToolContext, ToolError


def make_skill(base: Path, folder: str, frontmatter: str, body: str = "Do the thing.\n") -> Path:
    d = base / folder
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}")
    return d


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    root = tmp_path / "proj"
    root.mkdir()
    return root, home


def test_frontmatter_variants():
    fields, body = S.parse_frontmatter('---\nname: "pdf"\ndescription: >\n  Fill forms\n  and merge.\n---\nBody\n')
    assert fields == {"name": "pdf", "description": "Fill forms and merge."} and body == "Body\n"
    assert S.parse_frontmatter("no frontmatter") == ({}, "no frontmatter")
    assert S.parse_frontmatter("---\nname: x\nunterminated") == ({}, "---\nname: x\nunterminated")


def test_discover_order_project_first_and_folder_name_fallback(dirs):
    root, home = dirs
    make_skill(root / ".claude" / "skills", "release", "name: release\ndescription: project version")
    make_skill(home / ".claude" / "skills", "release", "name: release\ndescription: user version")
    make_skill(home / ".config" / "tai" / "skills", "notes", "description: no name field")
    (home / ".claude" / "skills" / "synced").mkdir()  # Claude's synced skills live deeper: ignored
    found = S.discover(root)
    assert [(s.name, s.description) for s in found] == [("notes", "no name field"), ("release", "project version")]


def test_skill_tool_returns_body_and_folder(dirs):
    root, _ = dirs
    folder = make_skill(root / ".claude" / "skills", "release", "name: release\ndescription: Cut a release",
                        body="1. Bump version\n2. Tag\n")
    tool = S.skill_tool(S.discover(root))
    assert tool.spec()["function"]["parameters"]["properties"]["name"]["enum"] == ["release"]
    out = tool.run({"name": "release"}, ToolContext(root))
    assert out.startswith("# Skill: release") and f"Skill folder: {folder}" in out and "2. Tag" in out
    assert "description:" not in out
    with pytest.raises(ToolError, match="Unknown skill 'nope'"):
        tool.run({"name": "nope"}, ToolContext(root))


def test_prompt_section_lists_names_only(dirs):
    root, _ = dirs
    make_skill(root / ".claude" / "skills", "a", "name: a\ndescription: " + "x" * 1000, body="SECRET BODY")
    section = S.prompt_section(S.discover(root))
    assert "- a: " in section and "SECRET BODY" not in section and len(section) < 1000
    assert S.prompt_section([]) == ""
