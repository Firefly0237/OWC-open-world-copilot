"""Hardening · table importers must tolerate the dirty files real users upload: non-UTF-8
encodings, oversized cells, and corrupted/wrong-format files (clean error, never a raw crash)."""

from __future__ import annotations

from pathlib import Path

import pytest

from owcopilot.content.importers.csv import CSVImporter
from owcopilot.content.importers.json import JSONImporter
from owcopilot.content.importers.markdown import MarkdownImporter
from owcopilot.content.importers.xlsx import XLSXImporter


def _w(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_csv_oversized_cell_does_not_raise_raw_csv_error(tmp_path: Path) -> None:
    # a 2 MB cell exceeds the stdlib default field limit; used to leak a raw _csv.Error
    p = _w(tmp_path, "big.csv", b"id,name\nq1," + b"x" * 2_000_000 + b"\n")
    rows = CSVImporter().parse(p)
    assert len(rows) == 1 and len(rows[0].data["name"]) == 2_000_000


def test_csv_decodes_gb18030(tmp_path: Path) -> None:
    p = _w(tmp_path, "cn.csv", "id,name\nq1,任务一\n".encode("gb18030"))
    assert CSVImporter().parse(p)[0].data["name"] == "任务一"


def test_json_decodes_gb18030(tmp_path: Path) -> None:
    # JSON importer used to hardcode utf-8 and crash on a Chinese-tool export
    p = _w(tmp_path, "cn.json", '[{"id":"q1","name":"任务一"}]'.encode("gb18030"))
    assert JSONImporter().parse(p)[0].data["name"] == "任务一"


def test_json_malformed_is_clean_value_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="不是合法 JSON"):
        JSONImporter().parse(_w(tmp_path, "bad.json", b"<html>not json</html>"))


def test_markdown_decodes_gb18030(tmp_path: Path) -> None:
    md = "## NPCs\n\n| id | name |\n|----|----|\n| n1 | 张三 |\n".encode("gb18030")
    assert MarkdownImporter().parse(_w(tmp_path, "cn.md", md))


@pytest.mark.parametrize("data", [b"", b"not a zip", b"PK\x03\x04 truncated"])
def test_xlsx_corrupt_is_clean_value_error(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(ValueError, match="not a valid .xlsx file"):
        XLSXImporter().parse(_w(tmp_path, "bad.xlsx", data))
