# Security

This is not boilerplate. inspeg reads the clipboard on demand and stores what
it captures unencrypted on disk. You should understand the model — and the
non-model — before using it.

## Threat model

**What inspeg does:**

- Reads the clipboard **only** when you press the capture hotkey or call the
  capture endpoint. There is no clipboard listener, no `WM_CLIPBOARDUPDATE`
  subscription, no polling. Nothing observes the clipboard unless invoked.
- Records the foreground application name and window title at capture time
  (for provenance tier 3).
- Binds to `127.0.0.1` only, by default. No network egress: it makes no
  outbound connections, phones nothing home, and has no telemetry.
- Stores everything locally under `~/.inspeg/` (or `--data-dir`).

**What inspeg does not do:**

- **No encryption at rest.** Blobs and the SQLite database are plain files.
  If you capture secrets, they sit on disk in the clear. Use full-disk
  encryption if that matters to you, and be deliberate about what you capture.
- **No authentication on the local API.** Any process on your machine that can
  reach `127.0.0.1:8137` can read your captures and write assertions. That is
  the standard posture of local daemons; do not bind to other interfaces.
- **No sandboxing of captured content.** Captured HTML is stored verbatim.
  The UI renders extracted text (not HTML), but exports you build later may
  carry whatever you captured.

**You are responsible for what you capture.** The tool is manual by
construction precisely so that nothing enters the store without you pressing
something.

## Reporting a vulnerability

Open a GitHub security advisory on this repository, or email
<matthew.gudorf@gmail.com>. Please do not open public issues for
security-sensitive reports.
