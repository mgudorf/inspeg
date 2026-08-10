"""Capture adapters. Each is small, optional, and independently abandonable.

Nothing in this package persists anything passively: every capture happens
because the user pressed something (ADR 0004, part 2a), and nothing observes
*content* — clipboard, screen, mic, keystrokes — outside an explicit capture
gesture (part 2b). The one sanctioned exception (part 2c) is the ephemeral
display context in ``foreground.py``: foreground-window *metadata* held in
daemon memory for the HUD, never written anywhere, never capture-triggering,
and fully disabled by ``--no-context-watch``.
"""
