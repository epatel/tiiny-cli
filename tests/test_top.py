"""tai top: parsing the device's answers, the drawing (Unicode and ASCII), polling and keys (no curses, no device)."""

from tai.top import ASCII, Box, Poller, Readings, View, bar, graph, handle_key, parse, render

STATUS = {"cpu": {"total_percent": 14.7, "per_core_percent": [100.0, 40.9, 0.0, 35.0]},
          "memory": {"used_bytes": 14772576256, "total_bytes": 33342308352, "usage_percent": 44.31},
          "gpu": {"name": "GPU-0 (synthetic)", "utilization_percent": 27.65},
          "npus": [{"name": "NPU-0", "utilization_percent": 99.19, "memory_used_mb": 7835, "memory_total_mb": 12372}],
          "disk": {"total_bytes": 921123848192, "used_bytes": 301382225920}}
NPU = {"npu_total": 100, "npu_used": 52, "npu_available": 48}
RUNNING = {"instances": {"running": [{"model_id": "Qwen/Qwen3.8-27B", "display_name": "Qwen3.8-27B", "npu_usage": 52,
                                      "active_request_count": 1, "last_inference_time": 1000}]}}


def sample(now=1030.0):
    return parse(Readings(STATUS, NPU, RUNNING), now)


def test_parse_reads_every_field():
    s = sample()
    assert (s.npu, s.gpu, s.budget, len(s.cores)) == (99.19, 27.65, 52, 4)
    assert round(s.cpu, 2) == 43.98  # the mean of the cores: the device's total_percent is unreliable
    assert round(s.npu_mem) == 63 and round(s.disk) == 33
    assert s.models[0].name == "Qwen3.8-27B" and s.models[0].active == 1 and s.models[0].idle == 30


def test_parse_tolerates_missing_fields():
    s = parse(Readings({}, {}, {}), 0)
    assert s.npu == 0 and s.npu_mem == 0 and s.models == []


def test_bar():
    assert bar(50, 10) == "█████░░░░░"
    assert bar(55, 10) == "█████▌░░░░"  # 5.5 cells: a half block
    assert bar(99.9, 10) == "██████████" and bar(150, 4) == "████" and bar(-5, 4) == "░░░░"
    assert bar(50, 12, ASCII) == "[#####.....]"
    assert bar(150, 7, ASCII) == "[#####]" and bar(-5, 7, ASCII) == "[.....]"


def test_graph_columns_grow_from_the_bottom_newest_right():
    rows = graph([100, 50, 0, 75], Box(5, 4))
    assert rows == [" █   ", " █  █", " ██ █", " ██ █"]
    assert graph([50], Box(1, 3)) == [" ", "▄", "█"]  # 1.5 cells: one full, a half on top
    assert graph([50], Box(1, 3), ASCII) == [" ", ":", "#"]


def test_render_fills_the_screen():
    lines = render(View([sample(), sample()], "direct 10.0.0.2"), Box(80, 30))
    assert len(lines) == 30 and all(len(ln) <= 80 for ln in lines)
    text = "\n".join(lines)
    assert "direct 10.0.0.2" in text and "NPU  99.2%" in text and "CPU  44.0%" in text
    assert "Qwen3.8-27B" in text and "busy x1" in text and "52%" in text


def test_render_ascii_has_no_unicode():
    lines = render(View([sample()], "x", glyphs=ASCII), Box(80, 30))
    assert all(ln.isascii() for ln in lines)


def test_render_small_terminal_and_no_data():
    assert "needs at least" in render(View([]), Box(30, 10))[0]
    lines = render(View([], error="device unreachable: timeout"), Box(80, 20))
    assert "device unreachable" in lines[1]


class FakeClient:
    def __init__(self, fail=False):
        self.fail = fail

    def sys_status(self):
        if self.fail:
            raise OSError("no route")
        return STATUS

    def npu(self):
        return NPU

    def running(self):
        return RUNNING


def test_poller_keeps_history_and_reports_errors():
    p = Poller(FakeClient(), 1.0)
    p.poll_once()
    p.client = FakeClient(fail=True)
    p.poll_once()
    view = p.view("x")
    assert len(view.samples) == 1 and "no route" in view.error


def test_keys():
    p = Poller(FakeClient(), 1.0)
    assert handle_key(p, ord("p")) and p.paused
    handle_key(p, ord("+"))
    assert p.interval == 0.5
    handle_key(p, ord("-"))
    handle_key(p, ord("-"))
    assert p.interval == 2.0
    assert handle_key(p, -1) and not handle_key(p, ord("q"))


def test_tai_top_command_runs_the_monitor_with_the_app_client(monkeypatch):
    from click.testing import CliRunner

    from tai import cli as cli_mod, top as top_mod

    seen = {}
    monkeypatch.setattr(top_mod, "run", lambda client, interval, ascii_only: seen.update(i=interval, a=ascii_only))
    monkeypatch.setattr(cli_mod.App, "client", "the-client")
    result = CliRunner().invoke(cli_mod.cli, ["top", "-i", "2", "--ascii"])
    assert result.exit_code == 0, result.output
    assert seen == {"i": 2.0, "a": True}
