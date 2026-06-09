# control-transports

Decision: WebSocket everywhere — the chat/UI control surface and platform clients both hold a WS to the lab. (HTTP+SSE for extension MCP servers retired 2026-06-10.)

## Decision

- **Chat / UI ↔ lab: WebSocket.** `dev-lab serve` hosts a WebSocket control
  surface (`--host`/`--port`, default `ws://127.0.0.1:8765`). Clients submit
  instructions and receive a live, bidirectional event stream.
- **Platform clients ↔ lab: WebSocket, dialed by the client.** Clients connect
  outbound to the web app's `/ws/client`, announce capabilities, and receive
  task dispatch / send results + sync traffic over the same socket.
  *(Replaced the original HTTP+SSE MCP transport on 2026-06-10 — see
  cards/extension-clients.md for why the connection reversed.)*

## Why

- **WebSocket for chat/UI** — full duplex over one long-lived connection fits an
  interactive control surface that both sends commands and streams output (agent
  text + job lifecycle) with low latency.
- **WebSocket for platform clients** — the persistent connection *is* the
  presence/registry signal, and outbound-only fits NAT-ed client machines; only
  the Pi needs to be reachable.

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
