# claude-agent-team

An always-on autonomous development lab: a Claude Agent SDK client running
uninterrupted on a Raspberry Pi 5 that does real dev work on a GitHub repo,
steered by a chat client, and extended by capability-providing clients on other
machines (e.g. macOS build/test) exposed as MCP servers. Toolchain is Python
with per-component venvs; the owner is a single developer/small team.

The shared plan lives in `project-plan.md` — read it first for goal, milestones,
settled decisions, and open questions. Reference it from any subagent prompt.
New here? `HANDOFF.md` is the quick "where are we, what's left, what to watch out
for" orientation.

## Cards

Lazy-loaded reference material. Read a card when the trigger matches; cards are
self-contained.

### Architecture
- [architecture](cards/architecture.md) — cross-component work, data flow, onboarding, "how do the pieces fit"

### Domains
- [dev-lab](cards/dev-lab.md) — the autonomous agent loop, sessions, anything running on the Pi
- [chat-client](cards/chat-client.md) — starting/steering the lab, control surface, streaming agent output
- [extension-clients](cards/extension-clients.md) — remote build/test, capability providers, MCP servers on other machines
- [repo-sync](cards/repo-sync.md) — git, GitHub, branches, how the lab and extensions share code
- [deployment](cards/deployment.md) — running on the Pi 5, systemd, venvs, secrets, restarts

### Decisions
- [claude-agent-sdk-self-hosted](cards/claude-agent-sdk-self-hosted.md) — why self-hosted SDK and not Managed Agents
- [subscription-auth](cards/subscription-auth.md) — auth via Claude subscription not API key, OAuth token, the API-key trap
- [sqlite-runtime-data](cards/sqlite-runtime-data.md) — durable runtime data (run history, chat logs) in SQLite with migrations
- [control-transports](cards/control-transports.md) — WebSocket for chat/UI control, HTTP+SSE for extension MCP servers
- [mcp-for-extensions](cards/mcp-for-extensions.md) — why extension capabilities are MCP servers
- [python-venvs](cards/python-venvs.md) — Python + per-component venv layout
