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

## Revisit if

- Dependency/version conflicts across components grow painful enough to warrant a
  heavier tool (e.g. uv workspaces, Poetry, or containers).
