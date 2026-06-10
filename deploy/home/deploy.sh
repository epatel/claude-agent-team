#!/bin/sh
# Deploy the current working tree to the home lab (home.memention.net) and
# restart the console. Site specifics: ssh alias `home`, code at
# ~/dev-lab/claude-agent-team, service dev-lab-web. The Pi keeps its own
# dev-lab/.env (CLIENT_TOKEN) and venvs — never synced.
set -e
cd "$(dirname "$0")/../.."

rsync -a --delete \
  --exclude .git --exclude .venv --exclude .env \
  --exclude __pycache__ --exclude .pytest_cache --exclude .ruff_cache \
  ./ home:dev-lab/claude-agent-team/

ssh home 'sudo systemctl restart dev-lab-web && sleep 2 && systemctl is-active dev-lab-web'
curl -fsS https://home.memention.net/dev-lab/api/auth/state >/dev/null
echo "live: https://home.memention.net/dev-lab/"
