---
name: release
description: Cut an inspeg release — version bumps, extension packaging, fresh-profile checklist. Use when the user asks to release, tag, or package.
---

# Releasing inspeg

Nothing here is compiled or signed — the whole system is pure Python + two
plain-JS extensions.

1. **Versions**: bump `pyproject.toml` `[project] version`,
   `extension/manifest.json` `version`, `vscode-ext/package.json` `version`
   (keep them in lockstep).
2. **Gates**: `ruff check . && ruff format --check . && pytest` green;
   `node --check` over `extension/**/*.js ui/**/*.js vscode-ext/extension.js`
   (CI's `surfaces` job mirrors this).
3. **Package**:
   - VSIX: `python scripts/build_vsix.py` (pure Python — no Node or vsce
     needed; `vsce package` in `vscode-ext/` works too if Node is around);
   - Chrome stays load-unpacked — the pinned `key` in manifest.json keeps
     the id `hcoiigcfphgmjdpojodchgppadjkkkji` stable, so the daemon
     allowlist never changes. NEVER regenerate the key casually: a new key =
     new id = every install's `--extension-origin` breaks.
4. **Changelog**: update CHANGELOG.md from `git log` since the last tag.
5. **Fresh-profile checklist** (run the `surface-smoke` skill against a
   scratch data dir): install `pip install -e ".[dev,hud]"`, load unpacked
   extension, install VSIX, `inspeg install` writes exactly one HKCU Run
   value, `inspeg uninstall` removes exactly that value and leaves the data
   dir untouched.
6. Tag `vX.Y.Z` and push — CI (including the surfaces job) must be green
   before the tag.
