# claude-agent-team — per-component build tooling.
# Each component gets its own venv (see cards/python-venvs.md).

COMPONENTS := dev-lab chat-client extensions/platform-client extensions/macos-build-test
PY ?= python3

.DEFAULT_GOAL := help

.PHONY: help setup test lint fmt clean

help:
	@echo "Targets:"
	@echo "  make setup   - create a venv per component and install (editable + dev deps)"
	@echo "  make test    - run pytest in every component"
	@echo "  make lint    - run ruff check in every component"
	@echo "  make fmt     - run ruff format in every component"
	@echo "  make clean   - remove venvs and tooling caches"
	@echo ""
	@echo "Components: $(COMPONENTS)"

setup:
	@for c in $(COMPONENTS); do \
		echo "==> $$c"; \
		( cd $$c && $(PY) -m venv .venv \
			&& .venv/bin/python -m pip install --quiet --upgrade pip \
			&& .venv/bin/python -m pip install -e ".[dev]" ) || exit 1; \
	done
	@# Platform clients build on the sibling scaffold; a pyproject can't express
	@# a relative-path dep, so install it into each client's venv here. The lab
	@# needs it too (manifest sync source side, dev_lab/clients.py).
	@( cd extensions/macos-build-test \
		&& .venv/bin/python -m pip install --quiet -e ../platform-client )
	@( cd dev-lab \
		&& .venv/bin/python -m pip install --quiet -e ../extensions/platform-client )

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
