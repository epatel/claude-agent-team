# Known models — checking and updating the selectable list

The web console's model dropdowns are fed by one hand-maintained constant:
**`dev-lab/src/dev_lab/config.py` → `KNOWN_MODELS`** (a list of
`{"id", "label"}` dicts; `KNOWN_MODEL_IDS` is derived from it). Everything else
follows from there:

- `GET /api/models` serves the list (plus the lab default) to the console.
- `ProjectManager.create()` and `set_model()` validate against
  `KNOWN_MODEL_IDS` — an id not in the list is rejected with "unknown model".
- The lab default (`Config.model`, overridable via the `MODEL` env var) does
  **not** have to be in the list, but should be; the frontend injects any
  unknown current id into the dropdown so it's never silently lost.

## How to check what models currently exist

The lab runs on **subscription auth with no API key**
(cards/subscription-auth.md), so it cannot call `GET /v1/models` — that
endpoint needs an `x-api-key`. Use one of these instead:

1. **Docs page (no auth, scriptable)** — fetch
   `https://platform.claude.com/docs/en/about-claude/models/overview.md`
   (plain markdown). The "Claude API alias" rows are the exact ids to use.
   This is the primary method.
2. **Claude Code CLI** — `/model` in an interactive session lists what the
   subscription actually serves (useful to confirm plan-tier access, e.g.
   Opus/Fable need a Max plan).
3. **`ant` CLI, if installed** — `ant models list` (authenticates via the
   same `claude` login profile).

## How to update the list

1. Edit `KNOWN_MODELS` in `config.py`. Use the **exact alias** from the docs
   page — never construct or date-suffix an id (the one current exception:
   Haiku's published full id is `claude-haiku-4-5-20251001`). Keep the list
   lean: current tiers only, newest first; legacy models are noise in a
   dropdown.
2. No DB migration: `projects.model` stores raw TEXT and is validated only on
   write. A project holding a since-removed id keeps working —
   `effective_model()` returns it as-is and the dropdown shows it (injected as
   an extra option) until someone switches it.
3. Tests reference specific ids (`dev-lab/tests/test_projects.py`,
   `test_web.py`) — if an id used there is removed, update the tests. Run
   `pytest` in `dev-lab/`.
4. If the *default* changes (e.g. a new top model), also consider
   `Config.model` in `config.py` and the decision log in `project-plan.md`.

Last synced against the docs page: 2026-06-09 (Fable 5 GA; lineup: Fable 5,
Opus 4.8, Sonnet 4.6, Haiku 4.5; Opus 4.7/4.6 moved to legacy).
