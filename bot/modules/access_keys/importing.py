"""key 文件导入的解析与校验。

只负责「文件 → key 列表」，不碰数据库与 Discord；这样边界条件可以离线测。
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path

MAX_FILE_BYTES = 1024 * 1024
MAX_LINES = 5000
MAX_KEY_LENGTH = 200
ALLOWED_SUFFIXES = (".txt", ".csv")


class KeyFileError(ValueError):
    """文件本身不可用（扩展名、大小、编码）。"""


@dataclass(frozen=True)
class ParsedKeys:
    keys: tuple[str, ...]
    invalid: tuple[tuple[int, str], ...]
    duplicates_in_file: int


def parse_key_file(filename: str, data: bytes) -> ParsedKeys:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise KeyFileError(f"只支持 .txt / .csv，收到 {suffix or '无扩展名'}")
    if len(data) > MAX_FILE_BYTES:
        raise KeyFileError(f"文件超过 {MAX_FILE_BYTES // 1024} KB")

    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise KeyFileError("文件不是 UTF-8 文本") from exc

    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        raise KeyFileError(f"文件超过 {MAX_LINES} 行")

    if suffix == ".csv":
        entries = _csv_entries(text)
    else:
        entries = [(index, line) for index, line in enumerate(lines, start=1)]

    keys: list[str] = []
    invalid: list[tuple[int, str]] = []
    seen: set[str] = set()
    duplicates = 0

    for line_no, raw in entries:
        value = raw.strip()
        if not value:
            continue
        reason = _invalid_reason(value)
        if reason is not None:
            invalid.append((line_no, reason))
            continue
        if value in seen:
            duplicates += 1
            continue
        seen.add(value)
        keys.append(value)

    return ParsedKeys(keys=tuple(keys), invalid=tuple(invalid), duplicates_in_file=duplicates)


def _csv_entries(text: str) -> list[tuple[int, str]]:
    entries: list[tuple[int, str]] = []
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as exc:
        raise KeyFileError(f"CSV 无法解析：{exc}") from exc
    for index, row in enumerate(rows, start=1):
        if not row:
            continue
        if index == 1 and row[0].strip().lower() == "key":
            continue
        entries.append((index, row[0]))
    return entries


def _invalid_reason(value: str) -> str | None:
    if len(value) > MAX_KEY_LENGTH:
        return f"超过 {MAX_KEY_LENGTH} 字符"
    if any(ord(char) < 32 for char in value):
        return "包含控制字符"
    return None
