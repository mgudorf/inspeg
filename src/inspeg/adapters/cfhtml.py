"""Parse the Windows CF_HTML ("HTML Format") clipboard payload.

Reference: https://learn.microsoft.com/en-us/windows/win32/dataxchg/html-clipboard-format

The payload is a UTF-8 byte buffer with an ASCII key:value header whose
Start*/End* fields are BYTE offsets into the whole buffer. A description
header written by a browser carries ``SourceURL`` — that is what earns a
capture provenance tier 2 (``sourced``) instead of tier 3 (``attributed``).

This module is pure (no win32 imports) so it is testable on any platform.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADER_LINE = re.compile(rb"^([A-Za-z]+):(.*?)\r?$")
_KNOWN_KEYS = {
    b"Version",
    b"StartHTML",
    b"EndHTML",
    b"StartFragment",
    b"EndFragment",
    b"StartSelection",
    b"EndSelection",
    b"SourceURL",
}


class CfHtmlError(ValueError):
    """The buffer is not a well-formed CF_HTML payload."""


@dataclass(frozen=True)
class CfHtml:
    html: str  # full HTML context, decoded
    fragment_start: int  # char offset of the fragment within `html`
    fragment_end: int
    source_url: str | None
    version: str | None

    @property
    def fragment(self) -> str:
        return self.html[self.fragment_start : self.fragment_end]


def _to_int(fields: dict[str, str], key: str, default: int) -> int:
    raw = fields.get(key, "")
    try:
        return int(raw)
    except ValueError:
        return default


def parse_cf_html(data: bytes) -> CfHtml:
    data = data.rstrip(b"\x00")
    if not data:
        raise CfHtmlError("empty CF_HTML buffer")

    # Read consecutive key:value header lines from the top of the buffer.
    fields: dict[str, str] = {}
    pos = 0
    while pos < len(data):
        newline = data.find(b"\n", pos)
        end = newline if newline != -1 else len(data)
        match = _HEADER_LINE.match(data[pos:end])
        if not match or match.group(1) not in _KNOWN_KEYS:
            break
        fields[match.group(1).decode("ascii")] = match.group(2).decode("utf-8", "replace").strip()
        pos = end + 1 if newline != -1 else len(data)

    if "StartFragment" not in fields or "EndFragment" not in fields:
        raise CfHtmlError("missing StartFragment/EndFragment header fields")

    start_html = _to_int(fields, "StartHTML", -1)
    end_html = _to_int(fields, "EndHTML", -1)
    if start_html < 0 or start_html > len(data):
        start_html = pos  # header end; some producers write StartHTML:-1
    if end_html < start_html or end_html > len(data):
        end_html = len(data)

    html_bytes = data[start_html:end_html]
    html = html_bytes.decode("utf-8", "replace")

    def to_char_offset(byte_offset: int) -> int:
        rel = min(max(byte_offset - start_html, 0), len(html_bytes))
        return len(html_bytes[:rel].decode("utf-8", "replace"))

    fragment_start = to_char_offset(_to_int(fields, "StartFragment", start_html))
    fragment_end = to_char_offset(_to_int(fields, "EndFragment", end_html))
    if fragment_end < fragment_start:
        fragment_start, fragment_end = fragment_end, fragment_start

    return CfHtml(
        html=html,
        fragment_start=fragment_start,
        fragment_end=fragment_end,
        source_url=fields.get("SourceURL") or None,
        version=fields.get("Version") or None,
    )
