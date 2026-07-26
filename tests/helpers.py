"""Test helpers: build a byte-accurate CF_HTML payload like Windows would."""

from __future__ import annotations

PREFIX = "<html>\r\n<body>\r\n<!--StartFragment-->"
SUFFIX = "<!--EndFragment-->\r\n</body>\r\n</html>"


def build_cf_html(fragment: str, source_url: str | None = None) -> bytes:
    body = PREFIX + fragment + SUFFIX
    lines = [
        "Version:0.9",
        "StartHTML:{0:010d}",
        "EndHTML:{1:010d}",
        "StartFragment:{2:010d}",
        "EndFragment:{3:010d}",
    ]
    if source_url:
        lines.append("SourceURL:" + source_url)
    header_fmt = "\r\n".join(lines) + "\r\n"

    header_len = len(header_fmt.format(0, 0, 0, 0).encode("utf-8"))
    body_bytes = body.encode("utf-8")
    start_html = header_len
    end_html = start_html + len(body_bytes)
    start_fragment = start_html + len(PREFIX.encode("utf-8"))
    end_fragment = end_html - len(SUFFIX.encode("utf-8"))

    header = header_fmt.format(start_html, end_html, start_fragment, end_fragment)
    return header.encode("utf-8") + body_bytes
