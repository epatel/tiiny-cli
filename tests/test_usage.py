"""`tai usage`: the device's weekly statistics as a table (no device)."""

import json
from datetime import date

from click.testing import CliRunner

import tai.cli as cli_mod
from tai import usage
from tai.config import Settings

WEEK = {
    "2026-11-16": {"q/Turbo": {"prompt_tokens": 128220, "completion_tokens": 5631, "total_tokens": 133851, "request_count": 17},
                   "o/gpt-oss": {"prompt_tokens": 2168, "completion_tokens": 98, "total_tokens": 2266, "request_count": 1}},
    "2026-11-17": {"q/Turbo": {"prompt_tokens": 22826053, "completion_tokens": 414231, "total_tokens": 23240284,
                               "request_count": 1354}},
    "2026-11-18": {}, "2026-11-19": None,
}


class FakeClient:
    def __init__(self):
        self.asked: list[str] = []
        self.weeks = {"2026-11-16": WEEK}

    def week_usage(self, start_date):
        self.asked.append(start_date)
        return self.weeks.get(start_date, {start_date: {}})


def run(monkeypatch, tmp_path, *args, columns=120):
    client = FakeClient()
    monkeypatch.setattr(cli_mod, "TiinyClient", lambda settings: client)
    monkeypatch.setattr(cli_mod, "load_settings", lambda **kw: Settings("dev", "tok"))
    monkeypatch.setattr(usage, "today", lambda: date(2026, 11, 20))  # a Friday
    monkeypatch.setenv("TAI_HOME", str(tmp_path))
    monkeypatch.setenv("COLUMNS", str(columns))
    # `out` is made at import; with COLUMNS already set then (as in a hook's shell), rich keeps that width
    monkeypatch.setattr(cli_mod.out, "_width", columns)
    return client, CliRunner().invoke(cli_mod.cli, ["usage", *args])


def test_monday_of_this_and_earlier_weeks():
    assert usage.monday(date(2026, 11, 20)) == date(2026, 11, 16)
    assert usage.monday(date(2026, 11, 16)) == date(2026, 11, 16)
    assert usage.monday(date(2026, 11, 22), weeks_ago=2) == date(2026, 11, 2)


def test_rows_skip_empty_days_and_put_the_most_used_model_first():
    found = usage.rows(WEEK)
    assert [(r.day, r.model) for r in found] == [("2026-11-16", "q/Turbo"), ("2026-11-16", "o/gpt-oss"),
                                                 ("2026-11-17", "q/Turbo")]
    assert usage.total(found).counts == (22956441, 419960, 23376401, 1372)
    assert usage.total([]).counts == (0, 0, 0, 0)


def test_usage_table_for_this_week(monkeypatch, tmp_path):
    client, r = run(monkeypatch, tmp_path)
    assert r.exit_code == 0, r.output
    assert client.asked == ["2026-11-16"]
    lines = r.output.splitlines()
    monday = next(ln for ln in lines if "Mon 16" in ln)
    assert monday.split() == ["Mon", "16", "Turbo", "128,220", "5,631", "133,851", "17"]
    assert next(ln for ln in lines if "gpt-oss" in ln).split()[0] == "gpt-oss"  # the day isn't repeated
    assert lines[-2].split() == ["Total", "22,956,441", "419,960", "23,376,401", "1,372"]
    assert "Week of 2026-11-16" in r.output and "Wed" not in r.output


def test_numbers_are_never_cut_short_in_a_narrow_terminal(monkeypatch, tmp_path):
    monkeypatch.setattr(FakeClient, "week_usage", lambda self, start: {
        start: {"t/A-very-long-model-name-that-does-not-fit-here": WEEK["2026-11-17"]["q/Turbo"]}})
    _, r = run(monkeypatch, tmp_path)
    _, narrow = run(monkeypatch, tmp_path, columns=70)
    assert "A-very-long-model-name-that-does-not-fit-here" in r.output
    for text in ("Mon 16", "22,826,053", "414,231", "23,240,284", "1,354", "Completion", "Requests"):
        assert text in narrow.output
    assert "does-not-fit-here" not in narrow.output and all(len(ln) <= 70 for ln in narrow.output.splitlines())


def test_an_earlier_week_without_usage_and_json(monkeypatch, tmp_path):
    client, r = run(monkeypatch, tmp_path, "-w", "1")
    assert client.asked == ["2026-11-09"] and "No usage in the week of 2026-11-09." in r.output
    _, r = run(monkeypatch, tmp_path, "--json")
    assert json.loads(r.stdout)["2026-11-17"]["q/Turbo"]["request_count"] == 1354
