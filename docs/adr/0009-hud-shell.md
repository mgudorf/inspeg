# ADR 0009 — HUD shell: pywebview/WebView2 in a separate process

- Status: accepted
- Date: 2026-08-08

## Context

The HUD is a docked, always-on-top panel rendering a web app served by the
daemon (`http://127.0.0.1:8137/hud/`). The stack rules out Qt, Electron, and
GPL dependencies (`docs/architecture.md` §4). The daemon's main thread
belongs to `uvicorn.run` (`__main__.py`), and its shutdown correctness (V14:
console-close cleanup) depends on that arrangement.

## Decision

- **pywebview 6.x** (BSD-3-Clause; Windows backend deps pythonnet and
  clr-loader are MIT) hosting **WebView2** (Evergreen runtime, preinstalled
  on Windows 11; presence checked at spawn).
- The HUD runs as a **separate process** (`inspeg-hud`), spawned by the
  daemon unless `--no-hud` (mirroring `--no-hotkey`). pywebview requires the
  main thread of its process; merging it into the daemon would collide with
  `uvicorn.run` and break the V14 cleanup path. The HUD process never opens
  the Store — it is a pure HTTP client, so the single-writer and data-dir
  lock (V8) are untouched.
- Window shape: frameless, `on_top`, created hidden with `focus=False`;
  `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` so it never steals focus and stays
  out of Alt+Tab; `SetProp(hwnd, "NonRudeHWND", TRUE)` so the shell never
  misclassifies it as a fullscreen app. Edge-snapped by default; optional
  AppBar work-area reservation (`SHAppBarMessage`) as a "hard dock" setting.
- Auto-hide when `SHQueryUserNotificationState` reports
  `D3D_FULL_SCREEN` or `PRESENTATION` (the PowerToys split: `BUSY` only
  suppresses unsolicited pops — an explicit hotkey toggle is still honored).
- Toggle: hotkey id 2 on the daemon's existing win32 message pump.
  `AllowSetForegroundWindow(hud_pid)` is granted **synchronously in the
  WM_HOTKEY handler** — activation rights follow the last user input, so a
  deferred or daemon-idle grant fails silently.
- Security: the HUD renders only daemon-served content under the daemon's
  CSP (V18); pywebview navigation is intercepted so any external link opens
  in the system browser — a hostile page must never load inside the HUD
  window.
- Fallback when WebView2 is unavailable: open `/hud/` in the default browser
  with a logged notice.

## Alternatives rejected

- **HUD inside the daemon process.** pywebview needs the main thread;
  uvicorn owns it; V14 cleanup depends on that. A refactor to share would
  couple UI lifetime to store lifetime for no gain.
- **Raw WebView2 via PyWinRT.** Owning the HWND host and message pump by
  hand is exactly the code pywebview already maintains under a permissive
  license.
- **A pinned browser tab as the only HUD.** No always-on-top, no
  edge-docking, no fullscreen awareness — the ambient-awareness requirement
  fails. (It remains the graceful fallback.)

## Consequences

- `pywebview` joins the dependency table (verify licenses at pin time, per
  policy).
- The HUD process's absence never blocks capture: all capture paths talk to
  the daemon, not the HUD.
- `/api/health` gains `hud_status` alongside `hotkey`.
