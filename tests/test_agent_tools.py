import os
import time

import pytest

from tai.agent import tools as T
from tai.agent.tools import TOOLS, ToolContext, ToolError


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n\nprint(add(1, 2))\n")
    (tmp_path / "README.md").write_text("# Demo\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: main\n")
    return ToolContext(root=tmp_path.resolve())


def run(name, ctx, **args):
    return TOOLS[name].run(args, ctx)


def test_specs_are_valid_openai_functions():
    for tool in TOOLS.values():
        spec = tool.spec()
        assert spec["type"] == "function" and spec["function"]["name"] == tool.name
        assert set(tool.required) <= set(spec["function"]["parameters"]["properties"])
    assert {t.name for t in TOOLS.values() if t.grant} == {"write", "edit", "bash"}


def test_read_numbers_lines_and_pages(ctx):
    out = run("read", ctx, path="src/app.py")
    assert out.splitlines()[0] == "     1\tdef add(a, b):"
    out = run("read", ctx, path="src/app.py", offset=2, limit=1)
    assert out.startswith("     2\t    return a - b") and "more lines (use offset=3)" in out


def test_read_errors(ctx):
    with pytest.raises(ToolError, match="not found"):
        run("read", ctx, path="nope.py")
    (ctx.root / "bin.dat").write_bytes(b"\0\1\2")
    with pytest.raises(ToolError, match="binary"):
        run("read", ctx, path="bin.dat")


def test_edit_preview_shows_diff_and_run_applies(ctx):
    args = {"path": "src/app.py", "old_string": "a - b", "new_string": "a + b"}
    pv = TOOLS["edit"].preview(args, ctx)
    assert pv.title == "Edit src/app.py" and "-    return a - b" in pv.body and "+    return a + b" in pv.body
    assert (ctx.root / "src/app.py").read_text().count("a - b") == 1  # preview changes nothing
    assert run("edit", ctx, **args).startswith("Edited src/app.py")
    assert "a + b" in (ctx.root / "src/app.py").read_text()


def test_edit_rejects_missing_and_ambiguous(ctx):
    with pytest.raises(ToolError, match="not found"):
        run("edit", ctx, path="src/app.py", old_string="zzz", new_string="y")
    with pytest.raises(ToolError, match="occurs 2 times"):
        run("edit", ctx, path="src/app.py", old_string="add", new_string="plus")
    run("edit", ctx, path="src/app.py", old_string="add", new_string="plus", replace_all=True)
    assert "add" not in (ctx.root / "src/app.py").read_text()


def test_write_creates_dirs_and_flags_outside(ctx, tmp_path_factory):
    pv = TOOLS["write"].preview({"path": "new/dir/x.txt", "content": "hi\n"}, ctx)
    assert pv.title == "Create new/dir/x.txt" and not pv.outside
    assert run("write", ctx, path="new/dir/x.txt", content="hi\n") == "Created new/dir/x.txt (1 lines)"
    other = tmp_path_factory.mktemp("elsewhere") / "y.txt"
    assert TOOLS["write"].preview({"path": str(other), "content": "x"}, ctx).outside


def test_bash_runs_in_root_with_exit_code(ctx):
    out = run("bash", ctx, command="pwd; echo err >&2; exit 3")
    assert out.splitlines()[0] == "exit code 3" and str(ctx.root) in out and "err" in out


def test_bash_timeout(ctx):
    with pytest.raises(ToolError, match="timed out after 1s"):
        run("bash", ctx, command="sleep 5", timeout=1)


@pytest.mark.parametrize("use_rg", [True, False])
def test_grep_skips_ignored_dirs(ctx, monkeypatch, use_rg):
    if not use_rg:
        monkeypatch.setattr(T.shutil, "which", lambda name: None)
    assert run("grep", ctx, pattern="return") == "src/app.py:2:    return a - b"
    assert run("grep", ctx, pattern="ref:") == "No matches"  # .git ignored
    assert run("grep", ctx, pattern="DEMO", ignore_case=True, glob="*.md") == "README.md:1:# Demo"


def test_grep_bad_regex(ctx, monkeypatch):
    monkeypatch.setattr(T.shutil, "which", lambda name: None)
    with pytest.raises(ToolError, match="Invalid regex"):
        run("grep", ctx, pattern="(")


def test_glob_newest_first_and_ls(ctx):
    newer = ctx.root / "src" / "b.py"
    newer.write_text("")
    os.utime(ctx.root / "src" / "app.py", (time.time() - 100, time.time() - 100))
    assert run("glob", ctx, pattern="*.py").splitlines() == ["src/b.py", "src/app.py"]
    assert run("ls", ctx) == "src/\nREADME.md"


def test_truncate_keeps_head_and_tail():
    out = T.truncate("a" * 50 + "b" * 50, limit=20)
    assert out.startswith("a" * 10) and out.endswith("b" * 10) and "80 characters omitted" in out


def test_summary_labels():
    assert TOOLS["bash"].summary({"command": "pytest -q\nmore"}) == "bash pytest -q"
    assert TOOLS["ls"].summary({}) == "ls"
