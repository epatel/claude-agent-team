# extension-clients

**Platform clients** — capability providers on other machines that **dial in to the lab** over WebSocket, announce what they can do, and run commands in a synced mirror of a project's working tree. (Settled name: *platform client*; the directory is still `extensions/`.)

## Responsibility

- Connect outbound to the lab's web app (`/ws/client`), authenticate (optional
  shared `CLIENT_TOKEN`), and announce `{name, platform, capabilities}`.
- Stay connected: **presence is the registry** — the console and the agent see
  exactly the clients that are online now.
- Maintain a **per-project mirror** of the lab's working tree via manifest
  sync, so the *uncommitted* mid-session state is testable without a GitHub
  round-trip.
- Execute dispatched commands in the mirror and report
  `{ok, returncode, stdout, stderr}` **plus the files the run changed**.

## Flow

```mermaid
sequenceDiagram
    participant C as Platform client (macOS)
    participant LAB as dev-lab (Pi)
    participant A as Agent
    C->>LAB: WS connect + hello {name, platform, capabilities}
    LAB-->>LAB: registry: client online (console shows it)
    A->>LAB: SDK tool run_on_client(client, command)
    LAB->>C: task {id, project, command, manifest}
    C->>LAB: need [stale/missing paths]
    LAB->>C: file data (delta only)
    C->>C: run command in warm mirror
    C->>LAB: result {ok, output, changed files}
    LAB-->>A: tool result
```

## Key pieces

- **Reversed connection** — clients are NAT-ed laptops that roam and sleep; the
  Pi is the one stable address. Outbound-only from clients means no inbound
  ports or per-client auth surface. The wire is a lab-owned JSON protocol over
  WebSocket (see `platform_client/runtime.py` and `dev_lab/clients.py`).
- **MCP stays at the agent boundary** — the agent sees `list_clients` /
  `run_on_client` as lab-local SDK tools; it never speaks to clients directly.
- **Manifest sync, not git** — the lab sends a content-hash manifest of the
  project tree (default ignores: `.git`, `.venv`, `__pycache__`, …); the client
  requests only changed files, deletes ones that vanished, and keeps the mirror
  warm between runs. Identity of "what ran" is the manifest hash, not a sha.
- **Changed-files report** — the client snapshots its mirror manifest before a
  run and diffs after, so generated/modified files are visible per run.
- **Preserving artifacts** — by default the sync deletes mirror files not in
  the source tree (clean runs), which also wipes the previous run's build
  output. `run_on_client` takes `preserve` glob patterns (fnmatch — `*`
  crosses `/`) for artifacts/caches that should survive between runs
  (incremental builds). Preserved files are excluded from the changed-report
  unless the run actually touches them.
- **Fetching artifacts back** — `fetch_from_client(client, paths)` pulls files
  from the client's mirror into the lab's project working tree (so a built
  binary or report lands where the agent — and the next commit — can see it;
  .gitignore what shouldn't land in history). Oversized files (> the manifest
  size cap) are refused per path.
- **Per-lab mirror namespace** — `hello_ok` carries the lab's stable id
  (minted once into `<labs>/.dev-lab/lab-id`); the client keys mirrors as
  `<mirrors>/<lab-id>/<project>`, so multiple labs can share one client
  machine without same-named projects colliding. A lab that sends no id (older
  lab) gets the flat legacy layout.
- **Inspecting and cleaning mirrors** — the lab can ask a client what its
  mirror of a project holds (`mirror` → manifest, no content; the web console's
  file-browser source tabs are built on this) and delete a project's mirror
  outright (`clean` — exposed as "remove mirror" in the console; the client
  stays connected and re-syncs on its next run).
- **Tunneled MCP servers** — a client can run local stdio MCP servers
  (`--mcp name="command"`) and the lab tunnels tool calls over the same
  dialed-in socket; the agent drives a real browser on the client and sees
  the screenshots. The full design is cards/client-mcp-servers.md.
- **Shared scaffold** — `extensions/platform-client` (package
  `platform_client`) owns manifest sync, the runtime, and the
  `platform-client connect --lab <url>` CLI. A new client is configuration
  (name + capabilities) more than code.

## History

v1 (M4, superseded 2026-06-10): clients were FastMCP servers over HTTP+SSE the
lab dialed via `EXTENSIONS=name=url`; code transport was a fresh git clone per
call. Reversed because reachability, discovery, and mid-session testability all
pushed the same way. The old model was **deleted 2026-06-10**
(`extensions/macos-build-test`, the scaffold's `run_in_checkout`/`extension_cli`,
and the lab's `EXTENSIONS` env + SSE wiring) — a new capability machine is just
`platform-client connect --capability …`.

## Not covered here

The MCP rationale (cards/mcp-for-extensions.md), branch/commit conventions
(cards/repo-sync.md), and the agent loop (cards/dev-lab.md).
