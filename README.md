# claude-agent-team

An always-on autonomous development lab: a Claude Agent SDK client that runs
uninterrupted on a Raspberry Pi 5, does real dev work on a GitHub repo, is
steered by a chat client, and borrows capabilities it lacks (e.g. building and
testing on macOS) from extension clients exposed as MCP servers.

- **Plan:** [`project-plan.md`](project-plan.md) — goal, milestones, decisions, open questions.
- **Architecture & docs:** [`CLAUDE.md`](CLAUDE.md) — orientation + a card index.
- **Run it:** [`QUICKSTART.md`](QUICKSTART.md) — local/test and production setup.
- **Status:** [`HANDOFF.md`](HANDOFF.md) — where things are and what's left.

## Repository layout

```
dev-lab/                      # the autonomous agent loop (runs on the Pi)
chat-client/                  # control surface to instruct/steer the lab
extensions/
  macos-build-test/           # an MCP server exposing build/test on macOS
```

Each component is independently deployable with its **own venv** and its own
`pyproject.toml` (see `cards/python-venvs.md`).

## Quickstart

Requires Python 3.11+ and `make`.

```sh
make setup    # create a venv per component, install editable + dev deps
make test     # run pytest in every component
make lint     # ruff check
make fmt      # ruff format
```

Scope to one component by `cd`-ing into it and using its `.venv` directly.

## Credentials

The dev lab authenticates with a **Claude subscription** (not an API key) via a
one-time `claude` login, plus a GitHub token. Log in once on the lab host, then
fill in the gitignored `.env` with just the GitHub token:

```sh
claude                               # complete login (SSH: press `c` to copy the URL, paste the code back)
cp dev-lab/.env.example dev-lab/.env # then set GITHUB_TOKEN
```

The login credentials live in `~/.claude` and auto-refresh. Do not set
`ANTHROPIC_API_KEY` — it overrides subscription auth. See
`cards/subscription-auth.md`.

## Status

**M0 (skeleton)** — package layout, per-component venvs, lint/test, and `.env`
handling are in place. The components are stubs; the agent loop, control surface,
and MCP server are not yet implemented. Next: **M1** (minimal dev-lab loop). See
`project-plan.md`.
