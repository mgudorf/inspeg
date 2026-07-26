"""Run the inspeg daemon: API + quick-capture UI + (on Windows) the capture hotkey."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

import uvicorn

from inspeg.api.app import create_app
from inspeg.store import Store, StoreLockedError

DEFAULT_PORT = 8137
# Not bare ctrl+alt+<key>: AltGr on many non-US layouts is synthesized as
# Ctrl+Alt, so such a hotkey fires while people type. Requiring Shift as well
# keeps plain AltGr typing safe. See docs/security.md. RegisterHotKey cannot
# distinguish left from right modifiers; left-only would need a low-level
# keyboard hook, which the no-passive-observation rule forbids.
DEFAULT_HOTKEY = "ctrl+shift+alt+i"

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="inspeg",
        description="Manual capture into a provenance-anchored knowledge graph.",
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
        "--hotkey", default=DEFAULT_HOTKEY, help="capture hotkey, e.g. ctrl+shift+alt+i"
    )
    parser.add_argument("--no-hotkey", action="store_true", help="run the API/UI only")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="DANGEROUS: permit a non-loopback --host. The API has no authentication; "
        "every device that can reach it gets full read/write access to your captures.",
    )
    args = parser.parse_args(argv)

    if not _is_loopback(args.host) and not args.allow_remote:
        parser.error(
            f"refusing to bind non-loopback host {args.host!r}: the API has no "
            "authentication. Pass --allow-remote only if you accept that every "
            "device on the network can read and write your captures."
        )

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
    app = create_app(
        store,
        allow_remote=args.allow_remote,
        hotkey_status=lambda: listener.status if listener is not None else "disabled",
    )
    base_url = f"http://{args.host}:{args.port}"

    if not args.no_hotkey and sys.platform == "win32":
        from inspeg import service
        from inspeg.adapters.clipboard import read_clipboard_snapshot
        from inspeg.adapters.hotkey import HotkeyListener

        def do_capture() -> None:
            try:
                capture = service.ingest_clipboard(store, read_clipboard_snapshot())
            except (service.EmptyCaptureError, service.CaptureTooLargeError) as exc:
                log.warning("capture skipped: %s", exc)
                return
            except Exception:
                log.exception("capture failed")
                return
            log.info(
                "captured artifact %s… (%s) -> anchor %s",
                capture.artifact_id[:12],
                capture.provenance,
                capture.anchor_id,
            )
            webbrowser.open(f"{base_url}/?anchor={capture.anchor_id}")

        def on_hotkey() -> None:
            # Off the message-loop thread so a slow capture cannot make the
            # hotkey (or its message pump) unresponsive.
            threading.Thread(target=do_capture, name="inspeg-capture", daemon=True).start()

        listener = HotkeyListener(args.hotkey, on_hotkey)
        listener.start()
        log.info("hotkey %s registered — copy something, then press it", args.hotkey)
    elif not args.no_hotkey:
        log.info("hotkey capture disabled (only available on Windows)")

    def cleanup() -> None:
        if listener is not None:
            listener.stop()
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
