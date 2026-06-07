# subscription-auth

Decision: the dev lab authenticates with a Claude subscription (Pro/Max) via a one-time `claude` login, not an API key.

## Decision

Authenticate the Claude Agent SDK loop with a **Claude subscription**. Log in
once with the `claude` CLI on the lab host; the SDK reads the stored login
credentials. No API key, and no auth token in `.env`.

## Why

- A continuously running lab on a subscription avoids per-token API charges.
- Native to the SDK — same credential path as the Claude Code engine it wraps.
- Login credentials are auto-refreshed, so "log in once" is durable: no
  scheduled token rotation to manage.

## How

1. On the lab host, run `claude` and complete the login. Over SSH with no local
   browser, press `c` to copy the login URL, open it in a browser elsewhere, and
   paste the returned code at the prompt (the standard SSH/headless flow).
2. That's it — credentials are stored at `~/.claude/.credentials.json` (Linux;
   macOS uses the Keychain), mode `0600`, and Claude Code refreshes them in the
   background while the subscription is active. They survive restarts.
3. Keep `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` **unset** — both outrank
   the login credentials in the SDK's auth precedence and would silently bill the
   API. The lab refuses to start if either is present.

## Constraints

- **Service user** — credentials are user-scoped in `~/.claude`; the unattended
  service must run as the user who logged in, or set `CLAUDE_CONFIG_DIR` to that
  user's config dir.
- **Plan tier** — Opus (`claude-opus-4-8`) requires a **Max** plan; pick a model
  the plan allows.
- **Terms of Service** — subscription auth is **personal/non-commercial use
  only**; don't build a multi-user service on it. Use an API key for
  multi-user/production.
- **Credit pool** — subscription Agent SDK usage draws from a separate monthly
  Agent SDK credit (effective 2026-06-15).
- **Re-login only on logout/lapse** — you re-login only if you `/logout`, the
  subscription lapses, or the device is unregistered.

## Fallback

For a fully non-interactive box where an interactive login isn't possible,
`claude setup-token` mints a fixed ~1-year, inference-only token to set as
`CLAUDE_CODE_OAUTH_TOKEN`. It does **not** auto-refresh (needs annual re-minting)
— prefer interactive login.

## Revisit if

- The lab becomes multi-user or commercial → switch to an `ANTHROPIC_API_KEY`
  (Console credits), which has no personal-use restriction.
- Subscription Agent SDK credits prove insufficient for the workload.

## To confirm

The active Claude plan tier (must be Max for Opus) before M1.
