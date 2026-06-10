# Project Plan — claude-agent-team

## Goal

Build an always-on autonomous development "lab": a Claude Agent SDK client that
runs uninterrupted on a Raspberry Pi 5, does real development work on a GitHub
repository, is steered by a chat client, and borrows capabilities it lacks
(e.g. building and testing on macOS) from extension clients running on other
machines.

## Non-goals

- Not a hosted/multi-tenant SaaS — single owner, personal/small-team lab.
- Not using Anthropic Managed Agents — the loop runs on owned hardware so it can
  run uninterrupted and keep compute local (see Decisions).
- Not building a general MCP framework — extension clients use existing MCP
  server tooling; we only write the servers we need.
- No web GUI in the initial scope — the chat client is the only control surface.
- Not targeting non-Pi deployment in v1 (Pi 5 is the reference lab host).

## Milestones

Backward-planned from "a Pi runs unattended, takes chat instructions, and ships
commits that an extension client has built and tested."

- [x] M0 — Repo + tooling skeleton: package layout, per-component venvs, lint/test, `.env` handling (owner: agent, status: done 2026-06-08)
- [x] M1 — Minimal dev lab: Claude Agent SDK loop working in a local git clone; one instruction → one commit (owner: agent, status: done 2026-06-08, live-verified)
- [x] M2 — Run uninterrupted on Pi 5: systemd service, restart-on-crash, durable job queue (owner: agent, status: done 2026-06-08; the web console runs on the real Pi since 2026-06-10 — see Current state)
- [x] M3 — Chat client: control surface to send instructions, stream agent output (owner: agent, status: done 2026-06-08, live-verified)
- [x] M4 — First extension client: macOS build/test MCP server; lab connects and uses it as tools (owner: agent, status: done 2026-06-08, live-verified end-to-end; superseded by M6's reversed-connection model)
- [x] M6 — Platform clients v1 (reversed connection): clients dial the lab over WebSocket, announce capabilities, maintain a manifest-synced mirror of a project tree, run commands there, report results + changed files; agent reaches them via lab-local SDK tools; console shows connected clients (owner: agent, status: done 2026-06-10 — single-host live-verified, legacy model deleted; cross-machine verification tracked under M5)
- [ ] M5 — Hardening: cross-component auth (console WS, client WS), reconnection, observability (owner: unassigned, status: not started)

## Decisions

Each line is a settled choice no agent should reopen without flagging here.

- 2026-06-08 — Dev lab uses the self-hosted **Claude Agent SDK for Python**, not Managed Agents — must run uninterrupted on the owned Pi 5 with local compute.
- ~~2026-06-08 — Extension clients expose their capabilities as **MCP servers**; the lab's agent connects to them as tools.~~ **Superseded 2026-06-10** by the reversed-connection decision below; MCP survives at the *agent* boundary (lab-local SDK tools), not as the client wire.
- 2026-06-08 — The **GitHub repo is the source of truth** between the lab and other machines. *(Amended 2026-06-10: it is no longer the code-transport for platform clients — they receive the working tree via manifest sync — but remains source of truth for landing work.)*
- 2026-06-10 — **Platform clients dial the lab** (outbound WebSocket to `dev-lab web`), announce a capability list on connect, and stay connected: presence = registry, shown per project in the console. Code reaches a client via **manifest sync** (content-hash manifest, delta file transfer over the same socket) into a **maintained per-project mirror**, so the *uncommitted working tree* can be tested without a GitHub round-trip; after a run the client reports the result plus changed files. The agent reaches clients through **lab-local SDK tools** (`list_clients`, `run_on_client`) — MCP remains the agent-facing contract; the client wire is a lab-owned WS protocol. Rationale: reachability (clients are NAT-ed laptops; the Pi is the one stable address), discovery for free, and mid-session testability. Replaces the SSE `EXTENSIONS=name=url` wiring.
- 2026-06-08 — **Python + per-component venvs** for the toolchain.
- 2026-06-08 — Default model is **`claude-opus-4-8`** with adaptive thinking.
- 2026-06-10 — Platform clients share a scaffold: **`extensions/platform-client`** owns the capability-independent parts; each client is only its capability layer. Component naming settled on **"platform client"**. *(Amended same day by the reversed-connection decision: the scaffold's contents shift from throwaway-checkout runner + FastMCP serving to manifest sync + the dial-the-lab runtime.)*
- 2026-06-09 — Model is **selectable per project** in the web console (chosen at clone time, switchable mid-chat like the CLI's `/model`): stored on the project row (NULL = lab default), the switch drops the cached session so the next turn rebuilds with the new model while the resumed conversation continues. The lab default (`MODEL` env, else `claude-opus-4-8`) is the fallback; selectable ids live in `config.KNOWN_MODELS`.
- 2026-06-08 — The lab authenticates via a **Claude subscription** through a one-time interactive `claude` login (credentials persist in `~/.claude` and auto-refresh), **not** an API key or a token in `.env`; `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` must stay unset (they override it and bill the API).
- 2026-06-08 — Agent SDK package is **`claude-agent-sdk`** (0.2.x, Python). The agent runs with `permission_mode=bypassPermissions` and a workspace-scoped tool set (Read/Write/Edit/Glob/Grep/Bash); the **lab owns commits**, the agent is told not to touch git. Runtime requires the Claude Code CLI on the host.
- 2026-06-08 — M2 instruction intake is a **file-backed job queue** (`pending/running/done/failed` dirs); the supervisor (`dev-lab serve`) drains it one job at a time, survives per-job errors, and requeues in-flight jobs after a crash. Runs as a `systemd` unit (`deploy/dev-lab.service`, `Restart=always`, start-on-boot). The future chat client enqueues into this same queue.
- ~~2026-06-08 — The first extension client (`extensions/macos-build-test`) is a **FastMCP server over SSE** exposing `run_tests` / `build` tools; the lab wires extensions in via `EXTENSIONS=name=url,...`.~~ **Superseded 2026-06-10** by the reversed-connection model (clients dial the lab; `EXTENSIONS` wiring retired).
- 2026-06-08 — The chat client has an **interactive session mode** (`chat-client chat`): each connection gets a persistent `chat/<ts>` branch and the agent conversation is **resumed** across turns (SDK `resume`), so follow-ups build on prior work instead of each message making a fresh branch. `submit` stays for independent fire-and-forget jobs. A single lab lock serializes all agent runs (sessions + queue) over the one working clone. Live activity streams `agent_message` + `tool_use` events.
- 2026-06-08 — Control transports: **chat/UI ↔ lab over WebSocket** (`dev-lab serve` hosts a WebSocket control surface; clients submit instructions and receive a live event stream via an in-process event bus). *(Amended 2026-06-10: platform clients also use WebSocket — dialing in to the lab; the extensions' HTTP+SSE is retired.)*
- 2026-06-08 — Durable runtime data (run history, and future chat logs) is stored in **SQLite with a migration runner** (`dev_lab/db.py`, append-only migrations keyed on `PRAGMA user_version`) — not ad-hoc files. The job **queue** stays a filesystem work-state by design (atomic-rename claiming); SQLite is for records/logs/history.
- 2026-06-08 — **v2 redesign — web console.** The primary surface is now a **FastAPI web app** (`dev-lab web`): a login page (multi-user accounts, scrypt + signed session cookie), a project list, and per-project chat in the browser. The CLI `serve`/`chat-client`/queue remain as the older single-project surface (secondary).
- 2026-06-08 — **Landing work back to the base branch.** A chat session commits to a `chat/<ts>` branch; a **merge → base** action (`POST /api/projects/{id}/merge`, button in the web UI) merges it into the project's default branch locally (aborts on conflict, restores the chat branch). This closes the "can create a branch but can't merge it back" gap. Pushing to the remote is still a separate open item.
- 2026-06-08 — **Multi-project `labs/`.** Projects live as separate git clones under `labs/` (auto-discovered, or created by cloning a URL). Each is its own Claude agent/context (own clone/cwd/branch/resumed session) — which removes the single-working-clone limitation; turns within a project serialize, across projects run concurrently. All lab state lives under `<labs>/.dev-lab/` (SQLite + cookie secret).
- 2026-06-08 — **Frontend is no-build vanilla JS** served as static files; assistant output rendered as markdown (`marked`) + mermaid, sanitized with `DOMPurify`; libs vendored under `static/vendor/` (offline-friendly, no Node toolchain).
- 2026-06-08 — Documentation follows the memention.net **Context Cards** + **Shared Project Plan** patterns; feature-first / two-tier deferred until code exists.

- 2026-06-10 — **Strict per-user project lists**: each project is owned by its creator (`projects.owner_id`, migration #8); non-supers see only their own projects (REST + console WS + event stream all gated; foreign ids 404), super-users see all, ownerless rows (auto-discovered checkouts) are super-only.
- 2026-06-10 — **Per-lab client-mirror namespace**: the lab mints a stable id (`<labs>/.dev-lab/lab-id`), sends it in `hello_ok`, and clients key mirrors `<mirrors>/<lab-id>/<project>` — several labs can share one client machine without same-named projects colliding (no id → flat legacy layout).

- 2026-06-10 — **Per-project agent config in the console** ("agent" tab): an extra system-prompt append and MCP servers live on the project row (migration #9, validated JSON), skills live in the repo under `.claude/skills/` (committed like code, `skills="all"` on the SDK options).

## Current state / handoff

**M0–M4 done** (branch `m0-skeleton`). **v2 web console done** (branch
`v2-web-console`): multi-project `labs/` backend, FastAPI app with multi-user
auth, and a vanilla web UI (login + project sidebar + chat with markdown/mermaid)
— live-verified end-to-end (login → clone project → cookie-authed WebSocket chat
→ streamed tool+markdown+mermaid → commit → per-project persistence). New files:
`projects.py`, `auth.py`, `web.py`, `static/`; new command `dev-lab web`; SQLite
migrations #2 (projects/messages) and #3 (users). **Merged to `main`** (2026-06-08,
fast-forward) — `main` now carries the full history (M0–M4 + v2).

Repo has four `src`-layout components — `dev-lab/`, `chat-client/`,
`extensions/platform-client/`, `extensions/macos-build-test/` — each with its
own venv and `pyproject.toml`. A root `Makefile` drives per-component venvs
(`make setup|test|lint|fmt|clean`); lint is ruff, tests are pytest.

**dev-lab (M1):** `workspace.py` (git wrapper), `agent.py` (Claude Agent SDK
loop — `bypassPermissions`, workspace-scoped tools), `lab.py` (`run_once`:
clean-tree check → work branch → agent edits → one commit), and a real CLI
(`dev-lab "<instruction>" --repo <path>`). Auth is the `claude` login; config
guards against API-key billing. GitHub auth is per project (each project's token
lives on its `projects` row, entered in the web console), not a global env var.

Verified: `make test`/`lint` pass (11 tests). The git orchestration and the
"one instruction → one commit" / "no changes → no commit" / "refuse dirty tree"
logic are unit-tested with a fake agent (no network). **Live-verified** against a
throwaway repo with subscription auth (`ANTHROPIC_API_KEY` unset → `claude`
login): the agent edited inside the workspace and the lab produced a single
commit on a `lab/…` branch (~$0.13, 2 turns). Note: `agent.py` must use Claude
Code's `claude_code` system-prompt **preset with `append`** — a bare custom
`system_prompt` string drops the engine's working-directory context and the
agent writes files outside the clone.

**dev-lab (M2):** `queue.py` (file-backed `FileQueue` — `pending/running/done/
failed`, atomic claim, crash recovery), `supervisor.py` (`serve`: recover →
drain one job at a time, fail-and-continue on errors), and CLI subcommands
`run` / `serve` / `submit`. `deploy/dev-lab.service` (+ `deploy/README.md`) runs
it under systemd (`Restart=always`, boot start, as the `claude`-login user).
Verified: 17 dev-lab tests; CLI smoke (submit enqueues; serve idles and stops
cleanly on SIGTERM). Not yet run on real Pi hardware.

**dev-lab + chat-client (M3):** `events.py` (in-process `EventBus`), `server.py`
(WebSocket control surface — `submit` enqueues into the file queue; bus events
forwarded to clients), supervisor publishes `job_running`/`agent_message`/
`job_done`/`job_failed` and streams agent text via an `on_event` hook through
`run_once`/`run_task`. `dev-lab serve` now runs supervisor + WebSocket server
together (`--host`/`--port`). The `chat-client` package talks over WebSocket:
`chat-client submit "<instr>"` (stream until the job finishes) and
`chat-client listen`. Verified: 34 tests (incl. a real WebSocket round-trip and
bus-event publishing); lint clean. **Live-verified**: `chat-client submit` over
WebSocket streamed `ack → job_running → agent_message(s) → job_done`, a real
commit landed, and the run was recorded in SQLite (~$0.13).

**extensions/platform-client (scaffold, 2026-06-10):** the capability-independent
parts of an extension MCP server, extracted from macos-build-test so a new
platform client is only its tool definitions: `run_in_checkout`/`CommandResult`
(clone ref → run command → bounded result) and `extension_cli` (the standard
`serve --host --port` SSE entry point). macos-build-test now depends on it
(installed by `make setup`; relative-path deps aren't expressible in pyproject).
How to create a new client: cards/extension-clients.md. Naming is settling on
**"platform client"** for these components.

**extensions/macos-build-test (M4):** a FastMCP server over SSE
(`macos-build-test serve --host --port`) with `run_tests` / `build` tools
(via the platform-client scaffold: clone → checkout ref → run command → return
result). The lab reaches it via `EXTENSIONS` env; `agent.build_agent_options`
turns each into an `{type: sse, url}` MCP server plus an `mcp__<name>` allowed
tool. Verified: 41 tests; lint clean. **Live-verified end-to-end**: with the extension
server running and `dev-lab serve` wired via `EXTENSIONS`, a chat instruction
drove the agent to autonomously call the remote `run_tests` tool — confirmed by
the extension log (`Processing request of type CallToolRequest`), an
absolute-path side-effect file written by the test command, and the unique
stdout token flowing back into the chat report.

**M6 platform clients v1 (2026-06-10):** the reversed-connection model is
implemented and end-to-end tested (real uvicorn + real websocket in
`dev-lab/tests/test_clients_live.py`). `platform_client.manifest` (content-hash
manifests, deltas, changed-report), `platform_client.runtime` +
`platform-client connect` CLI (dial, hello, sync mirror, run, report, reconnect
loop), `dev_lab/clients.py` (`ClientRegistry`: presence, task dispatch, file
serving) + `/ws/client` and `/api/clients` in web.py (optional `CLIENT_TOKEN`),
agent tools `mcp__lab` `list_clients`/`run_on_client`/`fetch_from_client`
(in-process SDK MCP server bound to the project tree), and a connected-clients
section in the console sidebar. `run_on_client` takes `preserve` glob patterns
so build artifacts/caches survive between runs (default stays clean-sync);
`fetch_from_client` pulls mirror files (e.g. built binaries) back into the
lab's working tree. **Live-verified 2026-06-10 on a single host** (real
`dev-lab web` + real `platform-client connect` + a real agent chat turn: the
agent autonomously called `list_clients` → `run_on_client` → `fetch_from_client`,
the artifact landed in the lab tree and was committed; wrong-token hello
rejected with 1008; registry emptied on client kill). Cross-machine (Mac ↔ Pi)
verification still open.
**Deployed on the real Pi (2026-06-10):** the web console runs at
`https://home.memention.net/dev-lab/` — systemd unit `deploy/dev-lab-web.service`
(loopback bind), Apache terminating TLS and proxying the `/dev-lab/` path
prefix incl. both WebSocket endpoints (`deploy/apache-dev-lab.conf`); the SPA
derives its path prefix from the page URL. `CLIENT_TOKEN` gates `/ws/client`.

**Console features (2026-06-10):** file browser grew source tabs for
**client mirrors** (browse a connected client's mirror, fetch a file back,
remove the mirror — wire frames `mirror`/`clean` next to `task`/`fetch` in
`dev_lab/clients.py`); repo actions: fetch/pull/push, **reset** (discard
uncommitted changes), **rebase-on-base** (replaces merge-base→branch; on
conflict the UI offers to hand resolution to the agent in chat),
**download zip**, **remove project** (deletes clone + history, cleans client
mirrors); **uploads** — into the repo (auto-committed; a dirty tree would
block the next session) and chat attachments via `.lab-uploads/` (excluded
from commits via `.git/info/exclude` and from mirrors via manifest
`DEFAULT_IGNORES`); **blank projects** (git-init by name, no remote);
favicon. Versions: dev-lab 0.7.0, platform-client 0.3.0.

**Legacy model deleted (2026-06-10):** `extensions/macos-build-test`, the
scaffold's `run_in_checkout`/`extension_cli`, and the lab's `EXTENSIONS` env +
SSE wiring are gone — a capability machine is now just
`platform-client connect --capability …`.

Next: **cross-machine verification** (a `platform-client connect` from the Mac
against the Pi lab over `wss://…/dev-lab/ws/client`), then **M5** — hardening:
`Secure` cookie flag, auth for the old CLI `serve` socket (or retire it),
observability (health endpoint, structured logs, run history in the UI).

## Open questions

- The console WS is cookie-authed and `/ws/client` token-gated behind TLS; the old CLI `serve` socket is still unauthenticated/loopback — auth it or retire it with the CLI surface (M5).
- Network topology: resolved for the reference deployment — the Pi is reachable via Apache TLS at home.memention.net; clients dial `wss://…/dev-lab/ws/client`.
- Auth model across components (client → lab WS, chat → lab) — v1 has an optional shared `CLIENT_TOKEN`; mTLS / Tailscale ACLs are M5.
- Manifest sync caps per-file size and hashes the whole tree per task — fine for small repos; revisit (mtime cache, chunked transfer) if trees grow.
- Multiple chat clients share one working clone — only one session/job runs at a time (lab lock), and concurrent sessions on different branches aren't isolated. Multi-session concurrency would need git worktrees or per-session clones.
- Whether to use the SDK's session resume (`resume`/`session_id`) so a single long task survives a mid-run crash — currently only the job queue is durable (a crashed job is requeued and re-run from scratch, on a fresh branch).
- Secrets on the Pi: GitHub tokens are per project in SQLite (plaintext on the project row), `CLIENT_TOKEN` in a chmod-600 `.env` — fine for a single-owner box, revisit if that changes.
- The systemd service must run as the user who ran `claude` login (credentials are user-scoped in `~/.claude`), or set `CLAUDE_CONFIG_DIR` to that user's config dir.
- Confirm the Claude plan tier: Opus (`claude-opus-4-8`) needs a **Max** plan; subscription Agent SDK usage draws from a separate monthly credit pool (effective 2026-06-15) and is personal-use only under Anthropic's ToS.
