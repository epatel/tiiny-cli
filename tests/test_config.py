import json

import pytest

from tai.config import load_settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("TAI_DEVICE", "TAI_TOKEN", "TIINY_API_KEY", "TAI_URL", "TAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    cfg = tmp_path / "tiiny.json"
    cfg.write_text(json.dumps({"deviceAddress": "10.0.0.2", "account": {"authKey": "from-cfg"}}))
    monkeypatch.setenv("TIINY_CONFIG", str(cfg))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # no real ~/.config/tai/.env
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


def test_tai_url_switches_to_client_mode_with_the_api_key(env):
    (env / ".env").write_text("TAI_URL=https://host/tai/\nTAI_API_KEY=k\n")
    s = load_settings()
    assert (s.base_url, s.token, s.via_server, s.where) == ("https://host/tai", "k", True, "via https://host/tai")
    assert load_settings(direct=True).device == "10.0.0.2"  # tai serve ignores TAI_URL


def test_client_mode_needs_a_key_and_a_scheme(env, monkeypatch):
    from tai.config import ConfigError

    monkeypatch.setenv("TAI_URL", "https://host/tai")
    with pytest.raises(ConfigError, match="TAI_API_KEY"):
        load_settings()
    monkeypatch.setenv("TAI_API_KEY", "k")
    with pytest.raises(ConfigError, match="scheme"):
        load_settings(url="host/tai")


def test_user_dotenv_applies_everywhere_and_the_folder_env_wins(env):
    user = env / "home" / ".config" / "tai" / ".env"
    user.parent.mkdir(parents=True)
    user.write_text("TAI_URL=https://host/tai\nTAI_API_KEY=user-key\n")
    assert (load_settings().device, load_settings().token) == ("https://host/tai", "user-key")
    (env / ".env").write_text("TAI_API_KEY=folder-key\n")
    assert load_settings().token == "folder-key"
