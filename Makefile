# claude-agent-team — per-component build tooling.
# Each component gets its own venv (see cards/python-venvs.md).

COMPONENTS := dev-lab chat-client extensions/platform-client

# Short component names usable as `make setup.<name>`, mapped to their dir (the
# platform client lives under extensions/, so the short name and path differ).
SETUP_NAMES := dev-lab chat-client platform-client
DIR_dev-lab := dev-lab
DIR_chat-client := chat-client
DIR_platform-client := extensions/platform-client

# Every component declares requires-python >= 3.11, but a stock Mac's plain
# `python3` can be 3.9 — auto-pick the newest interpreter that qualifies.
# `make setup PY=/path/to/python` still overrides.
PY ?= $(shell for p in python3.14 python3.13 python3.12 python3.11 python3; do \
	command -v $$p >/dev/null 2>&1 \
	&& $$p -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null \
	&& { echo $$p; break; }; done)

.DEFAULT_GOAL := help

# Note: the `setup.<name>` targets are matched by the `setup.%` pattern rule and
# must NOT be listed in .PHONY — GNU make skips pattern-rule search for phony
# targets. They never correspond to real files, so they rebuild every time.
.PHONY: help setup test lint fmt clean _check-py

help:
	@echo "Targets:"
	@echo "  make setup          - set up every component (venv + editable + dev deps)"
	@echo "  make setup.<name>   - set up just one component"
	@echo "  make test           - run pytest in every component"
	@echo "  make lint           - run ruff check in every component"
	@echo "  make fmt            - run ruff format in every component"
	@echo "  make clean          - remove venvs and tooling caches"
	@echo ""
	@echo "Components: $(COMPONENTS)"
	@echo "Per-component setup: $(addprefix setup.,$(SETUP_NAMES))"

_check-py:
	@test -n "$(PY)" || { echo "error: no Python >= 3.11 found on PATH."; \
		echo "Install one (e.g. 'uv python install 3.12' or 'brew install python@3.12')"; \
		echo "or run: make setup PY=/path/to/python3.12"; exit 1; }
	@$(PY) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null || { \
		echo "error: $(PY) is $$($(PY) --version 2>&1) — the components need >= 3.11."; \
		echo "Run: make setup PY=/path/to/python3.12"; exit 1; }
	@echo "Using $(PY) ($$($(PY) --version))"

setup: $(addprefix setup.,$(SETUP_NAMES))

# Per-component setup. `_check-py` is a shared prerequisite, so it runs once
# even when `make setup` fans out to all of them. setup.dev-lab also installs
# the platform client into the lab's venv: the lab shares the manifest-sync
# primitives, and a pyproject can't express that relative-path dep
# (dev_lab/clients.py imports platform_client.manifest).
setup.%: _check-py
	@dir="$(DIR_$*)"; \
	test -n "$$dir" || { echo "error: unknown component '$*'"; \
		echo "Known: $(SETUP_NAMES)"; exit 1; }; \
	echo "==> $$dir"; \
	( cd $$dir && $(PY) -m venv .venv \
		&& .venv/bin/python -m pip install --quiet --upgrade pip \
		&& .venv/bin/python -m pip install -e ".[dev]" ) || exit 1; \
	if [ "$*" = "dev-lab" ]; then \
		echo "    + platform-client (shared manifest-sync primitives)"; \
		( cd dev-lab \
			&& .venv/bin/python -m pip install --quiet -e ../extensions/platform-client ) || exit 1; \
	fi

test:
	@for c in $(COMPONENTS); do \
		echo "==> $$c"; \
		( cd $$c && .venv/bin/python -m pytest -q ) || exit 1; \
	done

lint:
	@for c in $(COMPONENTS); do \
		echo "==> $$c"; \
		( cd $$c && .venv/bin/ruff check . ) || exit 1; \
	done

fmt:
	@for c in $(COMPONENTS); do \
		echo "==> $$c"; \
		( cd $$c && .venv/bin/ruff format . ) || exit 1; \
	done

clean:
	@for c in $(COMPONENTS); do rm -rf $$c/.venv; done
	@find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .pytest_cache -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .ruff_cache -prune -exec rm -rf {} + 2>/dev/null || true
