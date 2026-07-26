"""Read the clipboard once, on explicit invocation (hotkey or API call).

Manual by construction: there is no listener, no WM_CLIPBOARDUPDATE, nothing
that observes the clipboard unless the user invoked a capture.

Windows-only (pywin32); import errors surface only when actually called.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import PureWindowsPath


class ClipboardBusyError(RuntimeError):
    """Another process held the clipboard open for too long."""


@dataclass(frozen=True)
class ClipboardSnapshot:
    cf_html: bytes | None = None  # raw "HTML Format" payload
    text: str | None = None  # CF_UNICODETEXT
    source_app: str | None = None  # foreground exe + window title at capture time


def read_clipboard_snapshot() -> ClipboardSnapshot:
    import win32clipboard as wc

    cf_html_format = wc.RegisterClipboardFormat("HTML Format")

    for attempt in range(5):
        try:
            wc.OpenClipboard()
            break
        except Exception:  # pywintypes.error: clipboard held by another process
            if attempt == 4:
                raise ClipboardBusyError("could not open the clipboard") from None
            time.sleep(0.05)
    try:
        cf_html = (
            wc.GetClipboardData(cf_html_format)
            if wc.IsClipboardFormatAvailable(cf_html_format)
            else None
        )
        text = (
            wc.GetClipboardData(wc.CF_UNICODETEXT)
            if wc.IsClipboardFormatAvailable(wc.CF_UNICODETEXT)
            else None
        )
    finally:
        wc.CloseClipboard()

    if isinstance(cf_html, str):  # pywin32 may hand back str for some producers
        cf_html = cf_html.encode("utf-8")

    return ClipboardSnapshot(cf_html=cf_html, text=text, source_app=foreground_app())


def foreground_app() -> str | None:
    """Best-effort '<exe> | <window title>' for provenance tier 3."""
    try:
        import win32gui
        import win32process

        hwnd = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(hwnd) or ""
        exe = _process_image(win32process.GetWindowThreadProcessId(hwnd)[1])
        parts = [p for p in (exe, title) if p]
        return " | ".join(parts) or None
    except Exception:
        return None


def _process_image(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return PureWindowsPath(buffer.value).name
        return None
    finally:
        kernel32.CloseHandle(handle)
