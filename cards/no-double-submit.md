# no-double-submit

Decision: every button that triggers an async or slow action must guard against double-tap / double-submit.

## Why

Clone, merge, send, login, create — these take time (network, git, the agent). The
moment a user is most likely to tap again is exactly while they're waiting ("did
that register?"). A second tap can double-create a project, double-merge, or send
a turn twice. A disabled, visibly-busy button removes the temptation and the bug.

## Pattern

Use the `withButton(btn, busyLabel, fn)` helper in `static/app.js`:

- **Guard:** early-return if the button is already busy (a `data-busy` flag) —
  this is the actual double-tap guard, not just the `disabled` attribute.
- **Lock:** set `disabled` + `pointer-events:none` for the duration.
- **Show it's active:** add `.is-loading` (a spinner) and swap the label
  (e.g. `clone` → `cloning`), so a slow action visibly *looks* busy.
- **Always restore** in `finally`, on success or error.

```js
withButton($("#np-submit"), "cloning", async () => {
  try { await api("/api/projects", { method: "POST", body: ... }); ... }
  catch (err) { showError(err.message); }
});
```

## Defense in depth (server side)

Make the underlying operation collision-safe so a slipped double-tap can't corrupt
state: project create derives a unique `_2` name, and re-merge is a no-op. Prefer
idempotent/guarded operations over trusting the UI alone.

## Applied to

- ✅ clone (new project), merge → base.
- ⏳ candidates: login/register (mostly moot — it reloads), chat send (the input
  clears on submit, so a re-tap sends empty and no-ops). Wrap them too if they
  grow side effects.

## Checklist for any new button

- [ ] Async/long action? → wrap in `withButton` (or disable + busy-flag guard).
- [ ] Visible busy state (spinner + label), not just `disabled`.
- [ ] Re-enabled on error.
- [ ] Server op safe under a duplicate call.
