"use strict";

/* Renders a unified `git` patch into a container as collapsible per-file blocks.
   Pure DOM, no deps — given the raw diff text from /commits/<sha>/diff. */
const DiffViewer = {
  render(diffText, container) {
    container.innerHTML = "";
    const files = this.parseDiff(diffText || "");
    if (files.length === 0) {
      const p = document.createElement("p");
      p.className = "diff-empty";
      p.textContent = "no file changes in this commit";
      container.appendChild(p);
      return;
    }

    for (const file of files) {
      const fileEl = document.createElement("div");
      fileEl.className = "diff-file";

      const header = document.createElement("div");
      header.className = "diff-file-header";
      const adds = file.lines.filter((l) => l.startsWith("+") && !l.startsWith("+++")).length;
      const dels = file.lines.filter((l) => l.startsWith("-") && !l.startsWith("---")).length;
      header.innerHTML =
        `<span class="diff-path"></span>` +
        `<span class="diff-stat"><span class="diff-stat-add">+${adds}</span> ` +
        `<span class="diff-stat-del">-${dels}</span></span>`;
      header.querySelector(".diff-path").textContent = file.path;
      header.addEventListener("click", () => fileEl.classList.toggle("collapsed"));
      fileEl.appendChild(header);

      const body = document.createElement("div");
      body.className = "diff-file-body";
      for (const line of file.lines) {
        const lineEl = document.createElement("div");
        lineEl.className = "diff-line";
        if (line.startsWith("+") && !line.startsWith("+++")) lineEl.classList.add("diff-add");
        else if (line.startsWith("-") && !line.startsWith("---")) lineEl.classList.add("diff-del");
        else if (line.startsWith("@@")) lineEl.classList.add("diff-hunk");
        lineEl.textContent = line || " ";
        body.appendChild(lineEl);
      }
      fileEl.appendChild(body);
      container.appendChild(fileEl);
    }
  },

  parseDiff(text) {
    const files = [];
    let current = null;
    for (const line of text.split("\n")) {
      if (line.startsWith("diff --git")) {
        const m = line.match(/ b\/(.+)$/);
        current = { path: m ? m[1] : "?", lines: [] };
        files.push(current);
      } else if (current) {
        current.lines.push(line);
      }
    }
    return files;
  },
};
