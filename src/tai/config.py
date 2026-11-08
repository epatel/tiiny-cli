"""Where the device is and how to authenticate.

Every TAI_*/TIINY_* setting comes from the environment, then ./.env, then
~/.config/tai/.env (``Env``). Two ways to reach the device:

* **Client mode** — ``--url`` or TAI_URL points at a ``tai serve`` (e.g.
  ``https://host/tai/``), authenticated with TAI_API_KEY. Device settings are
  then not needed.
* **Direct** (also what ``tai serve`` uses) — TAI_DEVICE; token from TAI_TOKEN,
  then TIINY_API_KEY; else the official tiiny CLI config
  (~/.local/tiiny/config.json, or TIINY_CONFIG). The tiiny config's
  ``account.authKey`` doubles as the Bearer token for the device's HTTP API.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIINY_CONFIG = Path.home() / ".local" / "tiiny" / "config.json"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    device: str  # host or host:port (direct), or a tai serve URL with scheme and optional path prefix
    token: str
    via_server: bool = False  # client mode: `device` is a `tai serve` URL and `token` its API key

    @property
    def base_url(self) -> str:
        return self.device if "://" in self.device else f"http://{self.device}"

    @property
    def where(self) -> str:
        """For status lines: how this run reaches the device."""
        return f"via {self.device}" if self.via_server else f"direct {self.device}"


def data_dir() -> Path:
    """Directory for tai's own state (the sqlite database)."""
    d = Path(os.environ.get("TAI_HOME") or Path.home() / ".local" / "share" / "tai")
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_dotenv(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE parser (comments, `export`, quotes). Empty values are skipped."""
    env: dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.removeprefix("export ").partition("=")
        val = val.strip().strip("'\"")
        if val:
            env[key.strip()] = val
    return env


def user_dotenv() -> Path:
    """User-wide settings, read after the current folder's .env."""
    return Path.home() / ".config" / "tai" / ".env"


class Env:
    """Settings from the real environment, then ./.env, then ~/.config/tai/.env;
    empty values count as unset."""

    def __init__(self) -> None:
        self.dotenv = {**read_dotenv(user_dotenv()), **read_dotenv(Path.cwd() / ".env")}

    def first(self, *names: str) -> str | None:
        """The first of ``names`` that is set."""
        for name in names:
            value = os.environ.get(name) or self.dotenv.get(name)
            if value:
                return value
        return None


def load_settings(device: str | None = None, url: str | None = None, direct: bool = False) -> Settings:
    """Client mode when a server URL is given (``url``, else TAI_URL) unless ``direct``;
    otherwise the device itself."""
    env = Env()
    url = None if direct else (url or env.first("TAI_URL"))
    if url:
        if "://" not in url:
            raise ConfigError(f"TAI_URL must include the scheme, e.g. https://{url}")
        return Settings(device=url.rstrip("/"), token=api_key(env), via_server=True)
    return _device_settings(device, env)


def api_key(env: Env | None = None) -> str:
    """The key a client sends to `tai serve`, and the one `tai serve` requires."""
    key = (env or Env()).first("TAI_API_KEY")
    if not key:
        raise ConfigError("No TAI_API_KEY. Make one with `tai serve --new-key` and set it on the server and the client.")
    return key


def _tiiny_config() -> dict:
    """The official tiiny CLI's config (device address, account.authKey), or {}."""
    path = Path(os.environ.get("TIINY_CONFIG") or DEFAULT_TIINY_CONFIG)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"Cannot read {path}: {e}") from e


def _device_settings(device: str | None, env: Env) -> Settings:
    cfg = _tiiny_config()
    device = device or env.first("TAI_DEVICE") or cfg.get("deviceAddress")
    token = env.first("TAI_TOKEN", "TIINY_API_KEY") or (cfg.get("account") or {}).get("authKey")
    if not device:
        raise ConfigError("No device address. Run `tiiny scan` + `tiiny connect <ip>`, or set TAI_DEVICE.")
    if not token:
        raise ConfigError("No auth token. Run `tiiny login`, or set TAI_TOKEN.")
    return Settings(device=device, token=token)
