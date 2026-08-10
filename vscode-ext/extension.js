/* inspeg VS Code extension: the in-editor capture surface (ADR 0007).
 *
 * Trust model: plain loopback fetches with the X-Inspeg-Capture header and
 * no Origin — indistinguishable from curl, the daemon's already-accepted
 * local-process posture. No new channel, no new secret.
 */
"use strict";

const vscode = require("vscode");

let lastLabel = null;
let decorationType = null;
let statusItem = null;

function config() {
  return vscode.workspace.getConfiguration("inspeg");
}

function daemonUrl() {
  return config().get("daemonUrl", "http://127.0.0.1:8137").replace(/\/+$/, "");
}

async function daemonFetch(path, options = {}) {
  const headers = Object.assign(
    { "X-Inspeg-Capture": "1" },
    options.body ? { "Content-Type": "application/json" } : {},
  );
  const response = await fetch(daemonUrl() + path, Object.assign({}, options, { headers }));
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `HTTP ${response.status}`);
  }
  return response.json();
}

// ── git identity (best-effort, via the built-in git extension) ──────────────

function gitInfo(uri) {
  try {
    const gitExtension = vscode.extensions.getExtension("vscode.git");
    if (!gitExtension || !gitExtension.isActive) return {};
    const api = gitExtension.exports.getAPI(1);
    const repository = api.getRepository(uri);
    if (!repository) return {};
    const head = repository.state.HEAD;
    const remote = repository.state.remotes.find((r) => r.name === "origin") ||
      repository.state.remotes[0];
    return {
      git_commit: head && head.commit ? head.commit : null,
      git_remote: remote && (remote.fetchUrl || remote.pushUrl) ? remote.fetchUrl || remote.pushUrl : null,
    };
  } catch {
    return {};
  }
}

// ── label picking ───────────────────────────────────────────────────────────

async function pickLabel() {
  let recent = [];
  try {
    recent = await daemonFetch("/api/labels?sort=recent&limit=10");
  } catch {
    /* daemon down: free-typing still works */
  }
  return new Promise((resolve) => {
    const picker = vscode.window.createQuickPick();
    picker.title = "inspeg — label this capture";
    picker.placeholder = "Pick a recent label or type a new one (Esc = capture unlabeled)";
    picker.items = recent.map((entry) => ({ label: entry.label, description: `×${entry.count}` }));
    picker.onDidAccept(() => {
      const chosen = picker.selectedItems[0]
        ? picker.selectedItems[0].label
        : picker.value.trim();
      picker.hide();
      resolve(chosen || null);
    });
    picker.onDidHide(() => {
      picker.dispose();
      resolve(null);
    });
    picker.show();
  });
}

// ── captures ────────────────────────────────────────────────────────────────

async function captureSelection(label) {
  const editor = vscode.window.activeTextEditor;
  if (!editor || editor.selection.isEmpty) {
    vscode.window.showWarningMessage("inspeg: nothing selected");
    return;
  }
  const selection = editor.selection;
  const document = editor.document;
  const workspaceFolder = vscode.workspace.getWorkspaceFolder(document.uri);
  const body = Object.assign(
    {
      text: document.getText(selection),
      path: document.uri.fsPath,
      start_line: selection.start.line + 1,
      start_col: selection.start.character,
      end_line: selection.end.line + 1,
      end_col: selection.end.character,
      workspace: workspaceFolder ? workspaceFolder.uri.fsPath : null,
      labels: label ? [label] : [],
    },
    gitInfo(document.uri),
  );
  try {
    await daemonFetch("/api/captures/code", { method: "POST", body: JSON.stringify(body) });
    if (label) lastLabel = label;
    vscode.window.setStatusBarMessage(
      `inspeg: captured${label ? ` as “${label}”` : ""} (exact)`,
      3000,
    );
    refreshDecorations(editor);
  } catch (error) {
    vscode.window.showErrorMessage(`inspeg: capture failed — ${error.message}`);
  }
}

async function captureFile(uri) {
  const target = uri || (vscode.window.activeTextEditor && vscode.window.activeTextEditor.document.uri);
  if (!target) return;
  const label = await pickLabel();
  try {
    const result = await daemonFetch("/api/captures/pointer", {
      method: "POST",
      body: JSON.stringify({
        kind: "file",
        target: target.fsPath,
        mimetype: guessMime(target.fsPath),
        labels: label ? [label] : [],
        surface: "vscode",
      }),
    });
    if (label) lastLabel = label;
    vscode.window.setStatusBarMessage(
      `inspeg: file captured${label ? ` as “${label}”` : ""} (${result.provenance})`,
      3000,
    );
  } catch (error) {
    vscode.window.showErrorMessage(`inspeg: capture failed — ${error.message}`);
  }
}

function guessMime(fsPath) {
  const lower = fsPath.toLowerCase();
  const table = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml", ".pdf": "application/pdf",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mp3": "audio/mpeg", ".wav": "audio/wav",
    ".ipynb": "application/x-ipynb+json", ".md": "text/markdown", ".txt": "text/plain",
    ".csv": "text/csv", ".json": "application/json",
  };
  for (const [ext, mime] of Object.entries(table)) {
    if (lower.endsWith(ext)) return mime;
  }
  return "application/octet-stream";
}

// ── decorations: highlight captured ranges in open files ────────────────────

async function refreshDecorations(editor) {
  if (!editor || !config().get("decorateCapturedRanges", true)) return;
  if (editor.document.uri.scheme !== "file") return;
  let resolved;
  try {
    resolved = await daemonFetch(
      `/api/resolve?path=${encodeURIComponent(editor.document.uri.fsPath)}`,
    );
  } catch {
    return;
  }
  const ranges = [];
  for (const item of resolved.items) {
    if (item.artifact.redacted) continue;
    for (const anchor of item.anchors) {
      if (anchor.selector_type !== "code_span") continue;
      const s = anchor.selector;
      const labels = item.labels.map((l) => l.label).join(", ");
      const lastLine = editor.document.lineCount;
      if (s.start_line > lastLine) continue; // file shrank since capture
      const range = new vscode.Range(
        s.start_line - 1,
        s.start_col,
        Math.min(s.end_line, lastLine) - 1,
        s.end_col,
      );
      ranges.push({
        range,
        hoverMessage: `inspeg: ${labels || "captured"} (${item.artifact.provenance})`,
      });
    }
  }
  if (!decorationType) {
    decorationType = vscode.window.createTextEditorDecorationType({
      backgroundColor: "rgba(255, 200, 60, 0.12)",
      overviewRulerColor: "rgba(255, 200, 60, 0.8)",
      overviewRulerLane: vscode.OverviewRulerLane.Right,
    });
  }
  editor.setDecorations(decorationType, ranges);
}

// ── ephemeral workspace context (ADR 0004; daemon may refuse — that's fine) ─

async function pushWorkspaceContext(editor) {
  if (!config().get("reportWorkspaceContext", true)) return;
  if (!editor || editor.document.uri.scheme !== "file") return;
  const workspaceFolder = vscode.workspace.getWorkspaceFolder(editor.document.uri);
  try {
    await fetch(daemonUrl() + "/api/context/workspace", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Inspeg-Context": "1" },
      body: JSON.stringify({
        root: workspaceFolder ? workspaceFolder.uri.fsPath : null,
        file: editor.document.uri.fsPath,
      }),
    });
  } catch {
    /* daemon down or context watch disabled: silently fine */
  }
}

// ── activation ──────────────────────────────────────────────────────────────

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("inspeg.captureAsLastLabel", async () => {
      if (lastLabel) {
        await captureSelection(lastLabel);
      } else {
        await captureSelection(await pickLabel());
      }
    }),
    vscode.commands.registerCommand("inspeg.captureWithLabel", async () => {
      await captureSelection(await pickLabel());
    }),
    vscode.commands.registerCommand("inspeg.captureFile", captureFile),
    vscode.commands.registerCommand("inspeg.open", () => {
      vscode.env.openExternal(vscode.Uri.parse(daemonUrl() + "/"));
    }),
    vscode.window.onDidChangeActiveTextEditor((editor) => {
      refreshDecorations(editor);
      pushWorkspaceContext(editor);
    }),
    vscode.window.onDidChangeWindowState((state) => {
      if (state.focused) pushWorkspaceContext(vscode.window.activeTextEditor);
    }),
  );
  statusItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 50);
  statusItem.text = "$(bookmark) inspeg";
  statusItem.command = "inspeg.captureWithLabel";
  statusItem.tooltip = "Capture selection into inspeg";
  statusItem.show();
  context.subscriptions.push(statusItem);
  refreshDecorations(vscode.window.activeTextEditor);
  pushWorkspaceContext(vscode.window.activeTextEditor);
}

function deactivate() {
  if (decorationType) decorationType.dispose();
}

module.exports = { activate, deactivate };
