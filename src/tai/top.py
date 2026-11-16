"""`tai top`: a full-screen view of the device's utilisation, refreshed every second.

    tai top                # uses tai's device config (or tai --url / TAI_URL for client mode)
    tai top -i 2           # poll every 2 seconds
    tai top --ascii        # plain ASCII instead of Unicode block characters

Keys: q quit · p pause · + / - poll faster / slower.

Polling runs in a background thread, so a slow or unreachable device never freezes the
screen. ``render`` is a pure function from samples to text lines (tested without curses).
"""

from __future__ import annotations

import curses
import locale
import threading
import time
from collections import deque
from dataclasses import dataclass, field

HISTORY = 600  # samples kept for the graphs (10 minutes at 1 s)
MIN_WIDTH, MIN_HEIGHT = 50, 16


@dataclass(frozen=True)
class Glyphs:
    """The characters everything is drawn with. ``*_parts`` are partial cells, emptiest first."""
    full: str
    empty: str  # unfilled part of a meter bar
    bar_parts: str  # left-to-right fill of a meter cell
    col_parts: str  # bottom-up fill of a graph cell
    ends: str  # meter bar brackets ("" for none)
    hline: str
    vline: str
    tick: str  # the axis at a 100/50/0 label
    dot: str  # header separator


UNICODE = Glyphs("█", "░", " ▏▎▍▌▋▊▉", " ▁▂▃▄▅▆▇", "", "─", "│", "┤", "·")
ASCII = Glyphs("#", ".", " ", " .:", "[]", "-", "|", "|", "-")


@dataclass(frozen=True)
class Box:
    """A screen area in characters."""
    width: int
    height: int


@dataclass
class Readings:
    """The raw answers of one poll: /sys/status, /models/npu/status and /models/running."""
    status: dict
    npu: dict
    running: dict


@dataclass
class ModelRow:
    name: str
    npu: int  # % of the NPU budget it holds
    active: int  # requests in flight
    idle: float  # seconds since its last inference


@dataclass
class Sample:
    """One poll of the device."""
    when: float
    cpu: float
    cores: list[float]
    mem: float
    mem_used: int
    mem_total: int
    gpu: float
    npu: float
    npu_mem_used: int  # MB
    npu_mem_total: int  # MB
    disk_used: int
    disk_total: int
    budget: int  # % of the NPU budget held by loaded models
    models: list[ModelRow] = field(default_factory=list)

    @property
    def npu_mem(self) -> float:
        return 100 * self.npu_mem_used / self.npu_mem_total if self.npu_mem_total else 0.0

    @property
    def disk(self) -> float:
        return 100 * self.disk_used / self.disk_total if self.disk_total else 0.0


def parse(r: Readings, now: float) -> Sample:
    """A Sample from one poll's readings."""
    status, npu, running = r.status, r.npu, r.running
    chip = (status.get("npus") or [{}])[0]
    mem, disk, cpu = status.get("memory", {}), status.get("disk", {}), status.get("cpu", {})
    cores = list(cpu.get("per_core_percent", []))
    return Sample(
        # the device's total_percent disagrees with its own per-core figures (0.0 next to cores at 40%),
        # so the total is their mean
        when=now, cpu=sum(cores) / len(cores) if cores else cpu.get("total_percent", 0.0), cores=cores,
        mem=mem.get("usage_percent", 0.0), mem_used=mem.get("used_bytes", 0), mem_total=mem.get("total_bytes", 0),
        gpu=status.get("gpu", {}).get("utilization_percent", 0.0),
        npu=chip.get("utilization_percent", 0.0), npu_mem_used=chip.get("memory_used_mb", 0),
        npu_mem_total=chip.get("memory_total_mb", 0),
        disk_used=disk.get("used_bytes", 0), disk_total=disk.get("total_bytes", 0),
        budget=npu.get("npu_used", 0), models=_models(running, now))


def _models(running: dict, now: float) -> list[ModelRow]:
    rows = []
    for inst in (running.get("instances") or {}).get("running", []):
        last = inst.get("last_inference_time") or inst.get("use_time") or now
        rows.append(ModelRow(inst.get("display_name") or inst.get("model_id", "?"), int(inst.get("npu_usage") or 0),
                             int(inst.get("active_request_count") or 0), max(0.0, now - last)))
    return rows


# -- drawing (pure) ---------------------------------------------------------------------
def _fill(pct: float, cells: int, full: str, parts: str) -> str:
    """``pct`` percent of ``cells`` cells: whole cells, then at most one partial cell."""
    amount = max(0.0, min(100.0, pct)) / 100 * cells
    whole = int(amount)
    steps = len(parts)
    part = round((amount - whole) * steps)
    if part == steps:
        whole, part = whole + 1, 0
    return full * whole + (parts[part].strip() if whole < cells else "")


def bar(pct: float, width: int, g: Glyphs = UNICODE) -> str:
    """A meter for ``pct`` percent, ``width`` characters wide including any brackets."""
    inner = max(1, width - len(g.ends))
    filled = _fill(pct, inner, g.full, g.bar_parts)
    body = filled + g.empty * (inner - len(filled))
    return g.ends[:1] + body + g.ends[1:]


def graph(values: list[float], box: Box, g: Glyphs = UNICODE) -> list[str]:
    """Column chart of percentages filling ``box``, newest on the right."""
    cols = [_column(v, box.height, g) for v in list(values)[-box.width:]]
    cols = [" " * box.height] * (box.width - len(cols)) + cols
    return ["".join(c[row] for c in cols) for row in range(box.height)]


def _column(value: float, height: int, g: Glyphs) -> str:
    """One column, top to bottom."""
    column = _fill(value, height, g.full, g.col_parts)
    return " " * (height - len(column)) + column[::-1]


def size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return ""


def ago(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.0f}m" if seconds < 3600 else f"{seconds / 3600:.1f}h"


@dataclass
class View:
    """What render needs: history, where, and the poll state."""
    samples: list[Sample]
    where: str = ""
    interval: float = 1.0
    paused: bool = False
    error: str = ""
    glyphs: Glyphs = UNICODE


def render(view: View, box: Box) -> list[str]:
    """The whole screen: ``box.height`` lines of at most ``box.width`` characters."""
    if box.width < MIN_WIDTH or box.height < MIN_HEIGHT:
        return [f"tai top needs at least {MIN_WIDTH}x{MIN_HEIGHT} (have {box.width}x{box.height})"]
    lines = [_header(view, box)]
    if not view.samples:
        lines.append(view.error or "Waiting for the first sample…")
        return _fit(lines, box)
    last = view.samples[-1]
    g = view.glyphs
    below = _meters(last, box, g) + _model_lines(last, box, g) + ([f"! {view.error}"] if view.error else [])
    panel = Box(box.width, max(3, (box.height - len(below) - 3) // 2))
    lines += _panel(f"NPU {last.npu:5.1f}%", [s.npu for s in view.samples], panel, g)
    lines += _panel(f"CPU {last.cpu:5.1f}%", [s.cpu for s in view.samples], panel, g)
    return _fit(lines + below, box)


def _fit(lines: list[str], box: Box) -> list[str]:
    lines = [ln[:box.width] for ln in lines[:box.height]]
    return lines + [""] * (box.height - len(lines))


def _header(view: View, box: Box) -> str:
    state = "PAUSED" if view.paused else f"every {view.interval:g}s"
    d = view.glyphs.dot
    left = f" tai top {d} {view.where} {d} {time.strftime('%H:%M:%S')} {d} {state}"
    right = "q quit  p pause  +/- speed "
    return left + " " * max(1, box.width - len(left) - len(right)) + right


def _rule(title: str, width: int, g: Glyphs) -> str:
    """A horizontal line with an optional title near its left end."""
    if not title:
        return g.hline * width
    return f"{g.hline * 2} {title} " + g.hline * (width - len(title) - 4)


def _panel(title: str, values: list[float], box: Box, g: Glyphs) -> list[str]:
    """A title line, then a ``box.height`` graph with a 100/50/0 axis on the left."""
    rows = graph(values, Box(box.width - 5, box.height), g)
    labels = {0: "100", box.height // 2: " 50", box.height - 1: "  0"}
    return [_rule(title, box.width, g)] + \
        [f"{labels[i]} {g.tick}{row}" if i in labels else f"    {g.vline}{row}" for i, row in enumerate(rows)]


def _meters(s: Sample, box: Box, g: Glyphs) -> list[str]:
    w = max(10, box.width - 40)
    lines = [_rule("", box.width, g),
             f"NPU mem {bar(s.npu_mem, w, g)} {size(s.npu_mem_used * 2**20):>9} / {size(s.npu_mem_total * 2**20)}",
             f"Memory  {bar(s.mem, w, g)} {size(s.mem_used):>9} / {size(s.mem_total)}",
             f"Budget  {bar(s.budget, w, g)} {s.budget:>7}% held by models",
             f"GPU     {bar(s.gpu, w, g)} {s.gpu:>7.1f}%",
             f"Disk    {bar(s.disk, w, g)} {size(s.disk_used):>9} / {size(s.disk_total)}"]
    return lines + _cores(s.cores, box, g)


def _cores(cores: list[float], box: Box, g: Glyphs) -> list[str]:
    """Per-core mini bars, as many per line as fit."""
    cells = [f"{i:>2} {bar(v, 10, g)}{v:4.0f}" for i, v in enumerate(cores)]
    per_line = max(1, (box.width + 2) // (len(cells[0]) + 2)) if cells else 1
    return ["  ".join(cells[i:i + per_line]) for i in range(0, len(cells), per_line)]


def _model_lines(s: Sample, box: Box, g: Glyphs) -> list[str]:
    width = box.width
    head = _rule("Loaded models", width, g)
    if not s.models:
        return [head, "  (none)"]
    rows = [f"  {m.name[:width - 40]:<{min(32, width - 40)}} {m.npu:>3}% NPU  "
            f"{('busy x' + str(m.active)) if m.active else 'idle':<8} last {ago(m.idle)} ago" for m in s.models]
    return [head, *rows]


# -- polling and the screen -------------------------------------------------------------
class Poller:
    """Fetches samples in the background and keeps the history."""

    def __init__(self, client, interval: float):
        self.client, self.interval = client, interval
        self.samples: deque[Sample] = deque(maxlen=HISTORY)
        self.error, self.paused = "", False
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def poll_once(self) -> None:
        try:
            c = self.client
            self.samples.append(parse(Readings(c.sys_status(), c.npu(), c.running()), time.time()))
            self.error = ""
        except Exception as e:  # noqa: BLE001 — show any failure and keep polling
            self.error = f"device unreachable: {e}"[:200]

    def _loop(self) -> None:
        while not self._stop.is_set():
            if not self.paused:
                self.poll_once()
            self._stop.wait(self.interval)

    def view(self, where: str, glyphs: Glyphs = UNICODE) -> View:
        return View(list(self.samples), where, self.interval, self.paused, self.error, glyphs)


KEYS = {ord("+"): 0.5, ord("="): 0.5, ord("-"): 2.0, ord("_"): 2.0}  # interval multipliers


def handle_key(poller: Poller, key: int) -> bool:
    """Apply a key press; False means quit."""
    if key in (ord("q"), ord("Q"), 27):
        return False
    if key in (ord("p"), ord("P")):
        poller.paused = not poller.paused
    elif key in KEYS:
        poller.interval = min(30.0, max(0.25, poller.interval * KEYS[key]))
    return True


def screen(stdscr, poller: Poller, where: str, glyphs: Glyphs) -> None:
    curses.curs_set(0)
    stdscr.timeout(250)
    while True:
        height, width = stdscr.getmaxyx()
        stdscr.erase()
        for y, line in enumerate(render(poller.view(where, glyphs), Box(width, height))):
            stdscr.addnstr(y, 0, line, width - 1, curses.A_BOLD if y == 0 else curses.A_NORMAL)
        stdscr.refresh()
        if not handle_key(poller, stdscr.getch()):
            return


def run(client, interval: float = 1.0, ascii_only: bool = False) -> None:
    """`tai top`: poll the device in the background and draw until q (or Ctrl-C)."""
    locale.setlocale(locale.LC_ALL, "")  # curses needs it to draw Unicode
    utf8 = locale.getpreferredencoding(False).lower().replace("-", "") == "utf8"
    glyphs = ASCII if ascii_only or not utf8 else UNICODE
    poller = Poller(client, interval)
    poller.start()
    try:
        curses.wrapper(screen, poller, client.settings.where, glyphs)
    except KeyboardInterrupt:
        pass
    finally:
        poller.stop()
