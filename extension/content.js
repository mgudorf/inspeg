/* inspeg content script: selection grab, toasts, label prompt, and
 * highlight-on-revisit.
 *
 * Selection state is snapshotted on contextmenu BEFORE the menu opens —
 * info.selectionText is whitespace-mangled and href-free, so the Range is
 * the only faithful source. Highlights paint via the CSS Custom Highlight
 * API (no DOM mutation, so no MutationObserver feedback loop).
 */
"use strict";

(() => {
  const anchoring = globalThis.inspegAnchoring;
  const CONTEXT_CHARS = 32;
  const REANCHOR_ATTEMPTS = 5;
  const REANCHOR_WINDOW_MS = 10_000;

  // ── selection snapshot ────────────────────────────────────────────────────

  let lastSelection = null;

  function snapshotSelection() {
    const selection = document.getSelection();
    if (!selection || selection.isCollapsed || selection.rangeCount === 0) {
      return null;
    }
    const range = selection.getRangeAt(0);
    const exact = range.toString();
    if (!exact.trim()) {
      return null;
    }
    const docText = anchoring.pageText();
    const position = anchoring.rangeToTextPosition(range);
    let prefix = "";
    let suffix = "";
    let start = null;
    let end = null;
    if (position) {
      start = position.start;
      end = position.end;
      prefix = docText.slice(Math.max(0, start - CONTEXT_CHARS), start);
      suffix = docText.slice(end, end + CONTEXT_CHARS);
    }
    const container = document.createElement("div");
    container.appendChild(range.cloneContents()); // hrefs survive; selectionText's don't
    return {
      url: location.href,
      title: document.title || null,
      doc_text: docText,
      selection_exact: docText && start !== null ? docText.slice(start, end) : exact,
      selection_prefix: prefix,
      selection_suffix: suffix,
      selection_start: start,
      selection_end: end,
      selection_html: container.innerHTML || null,
    };
  }

  document.addEventListener("contextmenu", () => {
    lastSelection = snapshotSelection();
  });

  // ── toast ─────────────────────────────────────────────────────────────────

  function showToast(text, ok) {
    const toast = document.createElement("div");
    toast.textContent = text; // textContent only — never markup (V18)
    Object.assign(toast.style, {
      position: "fixed",
      right: "16px",
      bottom: "16px",
      zIndex: "2147483647",
      padding: "10px 14px",
      borderRadius: "6px",
      font: "13px system-ui, sans-serif",
      color: "#fff",
      background: ok ? "#1f6f43" : "#8f2f27",
      boxShadow: "0 2px 12px rgba(0,0,0,.35)",
      transition: "opacity .4s",
    });
    document.documentElement.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = "0";
      setTimeout(() => toast.remove(), 500);
    }, 1800);
  }

  // ── new-label prompt ──────────────────────────────────────────────────────

  function promptLabel() {
    return new Promise((resolve) => {
      const wrap = document.createElement("div");
      Object.assign(wrap.style, {
        position: "fixed",
        left: "50%",
        top: "20%",
        transform: "translateX(-50%)",
        zIndex: "2147483647",
        background: "#1e1e24",
        padding: "12px",
        borderRadius: "8px",
        boxShadow: "0 4px 24px rgba(0,0,0,.5)",
        font: "13px system-ui, sans-serif",
      });
      const caption = document.createElement("div");
      caption.textContent = "inspeg — label this capture:";
      caption.style.color = "#ddd";
      caption.style.marginBottom = "8px";
      const input = document.createElement("input");
      Object.assign(input.style, {
        width: "280px",
        padding: "6px 8px",
        borderRadius: "4px",
        border: "1px solid #555",
        background: "#111",
        color: "#eee",
      });
      wrap.append(caption, input);
      document.documentElement.appendChild(wrap);
      input.focus();
      const finish = (value) => {
        wrap.remove();
        resolve(value);
      };
      input.addEventListener("keydown", (event) => {
        if (event.key === "Enter") finish(input.value.trim() || null);
        if (event.key === "Escape") finish(null);
        event.stopPropagation();
      });
      input.addEventListener("blur", () => finish(null));
    });
  }

  // ── highlight-on-revisit ──────────────────────────────────────────────────

  const HIGHLIGHT_NAME = "inspeg-capture";
  const FLASH_NAME = "inspeg-flash";

  function injectHighlightStyle() {
    if (document.getElementById("inspeg-highlight-style")) return;
    const style = document.createElement("style");
    style.id = "inspeg-highlight-style";
    style.textContent =
      `::highlight(${HIGHLIGHT_NAME}) { background-color: rgba(255, 200, 60, 0.35); }` +
      `::highlight(${FLASH_NAME}) { background-color: rgba(255, 140, 0, 0.75); }`;
    document.documentElement.appendChild(style);
  }

  function anchorSelector(quoteSelector, hint) {
    const docText = anchoring.pageText();
    const match = anchoring.matchQuote(docText, quoteSelector.exact, {
      prefix: quoteSelector.prefix || undefined,
      suffix: quoteSelector.suffix || undefined,
      hint: typeof hint === "number" ? hint : undefined,
    });
    if (!match || match.score < 0.6) {
      return null;
    }
    return anchoring.textPositionToRange(match.start, match.end);
  }

  function collectRanges(items) {
    const ranges = [];
    for (const item of items) {
      const quote = item.anchors.find((a) => a.selector_type === "text_quote");
      if (!quote) continue;
      const positionHint = item.anchors.find((a) => a.selector_type === "text_position");
      const range = anchorSelector(
        quote.selector,
        positionHint ? positionHint.selector.start : undefined,
      );
      if (range) {
        ranges.push({ range, anchorId: quote.id });
      }
    }
    return ranges;
  }

  let anchored = [];

  function paint(ranges) {
    if (typeof Highlight === "undefined" || !CSS.highlights) {
      return; // Custom Highlight API unavailable: skip painting, keep data
    }
    injectHighlightStyle();
    anchored = ranges;
    CSS.highlights.set(HIGHLIGHT_NAME, new Highlight(...ranges.map((entry) => entry.range)));
  }

  async function sha256Hex(text) {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
  }

  /* Mirror of the daemon's util.normalize_source_uri — a drift here only
   * costs a missed auto-highlight (the digest just won't match). */
  function normalizeUrl(raw) {
    let url;
    try {
      url = new URL(raw);
    } catch {
      return null;
    }
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    const scheme = url.protocol.slice(0, -1);
    let netloc = url.hostname.toLowerCase();
    if (url.port && !(scheme === "http" && url.port === "80") &&
        !(scheme === "https" && url.port === "443")) {
      netloc += `:${url.port}`;
    }
    let path = url.pathname.replace(/\/+$/, "");
    const params = [...new URLSearchParams(url.search).entries()];
    params.sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : 1));
    const query = params
      .map(([k, v]) => `${encodeURIComponent(k).replace(/%20/g, "+")}=${encodeURIComponent(v).replace(/%20/g, "+")}`)
      .join("&");
    return `${scheme}://${netloc}${path}${query ? "?" + query : ""}`;
  }

  async function maybeHighlight(attempt = 0) {
    const normalized = normalizeUrl(location.href);
    if (!normalized) return;
    const { urlDigests = [] } = await chrome.storage.local.get("urlDigests");
    const digest = await sha256Hex(normalized);
    if (!urlDigests.includes(digest)) return;
    const response = await chrome.runtime.sendMessage({ type: "resolve-url", url: location.href });
    if (!response || !response.ok) return;
    const items = response.result.items.filter((item) => !item.artifact.redacted);
    const ranges = collectRanges(items);
    paint(ranges);
    // Bounded retry for late-rendering pages (SPAs, lazy content).
    if (ranges.length < items.length && attempt < REANCHOR_ATTEMPTS) {
      const delay = Math.min(REANCHOR_WINDOW_MS / REANCHOR_ATTEMPTS, 2000);
      setTimeout(() => {
        const idle = globalThis.requestIdleCallback || ((fn) => setTimeout(fn, 50));
        idle(() => maybeHighlight(attempt + 1));
      }, delay * (attempt + 1));
    }
  }

  function scrollToAnchor(quoteSelector, hint) {
    const range = anchorSelector(quoteSelector, hint);
    if (!range) return false;
    const element =
      range.startContainer.nodeType === Node.ELEMENT_NODE
        ? range.startContainer
        : range.startContainer.parentElement;
    if (element) element.scrollIntoView({ behavior: "smooth", block: "center" });
    if (typeof Highlight !== "undefined" && CSS.highlights) {
      injectHighlightStyle();
      CSS.highlights.set(FLASH_NAME, new Highlight(range));
      setTimeout(() => CSS.highlights.delete(FLASH_NAME), 2500);
    }
    return true;
  }

  // ── message handlers ──────────────────────────────────────────────────────

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!message) return undefined;
    if (message.type === "grab-selection") {
      const payload = lastSelection || snapshotSelection();
      sendResponse(payload ? { ok: true, payload } : { ok: false });
      return undefined;
    }
    if (message.type === "toast") {
      showToast(message.text, message.ok);
      sendResponse({ ok: true });
      return undefined;
    }
    if (message.type === "prompt-label") {
      promptLabel().then((label) => sendResponse({ label }));
      return true; // async
    }
    if (message.type === "show-anchor") {
      sendResponse({ ok: scrollToAnchor(message.selector, message.hint) });
      return undefined;
    }
    return undefined;
  });

  // ── SPA navigation + initial pass ─────────────────────────────────────────

  if (globalThis.navigation && typeof globalThis.navigation.addEventListener === "function") {
    globalThis.navigation.addEventListener("navigatesuccess", () => {
      setTimeout(() => maybeHighlight(), 400);
    });
  }
  maybeHighlight();
})();
