# Deploying the dev lab on a Raspberry Pi 5

Runs the **web console** uninterrupted under `systemd`, behind an Apache TLS
reverse proxy (see `../cards/deployment.md`). This mirrors the live reference
deployment at `https://home.memention.net/dev-lab/`. Do every step as the
**same Linux user** — Claude subscription credentials are user-scoped in
`~/.claude`.

## 1. Prerequisites

```sh
# Claude Code CLI — the Agent SDK drives it, and it provides the `claude` login.
npm install -g @anthropic-ai/claude-code     # or the platform's install method
claude                                        # complete login (SSH: press `c`, paste code back)
```

Confirm `ANTHROPIC_API_KEY` is **not** exported anywhere for this user — it
would override subscription auth and the lab refuses to start.

## 2. Install the lab

```sh
# code (private repo: rsync from a checkout, or clone with a deploy key)
mkdir -p ~/dev-lab && cd ~/dev-lab
git clone <this repo> claude-agent-team      # or rsync a working tree here

cd claude-agent-team/dev-lab
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m pip install -e ../extensions/platform-client   # manifest sync dep

# optional overrides + the platform-client gate; chmod 600
printf 'CLIENT_TOKEN=%s\n' "$(openssl rand -hex 24)" > .env && chmod 600 .env

mkdir -p ~/dev-lab/labs                       # projects live here
```

## 3. The systemd service

Edit `deploy/dev-lab-web.service` (User, paths, port), then:

```sh
sudo cp deploy/dev-lab-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dev-lab-web
journalctl -u dev-lab-web -f                  # follow logs
```

`Restart=always` brings it back on crash; `WantedBy=multi-user.target` starts
it on boot. The service binds **127.0.0.1 only** — exposure is Apache's job.
The unit puts `~/.local/bin` on PATH so the SDK finds the `claude` CLI.

## 4. Apache reverse proxy (TLS + path prefix)

The SPA is path-prefix aware, so it can live at `https://<host>/dev-lab/`:

```sh
sudo cp deploy/apache-dev-lab.conf /etc/apache2/conf-available/dev-lab-proxy.conf
sudo a2enmod proxy proxy_http proxy_wstunnel
# inside the *:443 vhost:        Include conf-available/dev-lab-proxy.conf
# inside the *:80 vhost:         RedirectMatch permanent ^/dev-lab(.*)$ https://<host>/dev-lab$1
sudo apachectl configtest && sudo systemctl reload apache2
```

The snippet proxies `/dev-lab/` plus both WebSocket endpoints (`/ws` console,
`/ws/client` platform clients, `timeout=3600` so quiet streams aren't cut).

## 5. First run

- Open `https://<host>/dev-lab/` and **register immediately** — the first user
  becomes the super-user with no invite; later users need invite codes.
- Connect a capability machine:
  `platform-client connect --lab wss://<host>/dev-lab/ws/client --name mac \
   --capability run_tests --token <CLIENT_TOKEN from dev-lab/.env>`

## Updating

```sh
# from a workstation checkout: rsync, keeping the Pi's .env and venv
rsync -a --delete --exclude .git --exclude .venv --exclude .env \
  ./ <pi>:dev-lab/claude-agent-team/
ssh <pi> sudo systemctl restart dev-lab-web
```

## The older CLI surface

`deploy/dev-lab.service` runs the original single-project supervisor
(`dev-lab serve --repo … --queue …`, jobs via `dev-lab submit`). It still
works but is secondary to the web console; its WebSocket is unauthenticated —
keep it loopback.
