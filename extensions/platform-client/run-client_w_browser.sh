#!/usr/bin/env bash

cd "$(dirname "$0")"

source .venv/bin/activate

: "${PLATFORM_CLIENT_TOKEN:?set PLATFORM_CLIENT_TOKEN (the client auth token)}"

platform-client connect \
 --lab wss://home.memention.net/dev-lab/ws/client \
 --name mac-browser \
 --token "$PLATFORM_CLIENT_TOKEN" \
 --mcp browser="npx @playwright/mcp@latest"
