# Deploying the dev lab on a Raspberry Pi 5

Runs the supervisor uninterrupted under `systemd` (see `../cards/deployment.md`).
Do every step as the **same Linux user** — Claude subscription credentials are
user-scoped in `~/.claude`.

## 1. Prerequisites

```sh
# Claude Code CLI — the Agent SDK drives it, and it provides the `claude` login.
npm install -g @anthropic-ai/claude-code     # or the platform's install method
claude                                        # complete login (SSH: press `c`, paste code back)
```

Confirm `ANTHROPIC_API_KEY` is **not** exported anywhere for this user — it would
override subscription auth.

## 2. Install the lab

```sh
git clone <this repo> ~/claude-agent-team
cd ~/claude-agent-team/dev-lab
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp .env.example .env        # set GITHUB_TOKEN
```

## 3. The work repo

Clone the repository the lab should work on, and give it a git identity + push
credentials:

```sh
git clone <target repo> ~/work/target-repo
git -C ~/work/target-repo config user.name  "Dev Lab"
git -C ~/work/target-repo config user.email "lab@example.com"
```

## 4. Install the service

Edit `deploy/dev-lab.service` (User, paths, `--repo`, `--queue`), then:

```sh
sudo cp deploy/dev-lab.service /etc/systemd/system/dev-lab.service
sudo systemctl daemon-reload
sudo systemctl enable --now dev-lab
journalctl -u dev-lab -f
```

`Restart=always` brings it back on crash; `WantedBy=multi-user.target` starts it
on boot. On restart the supervisor requeues any job that was mid-run.

## 5. Give it work

```sh
dev-lab submit "Add a CHANGELOG.md" --queue ~/claude-agent-team/dev-lab-queue
```

The supervisor picks up pending jobs, runs each on its own `lab/…` branch, and
commits the result. Inspect the queue dirs (`pending/ running/ done/ failed/`)
or the logs.
