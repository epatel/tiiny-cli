"""`tai --version`: the build part comes from git, only for tai's own checkout."""

import subprocess

from click.testing import CliRunner

import tai.build as build_mod
import tai.cli as cli_mod


def fake_git(answers):
    def git(*args):
        value = answers[args[0]]
        if isinstance(value, Exception):
            raise value
        return value
    return git


def test_build_from_git(monkeypatch):
    answers = {"rev-parse": "", "rev-list": "47", "status": ""}
    monkeypatch.setattr(build_mod, "_git", lambda *a: str(build_mod.REPO) if a[1] == "--show-toplevel"
                        else ("9138ee6" if a[0] == "rev-parse" else answers[a[0]]))
    assert build_mod.build() == "build 47 (9138ee6)"
    answers["status"] = " M src/tai/cli.py"
    assert build_mod.build() == "build 47 (9138ee6, + changes)"


def test_no_build_outside_the_checkout(monkeypatch):
    monkeypatch.setattr(build_mod, "_git", lambda *a: "/somewhere/else")
    assert build_mod.build() is None
    monkeypatch.setattr(build_mod, "_git", fake_git({"rev-parse": subprocess.CalledProcessError(128, "git")}))
    assert build_mod.build() is None
    monkeypatch.setattr(build_mod, "_git", fake_git({"rev-parse": FileNotFoundError("git")}))  # no git at all
    assert build_mod.build() is None


def test_version_option(monkeypatch):
    monkeypatch.setattr(build_mod, "build", lambda: "build 47 (9138ee6)")
    r = CliRunner().invoke(cli_mod.cli, ["--version"])
    assert r.exit_code == 0 and r.output == f"tai, version {cli_mod.__version__}, build 47 (9138ee6)\n"
    monkeypatch.setattr(build_mod, "build", lambda: None)
    assert CliRunner().invoke(cli_mod.cli, ["--version"]).output == f"tai, version {cli_mod.__version__}\n"
