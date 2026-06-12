# claude-agent-team

An always-on autonomous development lab: a Claude Agent SDK client running
uninterrupted on a Raspberry Pi 5 that does real dev work on git repos,
steered from a browser (per-project chat), and extended by platform clients on
other machines (e.g. macOS build/test) that dial in over WebSocket. Toolchain
is Python with per-component venvs; the owner is a single developer/small team.

The shared plan lives in `project-plan.md` — read it first for goal, milestones,
settled decisions, open questions, and the current state. `README.md` has the
repo layout; `QUICKSTART.md` has how to run it.

## Cards

Lazy-loaded reference material. Read a card when the trigger matches; cards are
self-contained.

### Architecture
- [architecture](cards/architecture.md) — cross-component work, data flow, onboarding, "how do the pieces fit"

### Domains
- [web-console](cards/web-console.md) — the browser UI, login, projects in labs/, talking to a project (v2 primary surface)
- [known-models](cards/known-models.md) — the selectable model list (KNOWN_MODELS): how to check Anthropic's current lineup and update it
- [dev-lab](cards/dev-lab.md) — the autonomous agent loop, sessions, anything running on the Pi
- [chat-client](cards/chat-client.md) — the older CLI control surface (secondary to the web console)
- [extension-clients](cards/extension-clients.md) — platform clients: remote build/test, capability providers, manifest sync, the client wire protocol
- [client-mcp-servers](cards/client-mcp-servers.md) — tunneled MCP: a browser (or any stdio MCP server) on a client machine, driven by the agent over the dialed-in socket
- [repo-sync](cards/repo-sync.md) — git, GitHub, branches, how work lands
- [deployment](cards/deployment.md) — running on the Pi 5, systemd, venvs, secrets, restarts

### Decisions
- [claude-agent-sdk-self-hosted](cards/claude-agent-sdk-self-hosted.md) — why self-hosted SDK and not Managed Agents
- [subscription-auth](cards/subscription-auth.md) — auth via Claude subscription not API key, OAuth token, the API-key trap
- [sqlite-runtime-data](cards/sqlite-runtime-data.md) — durable runtime data (run history, chat logs) in SQLite with migrations
- [control-transports](cards/control-transports.md) — WebSocket for chat/UI control and for platform clients dialing in
- [no-double-submit](cards/no-double-submit.md) — guard buttons against double-tap (busy flag + disable + spinner); adding any button
- [mcp-for-extensions](cards/mcp-for-extensions.md) — why capabilities surface to the agent as MCP tools
- [python-venvs](cards/python-venvs.md) — Python + per-component venv layout
