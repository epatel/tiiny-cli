"""Where the device is and how to authenticate.

Precedence: environment > ./.env (TAI_DEVICE; token from TAI_TOKEN, then
TIINY_API_KEY) > the official tiiny CLI config
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
    device: str  # host or host:port, no scheme
    token: str

    @property
    def base_url(self) -> str:
        return self.device if "://" in self.device else f"http://{self.device}"


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


def _env(name: str, dotenv: dict[str, str]) -> str | None:
    """Real environment wins over .env; empty values count as unset."""
    return os.environ.get(name) or dotenv.get(name) or None


def load_settings(device: str | None = None, token: str | None = None) -> Settings:
    dotenv = read_dotenv(Path.cwd() / ".env")
    tiiny_cfg: dict = {}
    path = Path(os.environ.get("TIINY_CONFIG") or DEFAULT_TIINY_CONFIG)
    if path.exists():
        try:
            tiiny_cfg = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise ConfigError(f"Cannot read {path}: {e}") from e

    device = device or _env("TAI_DEVICE", dotenv) or tiiny_cfg.get("deviceAddress")
    token = (token or _env("TAI_TOKEN", dotenv) or _env("TIINY_API_KEY", dotenv)
             or (tiiny_cfg.get("account") or {}).get("authKey"))
    if not device:
        raise ConfigError(
            "No device address. Run `tiiny scan` + `tiiny connect <ip>`, or set TAI_DEVICE."
        )
    if not token:
        raise ConfigError("No auth token. Run `tiiny login`, or set TAI_TOKEN.")
    return Settings(device=device, token=token)
