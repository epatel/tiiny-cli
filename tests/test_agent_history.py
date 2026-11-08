"""PromptHistory: shell-style Up/Down over earlier prompts."""

from tai.agent.history import PromptHistory


def test_walks_back_and_forth_and_restores_the_draft():
    h = PromptHistory(["one", "two"])
    assert h.newer() is None  # already at the draft
    assert h.older("half typed") == "two"
    assert h.older("two") == "one"
    assert h.older("one") is None  # oldest: stay
    assert h.newer() == "two"
    assert h.newer() == "half typed"
    assert h.newer() is None


def test_add_skips_blanks_and_repeats_and_resets_to_an_empty_draft():
    h = PromptHistory(["one"])
    h.older("draft")
    assert h.add("two") and not h.add("two") and not h.add("")
    assert h.entries == ["one", "two"]
    assert h.older("") == "two" and h.newer() == ""


def test_empty_history_does_nothing():
    h = PromptHistory()
    assert h.older("x") is None and h.newer() is None
