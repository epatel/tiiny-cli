VENV   := .venv
BIN    := $(VENV)/bin
PREFIX ?= $(HOME)/.local

.PHONY: help setup test health scan check link unlink clean

help:  ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-8s %s\n", $$1, $$2}'

$(BIN)/tai: pyproject.toml
	python3 -m venv $(VENV)
	$(BIN)/pip install -q --upgrade pip
	$(BIN)/pip install -q -e '.[dev]'
	@touch $@

setup: $(BIN)/tai  ## Create .venv and install tai with dev tools (pytest, aihealth)

test: setup  ## Run the unit tests (mocked; no device needed)
	$(BIN)/pytest -q

health: setup  ## Code-health delta of uncommitted changes vs HEAD (fails if worse)
	$(BIN)/aihealth delta

scan: setup  ## Code-health overview of the whole project
	$(BIN)/aihealth scan . --max-findings 5

check: test health  ## Everything to run before committing

link: setup  ## Put `tai` on PATH via $(PREFIX)/bin
	mkdir -p $(PREFIX)/bin
	ln -sf $(CURDIR)/$(BIN)/tai $(PREFIX)/bin/tai

unlink:  ## Remove the `tai` link from $(PREFIX)/bin
	rm -f $(PREFIX)/bin/tai

clean:  ## Remove caches and build metadata (keeps .venv)
	rm -rf .pytest_cache src/*.egg-info
	find . -name __pycache__ -not -path './$(VENV)/*' -prune -exec rm -rf {} +
