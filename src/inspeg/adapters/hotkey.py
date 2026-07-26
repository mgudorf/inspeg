"""Global capture hotkey via RegisterHotKey (Windows).

RegisterHotKey must be called on the same thread that pumps the message loop,
so the listener owns both. The callback runs on this thread; keep it quick.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

_MOD_FLAGS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012
_HOTKEY_ID = 1


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'ctrl+alt+a' -> (modifier flags, virtual-key code)."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"empty hotkey spec: {spec!r}")
    mods = 0
    for part in parts[:-1]:
        if part not in _MOD_FLAGS:
            raise ValueError(f"unknown modifier {part!r} in hotkey {spec!r}")
        mods |= _MOD_FLAGS[part]
    key = parts[-1]
    if len(key) == 1 and (key.isalpha() or key.isdigit()):
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError(f"unsupported key {key!r} in hotkey {spec!r}")
    if not mods:
        raise ValueError(f"hotkey {spec!r} needs at least one modifier")
    return mods, vk


class HotkeyListener(threading.Thread):
    """Daemon thread: dies with the process, so a closed terminal never
    leaves a hotkey registration behind. ``status`` is surfaced through
    ``/api/health`` — a hotkey that failed to register or died must be
    visible, not silent."""

    def __init__(self, spec: str, callback: Callable[[], None]) -> None:
        super().__init__(name="inspeg-hotkey", daemon=True)
        self.spec = spec
        self.mods, self.vk = parse_hotkey(spec)  # fail fast, before the thread starts
        self.callback = callback
        self.status = "pending"  # -> registered | failed | stopped
        self._tid: int | None = None

    def run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, _HOTKEY_ID, self.mods | _MOD_NOREPEAT, self.vk):
            self.status = "failed"
            log.error("could not register hotkey %r (already in use by another app?)", self.spec)
            return
        self.status = "registered"
        try:
            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0:  # WM_QUIT
                    break
                if ret == -1:  # error — without this check the loop dies silently
                    log.error("hotkey message loop failed (GetMessageW returned -1)")
                    break
                if msg.message == _WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:
                        log.exception("hotkey callback failed")
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)
            self.status = "stopped"

    def stop(self) -> None:
        if self._tid is not None:
            import ctypes

            ctypes.windll.user32.PostThreadMessageW(self._tid, _WM_QUIT, 0, 0)
