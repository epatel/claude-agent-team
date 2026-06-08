# Quickstart

How to run the three components — `dev-lab` (the lab), `chat-client`, and the
`macos-build-test` extension — locally for testing and in production. For
architecture see `CLAUDE.md`; for the project status see `HANDOFF.md`.

## Prerequisites

- **Python 3.11+**, **git**, **make**.
- **Claude Code CLI** + a **`claude` login** (the lab authenticates with your
  Claude *subscription*, not an API key). Opus needs a **Max** plan.
  ```sh
  npm install -g @anthropic-ai/claude-code     # or your platform's install
  claude                                        # complete login (SSH: press `c`, paste code back)
  ```
- A **GitHub token** for the lab (currently required by config; reserved for the
  push step — see the note in `HANDOFF.md`).
- **Do NOT export `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`** — they override
  subscription auth and the lab refuses to start.

## Install

Each component has its own venv. From the repo root:

```sh
make setup            # creates dev-lab/.venv, chat-client/.venv, extensions/macos-build-test/.venv
```

Entry points live at `<component>/.venv/bin/<name>`. The examples below call
those paths directly; alternatively `source dev-lab/.venv/bin/activate` to use the
bare `dev-lab` command.

---

## A. Test / local (one machine)

### A0. No credits — unit tests

```sh
make test             # 41 tests, no network, no credits
make lint
```

### A1. One-shot, no chat client (simplest live run)

Spends subscription credits and needs `claude` logged in.

```sh
# a throwaway repo for the lab to work in
REPO=$(mktemp -d); git -C "$REPO" init -q
git -C "$REPO" config user.email lab@example.com; git -C "$REPO" config user.name "Dev Lab"
echo "# demo" > "$REPO/README.md"; git -C "$REPO" add -A; git -C "$REPO" commit -q -m init

env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN GITHUB_TOKEN=dummy \
  dev-lab/.venv/bin/dev-lab run "Create hello.txt with a greeting." --repo "$REPO"

git -C "$REPO" log --oneline       # see the lab's commit on a lab/… branch
```

### A2. Full stack — supervisor + chat client + extension

Three terminals (or background the servers).

**Terminal 1 — extension MCP server (HTTP+SSE):**
```sh
extensions/macos-build-test/.venv/bin/macos-build-test serve --port 8970
```

**Terminal 2 — the lab supervisor + WebSocket control surface:**
```sh
REPO=$(mktemp -d); git -C "$REPO" init -q
git -C "$REPO" config user.email lab@example.com; git -C "$REPO" config user.name "Dev Lab"
printf 'test:\n\t@echo "tests passed"\n' > "$REPO/Makefile"
git -C "$REPO" add -A; git -C "$REPO" commit -q -m init

env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN GITHUB_TOKEN=dummy \
  EXTENSIONS="macos=http://127.0.0.1:8970/sse" \
  dev-lab/.venv/bin/dev-lab serve --repo "$REPO" --host 127.0.0.1 --port 8765 --poll 1
```

Queue and run-history default to `~/.dev-lab/` (outside the work repo, on
purpose). Override with `--queue` / `--db` if you want them elsewhere — but never
put them inside `--repo`, or the lab's own files make the tree look dirty and it
will refuse to run.

**Terminal 3 — chat client:**
```sh
# stream one task to completion (the agent works in the lab's --repo; it can pwd
# to find the path the extension should check out)
chat-client/.venv/bin/chat-client --url ws://127.0.0.1:8765 \
  submit "Run this project's tests against HEAD using the macos run_tests tool, and report the result."

# or just watch the live event stream
chat-client/.venv/bin/chat-client --url ws://127.0.0.1:8765 listen
```

You can also enqueue without a chat client (same default queue as `serve`):
```sh
dev-lab/.venv/bin/dev-lab submit "Add a CHANGELOG.md"
```

Where things land: jobs flow through `~/.dev-lab/queue/{pending,running,done,failed}`,
run history is in `~/.dev-lab/lab.db` (SQLite), and the agent's commits are on
`lab/…` branches in `$REPO`.

---

## B. Production (Pi lab + macOS extension + GitHub)

### B1. The lab on the Raspberry Pi 5

Do everything as **one Linux user** (subscription creds are user-scoped in
`~/.claude`). Full steps are in `deploy/README.md`; the essentials:

```sh
# prerequisites (once)
npm install -g @anthropic-ai/claude-code
claude                                   # log in as this user

# install the lab
git clone <this repo> ~/claude-agent-team
cd ~/claude-agent-team/dev-lab
python3 -m venv .venv && .venv/bin/python -m pip install -e .
cp .env.example .env                     # set GITHUB_TOKEN (and EXTENSIONS, see B3)

# the work repo the lab operates on
git clone <target repo> ~/work/target-repo
git -C ~/work/target-repo config user.name "Dev Lab"
git -C ~/work/target-repo config user.email "lab@example.com"
```

Run it under systemd (restart-on-crash, start-on-boot):

```sh
# edit deploy/dev-lab.service: User, paths, --repo, --queue, and add
#   --host 0.0.0.0 --port 8765   and   --db <path>   to ExecStart
sudo cp deploy/dev-lab.service /etc/systemd/system/dev-lab.service
sudo systemctl daemon-reload
sudo systemctl enable --now dev-lab
journalctl -u dev-lab -f
```

`EnvironmentFile=.env` supplies `GITHUB_TOKEN` (and `EXTENSIONS`); Claude auth is
the `claude` login. **Never** put `ANTHROPIC_API_KEY` in that file.

### B2. The extension on the macOS host

```sh
git clone <this repo> ~/claude-agent-team
cd ~/claude-agent-team/extensions/macos-build-test
python3 -m venv .venv && .venv/bin/python -m pip install -e .

# bind to the LAN/VPN address the Pi can reach
.venv/bin/macos-build-test serve --host 0.0.0.0 --port 8970
```

Run it under launchd / a process supervisor for always-on. The endpoint is
`http://<mac-host>:8970/sse`.

### B3. Point the lab at the extension

In `~/claude-agent-team/dev-lab/.env` on the Pi:

```
GITHUB_TOKEN=ghp_...
EXTENSIONS=macos=http://<mac-host>:8970/sse
```

Restart the service (`sudo systemctl restart dev-lab`). The agent now has the
macOS `run_tests` / `build` tools.

### B4. Drive it from a workstation

```sh
chat-client/.venv/bin/chat-client --url ws://<pi-host>:8765 submit "<instruction>"
chat-client/.venv/bin/chat-client --url ws://<pi-host>:8765 listen
```

---

## Production caveats (read before relying on it)

- **Unauthenticated surfaces.** The WebSocket control surface and the extension
  SSE endpoint have **no auth** and are meant for loopback today. Only expose
  them over a trusted network (LAN/Tailscale/VPN) until M5 adds auth. Don't bind
  `0.0.0.0` on an untrusted network.
- **No GitHub push yet.** The lab commits to its local clone but does not push to
  GitHub, and the extension clones whatever repo path/URL the agent gives it. On
  one host that's the lab's local repo path; cross-host build/test via GitHub is
  not wired yet (see `HANDOFF.md`).
- **Not yet validated on real Pi hardware** — M2 was verified locally only.
- Live runs spend subscription credits (Agent SDK credit pool) and are
  personal-use only under Anthropic's ToS.
