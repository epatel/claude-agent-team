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
- [x] M2 — Run uninterrupted on Pi 5: systemd service, restart-on-crash, durable job queue (owner: agent, status: done 2026-06-08; verified locally, not yet on real Pi hardware)
- [x] M3 — Chat client: control surface to send instructions, stream agent output (owner: agent, status: done 2026-06-08; live agent streaming not yet exercised)
- [ ] M4 — First extension client: macOS build/test MCP server; lab connects and uses it as tools (owner: unassigned, status: not started)
- [ ] M5 — Hardening: cross-component auth, reconnection, observability, multi-extension discovery (owner: unassigned, status: not started)

## Decisions

Each line is a settled choice no agent should reopen without flagging here.

- 2026-06-08 — Dev lab uses the self-hosted **Claude Agent SDK for Python**, not Managed Agents — must run uninterrupted on the owned Pi 5 with local compute.
- 2026-06-08 — Extension clients expose their capabilities as **MCP servers**; the lab's agent connects to them as tools (chosen over custom RPC and job-queue).
- 2026-06-08 — The **GitHub repo is both source of truth and the synchronization substrate** between the lab and extension clients (the lab pushes a branch; extensions check out that commit to build/test).
- 2026-06-08 — **Python + per-component venvs** for the toolchain.
- 2026-06-08 — Default model is **`claude-opus-4-8`** with adaptive thinking.
- 2026-06-08 — The lab authenticates via a **Claude subscription** through a one-time interactive `claude` login (credentials persist in `~/.claude` and auto-refresh), **not** an API key or a token in `.env`; `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` must stay unset (they override it and bill the API).
- 2026-06-08 — Agent SDK package is **`claude-agent-sdk`** (0.2.x, Python). The agent runs with `permission_mode=bypassPermissions` and a workspace-scoped tool set (Read/Write/Edit/Glob/Grep/Bash); the **lab owns commits**, the agent is told not to touch git. Runtime requires the Claude Code CLI on the host.
- 2026-06-08 — M2 instruction intake is a **file-backed job queue** (`pending/running/done/failed` dirs); the supervisor (`dev-lab serve`) drains it one job at a time, survives per-job errors, and requeues in-flight jobs after a crash. Runs as a `systemd` unit (`deploy/dev-lab.service`, `Restart=always`, start-on-boot). The future chat client enqueues into this same queue.
- 2026-06-08 — Control transports: **chat/UI ↔ lab over WebSocket** (`dev-lab serve` hosts a WebSocket control surface; clients submit instructions and receive a live event stream via an in-process event bus). **Extension MCP servers use HTTP+SSE.**
- 2026-06-08 — Durable runtime data (run history, and future chat logs) is stored in **SQLite with a migration runner** (`dev_lab/db.py`, append-only migrations keyed on `PRAGMA user_version`) — not ad-hoc files. The job **queue** stays a filesystem work-state by design (atomic-rename claiming); SQLite is for records/logs/history.
- 2026-06-08 — Documentation follows the memention.net **Context Cards** + **Shared Project Plan** patterns; feature-first / two-tier deferred until code exists.

## Current state / handoff

**M0–M3 done** (committed on branch `m0-skeleton`).

Repo has three `src`-layout components — `dev-lab/`, `chat-client/`,
`extensions/macos-build-test/` — each with its own venv and `pyproject.toml`. A
root `Makefile` drives per-component venvs (`make setup|test|lint|fmt|clean`);
lint is ruff, tests are pytest.

**dev-lab (M1):** `workspace.py` (git wrapper), `agent.py` (Claude Agent SDK
loop — `bypassPermissions`, workspace-scoped tools), `lab.py` (`run_once`:
clean-tree check → work branch → agent edits → one commit), and a real CLI
(`dev-lab "<instruction>" --repo <path>`). Auth is the `claude` login; config
supplies `GITHUB_TOKEN` and guards against API-key billing.

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
bus-event publishing); lint clean; no-credit smoke (lab up, client connects,
submit enqueues over the socket). **Live agent-output streaming not yet
exercised** (needs a credit-spending run).

Next: **M4** — first extension client: a macOS build/test **MCP server over
HTTP+SSE** that the lab connects to as tools.

## Open questions

- The WebSocket control surface is currently unauthenticated and loopback-bound — add auth (token / Tailscale / mTLS) before exposing it off-host (M5 hardening).
- Network topology: are lab and extension clients on the same LAN, or reached over a VPN/Tailscale for off-LAN extensions?
- Auth model across components (lab → extension MCP, chat → lab) — mTLS, bearer tokens, Tailscale ACLs?
- How are extension clients discovered/registered by the lab (static config vs a registry)?
- Whether to use the SDK's session resume (`resume`/`session_id`) so a single long task survives a mid-run crash — currently only the job queue is durable (a crashed job is requeued and re-run from scratch, on a fresh branch).
- Secrets management on the Pi for the GitHub token (Claude auth is the one-time `claude` login stored in `~/.claude`, no secret in `.env`).
- The systemd service must run as the user who ran `claude` login (credentials are user-scoped in `~/.claude`), or set `CLAUDE_CONFIG_DIR` to that user's config dir.
- Confirm the Claude plan tier: Opus (`claude-opus-4-8`) needs a **Max** plan; subscription Agent SDK usage draws from a separate monthly credit pool (effective 2026-06-15) and is personal-use only under Anthropic's ToS.
