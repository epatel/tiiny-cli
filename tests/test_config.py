import json

import pytest

from tai.config import load_settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("TAI_DEVICE", "TAI_TOKEN", "TIINY_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = tmp_path / "tiiny.json"
    cfg.write_text(json.dumps({"deviceAddress": "10.0.0.2", "account": {"authKey": "from-cfg"}}))
    monkeypatch.setenv("TIINY_CONFIG", str(cfg))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_falls_back_to_tiiny_config(env):
    s = load_settings()
    assert (s.device, s.token) == ("10.0.0.2", "from-cfg")


def test_dotenv_api_key_beats_config(env):
    (env / ".env").write_text("# comment\nexport TIINY_API_KEY='from-dotenv'\n")
    assert load_settings().token == "from-dotenv"


def test_empty_dotenv_value_is_ignored(env):
    (env / ".env").write_text("TIINY_API_KEY=\n")
    assert load_settings().token == "from-cfg"


def test_real_env_beats_dotenv(env, monkeypatch):
    (env / ".env").write_text("TIINY_API_KEY=from-dotenv\n")
    monkeypatch.setenv("TIINY_API_KEY", "from-env")
    assert load_settings().token == "from-env"
    monkeypatch.setenv("TAI_TOKEN", "tai-token")
    assert load_settings().token == "tai-token"
