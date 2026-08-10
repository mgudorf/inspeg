"""Foreground-window watcher (ADR 0004, part 2c). Windows-only.

Event-driven, not polling: ``SetWinEventHook`` delivers foreground changes
and window renames (browser tab switches, editor file switches rename the
window) to a callback on the installing thread's message pump — which is
the hotkey listener's, the process's only pump. Only window *metadata*
(process image name, title) is read; content APIs are never touched.

``install_foreground_watch`` must be called on the pump thread and returns
an uninstall callable for the same thread. The ctypes trampoline is kept
module-referenced for the life of the process — releasing it while a hook
is installed is a crash, not a leak.
"""

from __future__ import annotations

import logging

from inspeg.context import ContextHub

log = logging.getLogger(__name__)

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_OBJECT_NAMECHANGE = 0x800C
WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002
OBJID_WINDOW = 0

# QUNS_* values from SHQueryUserNotificationState.
_QUNS_TO_STATE = {2: "busy", 3: "d3d", 4: "presentation"}

# Trampolines must outlive their hooks (see module docstring).
_trampoline_refs: list = []


def _fullscreen_state() -> str:
    import ctypes

    state = ctypes.c_int(0)
    if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
        return _QUNS_TO_STATE.get(state.value, "none")
    return "none"


def install_foreground_watch(hub: ContextHub):
    import ctypes
    from ctypes import wintypes

    from inspeg.adapters.clipboard import _process_image

    user32 = ctypes.windll.user32

    def update_from(hwnd: int) -> None:
        if not hwnd:
            return
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _process_image(pid.value) if pid.value else None
        hub.set_window(exe=exe, title=buffer.value or None)
        hub.set_fullscreen(_fullscreen_state())

    win_event_proc = ctypes.WINFUNCTYPE(
        None,
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.HWND,
        wintypes.LONG,
        wintypes.LONG,
        wintypes.DWORD,
        wintypes.DWORD,
    )

    def callback(hook, event, hwnd, id_object, id_child, thread_id, event_time):
        try:
            # Renames matter only for the window the user is looking at
            # (tab/file switches retitle it); everything else is noise.
            if event == EVENT_OBJECT_NAMECHANGE and (
                id_object != OBJID_WINDOW or hwnd != user32.GetForegroundWindow()
            ):
                return
            update_from(hwnd or user32.GetForegroundWindow())
        except Exception:  # a watcher hiccup must never kill the pump
            log.debug("foreground update failed", exc_info=True)

    trampoline = win_event_proc(callback)
    _trampoline_refs.append(trampoline)
    flags = WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS
    hooks = [
        user32.SetWinEventHook(event, event, 0, trampoline, 0, 0, flags)
        for event in (EVENT_SYSTEM_FOREGROUND, EVENT_OBJECT_NAMECHANGE)
    ]
    update_from(user32.GetForegroundWindow())  # seed the initial state

    def uninstall() -> None:
        for hook in hooks:
            if hook:
                user32.UnhookWinEvent(hook)

    log.info("foreground context watcher installed (disable with --no-context-watch)")
    return uninstall
