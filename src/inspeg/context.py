"""Ephemeral display context (ADR 0004, part 2c).

The one place foreground metadata is allowed to exist: daemon process
memory, bounded lifetime, never a file, never the Store. Nothing in this
module imports the Store — that is the structural half of the "context can
never reach the log" guarantee; the V16 regression test is the other half.

Pure and cross-platform: the win32 feed lives in ``adapters/foreground.py``;
this module is just state + TTLs and is fully testable anywhere.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterable

from inspeg.util import normalize_source_uri

TAB_TTL_SECONDS = 15.0
WORKSPACE_TTL_SECONDS = 60.0

FULLSCREEN_STATES = ("none", "busy", "d3d", "presentation")

# Window identities that are noise, not context. python/pythonw is inspeg
# itself (the daemon console, the HUD); chrome/edge/code are content surfaces
# whose *content* context arrives via extension pushes (tab, workspace) — the
# bare exe tells the user nothing they asked for. An ignored foreground app
# clears the window slot rather than displaying itself. Extend with
# --ignore-exe.
DEFAULT_IGNORED_EXES = frozenset(
    {"python.exe", "pythonw.exe", "py.exe", "chrome.exe", "msedge.exe", "code.exe"}
)


class ContextHub:
    """Current-foreground snapshot for the HUD. All setters are cheap and
    thread-safe (they run on the win32 pump thread and API threads)."""

    def __init__(
        self,
        on_change: Callable[[dict], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        ignored_exes: Iterable[str] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._clock = clock
        self.on_change = on_change
        self.ignored_exes = frozenset(
            e.lower() for e in (DEFAULT_IGNORED_EXES if ignored_exes is None else ignored_exes)
        )
        self._window: dict | None = None
        self._tab: dict | None = None
        self._workspace: dict | None = None
        self._fullscreen = "none"

    def _publish(self) -> None:
        callback = self.on_change
        if callback is not None:
            with contextlib.suppress(Exception):  # display plumbing never breaks a feed
                callback(self.snapshot())

    def set_window(self, *, exe: str | None, title: str | None) -> None:
        with self._lock:
            if exe is not None and exe.lower() in self.ignored_exes:
                # Ignored app in the foreground: clear the slot (a stale
                # previous window would be a lie) instead of showing it.
                self._window = None
            else:
                self._window = {"exe": exe, "title": title, "at": self._clock()}
        self._publish()

    def set_fullscreen(self, state: str) -> None:
        if state not in FULLSCREEN_STATES:
            state = "none"
        changed = False
        with self._lock:
            if state != self._fullscreen:
                self._fullscreen = state
                changed = True
        if changed:
            self._publish()

    def set_tab(self, url: str, title: str | None = None) -> None:
        with self._lock:
            self._tab = {
                "url": url,
                "url_norm": normalize_source_uri(url),
                "title": title,
                "at": self._clock(),
            }
        self._publish()

    def set_workspace(self, root: str | None, file: str | None) -> None:
        with self._lock:
            self._workspace = {"root": root, "file": file, "at": self._clock()}
        self._publish()

    def snapshot(self) -> dict:
        """Current context with TTL expiry applied. Extension/IDE pushes go
        stale fast (the browser may have lost focus); window identity does
        not expire — it is replaced by the next foreground event."""
        now = self._clock()
        with self._lock:
            if self._tab and now - self._tab["at"] > TAB_TTL_SECONDS:
                self._tab = None
            if self._workspace and now - self._workspace["at"] > WORKSPACE_TTL_SECONDS:
                self._workspace = None
            return {
                "window": (
                    {"exe": self._window["exe"], "title": self._window["title"]}
                    if self._window
                    else None
                ),
                "tab": (
                    {
                        "url": self._tab["url"],
                        "url_norm": self._tab["url_norm"],
                        "title": self._tab["title"],
                    }
                    if self._tab
                    else None
                ),
                "workspace": (
                    {"root": self._workspace["root"], "file": self._workspace["file"]}
                    if self._workspace
                    else None
                ),
                "fullscreen": self._fullscreen,
            }
