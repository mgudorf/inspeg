"""Run the inspeg daemon: API + quick-capture UI + (on Windows) the capture hotkey."""

from __future__ import annotations

import argparse
import logging
import sys
import webbrowser
from pathlib import Path

import uvicorn

from inspeg.api.app import create_app
from inspeg.store import Store

DEFAULT_PORT = 8137
DEFAULT_HOTKEY = "ctrl+alt+a"

log = logging.getLogger("inspeg")


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
    parser.add_argument("--host", default="127.0.0.1", help="bind address (keep it local)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--hotkey", default=DEFAULT_HOTKEY, help="capture hotkey, e.g. ctrl+alt+a")
    parser.add_argument("--no-hotkey", action="store_true", help="run the API/UI only")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    store = Store(args.data_dir)
    app = create_app(store)
    base_url = f"http://{args.host}:{args.port}"

    listener = None
    if not args.no_hotkey and sys.platform == "win32":
        from inspeg import service
        from inspeg.adapters.clipboard import read_clipboard_snapshot
        from inspeg.adapters.hotkey import HotkeyListener

        def on_hotkey() -> None:
            try:
                capture = service.ingest_clipboard(store, read_clipboard_snapshot())
            except service.EmptyCaptureError as exc:
                log.warning("capture skipped: %s", exc)
                return
            log.info(
                "captured artifact %s… (%s) -> anchor %s",
                capture.artifact_id[:12],
                capture.provenance,
                capture.anchor_id,
            )
            webbrowser.open(f"{base_url}/?anchor={capture.anchor_id}")

        listener = HotkeyListener(args.hotkey, on_hotkey)
        listener.start()
        log.info("hotkey %s registered — copy something, then press it", args.hotkey)
    elif not args.no_hotkey:
        log.info("hotkey capture disabled (only available on Windows)")

    log.info("quick-capture UI at %s (data in %s)", base_url, args.data_dir)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        if listener is not None:
            listener.stop()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
