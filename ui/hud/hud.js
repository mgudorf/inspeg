/* inspeg HUD: context band, tree, labels, annotate queue, quick-assert.
 *
 * V18 discipline: every attacker-influenced string (captured excerpts,
 * labels, window/tab titles from the context layer) is rendered with
 * textContent — markup injection sinks are banned here and enforced by a
 * security test. External navigation always goes through POST /api/open
 * (scheme-allowlisted, V17); this page never assigns location to captured
 * data.
 */
"use strict";

const $ = (id) => document.getElementById(id);

// ── tiny DOM helpers (text nodes only) ──────────────────────────────────────

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : response.statusText);
  }
  return response.json();
}

function post(path, body, headers) {
  return api(path, {
    method: "POST",
    headers: Object.assign({ "Content-Type": "application/json" }, headers || {}),
    body: JSON.stringify(body),
  });
}

const CAPTURE = { "X-Inspeg-Capture": "1" };
const OPEN = { "X-Inspeg-Open": "1" };

function openExternal(target) {
  return post("/api/open", target, OPEN).catch((error) => flashAssert(error.message, false));
}

// Two-click confirm button. Native dialogs (window.confirm) are unreliable
// inside the embedded WebView2 shell, so destructive actions arm on the
// first click and fire on the second; arming times out after 4 s.
function confirmButton(label, armedLabel, action, title) {
  const button = el("button", "danger", label);
  if (title) button.title = title;
  let armed = false;
  let timer = null;
  button.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      button.textContent = armedLabel;
      timer = setTimeout(() => {
        armed = false;
        button.textContent = label;
      }, 4000);
      return;
    }
    clearTimeout(timer);
    try {
      await action();
    } catch (error) {
      flashAssert(error.message, false);
    }
  });
  return button;
}

// ── view plumbing ───────────────────────────────────────────────────────────

let currentView = "context";
let lastContext = null;
let contextDebounce = null;

const views = {
  context: renderContextView,
  tree: renderTreeView,
  labels: renderLabelsView,
  graph: renderGraphView,
  queue: renderQueueView,
};

function switchView(name) {
  currentView = name;
  for (const button of document.querySelectorAll("#tabs button")) {
    button.classList.toggle("active", button.dataset.view === name);
  }
  render();
}

async function render() {
  const main = $("content");
  main.replaceChildren();
  try {
    await views[currentView](main);
  } catch (error) {
    main.replaceChildren(el("div", "empty", `unavailable: ${error.message}`));
  }
}

// ── items ───────────────────────────────────────────────────────────────────

function tierBadge(provenance) {
  return el("span", `badge ${provenance}`, provenance);
}

function renderItem(item) {
  const artifact = item.artifact;
  const box = el("div", "item");
  const meta = el("div", "meta");
  meta.append(tierBadge(artifact.provenance));
  if (artifact.source_title) meta.append(el("span", "dim", artifact.source_title));
  box.append(meta);
  if (item.excerpt) box.append(el("div", "excerpt", item.excerpt));
  if (artifact.redacted) box.append(el("div", "dim", "(redacted)"));

  const chips = el("div", "meta");
  for (const label of item.labels) {
    const chip = el("button", "chip", label.label);
    chip.addEventListener("click", () => openNode(label.id));
    chips.append(chip);
  }
  box.append(chips);

  const actions = el("div", "actions");
  if (artifact.source_link) {
    const open = el("button", null, "open");
    open.addEventListener("click", () => openExternal({ url: artifact.source_link }));
    actions.append(open);
  }
  const codeAnchor = item.anchors.find((a) => a.selector_type === "code_span");
  if (codeAnchor) {
    const s = codeAnchor.selector;
    const jump = el("button", null, "vs code");
    jump.addEventListener("click", () =>
      openExternal({
        url: `vscode://file/${s.path.replace(/\\/g, "/")}:${s.start_line}:${s.start_col + 1}`,
      }),
    );
    actions.append(jump);
  }
  const fileTarget =
    artifact.locator && artifact.locator.kind === "file" ? artifact.locator.target : null;
  if (fileTarget || codeAnchor) {
    const reveal = el("button", null, "reveal");
    reveal.addEventListener("click", () =>
      openExternal({ reveal: fileTarget || codeAnchor.selector.path }),
    );
    actions.append(reveal);
  }
  const primaryAnchor = item.anchors[0];
  if (primaryAnchor) {
    const similar = el("button", null, "similar");
    similar.addEventListener("click", () => showSimilar(primaryAnchor.id));
    actions.append(similar);
    const relate = el("button", null, "relate");
    relate.addEventListener("click", () => openAssert(primaryAnchor.id, item.excerpt));
    actions.append(relate);
  }
  if (!artifact.redacted) {
    actions.append(
      confirmButton(
        "redact",
        "destroy content?",
        async () => {
          await post(`/api/artifacts/${artifact.id}/redact`, {}, CAPTURE);
          render();
        },
        "Destroy the content but keep the where/when record",
      ),
    );
  }
  actions.append(
    confirmButton(
      "delete",
      "really delete?",
      async () => {
        await api(`/api/artifacts/${encodeURIComponent(artifact.id)}`, {
          method: "DELETE",
          headers: CAPTURE,
        });
        render();
      },
      "Remove this item entirely — content, anchors, and labels",
    ),
  );
  box.append(actions);

  if (primaryAnchor && !artifact.redacted) {
    box.append(labelAdder(primaryAnchor.id));
  }
  return box;
}

function labelAdder(anchorId) {
  const wrap = el("div", "label-add");
  const input = el("input");
  input.placeholder = "add label…";
  input.addEventListener("keydown", async (event) => {
    if (event.key !== "Enter" || !input.value.trim()) return;
    try {
      await post(`/api/anchors/${anchorId}/labels`, { label: input.value.trim() }, CAPTURE);
      input.value = "";
      render();
    } catch (error) {
      input.value = "";
      input.placeholder = error.message;
    }
  });
  wrap.append(input);
  return wrap;
}

function renderItems(main, items, emptyText) {
  if (!items.length) {
    main.append(el("div", "empty", emptyText));
    return;
  }
  for (const item of items) main.append(renderItem(item));
}

// ── views ───────────────────────────────────────────────────────────────────

async function renderContextView(main) {
  const query = contextQuery();
  if (!query) {
    main.append(el("div", "empty", "focus a captured page, file, or app to see items from it"));
    return;
  }
  const resolved = await api(`/api/resolve?${query}`);
  renderItems(main, resolved.items, "nothing captured from here yet");
}

async function renderTreeView(main) {
  const tree = await api("/api/tree?group_by=context&limit=100");
  if (!tree.groups.length) {
    main.append(el("div", "empty", "nothing captured yet"));
    return;
  }
  for (const group of tree.groups) {
    const box = el("div", "group");
    const head = el("div", "group-head");
    head.append(
      el("span", "group-name", group.display),
      el("span", "group-kind", `${group.kind} · ${group.count}`),
    );
    const itemsBox = el("div");
    let expanded = false;
    head.addEventListener("click", async () => {
      expanded = !expanded;
      itemsBox.replaceChildren();
      if (expanded && group.key) {
        const resolved = await api(`/api/resolve?key=${encodeURIComponent(group.key)}`);
        renderItems(itemsBox, resolved.items, "no items");
      }
    });
    box.append(head, itemsBox);
    main.append(box);
  }
}

async function renderLabelsView(main) {
  const tree = await api("/api/tree?group_by=label&limit=100");
  if (!tree.groups.length) {
    main.append(el("div", "empty", "no labels yet — capture something with 'Capture as'"));
    return;
  }
  const cloud = el("div", "meta");
  for (const group of tree.groups) {
    const chip = el("button", "chip", `${group.display} ×${group.count}`);
    chip.addEventListener("click", () => openNode(group.key));
    cloud.append(chip);
  }
  main.append(cloud);
}

async function showSimilar(anchorId) {
  const main = $("content");
  main.replaceChildren(el("div", "dim", "similar items (shared labels/nodes):"));
  const result = await api(`/api/items/${encodeURIComponent(anchorId)}/similar`);
  if (!result.items.length) {
    main.append(el("div", "empty", "nothing shares a node with this item yet"));
    return;
  }
  for (const entry of result.items) {
    const item = {
      artifact: entry.artifact,
      anchors: [{ id: entry.anchor_id, selector_type: "", selector: {} }],
      labels: [],
      excerpt: entry.excerpt,
    };
    const box = renderItem(item);
    box.prepend(el("div", "dim", `shares ${entry.shared}`));
    main.append(box);
  }
}

async function renderQueueView(main) {
  const queue = await api("/api/queue");
  renderItems(main, queue.items, "queue clear — every capture is labeled");
  $("queue-count").textContent = queue.items.length ? String(queue.items.length) : "";
}

// ── graph viewer (search bar + hyperlinked node pages) ──────────────────────

let graphSearchValue = "";
let graphStack = []; // node ids on the navigation trail; empty = search results

const KIND_NAMES = { topic: "tag", edge_type: "predicate" };

function kindBadge(kind) {
  return el("span", "badge kind", KIND_NAMES[kind] || "subject");
}

function openNode(nodeId) {
  graphStack.push(nodeId);
  switchView("graph");
}

async function renderGraphView(main) {
  const bar = el("div", "graph-search");
  const input = el("input");
  input.placeholder = "search subjects, tags & captured text…";
  input.value = graphSearchValue;
  const body = el("div");
  let debounce = null;
  input.addEventListener("input", () => {
    graphSearchValue = input.value;
    graphStack = []; // typing always returns to search results
    clearTimeout(debounce);
    debounce = setTimeout(() => renderGraphResults(body, graphSearchValue), 250);
  });
  bar.append(input);
  main.append(bar, body);
  if (graphStack.length) {
    await renderNodePage(body, graphStack[graphStack.length - 1]);
  } else {
    await renderGraphResults(body, graphSearchValue);
  }
}

async function renderGraphResults(container, q) {
  try {
    const result = await api(`/api/graph/search?q=${encodeURIComponent(q)}`);
    container.replaceChildren();
    if (!result.nodes.length) {
      container.append(el("div", "empty", "no nodes match — assert or label something first"));
    }
    for (const node of result.nodes) {
      const row = el("div", "node-row");
      const chip = el("button", "chip node", node.label);
      chip.addEventListener("click", () => openNode(node.id));
      row.append(chip, kindBadge(node.kind));
      const facts = [];
      if (node.label_count) facts.push(`${node.label_count} items`);
      if (node.edge_count) facts.push(`${node.edge_count} edges`);
      if (facts.length) row.append(el("span", "dim", facts.join(" · ")));
      container.append(row);
    }
    if (q.trim()) await renderTextHits(container, q);
  } catch (error) {
    container.replaceChildren(el("div", "empty", `unavailable: ${error.message}`));
  }
}

async function renderTextHits(container, q) {
  // Quote every term so raw user input can never be invalid FTS5 syntax.
  const query = q
    .trim()
    .split(/\s+/)
    .map((term) => `"${term.replace(/"/g, '""')}"`)
    .join(" ");
  try {
    const result = await api(`/api/search?q=${encodeURIComponent(query)}`);
    if (!result.items.length) return;
    container.append(el("div", "dim section-head", "text matches"));
    for (const hit of result.items) {
      const box = renderItem(hit.item);
      if (hit.snippet) box.prepend(el("div", "dim", hit.snippet));
      container.append(box);
    }
  } catch {
    /* search index off (503) or bad query — node results still shown */
  }
}

function edgeRow(edge, direction) {
  const row = el("div", "edge-row");
  if (direction === "out") row.append(el("span", "pred", edge.type));
  const chip = el("button", "chip node", edge.other.label);
  chip.addEventListener("click", () => openNode(edge.other.id));
  row.append(chip);
  if (direction === "in") row.append(el("span", "pred", edge.type));
  return row;
}

async function renderNodePage(container, nodeId) {
  try {
    const detail = await api(`/api/graph/nodes/${encodeURIComponent(nodeId)}`);
    container.replaceChildren();

    const head = el("div", "group-head");
    const back = el("button", "ghost", "‹ back");
    back.addEventListener("click", () => {
      graphStack.pop();
      render();
    });
    head.append(back, el("span", "group-name", detail.label), kindBadge(detail.kind));
    container.append(head);

    if (detail.out_edges.length || detail.in_edges.length) {
      container.append(el("div", "dim section-head", "relations"));
      for (const edge of detail.out_edges) container.append(edgeRow(edge, "out"));
      for (const edge of detail.in_edges) container.append(edgeRow(edge, "in"));
    }

    if (detail.co_labels.length) {
      container.append(el("div", "dim section-head", "appears together with"));
      const cloud = el("div", "meta");
      for (const co of detail.co_labels) {
        const chip = el("button", "chip", `${co.label} ×${co.shared}`);
        chip.addEventListener("click", () => openNode(co.id));
        cloud.append(chip);
      }
      container.append(cloud);
    }

    if (detail.label_count) {
      container.append(el("div", "dim section-head", `items (${detail.label_count})`));
      const result = await api(`/api/labels/${encodeURIComponent(nodeId)}/items`);
      for (const item of result.items) container.append(renderItem(item));
    } else if (!detail.out_edges.length && !detail.in_edges.length && !detail.co_labels.length) {
      container.append(el("div", "empty", "no relations or items yet — use relate on an item"));
    }
  } catch (error) {
    container.replaceChildren(el("div", "empty", `unavailable: ${error.message}`));
  }
}

// ── quick-assert (the only place full triples are typed) ────────────────────

let assertAnchorId = null;

async function openAssert(anchorId, excerpt) {
  assertAnchorId = anchorId;
  $("assert-subject").textContent = excerpt ? `evidence: ${excerpt.slice(0, 120)}` : "";
  $("assert-bar").classList.remove("hidden");
  try {
    const predicates = await api("/api/predicates");
    $("predicate-list").replaceChildren(
      ...predicates.map((p) => {
        const option = document.createElement("option");
        option.value = p.label;
        return option;
      }),
    );
  } catch {
    /* daemon hiccup: free typing still works */
  }
  $("assert-src").focus();
}

function flashAssert(message, ok) {
  const flash = $("assert-flash");
  flash.textContent = message;
  flash.className = `flash ${ok ? "ok" : "err"}`;
  if (ok) setTimeout(() => (flash.textContent = ""), 2500);
}

let pendingPredicate = null;

async function submitAssert() {
  const body = {
    anchor_id: assertAnchorId,
    src_label: $("assert-src").value.trim(),
    edge_type: $("assert-type").value.trim(),
    dst_label: $("assert-dst").value.trim(),
  };
  if (!body.src_label || !body.edge_type || !body.dst_label) {
    flashAssert("all three parts required", false);
    return;
  }
  try {
    // Second press with the same new predicate = the deliberate create step
    // (no native confirm dialogs in the embedded shell).
    const label = body.edge_type.replace(/[\s-]+/g, "_").toUpperCase();
    const create = pendingPredicate === label;
    await post("/api/edges", create ? Object.assign({}, body, { create_predicate: true }) : body);
    pendingPredicate = null;
    flashAssert(create ? "asserted ✓ (new predicate)" : "asserted ✓", true);
    $("assert-src").value = $("assert-type").value = $("assert-dst").value = "";
  } catch (error) {
    if (error.message.startsWith("unknown predicate")) {
      pendingPredicate = body.edge_type.replace(/[\s-]+/g, "_").toUpperCase();
      flashAssert(`${pendingPredicate} is new — press Assert again to create it`, false);
      return;
    }
    pendingPredicate = null;
    flashAssert(error.message, false);
  }
}

$("assert-go").addEventListener("click", submitAssert);
$("assert-cancel").addEventListener("click", () => $("assert-bar").classList.add("hidden"));

// ── context band + SSE ──────────────────────────────────────────────────────

function contextQuery() {
  if (!lastContext) return null;
  if (lastContext.tab && lastContext.tab.url) {
    return `url=${encodeURIComponent(lastContext.tab.url)}`;
  }
  if (lastContext.workspace && lastContext.workspace.file) {
    return `path=${encodeURIComponent(lastContext.workspace.file)}`;
  }
  if (lastContext.window && lastContext.window.exe) {
    return `exe=${encodeURIComponent(lastContext.window.exe)}`;
  }
  return null;
}

function renderContextBand() {
  const band = $("context-now");
  band.replaceChildren();
  if (!lastContext) {
    band.append(el("span", "dim", "context watch off — showing static views"));
    return;
  }
  const title =
    (lastContext.tab && (lastContext.tab.title || lastContext.tab.url)) ||
    (lastContext.workspace && lastContext.workspace.file) ||
    (lastContext.window && lastContext.window.title) ||
    "—";
  const sub =
    (lastContext.window && lastContext.window.exe) ||
    (lastContext.workspace && lastContext.workspace.root) ||
    "";
  band.append(el("div", "ctx-title", title), el("div", "ctx-sub", sub));
}

function onContextChange(state) {
  lastContext = state;
  renderContextBand();
  if (window.pywebview && window.pywebview.api) {
    // Auto-hide in real fullscreen; the explicit hotkey still summons.
    if (state.fullscreen === "d3d" || state.fullscreen === "presentation") {
      window.pywebview.api.auto_hide();
    } else {
      window.pywebview.api.auto_show();
    }
  }
  if (currentView === "context") {
    clearTimeout(contextDebounce);
    contextDebounce = setTimeout(render, 300);
  }
}

function connectStream() {
  const source = new EventSource("/api/events/stream");
  source.addEventListener("context", (event) => {
    onContextChange(JSON.parse(event.data).state);
  });
  source.addEventListener("store", () => {
    if (currentView === "queue" || currentView === "tree") {
      clearTimeout(contextDebounce);
      contextDebounce = setTimeout(render, 300);
    }
    api("/api/queue")
      .then((queue) => {
        $("queue-count").textContent = queue.items.length ? String(queue.items.length) : "";
      })
      .catch(() => {});
  });
  source.addEventListener("hud", (event) => {
    const data = JSON.parse(event.data);
    if (data.action === "toggle" && window.pywebview && window.pywebview.api) {
      window.pywebview.api.toggle();
    }
  });
  source.onerror = () => {
    source.close();
    setTimeout(connectStream, 3000);
  };
}

// ── boot ────────────────────────────────────────────────────────────────────

for (const button of document.querySelectorAll("#tabs button")) {
  button.addEventListener("click", () => switchView(button.dataset.view));
}

async function boot() {
  try {
    const state = await api("/api/context");
    lastContext = state;
    $("watch-state").textContent = "";
  } catch {
    lastContext = null;
    $("watch-state").textContent = "context off";
  }
  renderContextBand();
  connectStream();
  render();
  api("/api/queue")
    .then((queue) => {
      $("queue-count").textContent = queue.items.length ? String(queue.items.length) : "";
    })
    .catch(() => {});
}

boot();
