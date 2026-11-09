"""Click option bundles: related CLI options grouped into one typed parameter.

``@bundle("opts", ChatOptions, "model", "system", "think")`` pops those keyword
arguments from the click call and passes ``opts=ChatOptions(...)`` instead.
Apply it below the options it collects (closest to the function).
"""

from __future__ import annotations

# CLI boundary: click hands over plain strings, which this module turns into typed options.
# aihealth: disable=primitive_obsession

import functools
import json
from dataclasses import dataclass, field
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


# ---------------------------------------------------------------- -x KEY=VALUE
def _value(raw: str):
    """JSON when it parses (0.2, true, [1,2], {"a":1}), else the text as is."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def _set_path(target: dict, key: str, value) -> None:
    *parents, last = key.split(".")
    for name in parents:
        target = target.setdefault(name, {})
        if not isinstance(target, dict):
            raise click.BadParameter(f"{key}: {name} is already set to a value, not an object")
    target[last] = value


def parse_extras(ctx, param, pairs: tuple[str, ...]) -> dict:
    """``("language=sv", "a.b=1")`` -> ``{"language": "sv", "a": {"b": 1}}``."""
    extra: dict = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise click.BadParameter(f"{pair!r}: use KEY=VALUE, e.g. language=sv")
        _set_path(extra, key, _value(raw))
    return extra


def merge(base: dict, extra: dict) -> dict:
    """``extra`` over ``base``, merging nested objects instead of replacing them."""
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            value = merge(merged[key], value)
        merged[key] = value
    return merged


def extra_option(where: str):
    """-x/--extra KEY=VALUE (repeatable) as ``extra: dict``; ``where`` says where it is sent."""
    return click.option(
        "-x", "--extra", "extra", multiple=True, callback=parse_extras, metavar="KEY=VALUE",
        help=f"Extra field sent {where}, for options tai has no flag for (see `tai info`). "
             "Repeatable; a.b=1 nests; values are JSON when they parse (0.2, true), else text.")


@dataclass(frozen=True)
class ChatOptions:
    """Shared by `ask` and `chat`."""
    model: str | None = None
    system: str | None = None
    think: bool | None = None
    extra: dict = field(default_factory=dict)  # -x fields for the chat request body


def chat_options(f):
    """Add -m/--model, -s/--system, --think/--no-think and -x as ``opts: ChatOptions``."""
    f = bundle("opts", ChatOptions, "model", "system", "think", "extra")(f)
    f = extra_option("in the chat request (e.g. temperature=0.2)")(f)
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
    """Which chat session to use: resume by id/name, continue the latest, or a new named one.
    ``no_save``: a new session kept only in memory, never written to the history."""
    resume: str | None = None
    cont: bool = False
    name: str | None = None
    no_save: bool = False

    @property
    def resuming(self) -> bool:
        return bool(self.resume or self.cont)


@dataclass(frozen=True)
class AgentRequest:
    """`tai agent` options."""
    task: tuple[str, ...] = ()
    model: str | None = None
    resume: str | None = None
    cont: bool = False
    no_mcp: bool = False
    no_save: bool = False

    @property
    def text(self) -> str | None:
        return " ".join(self.task) or None

    @property
    def session(self) -> SessionRef:
        return SessionRef(resume=self.resume, cont=self.cont, no_save=self.no_save)


@dataclass(frozen=True)
class ImageOptions:
    """`tai image` generation settings. The device doesn't report the seed it used,
    so a missing one is picked here to keep every result reproducible."""
    size: str = "1024x1024"
    seed: int | None = None
    steps: int | None = None
    guidance: float | None = None
    negative: str | None = None
    extra: dict = field(default_factory=dict)  # -x fields, over the ones above

    def request(self, seed: int) -> dict:
        fields = {"size": self.size, "seed": seed, "steps": self.steps,
                  "guidance_scale": self.guidance, "negative_prompt": self.negative}
        return merge({k: v for k, v in fields.items() if v is not None}, self.extra)


@dataclass(frozen=True)
class TtsConfig:
    """`tai tts` voice settings, sent as the request's ``config``."""
    instruct: str | None = None
    extra: dict = field(default_factory=dict)  # -x fields, e.g. language

    def config(self, default_instruct: str | None = None) -> dict | None:
        instruct = self.instruct or default_instruct
        return merge({"instruct": instruct} if instruct else {}, self.extra) or None


@dataclass(frozen=True)
class AsrOptions:
    """`tai asr` form fields besides model and file."""
    language: str | None = None
    extra: dict = field(default_factory=dict)

    def fields(self) -> dict[str, str]:
        given = merge({"language": self.language} if self.language else {}, self.extra)
        return {k: v if isinstance(v, str) else json.dumps(v) for k, v in given.items()}
