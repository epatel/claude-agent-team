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
the ⚙ admin panel). Projects are **per user**: you see only the ones you
created; super-users see everything (with owner labels). Then:

- **+ new project** opens a dialog with two tabs: **from git url** (clone;
  token for private repos) or **blank repo** (just a name; git-inits an empty
  repo, no remote). The new project opens right away. Existing checkouts
  dropped into `labs/` auto-appear (visible to super-users).
- Chat with the project's agent on the **chat** tab. Follow-ups continue
  the same `chat/<ts>` branch with the same resumed context; replies render as
  markdown + mermaid; tool calls stream as live activity. Attach files for the
  agent with the `+` button (or paste a screenshot).
- The **repo** tab has the file browser (+ uploads into the tree, zip download)
  and the git actions: fetch / pull / push / reset, rebase-on-base (conflicts
  can be handed to the agent in chat), merge branch → base, remove project.
- The **agent** tab is per-project agent setup: a project prompt (appended to
  the system prompt), MCP servers (JSON name → config), and skills — managed
  as `.claude/skills/<name>/SKILL.md` files committed in the repo.

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

## B. Production (any always-on host)

The lab runs on anything with Python 3.11+ and the Claude Code CLI — a Linux
server, a Mac mini, a VPS, a Raspberry Pi. The production shape is: the web
console under a process supervisor, bound to **loopback only**, behind a
TLS-terminating reverse proxy under a path prefix (the SPA is prefix-aware).
Full steps live in `deploy/README.md`; `deploy/home/` is a complete worked
example (the reference site, a Pi 5 + Apache). The shape:

```sh
# on the lab host, as one user (claude credentials are user-scoped):
npm install -g @anthropic-ai/claude-code && claude     # one-time login
# venv per deploy/README.md, then (systemd hosts; adapt the unit's user/paths):
sudo cp deploy/dev-lab-web.service /etc/systemd/system/ && sudo systemctl enable --now dev-lab-web
# reverse proxy: deploy/apache-dev-lab.conf (Apache) or deploy/nginx-dev-lab.conf (nginx)
```

- The proxy forwards `/dev-lab/` — plain HTTP plus both WebSocket endpoints
  (`/ws` console, `/ws/client` platform clients) — to 127.0.0.1:8770.
- Platform clients connect from anywhere via
  `wss://<host>/dev-lab/ws/client` with the shared `CLIENT_TOKEN`.
- Register your account **immediately after first deploy** — the first
  registration becomes the super-user without an invite.
- On non-systemd hosts (e.g. macOS) use launchd or any supervisor that
  restarts the same `dev-lab web …` command on crash/boot.

## Caveats

- Live agent turns spend subscription credits (separate Agent SDK pool,
  personal-use only under Anthropic's ToS); typical turns run $0.10–0.40.
- Session cookies are not yet marked `Secure` and the old CLI `serve` socket is
  unauthenticated (M5 hardening) — keep the console behind TLS and the CLI
  surface on loopback.
- Manifest sync hashes the whole tree per task and caps files at 10 MB — fine
  for small repos; revisit if trees grow.
