# python-venvs

Decision: Python is the toolchain, with a separate venv per component.

## Decision

Build the lab and its tooling in **Python**, isolating dependencies in a
**per-component virtual environment** (the dev lab gets its own venv; each
extension client gets its own on its host).

## Why

- **Simplicity** — venvs are the lowest-friction isolation that works the same on
  the Pi and on macOS, no extra runtime to install.
- **Per-component isolation** — the lab and an extension client have different
  dependency sets (and run on different OSes); separate venvs keep them from
  colliding.
- **SDK fit** — the Claude Agent SDK and MCP server tooling are first-class in
  Python.

## Conventions

- One venv per deployable component, created from that component's own
  `requirements.txt` / `pyproject.toml`.
- Never commit a venv; `.env` and venv directories stay gitignored.
- Pin dependencies for reproducible installs on the Pi.

## Setup

`make setup` creates and populates every component's venv. Each component also
has its own `setup.<name>` target for setting up (or re-setting-up) just one:
`make setup.dev-lab`, `make setup.chat-client`, `make setup.platform-client`.

`setup.dev-lab` additionally installs the platform client into the lab's venv —
the lab shares the manifest-sync primitives (`dev_lab/clients.py` imports
`platform_client.manifest`) and a `pyproject` can't express that relative-path
dep, so the Makefile wires it up by hand.

## Revisit if

- Dependency/version conflicts across components grow painful enough to warrant a
  heavier tool (e.g. uv workspaces, Poetry, or containers).
