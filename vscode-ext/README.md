# inspeg capture — VS Code extension

The in-editor capture surface: right-click a selection → **Inspeg: Capture
as last label** (true one-click once you've used a label) or **Capture
as…** (QuickPick of recent labels; typing creates a new one — QuickPick is
the designed fallback because VS Code cannot build dynamic submenus,
vscode#110218). Right-click any file in the Explorer sidebar → **Inspeg:
Capture file** stores a metadata pointer (images, PDFs, notebooks — never a
copy).

Captures are tier-`exact`: verbatim buffer text, file path, line/column
span, and best-effort git commit/remote for rot detection. Previously
captured ranges are decorated when you open the file, with labels in the
hover.

## Install

```
npm install -g @vscode/vsce   # once
cd vscode-ext && vsce package
code --install-extension inspeg-capture-0.1.0.vsix
```

No build step and no dependencies — the extension is plain JS.

## Settings

- `inspeg.daemonUrl` — default `http://127.0.0.1:8137`.
- `inspeg.reportWorkspaceContext` — push the focused workspace/file to the
  daemon's ephemeral context layer so the HUD follows along (never stored;
  ADR 0004). On by default; the daemon side additionally requires its
  context watcher to be enabled.
- `inspeg.decorateCapturedRanges` — in-editor highlights on open files.

## Trust model

Plain loopback HTTP with the `X-Inspeg-Capture` header and no `Origin` —
indistinguishable from `curl`, which is the daemon's already-accepted
local-process posture (ADR 0007). No new channel, no secrets stored.
