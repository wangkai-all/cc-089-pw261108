"""比对结果 CSV 导出（纯本地，不经过任何外部服务）。"""
from __future__ import annotations

import csv
import io

from .model import CellResult, DIFF_LABELS, DIFF_MATCH

EXPORT_COLUMNS = [
    "entity_id", "event_time", "feature", "category", "category_label",
    "missing_side", "offline_raw", "online_raw",
    "offline_type", "online_type", "numeric_delta", "within_tolerance",
    "offline_source_time", "online_source_time", "detail",
]


def export_csv(results: list[CellResult], include_matches: bool = False,
               categories: list[str] | None = None) -> str:
    """把单元格结果序列化为 CSV 字符串。"""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for c in results:
        if c.category == DIFF_MATCH and not include_matches:
            continue
        if categories and c.category not in categories:
            continue
        row = c.to_dict()
        row["category_label"] = DIFF_LABELS.get(c.category, c.category)
        writer.writerow({k: _fmt(row.get(k)) for k in EXPORT_COLUMNS})
    # utf-8-sig 风格 BOM，Excel 直接打开不乱码
    return "﻿" + buf.getvalue()


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        import json
        return json.dumps(v, ensure_ascii=False)
    return v
