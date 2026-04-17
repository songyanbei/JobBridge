"""CSV 导出工具（Phase 5）。

统一写入 UTF-8 BOM，避免 Excel 打开中文乱码。
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Any, Iterable


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, dict)):
        import json
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def rows_to_csv_bytes(headers: list[str], rows: Iterable[list[Any]]) -> bytes:
    """将行数据写入 CSV 字节流（含 UTF-8 BOM）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_stringify(v) for v in row])
    # UTF-8 BOM + 内容
    return "\ufeff".encode("utf-8") + buffer.getvalue().encode("utf-8")
