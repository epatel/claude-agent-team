# mcp-for-extensions

Decision: extension clients expose their capabilities as MCP servers that the lab's agent consumes as tools.

## Decision

Each extension client (e.g. the macOS build/test host) runs an **MCP server**.
The dev lab's Claude Agent SDK loop connects to it and the capabilities appear as
tools (`build`, `run_tests`, …) the agent can call.

## Why

- **Native to the stack** — the Claude Agent SDK already speaks MCP, so remote
  capabilities become first-class tools with no custom plumbing.
- **Typed, language-agnostic contract** — each capability is a tool with a JSON
  schema; an extension can be written in any language.
- **Composable** — adding a new machine/capability is "run another MCP server and
  point the lab at it," not a change to the lab's core.

## Alternatives considered

- **Custom RPC / WebSocket protocol** — maximum control, but we'd build tool
  schemas, routing, reconnection, and the tool-shaped interface the agent needs
  from scratch. Rejected as reinventing MCP.
- **Job queue (Redis/NATS)** — lab publishes build/test jobs, workers pull and
  report back. Good for fan-out and offline workers, but not natively tool-shaped
  and adds a broker to operate. Rejected for v1; reconsider if we need heavy
  fan-out or durable offline queues.

## Revisit if

- We need durable, offline, or massively-parallel job dispatch (consider adding a
  queue behind an MCP tool rather than replacing MCP).
- A capability genuinely doesn't fit the request/response tool model.
