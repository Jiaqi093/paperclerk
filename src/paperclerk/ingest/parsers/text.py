from __future__ import annotations

import codecs
from pathlib import Path

from charset_normalizer import from_bytes

from paperclerk.ingest.parsers.base import Page, ParsedDocument, ParseError


class TextParser:
    suffixes = frozenset({".txt", ".md", ".csv"})
    version = 1

    def parse(self, data: bytes, path: Path) -> ParsedDocument:
        return ParsedDocument(mime_type="text/plain", pages=(Page(decode_text(data)),))


# Detection runs on a sample (it is slow and memory-hungry on whole files) and only for
# inputs long enough for statistics to mean something; it may only pick from encodings this
# app's users plausibly have, since unrestricted it confuses GBK with Korean and Windows-1252
# with Baltic code pages.
DETECT_MIN_BYTES = 1024
DETECT_SAMPLE_BYTES = 64 * 1024
DETECT_CANDIDATES = ["gb18030", "big5", "shift_jis", "cp1252"]
# Short input is checked against Chinese encodings by plausibility instead, in this order;
# what isn't plausibly Chinese is read as Windows-1252 (Western European).
CJK_ENCODINGS = ("gb18030", "big5")
_BOMS = (  # UTF-32 first: its little-endian BOM starts with UTF-16's
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def decode_text(data: bytes) -> str:
    """Bytes of a text file as str, whatever the encoding. Never raises except ParseError for
    content that isn't text at all.

    BOM-marked Unicode and plain UTF-8 are decoded directly; UTF-16 without a BOM is
    recognised by its zero bytes. Otherwise, e.g. a GBK CSV from a Chinese bank or a
    Windows-1252 note: long input goes to charset-normalizer; short input is GBK or Big5 if it
    decodes cleanly to Chinese characters, else Windows-1252.
    """
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data.decode(encoding, errors="replace")
    if data.count(0) > len(data) // 4:
        return _decode_utf16_without_bom(data)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if len(data) >= DETECT_MIN_BYTES:
        best = from_bytes(_sample(data), cp_isolation=DETECT_CANDIDATES).best()
        if best is not None:
            return data.decode(best.encoding, errors="replace")
    for encoding in CJK_ENCODINGS:
        text = _if_plausibly_chinese(data, encoding)
        if text is not None:
            return text
    return data.decode("cp1252", errors="replace")


def _decode_utf16_without_bom(data: bytes) -> str:
    # ASCII-range text in UTF-16 has a zero in every other byte: odd positions for LE.
    encoding = "utf-16-le" if data[1::2].count(0) >= data[0::2].count(0) else "utf-16-be"
    text = data.decode(encoding, errors="replace")
    readable = sum(1 for c in text if c.isprintable() or c in "\t\r\n")
    if readable < 0.9 * len(text):
        raise ParseError("binary data, not text")  # zero bytes that aren't UTF-16 text
    return text


def _sample(data: bytes) -> bytes:
    if len(data) <= DETECT_SAMPLE_BYTES:
        return data
    sample = data[:DETECT_SAMPLE_BYTES]
    cut = sample.rfind(b"\n")  # don't cut a multi-byte character in half
    return sample[:cut] if cut > 0 else sample


def _if_plausibly_chinese(data: bytes, encoding: str) -> str | None:
    text = data.decode(encoding, errors="replace")
    non_ascii = [c for c in text if ord(c) > 127 and c != "�"]
    bad = text.count("�")
    if len(non_ascii) < 2 or bad > max(1, len(non_ascii) // 200):
        return None
    if sum(map(_is_cjk, non_ascii)) < 0.9 * len(non_ascii):
        return None
    return text


def _is_cjk(c: str) -> bool:
    o = ord(c)
    return (
        0x4E00 <= o <= 0x9FFF  # unified ideographs
        or 0x3400 <= o <= 0x4DBF  # extension A
        or 0x3000 <= o <= 0x303F  # CJK punctuation
        or 0xFF00 <= o <= 0xFFEF  # full-width forms
    )
