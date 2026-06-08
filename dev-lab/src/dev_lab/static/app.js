"use strict";

const $ = (s) => document.querySelector(s);
const state = { user: null, projects: [], activeId: null, ws: null, assistantBody: null, activity: null, text: "" };

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
    state.user = (await api("/api/me")).username;
    await showApp();
  } catch {
    $("#login").hidden = false;
    $("#app").hidden = true;
  }
  document.body.classList.remove("loading");
}

async function doAuth(path) {
  const username = $("#username").value.trim();
  const password = $("#password").value;
  try {
    await api(path, { method: "POST", body: JSON.stringify({ username, password }) });
    location.reload();
  } catch (err) {
    $("#auth-error").textContent = err.message;
  }
}
$("#auth-form").addEventListener("submit", (e) => { e.preventDefault(); doAuth("/api/login"); });
$("#register-btn").addEventListener("click", () => doAuth("/api/register"));
$("#logout-btn").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  if (state.ws) state.ws.close();
  location.reload();
});

/* ---------- app shell ---------- */
async function showApp() {
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#who").textContent = state.user;
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
  $("#merge-btn").hidden = false;
  const t = $("#transcript");
  t.innerHTML = "";
  setStatus("idle");
  const msgs = await api(`/api/projects/${id}/messages`);
  for (const m of msgs) addMessage(m.role, m.content);
  $("#chat-text").focus();
}

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

checkAuth();
