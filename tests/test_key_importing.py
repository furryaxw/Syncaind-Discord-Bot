"""key 文件解析：扩展名、编码、CSV 首列、去重与非法行。"""

from __future__ import annotations

import pytest

from bot.modules.access_keys.importing import KeyFileError, parse_key_file


def test_txt_ignores_blanks_and_counts_duplicates() -> None:
    parsed = parse_key_file("keys.txt", "\ufeffK1\n\n K2 \nK1\nK3\n".encode())

    assert parsed.keys == ("K1", "K2", "K3")
    assert parsed.duplicates_in_file == 1
    assert parsed.invalid == ()


def test_txt_reports_invalid_lines() -> None:
    too_long = "X" * 201
    parsed = parse_key_file("keys.txt", f"K1\n{too_long}\nK2\u0001\n".encode())

    assert parsed.keys == ("K1",)
    assert [line for line, _reason in parsed.invalid] == [2, 3]


def test_csv_uses_first_column_and_skips_key_header() -> None:
    parsed = parse_key_file("keys.csv", b"key,note\nK1,first\nK2,second\n")

    assert parsed.keys == ("K1", "K2")


def test_csv_handles_quoted_values() -> None:
    parsed = parse_key_file("keys.csv", b'"K,1",x\nK2,y\n')

    assert parsed.keys == ("K,1", "K2")


def test_rejects_unsupported_extension() -> None:
    with pytest.raises(KeyFileError):
        parse_key_file("keys.json", b"[]")


def test_rejects_invalid_utf8() -> None:
    with pytest.raises(KeyFileError):
        parse_key_file("keys.txt", b"\xff\xfe\x00")


def test_rejects_oversized_file() -> None:
    with pytest.raises(KeyFileError):
        parse_key_file("keys.txt", b"K" * (1024 * 1024 + 1))


def test_rejects_too_many_lines() -> None:
    data = "\n".join(f"K{index}" for index in range(5001)).encode()

    with pytest.raises(KeyFileError):
        parse_key_file("keys.txt", data)
