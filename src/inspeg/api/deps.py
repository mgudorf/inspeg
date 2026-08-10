"""Shared route dependencies."""

from __future__ import annotations

from fastapi import Header, HTTPException


def require_capture_header(x_inspeg_capture: str | None = Header(None)) -> None:
    """CSRF belt-and-braces (V1): a custom header forces a CORS preflight,
    which no web origin can pass. Required on every write route reachable by
    the browser extension."""
    if x_inspeg_capture is None:
        raise HTTPException(
            403, "missing X-Inspeg-Capture header (CSRF guard; send 'X-Inspeg-Capture: 1')"
        )
