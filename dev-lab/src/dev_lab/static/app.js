"use strict";

const $ = (s) => document.querySelector(s);
const state = { user: null, isSuper: false, needsInvite: true, projects: [], activeId: null, ws: null, assistantBody: null, activity: null, text: "" };

mermaid.initialize({
  startOnLoad: false,
  securityLevel: "strict",
  theme: "dark",
  fontFamily: "IBM Plex Mono, monospace",
  flowchart: { htmlLabels: false, curve: "basis" },  // SVG <text> labels — survive sanitize, colored via CSS
});

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "content-type": "application/json" }, ...opts });
  if (!r.ok) {
    const e = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(e.detail || r.statusText);
  }
  return r.status === 204 ? null : r.json();
}

// Run an async action behind a button, guarding against double-tap and showing a
// busy/loading state. See cards/no-double-submit.md.
async function withButton(btn, busyLabel, fn) {
  if (btn.dataset.busy === "1") return; // already running — ignore the second tap
  btn.dataset.busy = "1";
  const original = btn.textContent;
  btn.disabled = true;
  btn.classList.add("is-loading");
  if (busyLabel) btn.textContent = busyLabel;
  try {
    return await fn();
  } finally {
    delete btn.dataset.busy;
    btn.disabled = false;
    btn.classList.remove("is-loading");
    btn.textContent = original;
  }
}

/* ---------- auth ---------- */
async function checkAuth() {
  try {
    const me = await api("/api/me");
    state.user = me.username;
    state.isSuper = !!me.is_super;
    await showApp();
  } catch {
    // Not logged in: find out whether registration requires an invite. The very
    // first user is the super-user and registers without one, so we hide the field.
    try {
      const s = await api("/api/auth/state");
      state.needsInvite = !!s.needs_invite;
    } catch { /* default to requiring an invite if the probe fails */ }
    $("#login").hidden = false;
    $("#app").hidden = true;
  }
  document.body.classList.remove("loading");
}

async function doAuth(path) {
  const username = $("#username").value.trim();
  const password = $("#password").value;
  const invite = $("#invite").value.trim();
  try {
    await api(path, { method: "POST", body: JSON.stringify({ username, password, invite }) });
    location.reload();
  } catch (err) {
    $("#auth-error").textContent = err.message;
  }
}
$("#auth-form").addEventListener("submit", (e) => { e.preventDefault(); doAuth("/api/login"); });
// The first ever user is the super-user and needs no invite, so we register them
// straight away. Once users exist, the first click reveals the invite field (so a
// returning user can paste a code) and a second click submits the registration.
$("#register-btn").addEventListener("click", () => {
  if (!state.needsInvite) {
    doAuth("/api/register");
    return;
  }
  const inv = $("#invite");
  if (inv.hidden) {
    inv.hidden = false;
    inv.focus();
    $("#auth-error").textContent = "";
    return;
  }
  doAuth("/api/register");
});
$("#logout-btn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  if (state.ws) state.ws.close();
  location.reload();
});

/* ---------- app shell ---------- */
async function showApp() {
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#who").textContent = state.user + (state.isSuper ? " ★" : "");
  $("#admin-btn").hidden = !state.isSuper;
  await loadProjects();
  connectWs();
}

/* ---------- projects ---------- */
async function loadProjects() {
  state.projects = await api("/api/projects");
  renderProjects();
}
function renderProjects() {
  const ul = $("#project-list");
  ul.innerHTML = "";
  for (const p of state.projects) {
    const li = document.createElement("li");
    li.className = "project" + (p.id === state.activeId ? " active" : "");
    li.dataset.id = p.id;
    li.innerHTML =
      `<span class="dot" data-status="idle"></span><span class="pname">${escapeHtml(p.name)}</span>` +
      (p.branch ? `<span class="branch-chip">${escapeHtml(p.branch)}</span>` : "");
    li.addEventListener("click", () => openProject(p.id));
    ul.appendChild(li);
  }
}
$("#new-project-btn").addEventListener("click", () => {
  $("#new-project-form").hidden = false;
  $("#new-project-btn").hidden = true;
});
$("#np-cancel").addEventListener("click", () => {
  $("#new-project-form").hidden = true;
  $("#new-project-btn").hidden = false;
  $("#np-error").textContent = "";
});
$("#new-project-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const remote_url = $("#np-url").value.trim();
  $("#np-error").textContent = "";
  withButton($("#np-submit"), "cloning", async () => {
    try {
      await api("/api/projects", { method: "POST", body: JSON.stringify({ remote_url }) });
      $("#np-url").value = "";
      $("#new-project-form").hidden = true;
      $("#new-project-btn").hidden = false;
      await loadProjects();
    } catch (err) {
      $("#np-error").textContent = err.message;
    }
  });
});

async function openProject(id) {
  state.activeId = id;
  renderProjects();
  const p = state.projects.find((x) => x.id === id);
  $("#active-name").textContent = p.name;
  $("#active-branch").textContent = p.branch || "";
  $("#chat-form").hidden = false;
  $("#pull-btn").hidden = false;
  $("#push-btn").hidden = false;
  $("#merge-btn").hidden = false;
  const t = $("#transcript");
  t.innerHTML = "";
  setStatus("idle");
  loadBaseBranches(id);
  const msgs = await api(`/api/projects/${id}/messages`);
  for (const m of msgs) addMessage(m.role, m.content);
  $("#chat-text").focus();
}

/* ---------- base branch (for new chat threads) ---------- */
const baseSelect = $("#base-select");
const basePicker = $("#base-picker");

async function loadBaseBranches(id) {
  basePicker.hidden = true;
  try {
    const { branches, base } = await api(`/api/projects/${id}/branches`);
    if (state.activeId !== id) return; // user switched projects mid-fetch
    baseSelect.innerHTML = "";
    for (const b of branches) {
      const opt = document.createElement("option");
      opt.value = b;
      opt.textContent = b;
      if (b === base) opt.selected = true;
      baseSelect.appendChild(opt);
    }
    baseSelect.dataset.current = base || "";
    basePicker.hidden = false;
  } catch {
    /* leave the picker hidden if branches can't be fetched */
  }
}

// Same no-double-submit pattern as withButton (cards/no-double-submit.md): a busy
// flag guards re-entry, the control is disabled, and the wrapper shows a spinner;
// always restored in finally. A <select> swaps no label, so we guard it directly.
baseSelect.addEventListener("change", async () => {
  if (state.activeId == null || baseSelect.dataset.busy === "1") return;
  const id = state.activeId;
  const name = baseSelect.value;
  const previous = baseSelect.dataset.current || "";
  if (name === previous) return;
  baseSelect.dataset.busy = "1";
  baseSelect.disabled = true;
  basePicker.classList.add("is-loading");
  try {
    const r = await api(`/api/projects/${id}/base`, {
      method: "POST",
      body: JSON.stringify({ branch: name }),
    });
    baseSelect.dataset.current = r.base_branch;
    const p = state.projects.find((x) => x.id === id);
    if (p) p.base_branch = r.base_branch;
    systemLine(`✓ base set to ${r.base_branch} — applies to new chat threads, not this one`);
  } catch (err) {
    baseSelect.value = previous; // revert the dropdown to the last good base
    systemLine(`✗ set base failed: ${err.message}`, true);
  } finally {
    delete baseSelect.dataset.busy;
    baseSelect.disabled = false;
    basePicker.classList.remove("is-loading");
  }
});

function setStatus(s) {
  const el = $("#active-status");
  el.dataset.status = s;
  el.textContent = s;
}

function systemLine(text, bad) {
  const div = document.createElement("div");
  div.className = "sys" + (bad ? " bad" : "");
  div.textContent = text;
  $("#transcript").appendChild(div);
  scrollBottom();
}

$("#pull-btn").addEventListener("click", () => {
  if (state.activeId == null) return;
  withButton($("#pull-btn"), "pulling", async () => {
    try {
      const r = await api(`/api/projects/${state.activeId}/pull`, { method: "POST" });
      systemLine(`✓ pulled ${r.base} @ ${r.commit.slice(0, 10)}`);
    } catch (err) {
      systemLine(`✗ pull failed: ${err.message}`, true);
    }
  });
});

$("#push-btn").addEventListener("click", () => {
  if (state.activeId == null) return;
  withButton($("#push-btn"), "pushing", async () => {
    try {
      const r = await api(`/api/projects/${state.activeId}/push`, { method: "POST" });
      systemLine(`✓ pushed ${r.base} @ ${r.commit.slice(0, 10)}`);
    } catch (err) {
      systemLine(`✗ push failed: ${err.message}`, true);
    }
  });
});

$("#merge-btn").addEventListener("click", () => {
  if (state.activeId == null) return;
  withButton($("#merge-btn"), "merging", async () => {
    try {
      const r = await api(`/api/projects/${state.activeId}/merge`, { method: "POST" });
      systemLine(`✓ merged ${r.branch} into ${r.base} @ ${r.commit.slice(0, 10)}`);
    } catch (err) {
      systemLine(`✗ merge failed: ${err.message}`, true);
    }
  });
});

/* ---------- chat ---------- */
const textarea = $("#chat-text");
textarea.addEventListener("input", () => {
  textarea.style.height = "auto";
  textarea.style.height = Math.min(textarea.scrollHeight, 200) + "px";
});
textarea.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#chat-form").requestSubmit(); }
});
$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = textarea.value.trim();
  if (!text || state.activeId == null || !state.ws) return;
  textarea.value = "";
  textarea.style.height = "auto";
  addMessage("user", text);
  state.ws.send(JSON.stringify({ type: "message", project_id: state.activeId, text }));
});

function scrollBottom() {
  const t = $("#transcript");
  t.scrollTop = t.scrollHeight;
}

function addMessage(role, content) {
  const t = $("#transcript");
  const div = document.createElement("div");
  div.className = "msg " + role;
  const body = document.createElement("div");
  body.className = "msg-body";
  div.appendChild(body);
  if (role === "assistant") renderMarkdown(body, content);
  else body.textContent = content;
  t.appendChild(div);
  scrollBottom();
  return body;
}

async function renderMarkdown(el, text) {
  el.innerHTML = DOMPurify.sanitize(marked.parse(text || ""));
  const blocks = el.querySelectorAll("code.language-mermaid");
  let i = 0;
  for (const code of blocks) {
    const src = code.textContent;
    try {
      const { svg } = await mermaid.render("m" + Date.now() + i++, src);
      const fig = document.createElement("figure");
      fig.className = "mermaid-fig";
      // Inject mermaid's own SVG directly: securityLevel:"strict" already strips
      // scripts/click-handlers/foreignObject, and a DOMPurify pass mangles the
      // diagram (reorders nodes so labels vanish). The untrusted *markdown* HTML
      // around it is still DOMPurify-sanitized above.
      fig.innerHTML = svg;
      const pre = code.closest("pre");
      if (pre) pre.replaceWith(fig);
    } catch { /* leave the code block as-is on render failure */ }
  }
}

/* ---------- websocket ---------- */
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;
  ws.onmessage = (ev) => handleEvent(JSON.parse(ev.data));
  ws.onclose = () => { state.ws = null; setTimeout(connectWs, 1500); };
}

function toolLabel(e) {
  const inp = e.input || {};
  const hint = inp.command || inp.file_path || inp.path || inp.pattern || "";
  return (e.name || "tool") + (hint ? "  " + String(hint).slice(0, 80) : "");
}

function startAssistant() {
  const t = $("#transcript");
  const div = document.createElement("div");
  div.className = "msg assistant pending";
  const act = document.createElement("div");
  act.className = "activity";
  const body = document.createElement("div");
  body.className = "msg-body";
  div.appendChild(act);
  div.appendChild(body);
  t.appendChild(div);
  state.activity = act;
  state.assistantBody = body;
  state.text = "";
  scrollBottom();
}

function handleEvent(e) {
  // sidebar status dots track every project
  if (e.project_id != null) {
    const dot = document.querySelector(`.project[data-id="${e.project_id}"] .dot`);
    if (dot) {
      if (e.type === "turn_running") dot.dataset.status = "running";
      if (e.type === "turn_done" || e.type === "turn_failed") dot.dataset.status = "idle";
    }
  }
  if (e.type === "projects_changed") { loadProjects(); return; }
  if (e.type === "state") { state.projects = e.projects; renderProjects(); return; }
  // transcript only reflects the active project
  if (e.project_id != null && e.project_id !== state.activeId) return;

  switch (e.type) {
    case "turn_running": startAssistant(); setStatus("running"); break;
    case "tool_use":
      if (state.activity) {
        const chip = document.createElement("span");
        chip.className = "tool-chip";
        chip.textContent = "⤷ " + toolLabel(e);
        state.activity.appendChild(chip);
        scrollBottom();
      }
      break;
    case "agent_message":
      state.text += (e.text || "") + "\n\n";
      if (state.assistantBody) renderMarkdown(state.assistantBody, state.text);
      scrollBottom();
      break;
    case "turn_done": {
      const div = state.assistantBody && state.assistantBody.closest(".msg");
      if (div) div.classList.remove("pending");
      if (e.commit_sha && state.activity) {
        const c = document.createElement("span");
        c.className = "commit-chip";
        c.textContent = "✓ " + e.commit_sha.slice(0, 10);
        state.activity.appendChild(c);
      }
      if (e.branch) {
        const p = state.projects.find((x) => x.id === e.project_id);
        if (p) { p.branch = e.branch; renderProjects(); }
        if (e.project_id === state.activeId) $("#active-branch").textContent = e.branch;
      }
      state.assistantBody = null;
      state.activity = null;
      setStatus("idle");
      break;
    }
    case "turn_failed":
    case "error": {
      const t = $("#transcript");
      const div = document.createElement("div");
      div.className = "msg-error";
      div.textContent = "✗ " + (e.error || "error");
      t.appendChild(div);
      setStatus("idle");
      scrollBottom();
      break;
    }
  }
}

/* ---------- admin (super-user) ---------- */
const adminOverlay = $("#admin-overlay");

function adminError(msg) { $("#admin-error").textContent = msg || ""; }

async function openAdmin() {
  adminError("");
  adminOverlay.hidden = false;
  await Promise.all([loadInvites(), loadUsers()]);
}
function closeAdmin() { adminOverlay.hidden = true; }

async function loadInvites() {
  const list = $("#invite-list");
  try {
    const invites = await api("/api/admin/invites");
    list.innerHTML = "";
    if (!invites.length) {
      list.innerHTML = `<li class="invite empty-row">no invites yet — mint one to onboard a user</li>`;
      return;
    }
    for (const inv of invites) {
      const li = document.createElement("li");
      li.className = "invite" + (inv.used_by ? " used" : "");
      li.innerHTML =
        `<code class="invite-code">${escapeHtml(inv.code)}</code>` +
        `<span class="invite-state">${inv.used_by ? "used" : "unused"}</span>`;
      list.appendChild(li);
    }
  } catch (err) { adminError(err.message); }
}

async function loadUsers() {
  const rows = $("#user-rows");
  try {
    const users = await api("/api/admin/users");
    rows.innerHTML = "";
    for (const u of users) {
      const self = u.username === state.user;
      const tr = document.createElement("tr");
      if (u.blocked) tr.className = "blocked-row";
      const actions = [
        `<button class="mini" data-act="password" data-id="${u.id}">reset pw</button>`,
        self ? "" : `<button class="mini" data-act="block" data-id="${u.id}" data-blocked="${u.blocked}">${u.blocked ? "unblock" : "block"}</button>`,
        self ? "" : `<button class="mini danger" data-act="delete" data-id="${u.id}">delete</button>`,
      ].join("");
      tr.innerHTML =
        `<td>${escapeHtml(u.username)}${self ? " <span class='you'>(you)</span>" : ""}</td>` +
        `<td>${u.is_super ? "super ★" : "user"}</td>` +
        `<td>${u.blocked ? "blocked" : "active"}</td>` +
        `<td class="actions-col">${actions}</td>`;
      rows.appendChild(tr);
    }
  } catch (err) { adminError(err.message); }
}

$("#admin-btn").addEventListener("click", openAdmin);
$("#admin-close").addEventListener("click", closeAdmin);
adminOverlay.addEventListener("click", (e) => { if (e.target === adminOverlay) closeAdmin(); });

$("#new-invite-btn").addEventListener("click", () => {
  adminError("");
  withButton($("#new-invite-btn"), "minting", async () => {
    try {
      await api("/api/admin/invites", { method: "POST" });
      await loadInvites();
    } catch (err) { adminError(err.message); }
  });
});

$("#user-rows").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = btn.dataset.id;
  adminError("");
  try {
    if (btn.dataset.act === "block") {
      const blocked = btn.dataset.blocked === "true";
      await api(`/api/admin/users/${id}/block`, {
        method: "POST", body: JSON.stringify({ blocked: !blocked }),
      });
      await loadUsers();
    } else if (btn.dataset.act === "delete") {
      if (!confirm("Delete this user permanently?")) return;
      await api(`/api/admin/users/${id}`, { method: "DELETE" });
      await loadUsers();
    } else if (btn.dataset.act === "password") {
      const password = prompt("New password for this user:");
      if (!password) return;
      await api(`/api/admin/users/${id}/password`, {
        method: "POST", body: JSON.stringify({ password }),
      });
      adminError("");
    }
  } catch (err) { adminError(err.message); }
});

checkAuth();
