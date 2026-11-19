import codecs

import pytest

from paperclerk.ingest.parsers.text import decode_text

CSV = "交易日期,摘要,金额\n2026-10-24,电费,214.37\n2026-10-25,水费,40.00\n"


@pytest.mark.parametrize(
    "data",
    [
        CSV.encode("utf-8"),
        codecs.BOM_UTF8 + CSV.encode("utf-8"),
        CSV.encode("utf-16"),  # with BOM, as Excel's "Unicode text" export writes it
        CSV.encode("gbk"),  # Chinese banks and Windows apps
    ],
    ids=["utf-8", "utf-8-bom", "utf-16", "gbk"],
)
def test_bank_csv_decodes_in_common_encodings(data):
    assert decode_text(data) == CSV


def test_big5_traditional_chinese():
    text = "交易日期,摘要,金額\n2026-10-24,電費,214.37\n2026-10-25,水費,40.00\n"
    assert decode_text(text.encode("big5")) == text


def test_undecodable_bytes_never_raise():
    assert isinstance(decode_text(bytes(range(256))), str)


@pytest.mark.parametrize(
    "text",
    ["café", "Müller", "Total: 12,50 €", "Rechnung für Herrn Müller, Café crème\n" * 100],
    ids=["cafe", "muller", "euro", "long"],
)
def test_western_windows_1252(text):
    assert decode_text(text.encode("cp1252")) == text


@pytest.mark.parametrize("text", ["电费", "交易日期,摘要,金额"], ids=["2-chars", "header"])
def test_short_gbk(text):
    assert decode_text(text.encode("gbk")) == text


def test_long_gbk_with_one_bad_byte_stays_chinese():
    data = (CSV * 2000).encode("gbk")
    text = decode_text(data[:50_000] + b"\x80" + data[50_000:])
    assert text.startswith(CSV) and text.count("电费") == 2000


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be", "utf-32"])
def test_utf16_without_bom_and_utf32(encoding):
    assert decode_text(CSV.encode(encoding)) == CSV


def test_binary_in_a_txt_file_is_a_parse_error():
    from paperclerk.ingest.parsers import ParseError

    with pytest.raises(ParseError, match="binary"):
        decode_text(b"".join(i.to_bytes(4, "little") for i in range(1000)))  # int32 array
