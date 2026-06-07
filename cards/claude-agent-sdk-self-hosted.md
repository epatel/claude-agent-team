# claude-agent-sdk-self-hosted

Decision: the dev lab runs the self-hosted Claude Agent SDK for Python, not Anthropic Managed Agents.

## Decision

Run the agent loop on the Pi using the **Claude Agent SDK for Python**
(`pip install claude-agent-sdk`), self-hosted. Do **not** use Managed Agents
(the Anthropic-hosted agent runtime).

## Why

- **Uninterrupted, owned hardware** — the core requirement is an always-on lab on
  a Pi we control. Self-hosting keeps the loop and compute on that hardware.
- **Local tool execution** — filesystem, git, and shell run directly on the Pi;
  no per-session cloud container needed.
- **MCP-native** — the SDK connects to MCP servers, which is exactly how
  extension clients expose capabilities.

## Alternatives considered

- **Managed Agents (Anthropic-hosted)** — Anthropic runs the loop and a
  per-session container. Great for hosted/stateful agents, but it puts the
  runtime off our hardware and is session-oriented rather than a single
  long-lived owned process. Rejected for this project's "always-on Pi" framing.
- **Raw Messages API + hand-rolled loop** — maximum control but we'd rebuild the
  tool loop, MCP wiring, and session handling the Agent SDK already provides.

## Revisit if

- Local compute on the Pi proves insufficient and we want Anthropic-hosted
  containers for heavy work.
- We need persisted, versioned multi-agent configs that Managed Agents provides
  out of the box.

## To confirm

Exact current package name/version of the Claude Agent SDK for Python before M1.
