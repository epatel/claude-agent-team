# claude-agent-team

An always-on autonomous development lab: a Claude Agent SDK client that runs
uninterrupted on a Raspberry Pi 5, does real dev work on GitHub repos, is
steered from a browser (per-project chat), and borrows capabilities it lacks
(e.g. building and testing on macOS) from platform clients that dial in from
other machines.

- **Plan:** [`project-plan.md`](project-plan.md) — goal, milestones, decisions, open questions.
- **Architecture & docs:** [`CLAUDE.md`](CLAUDE.md) — orientation + a card index.
- **Run it:** [`QUICKSTART.md`](QUICKSTART.md) — local/test and production setup.
- **Status:** [`HANDOFF.md`](HANDOFF.md) — where things are and what's left.

## Repository layout

```
dev-lab/                      # the lab: agent loop + web console (runs on the Pi)
chat-client/                  # CLI control surface (secondary to the web console)
extensions/
  platform-client/            # platform-client runtime + manifest sync + shared scaffold
  macos-build-test/           # first capability provider (being ported to the new model)
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

## Authentication — the `claude` CLI, no API key

All Claude auth is handled by a **one-time `claude` login** on the lab host
(Claude subscription). The Agent SDK reads the stored credentials from
`~/.claude`; they auto-refresh and survive restarts. There is **no API key
anywhere** — not in `.env`, not in the environment:

```sh
claude        # complete login once (SSH: press `c` to copy the URL, paste the code back)
```

Do **not** set `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` — they would
override subscription auth and bill the API, so the lab refuses to start if
either is present. See `cards/subscription-auth.md`.

Everything else is per-feature, not global:

- **GitHub** — per project: paste a token when adding a private repo in the web
  console (public repos need none). No global `GITHUB_TOKEN`.
- **Platform clients** — optionally set `CLIENT_TOKEN` on the lab and pass the
  same to `platform-client connect` (open on a trusted LAN otherwise).
- `dev-lab/.env` carries optional overrides only (`MODEL`, `CLIENT_TOKEN`) —
  never a credential for Claude.

## Status

The lab is live: multi-project web console (login, per-project chat with a
resumed agent session, repo actions, per-project model selection), durable
SQLite runtime data, systemd deployment for the Pi. Platform clients (M6 v1)
dial the lab over WebSocket, sync the working tree via content-hash manifests,
run builds/tests in a warm mirror, and report results + changed files; the
agent drives them through `list_clients` / `run_on_client` /
`fetch_from_client`. See `project-plan.md` for milestones and `HANDOFF.md` for
the current handoff.
