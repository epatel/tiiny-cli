"""Click option bundles: related CLI options grouped into one typed parameter.

``@bundle("opts", ChatOptions, "model", "system", "think")`` pops those keyword
arguments from the click call and passes ``opts=ChatOptions(...)`` instead.
Apply it below the options it collects (closest to the function).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from pathlib import Path

import click


def bundle(param: str, cls: type, *fields: str):
    def decorate(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            kwargs[param] = cls(**{name: kwargs.pop(name) for name in fields})
            return f(*args, **kwargs)
        return wrapper
    return decorate


@dataclass(frozen=True)
class ChatOptions:
    """Shared by `ask` and `chat`."""
    model: str | None = None
    system: str | None = None
    think: bool | None = None


def chat_options(f):
    """Add -m/--model, -s/--system and --think/--no-think as ``opts: ChatOptions``."""
    f = bundle("opts", ChatOptions, "model", "system", "think")(f)
    f = click.option("--think/--no-think", default=None,
                     help="Toggle reasoning on models that support it.")(f)
    f = click.option("-s", "--system", help="System prompt.")(f)
    return click.option("-m", "--model", help="Chat model (default: a loaded one, or your default).")(f)


@dataclass(frozen=True)
class Question:
    """What `ask` sends: the prompt words plus attached files."""
    prompt: tuple[str, ...] = ()
    files: tuple[Path, ...] = ()


@dataclass(frozen=True)
class AskOutput:
    show_thinking: bool = False
    as_json: bool = False


@dataclass(frozen=True)
class SessionRef:
    """Which chat session to use: resume by id/name, continue the latest, or a new named one."""
    resume: str | None = None
    cont: bool = False
    name: str | None = None

    @property
    def resuming(self) -> bool:
        return bool(self.resume or self.cont)
