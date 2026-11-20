"""`tai usage`: tokens per day and model, from the device's weekly statistics.

The device counts every request it serves (the TiinyOS Dashboard's "Weekly Usage"), not only
tai's. ``GET /api/v1/statistics/week_usage?start_date=<Monday>`` answers seven days, each
``{model_id: {prompt_tokens, completion_tokens, total_tokens, request_count}}``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from rich.table import Table

COUNTS = ("prompt_tokens", "completion_tokens", "total_tokens", "request_count")


@dataclass(frozen=True)
class UsageRow:
    day: str  # YYYY-MM-DD
    model: str  # model id
    prompt: int = 0
    completion: int = 0
    total: int = 0
    requests: int = 0

    @property
    def counts(self) -> tuple[int, int, int, int]:
        return (self.prompt, self.completion, self.total, self.requests)


def today() -> date:
    return date.today()


def monday(today: date, weeks_ago: int = 0) -> date:
    """The Monday of the week ``weeks_ago`` weeks before ``today``'s."""
    return today - timedelta(days=today.weekday() + 7 * weeks_ago)


def rows(week: dict) -> list[UsageRow]:
    """One row per day and model that was used, oldest day first, most tokens first within a day."""
    found = [UsageRow(day, model, *(int(counts.get(k) or 0) for k in COUNTS))
             for day, models in week.items() for model, counts in (models or {}).items()]
    return sorted(found, key=lambda r: (r.day, -r.total, r.model))


def total(found: list[UsageRow]) -> UsageRow:
    sums = [sum(column) for column in zip(*(r.counts for r in found))] or [0, 0, 0, 0]
    return UsageRow("", "Total", *sums)


def weekday(day: str) -> str:
    """'2026-11-17' -> 'Tue 17'."""
    return date.fromisoformat(day).strftime("%a %d")


NUMBERS = ("Prompt", "Completion", "Total", "Requests")


def table(found: list[UsageRow], width: int) -> Table:
    """The week as a table ``width`` cells wide, with a Total row. Numbers always show in full;
    a model name is what gets cut short in a narrow terminal."""
    sums = [f"{n:,}" for n in total(found).counts]
    widths = [max(len(col), len(widest)) for col, widest in zip(NUMBERS, sums)]
    t = Table(box=None, header_style="bold")
    t.add_column("Day", no_wrap=True, min_width=6)
    t.add_column("Model", no_wrap=True, max_width=max(8, width - 6 - sum(widths) - 12))  # 6 columns, 2 cells padding each
    for col, col_width in zip(NUMBERS, widths):
        t.add_column(col, no_wrap=True, justify="right", min_width=col_width)
    shown = ""
    for r in found:
        t.add_row("" if r.day == shown else weekday(r.day), r.model.split("/")[-1], *(f"{n:,}" for n in r.counts))
        shown = r.day
    t.add_section()
    t.add_row("", "[bold]Total[/]", *(f"[bold]{n}[/]" for n in sums))
    return t
