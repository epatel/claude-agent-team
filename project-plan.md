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
- [ ] M1 — Minimal dev lab: Claude Agent SDK loop working in a local git clone; one instruction → one commit (owner: unassigned, status: not started)
- [ ] M2 — Run uninterrupted on Pi 5: systemd service, restart-on-crash, secrets, session persistence (owner: unassigned, status: not started)
- [ ] M3 — Chat client: control surface to send instructions, stream agent output, start/stop/steer (owner: unassigned, status: not started)
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
- 2026-06-08 — Documentation follows the memention.net **Context Cards** + **Shared Project Plan** patterns; feature-first / two-tier deferred until code exists.

## Current state / handoff

**M0 done.** The repo has three components — `dev-lab/`, `chat-client/`,
`extensions/macos-build-test/` — each a `src`-layout Python package with its own
`pyproject.toml`, a runnable stub entry point, and a smoke test. A root
`Makefile` drives per-component venvs (`make setup|test|lint|fmt|clean`); lint is
ruff, tests are pytest. `.env` handling lives in `dev-lab/config.py` (loads a
gitignored `dev-lab/.env`, validates `GITHUB_TOKEN`, and refuses to start if
`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` is set so it can't silently bill the
API). Claude auth is a one-time `claude` login (credentials in `~/.claude`, no
token in `.env`) — see `cards/subscription-auth.md`. Verified: `make
setup`/`test`/`lint` all pass (6 tests), all three stubs run, venvs and `.env`
are gitignored. Nothing committed yet.

Next agent picks up **M1** — implement the minimal dev-lab agent loop. First
resolve the open question on the exact Claude Agent SDK package name/version,
add it to `dev-lab/pyproject.toml`, then drive: one instruction → one commit in a
local git clone.

## Open questions

- What transport carries the chat-client ↔ lab control surface — WebSocket, HTTP+SSE, or reuse an MCP server on the lab side?
- Network topology: are lab and extension clients on the same LAN, or reached over a VPN/Tailscale for off-LAN extensions?
- Auth model across components (lab → extension MCP, chat → lab) — mTLS, bearer tokens, Tailscale ACLs?
- How are extension clients discovered/registered by the lab (static config vs a registry)?
- How is agent session state persisted across Pi restarts?
- Secrets management on the Pi for the GitHub token (Claude auth is the one-time `claude` login stored in `~/.claude`, no secret in `.env`).
- The systemd service must run as the user who ran `claude` login (credentials are user-scoped in `~/.claude`), or set `CLAUDE_CONFIG_DIR` to that user's config dir.
- Confirm the Claude plan tier: Opus (`claude-opus-4-8`) needs a **Max** plan; subscription Agent SDK usage draws from a separate monthly credit pool (effective 2026-06-15) and is personal-use only under Anthropic's ToS.
- Confirm the exact PyPI package name/version for the Claude Agent SDK before M1.
