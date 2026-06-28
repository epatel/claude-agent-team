#!/usr/bin/env bash

cd "$(dirname "$0")"

source .venv/bin/activate
source .env

: "${PLATFORM_CLIENT_TOKEN:?set PLATFORM_CLIENT_TOKEN in .env (the client auth token)}"

# Client name: first argument, default to this machine's short hostname. Each
# machine needs its own name (a duplicate gets a _2 suffix from the lab), e.g.
# ./run-client.sh some-name to override.
name="${1:-$(hostname -s)}"

platform-client connect \
 --lab wss://home.memention.net/dev-lab/ws/client \
 --name "$name" \
 --token "$PLATFORM_CLIENT_TOKEN"
