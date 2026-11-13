"""生成带植入问题的样例数据，便于快速体验检查台。

植入的问题（运行后应被全部识别）：
- 值漂移：E001 的 score 线上 92.5 vs 离线 90.0（超容差）
- 容差内一致：E001 的 score 另有 0.03 抖动（在 0.05 容差内）
- 类型差异：E002 的 is_vip 线上为字符串 "VIP"
- 空值差异：E003 的 city 一侧为空
- 整行缺失：E004 仅出现在线上；E005 仅出现在离线
- 时间错位：E006 离线命中行滞后线上目标时间 2 小时（容忍 30 分钟）
- 未来数据泄漏防护：E007 离线仅有晚于目标时间的行，必须判缺失而非取未来值
- 重复主键：E008 离线同主键两行，默认 fail，配置改为 mean 后聚合
"""
from __future__ import annotations

import csv
import json
import os

HEADER = ["entity_id", "event_time", "score", "city", "is_vip"]

OFFLINE_ROWS = [
    # E001：常规 + 小抖动
    ["E001", "2026-11-01T10:00:00+08:00", "90.0", "Beijing", "true"],
    ["E001", "2026-11-01T11:00:00+08:00", "90.03", "Beijing", "true"],
    # E002：布尔正常
    ["E002", "2026-11-01T10:00:00+08:00", "75.5", "Shanghai", "false"],
    # E003：离线 city 为空
    ["E003", "2026-11-01T10:00:00+08:00", "60", "", "true"],
    # E004：离线完全没有该实体 -> 线上视角整行缺失
    # E005：离线独有
    ["E005", "2026-11-01T10:00:00+08:00", "88", "Shenzhen", "true"],
    # E006：离线行比目标时间早 2 小时（as-of 命中但时间错位）
    ["E006", "2026-11-01T08:00:00+08:00", "70", "Hangzhou", "true"],
    # E007：只有“未来”行（晚于线上目标 11:00），防泄漏必须拒绝
    ["E007", "2026-11-01T12:00:00+08:00", "55", "Chengdu", "false"],
    # E008：重复主键（两行），需要显式策略
    ["E008", "2026-11-01T10:00:00+08:00", "40", "Wuhan", "true"],
    ["E008", "2026-11-01T10:00:00+08:00", "60", "Wuhan", "true"],
]

ONLINE_ROWS = [
    # E001：score 漂移到 92.5；11:00 行抖动 0.03 在容差内
    ["E001", "2026-11-01T10:00:00+08:00", "92.5", "Beijing", "true"],
    ["E001", "2026-11-01T11:00:00+08:00", "90.06", "Beijing", "true"],
    # E002：is_vip 类型差异（字符串）
    ["E002", "2026-11-01T10:00:00+08:00", "75.5", "Shanghai", "VIP"],
    # E003：线上 city 有值 -> 空值差异
    ["E003", "2026-11-01T10:00:00+08:00", "60", "Guangzhou", "true"],
    # E004：线上独有
    ["E004", "2026-11-01T10:00:00+08:00", "33", "Xian", "false"],
    # E005 在线上缺失 -> 离线视角整行缺失
    # E006：目标时间 10:00，as-of 命中 08:00 的行 -> 时间错位
    ["E006", "2026-11-01T10:00:00+08:00", "70", "Hangzhou", "true"],
    # E007：目标 11:00，离线只有 12:00（未来）-> 整行缺失
    ["E007", "2026-11-01T11:00:00+08:00", "55", "Chengdu", "false"],
    # E008：线上单行，离线两行均值 50 vs 线上 50 -> 聚合后一致
    ["E008", "2026-11-01T10:00:00+08:00", "50", "Wuhan", "true"],
]

# 时区不一致的演示数据（朴素 +08:00 风格时间，与上面带偏移的混用会触发提示）
NAIVE_ROWS = [
    ["E100", "2026-11-01 10:00:00", "12.5", "Nanjing", "true"],
]


def write_csv(path: str, rows: list[list[str]]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(HEADER)
        writer.writerows(rows)


def build_config() -> dict:
    return {
        "name": "样例：用户画像特征一致性",
        "entity_column": "entity_id",
        "event_time_column": "event_time",
        "timezone": "+08:00",
        "time_tolerance_seconds": 1800,
        "null_policy": "strict",
        "duplicate_strategy": "fail",
        "default_abs_tolerance": 0.05,
        "default_rel_tolerance": 0.0,
        "default_decimal_precision": 2,
        "accept_timezone_warning": True,
        "features": [
            {"name": "score", "dtype": "number"},
            {"name": "city", "dtype": "string",
             "abs_tolerance": None, "rel_tolerance": None,
             "decimal_precision": None},
            {"name": "is_vip", "dtype": "bool"},
        ],
    }


def main(out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    write_csv(os.path.join(out_dir, "offline.csv"), OFFLINE_ROWS)
    write_csv(os.path.join(out_dir, "online.csv"), ONLINE_ROWS)
    write_csv(os.path.join(out_dir, "online_naive.csv"), NAIVE_ROWS)
    with open(os.path.join(out_dir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(build_config(), fh, ensure_ascii=False, indent=2)
    print(f"样例已生成到 {out_dir}")


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples"))
