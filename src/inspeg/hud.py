"""The HUD shell: a docked, always-on-top pywebview/WebView2 window
(ADR 0009), run as its own process because pywebview needs the main thread
and the daemon's belongs to uvicorn.

This process never opens the Store — it is a pure HTTP client of the
daemon. V18: navigation is confined to the daemon origin; anything else is
cancelled and handed to the system browser.
"""

from __future__ import annotations

import argparse
import logging
import sys
import webbrowser

log = logging.getLogger("inspeg.hud")

DEFAULT_URL = "http://127.0.0.1:8137/hud/"
DEFAULT_WIDTH = 380


class HudApi:
    """window.pywebview.api — the page's only handle on its own window."""

    def __init__(self) -> None:
        self.window = None
        self._visible = True
        self._auto_hidden = False

    def toggle(self) -> None:
        if self.window is None:
            return
        if self._visible:
            self.window.hide()
            self._visible = False
        else:
            self.window.show()
            self._visible = True
            self._auto_hidden = False

    def auto_hide(self) -> None:
        """Fullscreen guard: hide, but remember it was our doing."""
        if self.window is not None and self._visible:
            self.window.hide()
            self._visible = False
            self._auto_hidden = True

    def auto_show(self) -> None:
        """Undo an auto-hide; never overrides an explicit user hide."""
        if self.window is not None and self._auto_hidden:
            self.window.show()
            self._visible = True
            self._auto_hidden = False


def _apply_window_styles(window) -> None:
    """WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW (never steals focus, out of
    Alt+Tab) + NonRudeHWND (the shell must not misclassify a topmost panel
    as a fullscreen app). Best-effort: a failure degrades looks, not
    function."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        hwnd = None
        native = getattr(window, "native", None)
        if native is not None and hasattr(native, "Handle"):
            hwnd = int(str(native.Handle))
        if not hwnd:
            hwnd = ctypes.windll.user32.FindWindowW(None, window.title)
        if not hwnd:
            return
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        user32.SetPropW(hwnd, "NonRudeHWND", 1)
    except Exception:
        log.debug("window style tweak failed", exc_info=True)


def _work_area() -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the primary monitor's work area."""
    if sys.platform != "win32":
        return (0, 0, 1920, 1080)
    import ctypes
    from ctypes import wintypes

    rect = wintypes.RECT()
    SPI_GETWORKAREA = 0x0030
    ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="inspeg-hud")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        import webview
    except ImportError:
        log.warning("pywebview not installed (pip install inspeg[hud]) — opening in browser")
        webbrowser.open(args.url)
        return 0

    api = HudApi()
    _left, top, right, bottom = _work_area()
    origin = args.url.split("/hud")[0]

    window = webview.create_window(
        "inspeg hud",
        args.url,
        js_api=api,
        width=args.width,
        height=bottom - top,
        x=right - args.width,
        y=top,
        frameless=True,
        easy_drag=False,
        on_top=True,
        focus=False,
    )
    api.window = window

    def on_shown() -> None:
        _apply_window_styles(window)

    def on_loaded() -> None:
        # V18: a hostile page must never render inside the HUD window.
        try:
            current = window.get_current_url() or ""
            if not current.startswith(origin):
                log.warning("blocked off-origin navigation to %r", current)
                window.load_url(args.url)
                webbrowser.open(current)
        except Exception:
            log.debug("navigation guard check failed", exc_info=True)

    window.events.shown += on_shown
    window.events.loaded += on_loaded
    webview.start()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
