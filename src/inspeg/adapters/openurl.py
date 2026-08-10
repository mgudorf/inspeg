"""Launch external targets for the HUD (V17 — the dispatcher, not the guard).

Scheme allowlisting happens in the API layer *before* anything reaches this
module; these functions only perform the launch. Windows-only pieces keep
their imports inside functions so the package imports everywhere.
"""

from __future__ import annotations

import sys
import webbrowser


def open_url(url: str) -> None:
    """Open an allowlisted URL with its registered handler."""
    if sys.platform == "win32":
        import os

        # os.startfile routes through ShellExecute, which handles both web
        # URLs and registered app protocols (vscode://) uniformly. The V17
        # scheme allowlist ran before this call — never feed it stored data
        # directly.
        os.startfile(url)
    else:
        webbrowser.open(url)


def reveal_in_explorer(path: str) -> None:
    """Select ``path`` in a File Explorer window.

    SHOpenFolderAndSelectItems, never ``explorer.exe /select,<string>`` — the
    latter re-parses its argument and breaks (or worse) on commas and quotes.
    """
    if sys.platform != "win32":
        raise NotImplementedError("reveal is only available on Windows")
    import ctypes
    from ctypes import wintypes

    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32
    ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED; required first
    try:
        pidl = ctypes.c_void_p()
        sfgao = wintypes.ULONG()
        parse = shell32.SHParseDisplayName(
            ctypes.c_wchar_p(path), None, ctypes.byref(pidl), 0, ctypes.byref(sfgao)
        )
        if parse:
            raise FileNotFoundError(path)
        try:
            if shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0):
                raise OSError(f"could not reveal {path!r}")
        finally:
            ole32.CoTaskMemFree(pidl)
    finally:
        ole32.CoUninitialize()
