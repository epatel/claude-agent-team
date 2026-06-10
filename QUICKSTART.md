# Quickstart

How to run the lab locally and in production. The primary surface is the
**web console** (`dev-lab web`); capability machines join as **platform
clients** (`platform-client connect`). For architecture see `CLAUDE.md`; for
status see `project-plan.md`.

## Prerequisites

- **Python 3.11+**, **git**, **make**.
- **Claude Code CLI** + a **`claude` login** (the lab authenticates with your
  Claude *subscription*, not an API key). Opus needs a **Max** plan.
  ```sh
  npm install -g @anthropic-ai/claude-code     # or your platform's install
  claude                                        # complete login (SSH: press `c`, paste code back)
  ```
- **No other Claude credential** — auth is entirely the `claude` login above;
  nothing Claude-related goes in `.env`. GitHub tokens are **per project**,
  entered in the web console when adding a private repo (public repos need none).
- **Do NOT export `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`** — they override
  subscription auth and the lab refuses to start.

## Install

Each component has its own venv. From the repo root:

```sh
make setup            # creates dev-lab/.venv, chat-client/.venv, extensions/platform-client/.venv
make test             # unit tests — no network, no credits
```

Entry points live at `<component>/.venv/bin/<name>`; alternatively
`source dev-lab/.venv/bin/activate` to use the bare `dev-lab` command.

---

## A. Local (one machine)

### A1. The web console

```sh
dev-lab/.venv/bin/dev-lab web --labs-dir ~/labs --host 127.0.0.1 --port 8770
```

Open `http://127.0.0.1:8770` and **register** — the **first user becomes the
super-user** (no invite needed; everyone later needs an invite code minted in
the ⚙ admin panel). Then:

- **+ new project** → paste a git URL to clone (token for private repos), **or**
  leave the URL empty and give just a name to git-init a blank repo. Existing
  checkouts dropped into `labs/` auto-appear.
- Pick a project, chat with its agent on the **chat** tab. Follow-ups continue
  the same `chat/<ts>` branch with the same resumed context; replies render as
  markdown + mermaid; tool calls stream as live activity. Attach files for the
  agent with the `+` button (or paste a screenshot).
- The **repo** tab has the file browser (+ uploads into the tree, zip download)
  and the git actions: fetch / pull / push / reset, rebase-on-base (conflicts
  can be handed to the agent in chat), merge branch → base, remove project.

State (SQLite + cookie secret) lives under `<labs>/.dev-lab/`. Each project is
its own clone, agent context, and branch.

### A2. A platform client (same or another machine)

Platform clients provide capabilities the lab host lacks (build, test,
platform tooling). They **dial the lab**:

```sh
extensions/platform-client/.venv/bin/platform-client connect \
  --lab ws://127.0.0.1:8770/ws/client --name mac \
  --capability run_tests --capability build
```

The client appears in the console sidebar. In a project chat, ask the agent to
e.g. "run `make test` on client mac" — it uses the `mcp__lab` tools
(`list_clients` / `run_on_client` / `fetch_from_client`). The project's working
tree (uncommitted changes included) is mirrored to the client via content-hash
manifest sync; results and changed files come back; artifacts can be fetched
into the lab tree. The file browser can browse/clean client mirrors per
project. Mirrors live under `~/.platform-client/mirrors/<lab-id>/` — namespaced by the
lab's stable id (sent in `hello_ok`), so several labs can share one client
machine without same-named projects colliding.

To gate the client endpoint, set `CLIENT_TOKEN=<secret>` in `dev-lab/.env` and
pass `--token <secret>` (or the `CLIENT_TOKEN` env var) to the client.

### A3. The CLI surface (older, single-project; still works)

```sh
dev-lab/.venv/bin/dev-lab run "Create hello.txt with a greeting." --repo <path>   # one-shot
dev-lab/.venv/bin/dev-lab serve --repo <path> --host 127.0.0.1 --port 8765        # supervisor + WS
chat-client/.venv/bin/chat-client --url ws://127.0.0.1:8765 chat                   # interactive session
dev-lab/.venv/bin/dev-lab submit "Add a CHANGELOG.md"                              # enqueue a job
```

Jobs flow through `~/.dev-lab/queue/{pending,running,done,failed}`; run history
is in `~/.dev-lab/lab.db`. The `serve` WebSocket is unauthenticated — keep it
loopback.

---

## B. Production (the Pi)

The reference deployment runs the web console on a Raspberry Pi 5 behind an
Apache TLS reverse proxy under a path prefix (the SPA is prefix-aware). Full
steps live in `deploy/README.md`; the shape:

```sh
# on the Pi, as one user (claude credentials are user-scoped):
npm install -g @anthropic-ai/claude-code && claude     # one-time login
# code at ~/dev-lab/claude-agent-team, venv per deploy/README.md, then:
sudo cp deploy/dev-lab-web.service /etc/systemd/system/ && sudo systemctl enable --now dev-lab-web
# Apache: include deploy/apache-dev-lab.conf inside the TLS vhost
```

- `dev-lab web` binds **loopback only**; Apache terminates TLS and proxies
  `/dev-lab/` (HTTP + both WebSocket endpoints) to it.
- Platform clients connect from anywhere via
  `wss://<host>/dev-lab/ws/client` with the shared `CLIENT_TOKEN`.
- Register your account **immediately after first deploy** — the first
  registration becomes the super-user without an invite.

## Caveats

- Live agent turns spend subscription credits (separate Agent SDK pool,
  personal-use only under Anthropic's ToS); typical turns run $0.10–0.40.
- Session cookies are not yet marked `Secure` and the old CLI `serve` socket is
  unauthenticated (M5 hardening) — keep the console behind TLS and the CLI
  surface on loopback.
- Manifest sync hashes the whole tree per task and caps files at 10 MB — fine
  for small repos; revisit if trees grow.
