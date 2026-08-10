"use strict";

const DAEMON = "http://127.0.0.1:8137";

async function init() {
  const origin = `chrome-extension://${chrome.runtime.id}`;
  document.getElementById("origin").textContent = origin;
  document.getElementById("command").textContent =
    `python -m inspeg --extension-origin ${origin}`;

  const status = document.getElementById("status");
  try {
    const response = await fetch(`${DAEMON}/api/health`, {
      headers: { "X-Inspeg-Capture": "1" },
    });
    const health = await response.json();
    const listed = (health.extension_origins || []).includes(origin);
    status.textContent = listed
      ? `connected (v${health.version}) — origin allowlisted`
      : `connected (v${health.version}) — origin NOT allowlisted yet; restart the daemon with the command below`;
    status.className = listed ? "ok" : "bad";
  } catch {
    status.textContent = "unreachable — run: python -m inspeg";
    status.className = "bad";
  }

  const toggle = document.getElementById("context-toggle");
  const { reportContext = false } = await chrome.storage.local.get("reportContext");
  toggle.checked = reportContext;
  toggle.addEventListener("change", () => {
    chrome.storage.local.set({ reportContext: toggle.checked });
  });
}

init();
