/* inspeg service worker: context-menu slots, capture dispatch, digest cache.
 *
 * MV3 discipline: this worker can be killed and restarted between menu
 * creation and click, so nothing load-bearing lives in module state — the
 * slot->label map is in chrome.storage.session, menus are recreated on
 * install/startup, and onClicked re-reads storage.
 *
 * All daemon calls carry X-Inspeg-Capture and are allowlisted server-side by
 * this extension's pinned origin (ADR 0007 / V15).
 */
"use strict";

const DAEMON = "http://127.0.0.1:8137";
const SLOT_COUNT = 10;
const CAPTURE_CONTEXTS = ["selection", "image", "link", "video", "audio"];

// ── daemon client ───────────────────────────────────────────────────────────

async function daemonFetch(path, options = {}) {
  const headers = Object.assign(
    { "X-Inspeg-Capture": "1" },
    options.body ? { "Content-Type": "application/json" } : {},
    options.headers || {},
  );
  const response = await fetch(DAEMON + path, Object.assign({}, options, { headers }));
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`);
  }
  return response.json();
}

async function daemonUp() {
  try {
    await daemonFetch("/api/health");
    chrome.action.setBadgeText({ text: "" });
    return true;
  } catch {
    chrome.action.setBadgeText({ text: "!" });
    chrome.action.setBadgeBackgroundColor({ color: "#c0392b" });
    chrome.action.setTitle({ title: "inspeg daemon unreachable — run: python -m inspeg" });
    return false;
  }
}

// ── menus ───────────────────────────────────────────────────────────────────

function createMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "inspeg",
      title: "Inspeg",
      contexts: CAPTURE_CONTEXTS,
    });
    chrome.contextMenus.create({
      id: "capture",
      parentId: "inspeg",
      title: "Capture",
      contexts: CAPTURE_CONTEXTS,
    });
    chrome.contextMenus.create({
      id: "capture-as",
      parentId: "inspeg",
      title: "Capture as",
      contexts: CAPTURE_CONTEXTS,
    });
    for (let i = 0; i < SLOT_COUNT; i++) {
      chrome.contextMenus.create({
        id: `slot-${i}`,
        parentId: "capture-as",
        title: "…",
        visible: false,
        contexts: CAPTURE_CONTEXTS,
      });
    }
    chrome.contextMenus.create({
      id: "new-label",
      parentId: "capture-as",
      title: "New label…",
      contexts: CAPTURE_CONTEXTS,
    });
    chrome.contextMenus.create({
      id: "open-hud",
      parentId: "inspeg",
      title: "Open inspeg",
      contexts: CAPTURE_CONTEXTS,
    });
    refreshSlots();
  });
}

/* Chrome has no menus.onShown: slots are pre-created and re-titled after
 * every capture and on storage change — the menu is always one capture
 * stale at worst, never a right-click-time RPC. */
async function refreshSlots() {
  let labels = [];
  try {
    labels = await daemonFetch(`/api/labels?sort=recent&limit=${SLOT_COUNT}`);
  } catch {
    return; // daemon down: keep whatever the menu already shows
  }
  await chrome.storage.session.set({ slotLabels: labels.map((entry) => entry.label) });
  for (let i = 0; i < SLOT_COUNT; i++) {
    const label = labels[i];
    chrome.contextMenus.update(`slot-${i}`, {
      title: label ? label.label : "…",
      visible: Boolean(label),
    });
  }
}

chrome.runtime.onInstalled.addListener(() => {
  createMenus();
  refreshDigests();
  daemonUp();
});
chrome.runtime.onStartup.addListener(() => {
  createMenus();
  refreshDigests();
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "session" && changes.slotLabels) {
    // another worker instance refreshed; menu titles already updated there
  }
});

// ── captures ────────────────────────────────────────────────────────────────

function guessMime(url, info) {
  const path = (() => {
    try {
      return new URL(url).pathname.toLowerCase();
    } catch {
      return "";
    }
  })();
  const byExt = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".pdf": "application/pdf",
  };
  for (const [ext, mime] of Object.entries(byExt)) {
    if (path.endsWith(ext)) return mime;
  }
  if (info.mediaType === "image") return "image/unknown";
  if (info.mediaType === "video") return "video/unknown";
  if (info.mediaType === "audio") return "audio/unknown";
  if (info.linkUrl) return "text/html";
  return "application/octet-stream";
}

async function grabSelectionFromTab(tabId) {
  // Rejects when there is no content script (chrome PDF viewer, chrome://).
  return chrome.tabs.sendMessage(tabId, { type: "grab-selection" });
}

async function toast(tabId, text, ok) {
  try {
    await chrome.tabs.sendMessage(tabId, { type: "toast", text, ok });
  } catch {
    // No content script (PDF viewer): fall back to a transient badge.
    chrome.action.setBadgeText({ text: ok ? "✓" : "✗" });
    chrome.action.setBadgeBackgroundColor({ color: ok ? "#27ae60" : "#c0392b" });
    setTimeout(() => chrome.action.setBadgeText({ text: "" }), 2500);
  }
}

async function captureFromClick(info, tab, labels) {
  if (info.srcUrl || info.linkUrl) {
    const target = info.srcUrl || info.linkUrl;
    return daemonFetch("/api/captures/pointer", {
      method: "POST",
      body: JSON.stringify({
        kind: "url",
        target,
        mimetype: guessMime(target, info),
        page_uri: info.pageUrl,
        source_title: tab && tab.title ? tab.title : null,
        labels,
        surface: "browser",
      }),
    });
  }
  // Selection capture: full-fidelity via the content script; the Chrome PDF
  // viewer runs no content scripts, so fall back to selectionText + a
  // document pointer (provenance 'sourced' instead of 'exact').
  let grabbed = null;
  try {
    grabbed = await grabSelectionFromTab(tab.id);
  } catch {
    grabbed = null;
  }
  if (grabbed && grabbed.ok) {
    return daemonFetch("/api/captures/selection", {
      method: "POST",
      body: JSON.stringify(Object.assign({ labels }, grabbed.payload)),
    });
  }
  if (!info.selectionText) {
    throw new Error("nothing selected");
  }
  return daemonFetch("/api/captures/selection", {
    method: "POST",
    body: JSON.stringify({
      url: info.pageUrl,
      title: tab && tab.title ? tab.title : null,
      selection_exact: info.selectionText,
      labels,
    }),
  });
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (!tab || tab.id === undefined) return;
  const id = String(info.menuItemId);
  try {
    if (id === "open-hud") {
      chrome.tabs.create({ url: `${DAEMON}/` });
      return;
    }
    let labels = [];
    if (id.startsWith("slot-")) {
      const { slotLabels = [] } = await chrome.storage.session.get("slotLabels");
      const label = slotLabels[Number(id.slice(5))];
      if (label) labels = [label];
    } else if (id === "new-label") {
      let response = null;
      try {
        response = await chrome.tabs.sendMessage(tab.id, { type: "prompt-label" });
      } catch {
        response = null; // no content script: capture unlabeled, label in the UI
      }
      if (response && response.label) labels = [response.label];
    } else if (id !== "capture") {
      return;
    }
    const result = await captureFromClick(info, tab, labels);
    const labelNote = labels.length ? ` as “${labels[0]}”` : "";
    await toast(tab.id, `inspeg: captured${labelNote} (${result.provenance})`, true);
    refreshSlots();
    refreshDigests();
  } catch (error) {
    await daemonUp();
    await toast(tab.id, `inspeg: capture failed — ${error.message}`, false);
  }
});

// ── highlight-on-revisit support ────────────────────────────────────────────

/* The extension never asks the daemon "do you have anchors for this URL?" per
 * navigation — that would ship browsing history into the request log. The
 * daemon publishes digests of its captured URLs; content scripts check
 * locally and only a HIT triggers a resolve call. */
async function refreshDigests() {
  try {
    const { digests } = await daemonFetch("/api/anchors/url-digests");
    await chrome.storage.local.set({ urlDigests: digests, urlDigestsAt: Date.now() });
  } catch {
    /* daemon down: keep the stale cache */
  }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message && message.type === "resolve-url") {
    daemonFetch(`/api/resolve?url=${encodeURIComponent(message.url)}`)
      .then((result) => sendResponse({ ok: true, result }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true; // async response
  }
  if (message && message.type === "refresh-digests") {
    refreshDigests().then(() => sendResponse({ ok: true }));
    return true;
  }
  return undefined;
});
