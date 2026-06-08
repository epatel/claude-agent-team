# control-transports

Decision: WebSocket for the chat/UI control surface; HTTP+SSE for extension-client MCP servers.

## Decision

- **Chat / UI ↔ lab: WebSocket.** `dev-lab serve` hosts a WebSocket control
  surface (`--host`/`--port`, default `ws://127.0.0.1:8765`). Clients submit
  instructions and receive a live, bidirectional event stream.
- **Extension MCP servers: HTTP+SSE.** Extension clients expose their MCP servers
  over the HTTP+SSE transport; the lab's agent connects to them as remote tools.

## Why

- **WebSocket for chat/UI** — full duplex over one long-lived connection fits an
  interactive control surface that both sends commands and streams output (agent
  text + job lifecycle) with low latency.
- **HTTP+SSE for MCP** — a standard MCP transport that works cleanly across hosts
  and through proxies/firewalls, and needs no persistent client→server socket
  from the lab to each extension.

## How (control surface)

- An in-process `EventBus` (`dev_lab/events.py`) fans lab events out to each
  connected client; the supervisor publishes `job_running` / `agent_message` /
  `job_done` / `job_failed`.
- A client `submit` enqueues into the durable file queue, so WebSocket control
  and crash-safe persistence coexist (the socket is the live channel; the queue
  is the system of record for pending work).

## Open

- The control surface is currently unauthenticated and bound to loopback. Auth
  (token / Tailscale / mTLS) before exposing it off-host is M5 hardening.

## Revisit if

- A browser UI needs plain HTTP/SSE fallback, or a firewalled client can't hold a
  WebSocket — add an HTTP+SSE control path alongside the WebSocket one.
