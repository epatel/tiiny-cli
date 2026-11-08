"""Agent tools: read, write, edit, bash, grep, glob, ls.

Each tool has an OpenAI function schema, a ``run`` that returns text for the
model (errors raise ToolError and are reported back to the model, not the user),
and, for tools that change things, a ``preview`` the permission prompt shows.
``grant`` names the permission group an "always allow" answer covers.
"""

from __future__ import annotations

import difflib
import fnmatch
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

MAX_OUTPUT = 30_000  # characters returned to the model per tool call
MAX_LINES = 2000  # default lines per read
MAX_HITS = 200  # grep / glob results
IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
                ".pytest_cache", ".tox", "dist", "build", ".aihealth"}


class ToolError(Exception):
    pass


@dataclass
class ToolContext:
    root: Path
    bash_timeout: float = 120.0

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        return (p if p.is_absolute() else self.root / p).resolve()

    def inside(self, p: Path) -> bool:
        return p == self.root or self.root in p.parents

    def show(self, p: Path) -> str:
        """Path as the user should see it: relative inside the project."""
        return str(p.relative_to(self.root)) if self.inside(p) else str(p)


@dataclass
class Preview:
    title: str
    body: str
    kind: str  # "diff" | "command"
    outside: bool = False  # touches files outside the project: never covered by "always"


@dataclass
class Tool:
    name: str
    description: str
    params: dict
    run: Callable[[dict, ToolContext], str]
    preview: Callable[[dict, ToolContext], Preview] | None = None
    grant: str | None = None  # permission group; None = never asks
    required: list[str] = field(default_factory=list)
    schema: dict | None = None  # full JSON schema (MCP tools); overrides params/required
    display: str | None = None  # UI label instead of the name, e.g. "aihealth · score_file"

    def spec(self) -> dict:
        schema = self.schema or {"type": "object", "properties": self.params, "required": self.required}
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": schema}}

    def summary(self, args: dict) -> str:
        """Short one-line label for the UI, e.g. `edit src/x.py`."""
        key = next((k for k in ("path", "command", "pattern", "name", "prompt", "question", "title")
                    if args.get(k)), None)
        value = str(args[key]).splitlines()[0] if key else ""
        return f"{self.display or self.name} {value[:100]}".strip()


def truncate(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n… [{len(text) - limit} characters omitted] …\n\n{text[-half:]}"


# ------------------------------------------------------------------ read
def _read_text(p: Path) -> str:
    if not p.exists():
        raise ToolError(f"File not found: {p}")
    if p.is_dir():
        raise ToolError(f"{p} is a directory; use ls")
    data = p.read_bytes()
    if b"\0" in data[:8192]:
        raise ToolError(f"{p} looks like a binary file")
    return data.decode("utf-8", errors="replace")


def run_read(args: dict, ctx: ToolContext) -> str:
    lines = _read_text(ctx.resolve(args["path"])).splitlines()
    start = max(int(args.get("offset") or 1), 1)
    limit = int(args.get("limit") or MAX_LINES)
    chunk = lines[start - 1:start - 1 + limit]
    body = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(chunk, start))
    rest = len(lines) - (start - 1 + len(chunk))
    more = f"\n… {rest} more lines (use offset={start + len(chunk)})" if rest > 0 else ""
    return truncate(body + more) if chunk else f"(empty: file has {len(lines)} lines)"


# ------------------------------------------------------------------ write / edit
def _diff(path: str, old: str, new: str) -> str:
    lines = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                 f"a/{path}", f"b/{path}")
    return "".join(lines) or "(no changes)"


def _write_target(args: dict, ctx: ToolContext) -> tuple[Path, str]:
    p = ctx.resolve(args["path"])
    old = p.read_text(errors="replace") if p.is_file() else ""
    return p, old


def preview_write(args: dict, ctx: ToolContext) -> Preview:
    p, old = _write_target(args, ctx)
    verb = "Overwrite" if p.exists() else "Create"
    return Preview(f"{verb} {ctx.show(p)}", _diff(ctx.show(p), old, args["content"]), "diff",
                   outside=not ctx.inside(p))


def run_write(args: dict, ctx: ToolContext) -> str:
    p = ctx.resolve(args["path"])
    existed = p.exists()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(args["content"])
    n = args["content"].count("\n") + (0 if args["content"].endswith("\n") else 1)
    return f"{'Overwrote' if existed else 'Created'} {ctx.show(p)} ({n} lines)"


def _apply_edit(args: dict, old: str) -> str:
    find, repl = args["old_string"], args["new_string"]
    if find == repl:
        raise ToolError("old_string and new_string are identical")
    count = old.count(find)
    if count == 0:
        raise ToolError("old_string not found; read the file and copy the text exactly, including whitespace")
    if count > 1 and not args.get("replace_all"):
        raise ToolError(f"old_string occurs {count} times; add surrounding lines to make it unique, "
                        "or set replace_all")
    return old.replace(find, repl) if args.get("replace_all") else old.replace(find, repl, 1)


def preview_edit(args: dict, ctx: ToolContext) -> Preview:
    p = ctx.resolve(args["path"])
    old = _read_text(p)
    return Preview(f"Edit {ctx.show(p)}", _diff(ctx.show(p), old, _apply_edit(args, old)), "diff",
                   outside=not ctx.inside(p))


def run_edit(args: dict, ctx: ToolContext) -> str:
    p = ctx.resolve(args["path"])
    old = _read_text(p)
    new = _apply_edit(args, old)
    p.write_text(new)
    return f"Edited {ctx.show(p)}\n{truncate(_diff(ctx.show(p), old, new), 4000)}"


# ------------------------------------------------------------------ bash
def preview_bash(args: dict, ctx: ToolContext) -> Preview:
    return Preview("Run command", args["command"], "command")


def run_bash(args: dict, ctx: ToolContext) -> str:
    timeout = float(args.get("timeout") or ctx.bash_timeout)
    try:
        r = subprocess.run(["/bin/bash", "-c", args["command"]], cwd=ctx.root, stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as e:
        partial = (e.stdout or "") + (e.stderr or "") if isinstance(e.stdout, str) else ""
        raise ToolError(f"Command timed out after {timeout:.0f}s\n{truncate(partial, 4000)}") from e
    output = (r.stdout + ("\n" if r.stdout and r.stderr else "") + r.stderr).strip()
    return truncate(f"exit code {r.returncode}\n{output}" if output else f"exit code {r.returncode}")


# ------------------------------------------------------------------ search
def _walk(base: Path):
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in filenames:
            yield Path(dirpath) / name


@dataclass(frozen=True)
class GrepQuery:
    pattern: str
    base: Path
    glob: str | None = None
    ignore_case: bool = False


def _grep_file(rx: re.Pattern, f: Path, ctx: ToolContext) -> list[str]:
    try:
        text = f.read_text(errors="strict")
    except (UnicodeDecodeError, OSError):
        return []
    return [f"{ctx.show(f)}:{i}:{line}" for i, line in enumerate(text.splitlines(), 1) if rx.search(line)]


def _grep_python(q: GrepQuery, ctx: ToolContext) -> list[str]:
    rx = re.compile(q.pattern, re.IGNORECASE if q.ignore_case else 0)
    files = [q.base] if q.base.is_file() else _walk(q.base)
    hits: list[str] = []
    for f in files:
        if not q.glob or fnmatch.fnmatch(f.name, q.glob):
            hits += _grep_file(rx, f, ctx)
        if len(hits) >= MAX_HITS:
            break
    return hits


def _grep_rg(q: GrepQuery, ctx: ToolContext) -> list[str]:
    cmd = ["rg", "--line-number", "--no-heading", "--color", "never", "--max-columns", "300"]
    cmd += ["-i"] if q.ignore_case else []
    cmd += ["--glob", q.glob] if q.glob else []
    cmd += ["--", q.pattern]
    if q.base != ctx.root:  # rg prints "./x" when given "."; omit the root for clean paths
        cmd.append(ctx.show(q.base))
    r = subprocess.run(cmd, cwd=ctx.root, capture_output=True, text=True, errors="replace", timeout=60)
    if r.returncode > 1:
        raise ToolError(r.stderr.strip() or "grep failed")
    return r.stdout.splitlines()


def run_grep(args: dict, ctx: ToolContext) -> str:
    q = GrepQuery(args["pattern"], ctx.resolve(args.get("path") or "."), args.get("glob"),
                  bool(args.get("ignore_case")))
    finder = _grep_rg if shutil.which("rg") else _grep_python
    try:
        hits = finder(q, ctx)
    except re.error as e:
        raise ToolError(f"Invalid regex: {e}") from e
    if not hits:
        return "No matches"
    more = f"\n… {len(hits) - MAX_HITS} more matches; narrow the search" if len(hits) > MAX_HITS else ""
    return truncate("\n".join(hits[:MAX_HITS]) + more)


def run_glob(args: dict, ctx: ToolContext) -> str:
    base = ctx.resolve(args.get("path") or ".")
    pattern = args["pattern"]
    found = [f for f in _walk(base) if fnmatch.fnmatch(str(f.relative_to(base)), pattern)
             or fnmatch.fnmatch(f.name, pattern)]
    found.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    if not found:
        return "No files found"
    more = f"\n… {len(found) - MAX_HITS} more" if len(found) > MAX_HITS else ""
    return "\n".join(ctx.show(f) for f in found[:MAX_HITS]) + more


def run_ls(args: dict, ctx: ToolContext) -> str:
    p = ctx.resolve(args.get("path") or ".")
    if not p.is_dir():
        raise ToolError(f"Not a directory: {p}")
    entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower()))
    lines = [e.name + ("/" if e.is_dir() else "") for e in entries if e.name != ".git"]
    return "\n".join(lines[:500]) or "(empty directory)"


# ------------------------------------------------------------------ registry
_PATH = {"type": "string", "description": "File path, relative to the project root or absolute"}

TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("read", "Read a text file. Returns numbered lines. Use offset/limit for large files.",
         {"path": _PATH, "offset": {"type": "integer", "description": "First line (1-based)"},
          "limit": {"type": "integer", "description": f"Max lines (default {MAX_LINES})"}},
         run_read, required=["path"]),
    Tool("write", "Create or overwrite a file with the given content. Prefer edit for changes to existing files.",
         {"path": _PATH, "content": {"type": "string"}},
         run_write, preview_write, grant="files", required=["path", "content"]),
    Tool("edit", "Replace an exact string in a file. old_string must match exactly (including indentation) "
                 "and be unique unless replace_all is true. Read the file first.",
         {"path": _PATH, "old_string": {"type": "string"}, "new_string": {"type": "string"},
          "replace_all": {"type": "boolean", "description": "Replace every occurrence"}},
         run_edit, preview_edit, grant="files", required=["path", "old_string", "new_string"]),
    Tool("bash", "Run a shell command in the project root (non-interactive, no stdin). "
                 "Returns exit code and combined output.",
         {"command": {"type": "string"},
          "timeout": {"type": "number", "description": "Seconds (default 120)"}},
         run_bash, preview_bash, grant="bash", required=["command"]),
    Tool("grep", "Search file contents with a regular expression. Returns path:line:text.",
         {"pattern": {"type": "string"}, "path": {"type": "string", "description": "File or directory (default .)"},
          "glob": {"type": "string", "description": "Only files matching, e.g. *.py"},
          "ignore_case": {"type": "boolean"}},
         run_grep, required=["pattern"]),
    Tool("glob", "Find files by name pattern, e.g. **/*.py or test_*.py. Newest first.",
         {"pattern": {"type": "string"}, "path": {"type": "string", "description": "Directory (default .)"}},
         run_glob, required=["pattern"]),
    Tool("ls", "List a directory (directories end with /).",
         {"path": {"type": "string", "description": "Directory (default .)"}},
         run_ls),
]}
