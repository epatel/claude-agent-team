# dev-lab

The always-on autonomous agent loop that runs on the Raspberry Pi 5. See
`../cards/dev-lab.md` for its role and `../cards/deployment.md` for how it runs
unattended.

## Develop

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Or from the repo root: `make setup`, `make test`.

## Run (M0 stub)

```sh
.venv/bin/dev-lab          # prints a skeleton banner; agent loop lands in M1
```

## Credentials (Claude subscription, not an API key)

Log in once with the `claude` CLI on this host — the Agent SDK reads the stored
`~/.claude` credentials (auto-refreshed; survives restarts). Then put only the
GitHub token in `.env`:

```sh
claude               # complete login; over SSH press `c` to copy the URL, paste the code back
cp .env.example .env # then set GITHUB_TOKEN
```

Do not set `ANTHROPIC_API_KEY` — it overrides subscription auth and the lab
refuses to start if it's present. See `../cards/subscription-auth.md`.
