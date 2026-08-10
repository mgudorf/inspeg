"""Run the inspeg daemon: API + HUD + capture surfaces (Chrome / VS Code)."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
from collections.abc import Callable
from pathlib import Path

import uvicorn

from inspeg.api.app import create_app
from inspeg.context import DEFAULT_IGNORED_EXES, ContextHub
from inspeg.store import Store, StoreLockedError

DEFAULT_PORT = 8137
# Not bare ctrl+alt+<key>: AltGr on many non-US layouts is synthesized as
# Ctrl+Alt, so such a hotkey fires while people type. Requiring Shift as well
# keeps plain AltGr typing safe. See docs/security.md. RegisterHotKey cannot
# distinguish left from right modifiers; left-only would need a low-level
# keyboard hook, which the no-passive-observation rule forbids.
# (The old ctrl+shift+alt+i clipboard-capture hotkey was retired once the
# Chrome and VS Code surfaces landed; capture lives in their context menus.)
DEFAULT_HUD_HOTKEY = "ctrl+shift+alt+g"

log = logging.getLogger("inspeg")

# ctypes callbacks must stay referenced for the life of the process.
_console_handler_ref = None


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _install_console_close_handler(cleanup: Callable[[], None]) -> None:
    """Release resources when the console window is closed (Windows).

    Closing the terminal delivers CTRL_CLOSE_EVENT and then kills the process;
    atexit/finally may never run. All worker threads are daemons (they die
    with the process — nothing survives the window), but the store's file lock
    and SQLite connection deserve a clean release rather than OS teardown.
    """
    import ctypes
    from ctypes import wintypes

    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    # CTRL_CLOSE_EVENT / CTRL_LOGOFF_EVENT / CTRL_SHUTDOWN_EVENT. Ctrl+C and
    # Ctrl+Break are left to Python/uvicorn's normal shutdown path.
    terminal_events = (2, 5, 6)

    def _handler(ctrl_type: int) -> bool:
        if ctrl_type in terminal_events:
            cleanup()
        return False  # let the default handler terminate the process

    global _console_handler_ref
    _console_handler_ref = handler_type(_handler)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler_ref, True)


_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "inspeg"


def _autostart(args) -> int:
    """Per-user autostart via the HKCU Run key — the entire install story.

    No admin, no services, no registry surface beyond one value; uninstall
    deletes exactly that value and never touches the data dir.
    """
    if sys.platform != "win32":
        log.error("autostart install is Windows-only")
        return 1
    import winreg

    if args.command == "install":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        interpreter = pythonw if pythonw.exists() else Path(sys.executable)
        command = f'"{interpreter}" -m inspeg'
        for origin in args.extension_origin:
            command += f' --extension-origin "{origin}"'
        for exe in args.ignore_exe:
            command += f' --ignore-exe "{exe}"'
        if args.no_context_watch:
            command += " --no-context-watch"
        if args.no_hud:
            command += " --no-hud"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, command)
        log.info("autostart installed: HKCU\\%s\\%s = %s", _RUN_KEY, _RUN_VALUE, command)
        return 0
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        try:
            winreg.DeleteValue(key, _RUN_VALUE)
            log.info("autostart removed (data dir untouched)")
        except FileNotFoundError:
            log.info("autostart was not installed; nothing to remove")
    return 0


def _reindex(args) -> int:
    from inspeg.fts import FtsIndex

    try:
        store = Store(args.data_dir)
    except StoreLockedError as exc:
        log.error("%s (stop the daemon before reindexing)", exc)
        return 1
    try:
        fts = FtsIndex(store, args.data_dir / "cache.db", start_worker=False)
        count = fts.rebuild()
        fts.close()
        log.info("reindexed %d text artifacts into cache.db", count)
    finally:
        store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="inspeg",
        description="Manual capture into a provenance-anchored knowledge graph.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["run", "install", "uninstall", "reindex"],
        default="run",
        help="run the daemon (default); install/uninstall the per-user autostart "
        "Run key; reindex rebuilds the FTS cache (cache.db) from the store",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path.home() / ".inspeg",
        help="where the database and blobs live (default: ~/.inspeg)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (loopback only)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--no-hotkey",
        action="store_true",
        help="do not run the win32 message pump: no HUD toggle hotkey and no "
        "foreground context watch (API/UI only)",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="DANGEROUS: permit a non-loopback --host. The API has no authentication; "
        "every device that can reach it gets full read/write access to your captures.",
    )
    parser.add_argument(
        "--extension-origin",
        action="append",
        default=[],
        metavar="chrome-extension://<id>",
        help="allow this exact browser-extension origin on the extension routes "
        "(repeatable; wildcards refused; see ADR 0007). Off by default.",
    )
    parser.add_argument(
        "--no-context-watch",
        action="store_true",
        help="disable the ephemeral foreground-context layer entirely: the win32 "
        "hooks are never installed and the /api/context endpoints refuse (ADR 0004).",
    )
    parser.add_argument("--no-hud", action="store_true", help="do not spawn the HUD window")
    parser.add_argument(
        "--hud-hotkey", default=DEFAULT_HUD_HOTKEY, help="HUD show/hide toggle hotkey"
    )
    parser.add_argument(
        "--ignore-exe",
        action="append",
        default=[],
        metavar="NAME.exe",
        help="additional foreground apps the context layer treats as noise "
        f"(added to the defaults: {', '.join(sorted(DEFAULT_IGNORED_EXES))})",
    )
    args = parser.parse_args(argv)

    if not _is_loopback(args.host) and not args.allow_remote:
        parser.error(
            f"refusing to bind non-loopback host {args.host!r}: the API has no "
            "authentication. Pass --allow-remote only if you accept that every "
            "device on the network can read and write your captures."
        )

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command in ("install", "uninstall"):
        return _autostart(args)
    if args.command == "reindex":
        return _reindex(args)

    if args.allow_remote and not _is_loopback(args.host):
        log.warning(
            "binding %s — every device on this network has FULL unauthenticated "
            "read/write access to your captures",
            args.host,
        )

    try:
        store = Store(args.data_dir)
    except StoreLockedError as exc:
        log.error("%s", exc)
        return 1

    listener = None
    # ADR 0004: ephemeral display context. Never persisted, never a capture
    # trigger; --no-context-watch removes the hooks and refuses the endpoints.
    context_hub = None
    if not args.no_context_watch:
        context_hub = ContextHub(
            ignored_exes=DEFAULT_IGNORED_EXES | {e.strip().lower() for e in args.ignore_exe}
        )
    # FTS cache (ephemeral, deletable; redaction reaches it synchronously).
    from inspeg.fts import FtsIndex

    fts = FtsIndex(store, args.data_dir / "cache.db")
    store.on_commit.append(fts.on_commit)
    try:
        app = create_app(
            store,
            allow_remote=args.allow_remote,
            extension_origins=args.extension_origin,
            context_hub=context_hub,
            fts=fts,
            hotkey_status=lambda: listener.status if listener is not None else "disabled",
        )
    except ValueError as exc:
        store.close()
        fts.close()
        parser.error(str(exc))
    base_url = f"http://{args.host}:{args.port}"

    # ── HUD process (ADR 0009: pywebview needs its own main thread) ─────────
    hud_process = None
    if not args.no_hud:
        import importlib.util
        import subprocess

        if importlib.util.find_spec("webview") is not None:
            creationflags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
            hud_process = subprocess.Popen(
                [sys.executable, "-m", "inspeg.hud", "--url", f"{base_url}/hud/"],
                creationflags=creationflags,
            )
            log.info("HUD spawned (pid %s); toggle with %s", hud_process.pid, args.hud_hotkey)
        else:
            log.info("HUD skipped: pywebview not installed (pip install -e '.[hud]')")

    if not args.no_hotkey and sys.platform == "win32":
        from inspeg.adapters.hotkey import HotkeyListener

        on_pump_start = None
        if context_hub is not None:

            def on_pump_start():
                from inspeg.adapters.foreground import install_foreground_watch

                return install_foreground_watch(context_hub)

        def on_hud_hotkey() -> None:
            # Synchronous in the WM_HOTKEY handler: foreground-activation
            # rights follow the user's last input — a deferred grant fails
            # silently (ADR 0009).
            if hud_process is not None and hud_process.poll() is None:
                import ctypes

                ctypes.windll.user32.AllowSetForegroundWindow(hud_process.pid)
            app.state.event_bus.publish_threadsafe({"type": "hud", "action": "toggle"})

        # The HUD toggle is the pump's only hotkey now; capture lives in the
        # Chrome / VS Code context menus, not on a keyboard chord.
        listener = HotkeyListener(args.hud_hotkey, on_hud_hotkey, on_pump_start=on_pump_start)
        listener.start()
        log.info("HUD toggle hotkey %s registered", args.hud_hotkey)

    def cleanup() -> None:
        if listener is not None:
            listener.stop()
        if hud_process is not None and hud_process.poll() is None:
            hud_process.terminate()
        fts.close()
        store.close()

    if sys.platform == "win32":
        _install_console_close_handler(cleanup)

    log.info("quick-capture UI at %s (data in %s)", base_url, args.data_dir)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
