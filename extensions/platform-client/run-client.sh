#!/usr/bin/env bash

cd "$(dirname "$0")"

source .venv/bin/activate
source .env

: "${PLATFORM_CLIENT_TOKEN:?set PLATFORM_CLIENT_TOKEN in .env (the client auth token)}"

platform-client connect \
 --lab wss://home.memention.net/dev-lab/ws/client \
 --name mac \
 --token "$PLATFORM_CLIENT_TOKEN"
