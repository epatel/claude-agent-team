"use strict";

const $ = (s) => document.querySelector(s);
// Path prefix the console is served under ("" at the root, "/dev-lab" behind a
// reverse proxy). Derived from the page URL; prepended to every API/WS path.
const BASE = new URL(".", location.href).pathname.replace(/\/$/, "");
const state = { user: null, isSuper: false, needsInvite: true, projects: [], clients: [], activeId: null, ws: null, assistantBody: null, activity: null, text: "", models: [], defaultModel: null };

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

// Reusable in-app confirm dialog — styled like the other modals, replaces
// window.confirm(). Returns a Promise<boolean>: true = confirmed, false =
// cancelled (cancel button, close button, backdrop, or Escape).
function confirmDialog({ title = "confirm", message = "", confirmText = "confirm", danger = true } = {}) {
  return new Promise((resolve) => {
    const dlg = $("#confirm-dialog");
    const okBtn = $("#confirm-ok");
    const cancelBtn = $("#confirm-cancel");
    $("#confirm-title").textContent = title;
    $("#confirm-message").textContent = message;
    okBtn.textContent = confirmText;
    okBtn.classList.toggle("danger", danger);

    let settled = false;
    const finish = (val) => {
      if (settled) return;
      settled = true;
      okBtn.removeEventListener("click", onOk);
      cancelBtn.removeEventListener("click", onCancel);
      dlg.removeEventListener("close", onClose);
      resolve(val);
    };
    const onOk = () => { finish(true); dlg.close(); };
    const onCancel = () => dlg.close();   // triggers onClose → resolves false
    const onClose = () => finish(false);  // covers Escape / backdrop / close btn

    okBtn.addEventListener("click", onOk);
    cancelBtn.addEventListener("click", onCancel);
    dlg.addEventListener("close", onClose);
    dlg.showModal();
    okBtn.focus();
  });
}

async function api(path, opts = {}) {
  const r = await fetch(BASE + path, { headers: { "content-type": "application/json" }, ...opts });
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
  const who = $("#who");
  who.textContent = (state.isSuper ? "⚙ " : "") + state.user + (state.isSuper ? " ★" : "");
  who.classList.toggle("is-super", state.isSuper);
  who.title = state.isSuper
    ? "You're a super-user (★). Click to open the admin panel and manage users & invites."
    : "Signed in as " + state.user;
  await loadModels();
  await loadProjects();
  await loadClients();
  connectWs();
}

/* ---------- platform clients (connected capability providers) ---------- */
async function loadClients() {
  try {
    state.clients = await api("/api/clients");
  } catch {
    state.clients = [];
  }
  renderClients();
}

function renderClients() {
  const box = $("#clients-box");
  const ul = $("#client-list");
  ul.innerHTML = "";
  box.hidden = state.clients.length === 0;
  for (const c of state.clients) {
    const caps = (c.capabilities || []).map((x) => x.name).join(", ");
    const li = document.createElement("li");
    li.className = "client";
    li.title = `${c.platform}${caps ? " — " + caps : ""}`;
    li.innerHTML =
      `<span class="client-dot"></span><span class="cname">${escapeHtml(c.name)}</span>` +
      `<span class="client-platform">${escapeHtml(c.platform)}</span>`;
    ul.appendChild(li);
  }
}

/* ---------- models ---------- */
async function loadModels() {
  try {
    const r = await api("/api/models");
    state.models = r.models || [];
    state.defaultModel = r.default || null;
  } catch {
    state.models = [];
    state.defaultModel = null;
  }
  fillModelSelect($("#np-model"), state.defaultModel);
}

// Populate a <select> with the known models, pre-selecting ``selected``. If that
// id isn't in the known list (e.g. a custom MODEL env on the lab), it's added so
// the current choice is always representable rather than silently lost.
function fillModelSelect(sel, selected) {
  if (!sel) return;
  const ids = new Set(state.models.map((m) => m.id));
  const opts = state.models.slice();
  if (selected && !ids.has(selected)) opts.unshift({ id: selected, label: selected });
  sel.innerHTML = "";
  for (const m of opts) {
    const o = document.createElement("option");
    o.value = m.id;
    o.textContent = m.label;
    if (m.id === selected) o.selected = true;
    sel.appendChild(o);
  }
}

function modelLabel(id) {
  const m = state.models.find((x) => x.id === id);
  return m ? m.label : id;
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
    // Supers see everyone's projects — label each with its owner ("shared"
    // marks ownerless rows, e.g. checkouts dropped into labs/).
    const owner = state.isSuper ? `<span class="owner-chip">${escapeHtml(p.owner || "shared")}</span>` : "";
    li.innerHTML =
      `<span class="dot" data-status="idle"></span><span class="pname">${escapeHtml(p.name)}</span>${owner}` +
      (p.branch ? `<span class="branch-chip">${escapeHtml(p.branch)}</span>` : "");
    li.addEventListener("click", () => openProject(p.id));
    ul.appendChild(li);
  }
}
/* New-project dialog: two tabs — clone a git URL, or git-init a blank repo. */
let npMode = "clone";

function setNpMode(mode) {
  npMode = mode;
  document.querySelectorAll(".np-tab").forEach((t) => {
    const on = t.dataset.mode === mode;
    t.classList.toggle("is-active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  });
  $("#np-pane-clone").hidden = mode !== "clone";
  $("#np-pane-blank").hidden = mode !== "blank";
  $("#np-submit").textContent = mode === "clone" ? "clone" : "create";
  $("#np-error").textContent = "";
  (mode === "clone" ? $("#np-url") : $("#np-name")).focus();
}

document.querySelectorAll(".np-tab").forEach((t) => {
  t.addEventListener("click", () => setNpMode(t.dataset.mode));
});

$("#new-project-btn").addEventListener("click", () => {
  $("#np-url").value = "";
  $("#np-token").value = "";
  $("#np-name").value = "";
  $("#np-error").textContent = "";
  fillModelSelect($("#np-model"), state.defaultModel);
  $("#new-project-dialog").showModal();
  setNpMode("clone");
});

// Keep the blank-repo name to characters the server accepts as it's typed.
$("#np-name").addEventListener("input", (e) => {
  e.target.value = e.target.value.replace(/[^A-Za-z0-9._-]/g, "");
});

$("#new-project-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const clone = npMode === "clone";
  const remote_url = clone ? $("#np-url").value.trim() : "";
  const name = clone ? "" : $("#np-name").value.trim();
  const github_token = clone ? $("#np-token").value.trim() : "";
  const model = $("#np-model").value;
  $("#np-error").textContent = "";
  if (clone && !remote_url) {
    $("#np-error").textContent = "a git url is required";
    return;
  }
  if (!clone && !name) {
    $("#np-error").textContent = "a repo name is required";
    return;
  }
  withButton($("#np-submit"), clone ? "cloning" : "creating", async () => {
    try {
      const created = await api("/api/projects", {
        method: "POST",
        body: JSON.stringify({ remote_url, name, github_token, model }),
      });
      $("#new-project-dialog").close();
      await loadProjects();
      await openProject(created.id);  // jump straight into the new project
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
  $("#clear-chat-btn").hidden = false;
  attachments.length = 0;  // pending attachments belong to the previous project
  renderAttachChips();
  $("#browse-btn").hidden = false;
  $("#archive-btn").hidden = false;
  $("#fetch-btn").hidden = false;
  $("#pull-btn").hidden = false;
  $("#push-btn").hidden = false;
  $("#reset-btn").hidden = false;
  $("#rebase-btn").hidden = false;
  $("#merge-btn").hidden = false;
  $("#remove-project-btn").hidden = false;
  $("#head-tabs").hidden = false;
  renderToken(p);
  renderModel(p);
  setTab("chat");
  const t = $("#transcript");
  t.innerHTML = "";
  setStatus("idle");
  loadBaseBranches(id);
  const msgs = await api(`/api/projects/${id}/messages`);
  for (const m of msgs) addMessage(m.role, m.content);
  $("#chat-text").focus();
}

/* ---------- header tabs (chat | repo) ---------- */
function setTab(name) {
  document.querySelectorAll("#head-tabs .tab").forEach((t) => {
    const on = t.dataset.tab === name;
    t.classList.toggle("is-active", on);
    t.setAttribute("aria-selected", on ? "true" : "false");
  });
  $("#panel-chat").hidden = name !== "chat";
  $("#panel-repo").hidden = name !== "repo";
  $("#panel-agent").hidden = name !== "agent";
  if (name === "chat") scrollBottom();
  if (name === "agent") loadAgentPanel();
}
document.querySelectorAll("#head-tabs .tab").forEach((t) => {
  t.addEventListener("click", () => setTab(t.dataset.tab));
});

/* ---------- per-project GitHub token ---------- */
function renderToken(p) {
  $("#token-form").hidden = false;
  $("#token-input").value = "";
  $("#token-error").textContent = "";
  $("#token-status").textContent = p.has_token ? "🔒 token set" : "no token";
  $("#token-clear").hidden = !p.has_token;
}

async function saveToken(github_token) {
  const id = state.activeId;
  if (id == null) return;
  $("#token-error").textContent = "";
  try {
    await api(`/api/projects/${id}/token`, {
      method: "POST",
      body: JSON.stringify({ github_token }),
    });
    $("#token-input").value = "";
    await loadProjects();
    const p = state.projects.find((x) => x.id === id);
    if (p) renderToken(p);
  } catch (err) {
    $("#token-error").textContent = err.message;
  }
}

$("#token-form").addEventListener("submit", (e) => {
  e.preventDefault();
  withButton($("#token-save"), "saving", () => saveToken($("#token-input").value.trim()));
});
$("#token-clear").addEventListener("click", () => {
  withButton($("#token-clear"), "clearing", () => saveToken(""));
});

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

/* ---------- per-project model (switchable mid-session, like the CLI) ---------- */
const modelSelect = $("#model-select");
const modelPicker = $("#model-picker");

function renderModel(p) {
  fillModelSelect(modelSelect, p.model || state.defaultModel);
  modelSelect.dataset.current = p.model || state.defaultModel || "";
  modelPicker.hidden = state.models.length === 0 && !p.model;
}

// Same no-double-submit guard as the base-branch <select> (cards/no-double-submit.md):
// busy flag blocks re-entry, the control is disabled, a spinner shows, and the
// dropdown reverts on failure. The switch takes effect on the next turn — open()
// rebuilds the (still-resumed) session with the new model.
modelSelect.addEventListener("change", async () => {
  if (state.activeId == null || modelSelect.dataset.busy === "1") return;
  const id = state.activeId;
  const model = modelSelect.value;
  const previous = modelSelect.dataset.current || "";
  if (model === previous) return;
  modelSelect.dataset.busy = "1";
  modelSelect.disabled = true;
  modelPicker.classList.add("is-loading");
  try {
    const r = await api(`/api/projects/${id}/model`, {
      method: "POST",
      body: JSON.stringify({ model }),
    });
    modelSelect.dataset.current = r.model;
    const p = state.projects.find((x) => x.id === id);
    if (p) p.model = r.model;
    systemLine(`✓ model set to ${modelLabel(r.model)} — applies to your next message`);
  } catch (err) {
    modelSelect.value = previous; // revert to the last good model
    systemLine(`✗ set model failed: ${err.message}`, true);
  } finally {
    delete modelSelect.dataset.busy;
    modelSelect.disabled = false;
    modelPicker.classList.remove("is-loading");
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

$("#fetch-btn").addEventListener("click", () => {
  if (state.activeId == null) return;
  withButton($("#fetch-btn"), "fetching", async () => {
    try {
      const r = await api(`/api/projects/${state.activeId}/fetch`, { method: "POST" });
      systemLine(`✓ fetched ${r.base} @ ${r.commit.slice(0, 10)}`);
    } catch (err) {
      systemLine(`✗ fetch failed: ${err.message}`, true);
    }
  });
});

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

$("#rebase-btn").addEventListener("click", () => {
  if (state.activeId == null) return;
  withButton($("#rebase-btn"), "rebasing", async () => {
    try {
      const r = await api(`/api/projects/${state.activeId}/rebase`, { method: "POST" });
      if (r.status === "conflicts") {
        // The rebase was aborted, the branch is untouched. Offer to hand the
        // whole thing — rebase plus conflict resolution — to the agent.
        systemLine(`✗ rebase of ${r.branch} onto ${r.base} hit conflicts: ${r.files.join(", ")}`, true);
        const ok = await confirmDialog({
          title: "rebase conflicts",
          message: `Rebasing ${r.branch} onto ${r.base} conflicts in: ${r.files.join(", ")}. ` +
            "Ask the agent to do the rebase and resolve the conflicts in chat?",
          confirmText: "ask the agent",
          danger: false,
        });
        if (ok) {
          sendChat(
            `Rebase the current branch ${r.branch} onto ${r.base} and resolve the conflicts ` +
            `(expected in: ${r.files.join(", ")}). Keep the intent of both sides; explain how ` +
            "you resolved each conflict. Do not push.",
          );
        }
        return;
      }
      systemLine(`✓ rebased ${r.branch} onto ${r.base} @ ${r.commit.slice(0, 10)}`);
    } catch (err) {
      systemLine(`✗ rebase failed: ${err.message}`, true);
    }
  });
});

$("#reset-btn").addEventListener("click", async () => {
  if (state.activeId == null) return;
  const ok = await confirmDialog({
    title: "reset working tree",
    message: "Discard ALL uncommitted changes and untracked files in this project's " +
      "working tree? Commits are kept. This cannot be undone.",
    confirmText: "reset",
  });
  if (!ok) return;
  withButton($("#reset-btn"), "resetting", async () => {
    try {
      const r = await api(`/api/projects/${state.activeId}/reset`, { method: "POST" });
      systemLine(`✓ reset working tree on ${r.branch} @ ${r.commit.slice(0, 10)}`);
    } catch (err) {
      systemLine(`✗ reset failed: ${err.message}`, true);
    }
  });
});

$("#remove-project-btn").addEventListener("click", async () => {
  if (state.activeId == null) return;
  const p = state.projects.find((x) => x.id === state.activeId);
  const ok = await confirmDialog({
    title: "remove project",
    message: `Remove "${p ? p.name : "this project"}" from the lab? This deletes the local ` +
      "clone and the chat history, and removes its mirror from connected clients. " +
      "The remote repository is NOT touched. This cannot be undone.",
    confirmText: "remove project",
  });
  if (!ok) return;
  withButton($("#remove-project-btn"), "removing…", async () => {
    try {
      const r = await api(`/api/projects/${state.activeId}`, { method: "DELETE" });
      const mirrors = r.mirrors_cleaned.length ? ` (mirrors cleaned: ${r.mirrors_cleaned.join(", ")})` : "";
      const failed = Object.entries(r.mirror_errors || {})
        .map(([c, why]) => `${c}: ${why}`).join("; ");
      console.info(`removed ${r.name}${mirrors}${failed ? " — mirror errors: " + failed : ""}`);
      location.reload();  // the active project is gone — restart from the project list
    } catch (err) {
      systemLine(`✗ remove project failed: ${err.message}`, true);
    }
  });
});

/* ---------- agent tab: project prompt, MCP servers, skills ---------- */

async function loadAgentPanel() {
  if (state.activeId == null) return;
  try {
    const cfg = await api(`/api/projects/${state.activeId}/agent`);
    $("#agent-prompt").value = cfg.agent_prompt;
    $("#agent-mcp").value = cfg.mcp_servers;
    renderSkills(cfg.skills);
    $("#agent-prompt-status").textContent = "";
    $("#agent-mcp-status").textContent = "";
    $("#skill-status").textContent = "";
  } catch (err) {
    $("#agent-prompt-status").textContent = "failed to load: " + err.message;
  }
}

function renderSkills(skills) {
  const ul = $("#skill-list");
  ul.innerHTML = "";
  if (!skills.length) {
    ul.innerHTML = '<li class="skill-empty">no skills yet — drop a SKILL.md below</li>';
    return;
  }
  for (const skill of skills) {
    const li = document.createElement("li");
    li.className = "skill-row";
    const name = document.createElement("span");
    name.className = "skill-name";
    name.textContent = skill.name;
    const desc = document.createElement("span");
    desc.className = "skill-desc";
    desc.textContent = skill.description || "";
    desc.title = skill.description || "";
    const x = document.createElement("button");
    x.type = "button";
    x.className = "attach-remove";
    x.textContent = "×";
    x.title = "Delete this skill from the repo (commits the removal)";
    x.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: "remove skill",
        message: `Delete skill "${skill.name}" (.claude/skills/${skill.name}/) from the repo? The removal is committed.`,
        confirmText: "remove skill",
      });
      if (!ok) return;
      try {
        await api(`/api/projects/${state.activeId}/skills/${encodeURIComponent(skill.name)}`, { method: "DELETE" });
        loadAgentPanel();
      } catch (err) {
        $("#skill-status").textContent = "failed: " + err.message;
      }
    });
    li.append(name, desc, x);
    ul.appendChild(li);
  }
}

function saveAgentField(btn, statusEl, payload, busy) {
  withButton(btn, busy, async () => {
    statusEl.textContent = "";
    try {
      await api(`/api/projects/${state.activeId}/agent`, {
        method: "POST", body: JSON.stringify(payload),
      });
      statusEl.textContent = "saved — applies on the next turn";
    } catch (err) {
      statusEl.textContent = "✗ " + err.message;
    }
  });
}

$("#agent-prompt-save").addEventListener("click", () => {
  if (state.activeId == null) return;
  saveAgentField($("#agent-prompt-save"), $("#agent-prompt-status"),
    { agent_prompt: $("#agent-prompt").value }, "saving");
});

$("#agent-mcp-save").addEventListener("click", () => {
  if (state.activeId == null) return;
  saveAgentField($("#agent-mcp-save"), $("#agent-mcp-status"),
    { mcp_servers: $("#agent-mcp").value }, "saving");
});

async function uploadSkills(files) {
  if (state.activeId == null || !files.length) return;
  $("#skill-status").textContent = "uploading…";
  const fd = new FormData();
  for (const f of files) fd.append("files", f, f.name);
  try {
    const r = await api(`/api/projects/${state.activeId}/skills`, {
      method: "POST", body: fd, headers: {},
    });
    const failed = Object.entries(r.errors || {}).map(([f, why]) => `${f}: ${why}`);
    await loadAgentPanel();  // refresh rows first — it clears the status line
    $("#skill-status").textContent =
      (r.added.length ? `added ${r.added.join(", ")}${r.commit ? " @ " + r.commit.slice(0, 10) : ""}` : "") +
      (failed.length ? `  ✗ ${failed.join("; ")}` : "");
  } catch (err) {
    $("#skill-status").textContent = "✗ " + err.message;
  }
}

$("#skill-upload-btn").addEventListener("click", () => $("#skill-input").click());
$("#skill-input").addEventListener("change", async (e) => {
  await uploadSkills([...e.target.files]);
  e.target.value = "";
});
const skillDrop = $("#skill-drop");
skillDrop.addEventListener("dragover", (e) => {
  e.preventDefault();
  skillDrop.classList.add("drop-active");
});
skillDrop.addEventListener("dragleave", () => skillDrop.classList.remove("drop-active"));
skillDrop.addEventListener("drop", (e) => {
  e.preventDefault();
  skillDrop.classList.remove("drop-active");
  uploadSkills([...e.dataTransfer.files]);
});

$("#archive-btn").addEventListener("click", () => {
  if (state.activeId == null) return;
  // A plain navigation: the endpoint answers with Content-Disposition
  // attachment, so the browser downloads without leaving the page.
  window.location.assign(`${BASE}/api/projects/${state.activeId}/archive`);
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

// Clear chat: the web equivalent of /clear — wipes this project's conversation
// and resets the agent's context (same branch). Destructive, so confirm first.
$("#clear-chat-btn").addEventListener("click", async () => {
  if (state.activeId == null) return;
  const ok = await confirmDialog({
    title: "clear chat",
    message: "Clear this chat? This erases the conversation and starts a fresh agent context. Your code and branch are not affected.",
    confirmText: "clear chat",
  });
  if (!ok) return;
  withButton($("#clear-chat-btn"), "clearing", async () => {
    try {
      await api(`/api/projects/${state.activeId}/clear`, { method: "POST" });
      $("#transcript").innerHTML = "";
      systemLine("✓ chat cleared — fresh agent context, same branch");
    } catch (err) {
      systemLine(`✗ clear failed: ${err.message}`, true);
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
// Send a message to the active project's agent — used by the chat form and by
// repo actions that hand work to the agent (e.g. rebase-conflict resolution).
function sendChat(text) {
  if (!text || state.activeId == null || !state.ws) return;
  setTab("chat");
  addMessage("user", text);
  state.ws.send(JSON.stringify({ type: "message", project_id: state.activeId, text }));
}

/* ---------- chat attachments ----------
   Files for the agent to LOOK AT, not repo content: uploaded to .lab-uploads/
   inside the clone (excluded from commits and client mirrors), referenced by
   relative path in the message so the agent reads them with its Read tool. */
const attachments = [];

function renderAttachChips() {
  const box = $("#attach-chips");
  // Rebuild the completed chips; in-flight (pending) ones own their lifecycle.
  box.querySelectorAll(".attach-chip:not(.attach-pending)").forEach((el) => el.remove());
  const firstPending = box.querySelector(".attach-pending");
  box.hidden = attachments.length === 0 && firstPending === null;
  attachments.forEach((a, i) => {
    const chip = document.createElement("span");
    chip.className = "attach-chip";
    chip.textContent = a.name + " ";
    const x = document.createElement("button");
    x.type = "button";
    x.className = "attach-remove";
    x.textContent = "×";
    x.title = "Remove this attachment from the next message (the uploaded file stays in .lab-uploads/)";
    x.addEventListener("click", () => { attachments.splice(i, 1); renderAttachChips(); });
    chip.appendChild(x);
    box.insertBefore(chip, firstPending);
  });
}

// One request per file so each pill can show its own upload progress
// (fetch has no upload-progress events — this is XHR territory).
function uploadOneAttachment(file, projectId) {
  return new Promise((resolve) => {
    const box = $("#attach-chips");
    box.hidden = false;
    const chip = document.createElement("span");
    chip.className = "attach-chip attach-pending";
    chip.textContent = `${file.name} · 0%`;
    box.appendChild(chip);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${BASE}/api/projects/${projectId}/chat-upload`);
    xhr.upload.addEventListener("progress", (e) => {
      if (e.lengthComputable) {
        chip.textContent = `${file.name} · ${Math.round((e.loaded / e.total) * 100)}%`;
      }
    });
    xhr.addEventListener("loadend", () => {
      chip.remove();
      try {
        const r = JSON.parse(xhr.responseText || "{}");
        if (xhr.status >= 200 && xhr.status < 300) {
          // Only attach to the message if the user is still on this project.
          if (state.activeId === projectId) attachments.push(...(r.saved || []));
          for (const [name, why] of Object.entries(r.errors || {})) {
            systemLine(`✗ attach ${name} failed: ${why}`, true);
          }
        } else {
          systemLine(`✗ attach ${file.name} failed: ${r.detail || xhr.status}`, true);
        }
      } catch {
        systemLine(`✗ attach ${file.name} failed`, true);
      }
      renderAttachChips();
      resolve();
    });

    const fd = new FormData();
    fd.append("files", file, file.name);
    xhr.send(fd);
  });
}

async function uploadAttachments(fileList) {
  if (state.activeId == null || !fileList.length) return;
  const pid = state.activeId;
  await Promise.all([...fileList].map((f) => uploadOneAttachment(f, pid)));
}

$("#attach-btn").addEventListener("click", () => $("#attach-input").click());
$("#attach-input").addEventListener("change", async (e) => {
  await uploadAttachments([...e.target.files]);
  e.target.value = "";
});
// Pasting a screenshot (or any file) into the textarea attaches it.
textarea.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) {
    e.preventDefault();
    uploadAttachments(files);
  }
});

// Dropping files anywhere on the chat panel attaches them too. A depth
// counter keeps the overlay steady while dragging across child elements.
const chatPanel = $("#panel-chat");
let chatDragDepth = 0;
chatPanel.addEventListener("dragenter", (e) => {
  if (state.activeId == null || ![...e.dataTransfer.types].includes("Files")) return;
  e.preventDefault();
  chatDragDepth++;
  $("#chat-drop").hidden = false;
});
chatPanel.addEventListener("dragover", (e) => e.preventDefault());
chatPanel.addEventListener("dragleave", () => {
  if (--chatDragDepth <= 0) {
    chatDragDepth = 0;
    $("#chat-drop").hidden = true;
  }
});
chatPanel.addEventListener("drop", (e) => {
  e.preventDefault();
  chatDragDepth = 0;
  $("#chat-drop").hidden = true;
  const files = [...e.dataTransfer.files];
  if (files.length) uploadAttachments(files);
});

$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  let text = textarea.value.trim();
  if (!text && !attachments.length) return;
  if (attachments.length) {
    if (!text) text = "Have a look at the attached file(s).";
    text += "\n\nAttached files (in the project tree — read them from disk):\n" +
      attachments.map((a) => `- ${a.path}`).join("\n");
    attachments.length = 0;
    renderAttachChips();
  }
  textarea.value = "";
  textarea.style.height = "auto";
  sendChat(text);
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

async function renderMarkdown(el, text, baseDir) {
  el.innerHTML = DOMPurify.sanitize(marked.parse(text || ""));
  // When rendering a markdown *file*, resolve its relative image references
  // (e.g. ![](img/diagram.png)) against the file's directory and point them at
  // the project's raw-file endpoint — the browser can't fetch repo-relative
  // paths on its own. baseDir is undefined for chat markdown, leaving src as-is.
  if (baseDir !== undefined) {
    for (const img of el.querySelectorAll("img")) {
      const rel = resolveRepoPath(baseDir, img.getAttribute("src") || "");
      if (rel != null) {
        img.src = `${BASE}/api/projects/${state.activeId}/raw?path=${encodeURIComponent(rel)}`;
      }
    }
  }
  // Syntax-highlight fenced code blocks (skip mermaid — handled below).
  for (const code of el.querySelectorAll("pre code")) {
    if (code.classList.contains("language-mermaid")) continue;
    const cls = [...code.classList].find((c) => c.startsWith("language-"));
    highlightInto(code, code.textContent, cls ? cls.slice("language-".length) : "");
  }
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
  const ws = new WebSocket(`${proto}://${location.host}${BASE}/ws`);
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
  if (e.type === "clients_changed") { loadClients(); return; }
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
        const c = document.createElement("button");
        c.type = "button";
        c.className = "commit-chip commit-link";
        c.title = "view file diffs for this commit";
        c.textContent = "✓ " + e.commit_sha.slice(0, 10) + "  view diff";
        c.addEventListener("click", () => openDiff(e.project_id, e.commit_sha));
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
        `<button class="mini" data-act="password" data-id="${u.id}" title="Set a new password for this user">reset pw</button>`,
        self ? "" : `<button class="mini" data-act="block" data-id="${u.id}" data-blocked="${u.blocked}" title="${u.blocked ? "Restore this user's access" : "Block this user from logging in"}">${u.blocked ? "unblock" : "block"}</button>`,
        self ? "" : `<button class="mini danger" data-act="delete" data-id="${u.id}" title="Permanently delete this user account">delete</button>`,
      ].join("");
      tr.innerHTML =
        `<td>${escapeHtml(u.username)}${self ? " <span class='you' title='This is your own account'>you</span>" : ""}</td>` +
        `<td>${u.is_super ? "super ★" : "user"}</td>` +
        `<td>${u.blocked ? "blocked" : "active"}</td>` +
        `<td class="actions-col">${actions}</td>`;
      rows.appendChild(tr);
    }
  } catch (err) { adminError(err.message); }
}

$("#who").addEventListener("click", () => { if (state.isSuper) openAdmin(); });
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
      if (!(await confirmDialog({
        title: "delete user",
        message: "Delete this user permanently?",
        confirmText: "delete",
      }))) return;
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

/* ---------- modals: commit diff + repo browser ---------- */
// Close buttons (and Escape, handled natively by <dialog>) close their modal.
document.querySelectorAll("[data-close]").forEach((btn) => {
  btn.addEventListener("click", () => $("#" + btn.dataset.close).close());
});

async function openDiff(projectId, sha) {
  const body = $("#diff-body");
  $("#diff-title").textContent = sha.slice(0, 10);
  body.innerHTML = '<div class="modal-loading">loading diff…</div>';
  $("#diff-dialog").showModal();
  try {
    const r = await api(`/api/projects/${projectId}/commits/${sha}/diff`);
    $("#diff-title").textContent = sha.slice(0, 10) + (r.subject ? "  ·  " + r.subject : "");
    DiffViewer.render(r.diff, body);
  } catch (err) {
    body.innerHTML = "";
    const p = document.createElement("p");
    p.className = "diff-empty";
    p.textContent = "failed to load diff: " + err.message;
    body.appendChild(p);
  }
}

/* repo browser — lazy one-level-at-a-time tree over the working clone, plus
   tabs for any connected platform client holding a mirror of the project */
const fileBrowser = { activePath: null, source: "lab", clients: [] };

$("#browse-btn").addEventListener("click", () => {
  if (state.activeId != null) openFiles();
});

async function openFiles() {
  fileBrowser.source = "lab";
  fileBrowser.clients = [];
  const p = state.projects.find((x) => x.id === state.activeId);
  $("#files-title").textContent = (p ? p.name : "files") + " — files";
  $("#files-sources").hidden = true;
  $("#files-dialog").showModal();
  loadSource();
  // Probe for client mirrors in parallel — the lab tree must not wait on it.
  api(`/api/projects/${state.activeId}/clients`)
    .then((clients) => { fileBrowser.clients = clients; renderSourceTabs(); })
    .catch(() => {});
}

function renderSourceTabs() {
  const el = $("#files-sources");
  el.innerHTML = "";
  el.hidden = fileBrowser.clients.length === 0;
  const tabs = [{ name: "lab", label: "lab" }].concat(
    fileBrowser.clients.map((c) => ({ name: c.name, label: `${c.name} (${c.platform})` })),
  );
  for (const t of tabs) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "files-source ghost" + (fileBrowser.source === t.name ? " active" : "");
    btn.textContent = t.label;
    btn.addEventListener("click", () => {
      if (fileBrowser.source === t.name) return;
      fileBrowser.source = t.name;
      renderSourceTabs();
      loadSource();
    });
    el.appendChild(btn);
  }
}

async function loadSource() {
  fileBrowser.activePath = null;
  $("#file-content").innerHTML = '<div class="file-empty">select a file to view it</div>';
  const tree = $("#file-tree");
  tree.innerHTML = '<div class="modal-loading">loading…</div>';
  $("#files-context").hidden = true;
  try {
    if (fileBrowser.source === "lab") {
      const { entries, branch, base, missing } = await api(`/api/projects/${state.activeId}/tree`);
      renderFilesContext(branch, base, missing);
      renderTree(tree, entries, 0);
    } else {
      const { paths } = await api(
        `/api/projects/${state.activeId}/clients/${encodeURIComponent(fileBrowser.source)}/mirror`
      );
      renderMirrorContext(paths.length);
      renderTree(tree, mirrorEntries(paths), 0);
    }
  } catch (err) {
    tree.innerHTML = "";
    const d = document.createElement("div");
    d.className = "file-empty";
    d.textContent = "failed: " + err.message;
    tree.appendChild(d);
  }
}

// The mirror endpoint returns a flat sorted path list; nest it into the same
// {name, path, type, children} entries the tree renderer walks (dirs first).
function mirrorEntries(paths) {
  const root = { dirs: new Map(), files: [] };
  for (const path of paths) {
    const parts = path.split("/");
    let node = root;
    for (const part of parts.slice(0, -1)) {
      if (!node.dirs.has(part)) node.dirs.set(part, { dirs: new Map(), files: [] });
      node = node.dirs.get(part);
    }
    node.files.push(parts[parts.length - 1]);
  }
  const toEntries = (node, prefix) => {
    const dirs = [...node.dirs.keys()].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
    const files = node.files.sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
    return dirs.map((name) => ({
      name,
      path: prefix + name,
      type: "dir",
      children: toEntries(node.dirs.get(name), prefix + name + "/"),
    })).concat(files.map((name) => ({ name, path: prefix + name, type: "file" })));
  };
  return toEntries(root, "");
}

function renderMirrorContext(fileCount) {
  const el = $("#files-context");
  el.textContent = "";
  const b = document.createElement("span");
  b.className = "files-branch";
  b.textContent = `mirror on ${fileBrowser.source} · ${fileCount} file${fileCount === 1 ? "" : "s"}`;
  el.appendChild(b);
  const rm = document.createElement("button");
  rm.type = "button";
  rm.className = "danger files-mirror-remove";
  rm.textContent = "remove mirror";
  rm.title = "Delete this project's files from the client machine";
  rm.addEventListener("click", () => withButton(rm, "removing…", removeMirror));
  el.appendChild(rm);
  el.hidden = false;
}

async function removeMirror() {
  const client = fileBrowser.source;
  const ok = await confirmDialog({
    title: "remove mirror",
    message: `Delete this project's mirror (all synced files and run artifacts) from ${client}? ` +
      "The client stays connected; the mirror is re-synced on its next run.",
    confirmText: "remove mirror",
  });
  if (!ok) return;
  try {
    await api(
      `/api/projects/${state.activeId}/clients/${encodeURIComponent(client)}/clean`,
      { method: "POST" },
    );
  } catch (err) {
    $("#files-context").appendChild(Object.assign(document.createElement("span"), {
      className: "files-missing", textContent: "failed: " + err.message,
    }));
    return;
  }
  fileBrowser.clients = fileBrowser.clients.filter((c) => c.name !== client);
  fileBrowser.source = "lab";
  renderSourceTabs();
  loadSource();
}

function renderFilesContext(branch, base, missing) {
  const el = $("#files-context");
  if (!branch) {
    el.hidden = true;
    return;
  }
  el.textContent = "";
  const onBase = base && branch === base;
  const b = document.createElement("span");
  b.className = "files-branch";
  b.textContent = onBase ? `on ${branch}` : `on ${branch} · base ${base}`;
  el.appendChild(b);
  if (missing > 0) {
    const warn = document.createElement("span");
    warn.className = "files-missing";
    warn.textContent = `${missing} file${missing === 1 ? "" : "s"} on ${base} not in this checkout`;
    el.appendChild(warn);
  }
  appendUploadControls(el);
  el.hidden = false;
}

// Upload into the working tree (lab source only). Files are committed straight
// away — a dangling uncommitted upload would block the next chat session.
function appendUploadControls(el) {
  const dest = document.createElement("input");
  dest.className = "upload-dest";
  dest.placeholder = "dir (empty = root)";
  dest.title = "Repo-relative directory to upload into; empty for the repo root";
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "ghost files-upload";
  btn.textContent = "upload files";
  btn.title = "Upload files into the working tree at the given directory. They are committed immediately.";
  const picker = document.createElement("input");
  picker.type = "file";
  picker.multiple = true;
  picker.hidden = true;
  btn.addEventListener("click", () => picker.click());
  picker.addEventListener("change", () => {
    if (!picker.files.length) return;
    withButton(btn, "uploading…", async () => {
      const fd = new FormData();
      for (const f of picker.files) fd.append("files", f, f.name);
      fd.append("dest", dest.value.trim());
      try {
        const r = await api(`/api/projects/${state.activeId}/upload`, {
          method: "POST", body: fd, headers: {},
        });
        for (const [name, why] of Object.entries(r.errors || {})) {
          systemLine(`✗ upload ${name} failed: ${why}`, true);
        }
        if (r.written.length) {
          systemLine(`✓ uploaded ${r.written.join(", ")}` +
            (r.commit ? ` @ ${r.commit.slice(0, 10)}` : ""));
        }
        await loadSource();  // fresh tree (and context bar) with the new files
      } catch (err) {
        systemLine(`✗ upload failed: ${err.message}`, true);
      }
    });
  });
  el.appendChild(btn);
  el.appendChild(dest);
  el.appendChild(picker);
}

const STATUS_LABEL = { new: "A", modified: "M", deleted: "D" };

function renderTree(container, entries, depth) {
  container.innerHTML = "";
  for (const entry of entries) appendTreeNode(container, entry, depth);
}

function appendTreeNode(container, entry, depth) {
  const row = document.createElement("div");
  row.className = "tree-item tree-" + entry.type;
  row.style.paddingLeft = 8 + depth * 14 + "px";
  if (entry.path === fileBrowser.activePath) row.classList.add("active");
  const icon = entry.type === "dir" ? "▸ " : "";
  row.innerHTML = `<span class="tree-icon">${icon}</span><span class="tree-name"></span>`;
  row.querySelector(".tree-name").textContent = entry.name;
  if (entry.status) {
    row.classList.add("tree-changed", "tree-status-" + entry.status);
    const badge = document.createElement("span");
    badge.className = "tree-status";
    badge.textContent = entry.type === "dir" ? "●" : STATUS_LABEL[entry.status] || "●";
    badge.title =
      entry.type === "dir" ? "contains changes vs base" : "differs from base (" + entry.status + ")";
    row.appendChild(badge);
  }
  container.appendChild(row);

  if (entry.type === "dir") {
    const kids = document.createElement("div");
    kids.className = "tree-children";
    kids.hidden = true;
    container.appendChild(kids);
    let loaded = false;
    row.addEventListener("click", async () => {
      const open = kids.hidden;
      kids.hidden = !open;
      row.querySelector(".tree-icon").textContent = open ? "▾ " : "▸ ";
      if (open && !loaded) {
        loaded = true;
        if (entry.children) {
          // Mirror trees arrive whole — children are embedded, nothing to fetch.
          renderTree(kids, entry.children, depth + 1);
          return;
        }
        kids.innerHTML = '<div class="modal-loading" style="padding-left:' + (8 + (depth + 1) * 14) + 'px">…</div>';
        try {
          const { entries } = await api(
            `/api/projects/${state.activeId}/tree?path=${encodeURIComponent(entry.path)}`
          );
          renderTree(kids, entries, depth + 1);
        } catch (err) {
          loaded = false;
          kids.innerHTML = "";
          const d = document.createElement("div");
          d.className = "file-empty";
          d.textContent = "failed: " + err.message;
          kids.appendChild(d);
        }
      }
    });
  } else {
    row.addEventListener("click", () => openFile(entry.path, entry.name));
  }
}

// Endpoint prefixes for the active browser source: file/raw under the lab tree
// vs the same pair on a client mirror.
function sourceFileUrl(path) {
  return fileBrowser.source === "lab"
    ? `/api/projects/${state.activeId}/file?path=${encodeURIComponent(path)}`
    : `/api/projects/${state.activeId}/clients/${encodeURIComponent(fileBrowser.source)}/file?path=${encodeURIComponent(path)}`;
}

function sourceRawUrl(path) {
  return fileBrowser.source === "lab"
    ? `${BASE}/api/projects/${state.activeId}/raw?path=${encodeURIComponent(path)}`
    : `${BASE}/api/projects/${state.activeId}/clients/${encodeURIComponent(fileBrowser.source)}/raw?path=${encodeURIComponent(path)}`;
}

async function openFile(path, name) {
  fileBrowser.activePath = path;
  document.querySelectorAll("#file-tree .tree-item.active").forEach((el) => el.classList.remove("active"));
  document.querySelectorAll("#file-tree .tree-file").forEach((el) => {
    if (el.querySelector(".tree-name")?.textContent === name) el.classList.add("active");
  });
  const content = $("#file-content");
  content.innerHTML = '<div class="modal-loading">loading…</div>';
  try {
    const data = await api(sourceFileUrl(path));
    renderFile(content, name, data);
  } catch (err) {
    content.innerHTML = "";
    const d = document.createElement("div");
    d.className = "file-empty";
    d.textContent = "failed: " + err.message;
    content.appendChild(d);
  }
}

const IMAGE_RE = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i;

// Resolve a relative/repo-absolute reference (from a markdown file at baseDir)
// to a clean repo-relative path. Returns null for external/absolute URLs and
// data URIs, which should be left untouched.
function resolveRepoPath(baseDir, ref) {
  ref = (ref || "").replace(/[?#].*$/, "");
  if (!ref || /^(https?:|data:|blob:|\/\/|mailto:)/i.test(ref)) return null;
  try { ref = decodeURIComponent(ref); } catch { /* keep raw ref */ }
  const parts = ref.startsWith("/")
    ? ref.split("/")
    : (baseDir ? baseDir.split("/") : []).concat(ref.split("/"));
  const stack = [];
  for (const seg of parts) {
    if (seg === "" || seg === ".") continue;
    if (seg === "..") stack.pop();
    else stack.push(seg);
  }
  return stack.length ? stack.join("/") : null;
}

function dirOf(path) {
  const i = path.lastIndexOf("/");
  return i === -1 ? "" : path.slice(0, i);
}

// Map a filename to a highlight.js language id. Covers common source files by
// extension, plus a few well-known basenames (Dockerfile, Makefile, …). Returns
// "" when we have no good guess — highlightInto then falls back to auto-detect.
const EXT_LANG = {
  js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "javascript",
  ts: "typescript", tsx: "typescript", py: "python", pyw: "python",
  rb: "ruby", go: "go", rs: "rust", java: "java", kt: "kotlin", kts: "kotlin",
  swift: "swift", c: "c", h: "c", cc: "cpp", cpp: "cpp", cxx: "cpp",
  hpp: "cpp", hh: "cpp", cs: "csharp", php: "php", pl: "perl", pm: "perl",
  lua: "lua", r: "r", m: "objectivec", sh: "shell", bash: "shell",
  zsh: "shell", fish: "shell", sql: "sql", html: "xml", htm: "xml",
  xml: "xml", svg: "xml", vue: "xml", css: "css", scss: "scss", less: "less",
  json: "json", yaml: "yaml", yml: "yaml", toml: "ini", ini: "ini",
  cfg: "ini", conf: "ini", md: "markdown", markdown: "markdown",
  diff: "diff", patch: "diff", graphql: "graphql", gql: "graphql",
};
const BASENAME_LANG = {
  dockerfile: "dockerfile", makefile: "makefile", gnumakefile: "makefile",
  cmakelists: "makefile", gemfile: "ruby", rakefile: "ruby",
  ".bashrc": "shell", ".zshrc": "shell", ".gitignore": "plaintext",
};

function langForFilename(name) {
  const base = name.toLowerCase();
  if (BASENAME_LANG[base]) return BASENAME_LANG[base];
  const noext = base.replace(/\.[^.]+$/, "");
  if (BASENAME_LANG[noext]) return BASENAME_LANG[noext];
  const ext = base.includes(".") ? base.slice(base.lastIndexOf(".") + 1) : "";
  return EXT_LANG[ext] || "";
}

// Syntax-highlight `code` into the given <code> element with highlight.js.
// Falls back to plain text if hljs is unavailable, the language is unknown, or
// highlighting throws — never lets a render error blank the viewer.
function highlightInto(code, text, lang) {
  text = text || "";
  if (typeof hljs === "undefined") { code.textContent = text; return; }
  try {
    if (lang && lang !== "plaintext" && hljs.getLanguage(lang)) {
      code.innerHTML = hljs.highlight(text, { language: lang, ignoreIllegals: true }).value;
    } else {
      code.innerHTML = hljs.highlightAuto(text).value;
    }
    code.classList.add("hljs");
  } catch {
    code.textContent = text;
  }
}

function renderFile(container, name, data) {
  container.innerHTML = "";
  const crumb = document.createElement("div");
  crumb.className = "file-crumb";
  crumb.textContent = data.path + "  ·  " + formatSize(data.size);
  if (fileBrowser.source === "lab") {
    // Plain download of the working-tree file, served by the raw endpoint.
    const dl = document.createElement("a");
    dl.className = "file-download";
    dl.textContent = "download";
    dl.href = sourceRawUrl(data.path);
    dl.download = name;
    dl.title = "Download this file";
    crumb.appendChild(dl);
  } else {
    // A client-mirror file can be copied into the lab's working tree — the web
    // counterpart of the agent's fetch_from_client tool.
    const fetchBtn = document.createElement("button");
    fetchBtn.type = "button";
    fetchBtn.className = "ghost file-fetch";
    fetchBtn.textContent = "fetch → lab";
    fetchBtn.title = "Copy this file from the client mirror into the lab's working tree";
    fetchBtn.addEventListener("click", () => withButton(fetchBtn, "fetching…", async () => {
      const r = await api(
        `/api/projects/${state.activeId}/clients/${encodeURIComponent(fileBrowser.source)}/fetch`,
        { method: "POST", body: JSON.stringify({ paths: [data.path] }) },
      );
      const err = r.errors && r.errors[data.path];
      fetchBtn.replaceWith(Object.assign(document.createElement("span"), {
        className: "file-fetch-result" + (err ? " files-missing" : ""),
        textContent: err ? "failed: " + err : "copied to lab working tree",
      }));
    }));
    crumb.appendChild(fetchBtn);
  }
  container.appendChild(crumb);

  if (IMAGE_RE.test(name)) {
    const img = document.createElement("img");
    img.className = "file-img";
    img.alt = name;
    img.src = sourceRawUrl(data.path);
    img.addEventListener("error", () => {
      img.replaceWith(
        Object.assign(document.createElement("div"), {
          className: "file-empty",
          textContent: "could not load image — " + formatSize(data.size),
        }),
      );
    });
    container.appendChild(img);
    return;
  }

  if (data.binary) {
    const d = document.createElement("div");
    d.className = "file-empty";
    d.textContent = "binary file — " + formatSize(data.size);
    container.appendChild(d);
    return;
  }
  const view = document.createElement("div");
  if (/\.md$/i.test(name)) {
    view.className = "msg-body file-md";
    renderMarkdown(view, data.content, dirOf(data.path));
  } else {
    const pre = document.createElement("pre");
    pre.className = "file-code";
    const code = document.createElement("code");
    highlightInto(code, data.content, langForFilename(name));
    pre.appendChild(code);
    view.appendChild(pre);
  }
  container.appendChild(view);
  if (data.truncated) {
    const t = document.createElement("div");
    t.className = "file-truncated";
    t.textContent = "… truncated (showing first 512 KB)";
    container.appendChild(t);
  }
}

function formatSize(bytes) {
  if (bytes == null) return "";
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

checkAuth();
