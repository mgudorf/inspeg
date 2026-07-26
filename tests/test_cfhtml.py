import pytest

from helpers import build_cf_html
from inspeg.adapters.cfhtml import CfHtmlError, parse_cf_html


def test_roundtrip_fragment_and_source_url():
    payload = build_cf_html(
        '<p>Hello <a href="https://x.test/">world</a></p>',
        source_url="https://example.com/page",
    )
    cf = parse_cf_html(payload)
    assert cf.fragment == '<p>Hello <a href="https://x.test/">world</a></p>'
    assert cf.source_url == "https://example.com/page"
    assert cf.version == "0.9"
    assert 'href="https://x.test/"' in cf.html  # hrefs survive, unlike selectionText


def test_missing_source_url_is_none():
    cf = parse_cf_html(build_cf_html("<b>no url</b>"))
    assert cf.source_url is None


def test_trailing_null_bytes_are_stripped():
    payload = build_cf_html("<i>padded</i>", source_url="https://a.test/") + b"\x00\x00\x00"
    cf = parse_cf_html(payload)
    assert cf.fragment == "<i>padded</i>"


def test_multibyte_content_yields_correct_char_offsets():
    fragment = "<p>café ☕ — naïve</p>"
    cf = parse_cf_html(build_cf_html(fragment, source_url="https://u.test/"))
    assert cf.fragment == fragment
    # Offsets index into the decoded string, not the byte buffer.
    assert cf.html[cf.fragment_start : cf.fragment_end] == fragment


def test_fragment_must_be_declared():
    with pytest.raises(CfHtmlError):
        parse_cf_html(b"Version:0.9\r\n<html><body>hi</body></html>")


def test_empty_buffer_raises():
    with pytest.raises(CfHtmlError):
        parse_cf_html(b"\x00\x00")
