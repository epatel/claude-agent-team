# mcp-for-extensions

Decision (reframed 2026-06-10): MCP is the **agent-facing** contract for remote capabilities; the wire to platform clients is a lab-owned WebSocket protocol.

## Decision

Original (2026-06-08): each extension client runs an **MCP server** the lab
dials over HTTP+SSE, and capabilities appear as `mcp__<name>` tools.

**Reframed 2026-06-10**: platform clients now dial the lab
(cards/extension-clients.md), so MCP-over-SSE no longer fits the direction of
connection. What the decision protected survives at the agent boundary: the
agent still sees capabilities as **typed MCP tools** — now lab-local SDK tools
(`list_clients`, `run_on_client`) that route over the client WebSocket. The
agent never speaks the client wire.

## Why MCP at the agent boundary

- **Native to the stack** — the Claude Agent SDK speaks MCP; capabilities are
  first-class tools with typed schemas.
- **Stable contract** — the client wire can evolve (sync format, transport)
  without the agent's tool surface changing.

## Why a custom wire to clients (what changed)

The original card rejected a custom WebSocket protocol as "reinventing MCP".
The new requirements are exactly the ones MCP's request/response tool model
doesn't cover — **presence** (who's online), **capability announcement**,
**stateful synced mirrors**, **post-run diffs**, and an **inbound-only
connection from NAT-ed machines** — so the custom protocol now earns its keep.

## Alternatives considered

- **Keep MCP-over-SSE + Tailscale for reachability** — preserves the original
  decision but requires VPN setup on every client and still gives no
  presence/registry or warm sync. Rejected.
- **Job queue (Redis/NATS)** — still rejected for v1: adds a broker; the
  WS dispatch covers current fan-out needs.

## Amendment (2026-06-12): real MCP servers ON clients, tunneled

Clients can now run **actual stdio MCP servers** (e.g. Playwright's browser
MCP) and the lab forwards `tools/list`/`tools/call` to them inside the
lab-owned wire (`mcp`/`mcp_result` frames) — see cards/client-mcp-servers.md.
This is not a reversal of this decision but its completion: MCP remains the
tool contract at *both* ends (agent tools on the lab, an MCP session on the
client), while the lab-owned WebSocket keeps providing what raw MCP transport
couldn't — presence, announcement, and the inbound-only connection.

## Revisit if

- MCP standardizes a server-initiated / reverse-connection transport — the
  tunnel frames could become a pass-through of that instead.
- We need durable offline job dispatch (queue behind the same SDK tools).
