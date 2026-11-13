"""CSV 加载、类型解析与重复主键处理。

重复处理的硬约束：**绝不默认取最后一行**。
- 默认策略 ``fail``：发现 (实体, 事件时间) 重复即报错，并列出冲突键；
- 任何“取末行”的行为都必须由用户在配置里显式选择 ``last``；
- 数值聚合（min/max/mean/median）与众数（majority）按特征定义分别生效。
"""
from __future__ import annotations

import csv
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .model import (
    FeatureSpec, CheckConfig,
    DUP_FAIL, DUP_FIRST, DUP_LAST, DUP_MIN, DUP_MAX, DUP_MEAN,
    DUP_MEDIAN, DUP_MAJORITY,
    T_NUMBER, T_BOOL, T_STRING, T_AUTO,
)
from .timeutil import parse_timestamp, detect_timezone, TimezoneInfo, to_iso

_TRUE_TOKENS = {"true", "1", "yes", "y", "是", "真"}
_FALSE_TOKENS = {"false", "0", "no", "n", "否", "假"}


class LoadError(ValueError):
    """数据加载/重复键/类型解析错误。"""


@dataclass
class Row:
    entity_id: str
    event_dt: datetime            # 统一为 UTC aware datetime
    event_raw: str                # 原始事件时间字符串
    values: dict[str, Any]        # feature -> 解析值（None 表示空）
    raws: dict[str, str]          # feature -> 原始字符串（None 表示空）
    types: dict[str, str]         # feature -> 实际类型名


@dataclass
class LoadedTable:
    label: str                    # "offline" | "online"
    path: str
    rows: list[Row]
    timezone: TimezoneInfo
    duplicate_groups: list[dict] = field(default_factory=list)
    header: list[str] = field(default_factory=list)
    null_tokens_seen: set = field(default_factory=set)


# ---------------------------------------------------------------------------
# 类型解析
# ---------------------------------------------------------------------------

def _coerce(raw: Optional[str], spec: FeatureSpec) -> tuple[Any, str]:
    """按特征 dtype 把原始字符串转成 Python 值，返回 (值, 实际类型名)。

    空串/None 统一为 (None, 'null')。
    与声明类型不符的值**不抛异常**：保留原始字符串并标记实际类型，
    交由比对引擎归类为“类型差异”（否则类型差异永远无法进入比对结果）。
    """
    if raw is None:
        return None, "null"
    s = raw.strip()
    if s == "":
        return None, "null"

    dtype = spec.dtype
    if dtype == T_AUTO:
        return _coerce_auto(s)
    if dtype == T_NUMBER:
        try:
            return _to_number(s)
        except LoadError:
            return s, "string"      # 数值列里出现非数值 -> 保留原值，判类型差异
    if dtype == T_BOOL:
        try:
            return _to_bool(s)
        except LoadError:
            return s, "string"      # 布尔列里出现 "VIP" 等 -> 判类型差异
    if dtype == T_STRING:
        return s, "string"
    raise LoadError(f"特征 {spec.name!r} 配置了未知类型 {dtype!r}")


def _coerce_auto(s: str) -> tuple[Any, str]:
    low = s.lower()
    if low in _TRUE_TOKENS:
        return True, "bool"
    if low in _FALSE_TOKENS:
        return False, "bool"
    try:
        return _to_number(s)
    except LoadError:
        pass
    return s, "string"


def _to_number(s: str) -> tuple[float, str]:
    try:
        # 允许 1,000.5 这类千分位
        v = float(s.replace(",", ""))
    except ValueError as exc:
        raise LoadError(f"值 {s!r} 无法解析为数值") from exc
    return v, "number"


def _to_bool(s: str) -> tuple[bool, str]:
    low = s.lower()
    if low in _TRUE_TOKENS:
        return True, "bool"
    if low in _FALSE_TOKENS:
        return False, "bool"
    raise LoadError(f"值 {s!r} 无法解析为布尔值")


# ---------------------------------------------------------------------------
# 重复主键聚合
# ---------------------------------------------------------------------------

def _aggregate(rows: list[Row], feature: str, strategy: str) -> tuple[Any, str, str]:
    """对同一主键组内某特征的多个值按策略聚合，返回 (值, 类型, 原始表示)。"""
    vals = [r.values.get(feature) for r in rows]
    non_null = [v for v in vals if v is not None]
    raws = [r.raws.get(feature) for r in rows if r.raws.get(feature) not in (None, "")]

    if strategy == DUP_FIRST:
        chosen = rows[0]
        return chosen.values.get(feature), chosen.types.get(feature, "null"), chosen.raws.get(feature)
    if strategy == DUP_LAST:
        chosen = rows[-1]
        return chosen.values.get(feature), chosen.types.get(feature, "null"), chosen.raws.get(feature)

    if not non_null:
        return None, "null", raws[0] if raws else None

    if strategy in (DUP_MIN, DUP_MAX, DUP_MEAN, DUP_MEDIAN):
        nums = []
        for v in non_null:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise LoadError(
                    f"重复主键聚合策略 {strategy!r} 仅支持数值，"
                    f"特征 {feature!r} 出现非数值 {v!r}"
                )
            nums.append(float(v))
        if strategy == DUP_MIN:
            v = min(nums)
        elif strategy == DUP_MAX:
            v = max(nums)
        elif strategy == DUP_MEAN:
            v = statistics.fmean(nums)
        else:
            v = statistics.median(nums)
        return v, "number", " | ".join(str(x) for x in raws)

    if strategy == DUP_MAJORITY:
        counts: dict[Any, int] = {}
        for v in non_null:
            counts[v] = counts.get(v, 0) + 1
        top = max(counts.items(), key=lambda kv: kv[1])
        # 平票时取先出现者，保证确定性
        winners = [v for v, c in counts.items() if c == top[1]]
        chosen_val = next(v for v in non_null if v in winners)
        chosen_row = next(r for r in rows if r.values.get(feature) == chosen_val)
        return chosen_val, chosen_row.types.get(feature, "null"), chosen_row.raws.get(feature)

    raise LoadError(f"未知重复处理策略 {strategy!r}")


# ---------------------------------------------------------------------------
# 主加载流程
# ---------------------------------------------------------------------------

def load_csv(
    path: str,
    config: CheckConfig,
    label: str,
    expected_tz_offset_minutes: Optional[int] = None,
) -> LoadedTable:
    """加载并规整一份 CSV（离线或线上）。"""
    entity_col = config.entity_column
    time_col = config.event_time_column
    fmap = config.feature_map()

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            header = reader.fieldnames or []
            raw_records = list(reader)
    except FileNotFoundError as exc:
        raise LoadError(f"文件不存在: {path}") from exc
    except OSError as exc:
        raise LoadError(f"无法读取文件 {path}: {exc}") from exc

    missing = [c for c in [entity_col, time_col] if c not in header]
    if missing:
        raise LoadError(f"{label} 数据缺少必需列: {missing}；实际列为 {header}")
    missing_feats = [f.name for f in config.features if f.name not in header]
    if missing_feats:
        raise LoadError(f"{label} 数据缺少特征列: {missing_feats}")

    # --- 时区探测（先于解析，拿到假定偏移） ---
    tz_info = detect_timezone(
        [r.get(time_col, "") for r in raw_records],
        expected_offset_minutes=expected_tz_offset_minutes,
    )

    rows: list[Row] = []
    null_seen: set[str] = set()
    for line_no, rec in enumerate(raw_records, start=2):
        entity = (rec.get(entity_col) or "").strip()
        if not entity:
            raise LoadError(f"{path} 第 {line_no} 行实体列为空")
        time_raw = rec.get(time_col, "")
        try:
            dt, _ = parse_timestamp(time_raw, tz_info.assumed_tz_offset_minutes)
        except ValueError as exc:
            raise LoadError(f"{path} 第 {line_no} 行: {exc}") from exc

        values, raws, types = {}, {}, {}
        for spec in config.features:
            raw = rec.get(spec.name)
            if raw is not None and raw.strip() in {"", "null", "NULL", "None", "NaN", "nan", "NA"}:
                if raw.strip() not in {"", "null"}:
                    null_seen.add(raw.strip())
                raw = None
            try:
                val, typ = _coerce(raw, spec)
            except LoadError as exc:
                raise LoadError(f"{path} 第 {line_no} 行特征 {spec.name!r}: {exc}") from exc
            values[spec.name] = val
            raws[spec.name] = raw
            types[spec.name] = typ
        rows.append(Row(entity, dt, time_raw, values, raws, types))

    table = LoadedTable(
        label=label, path=path, rows=rows, timezone=tz_info, header=header,
        null_tokens_seen=null_seen,
    )
    _deduplicate(table, config)
    return table


def _deduplicate(table: LoadedTable, config: CheckConfig) -> None:
    """按 (实体, 事件时间) 分组，依特征定义处理重复。"""
    groups: dict[tuple[str, datetime], list[Row]] = {}
    for row in table.rows:
        groups.setdefault((row.entity_id, row.event_dt), []).append(row)

    deduped: list[Row] = []
    dup_info: list[dict] = []
    for (entity, dt), grp in groups.items():
        if len(grp) == 1:
            deduped.append(grp[0])
            continue

        key = f"{entity} @ {to_iso(dt)}"
        # 任一特征要求 fail 即整体中止——不能悄悄丢数据
        failing = [
            f.name for f in config.features
            if config.effective_dup_strategy(f) == DUP_FAIL
        ]
        if failing:
            raise LoadError(
                f"{table.label} 数据存在重复主键 {key}（{len(grp)} 行），"
                f"特征 {failing} 的重复策略为 fail。"
                "请在配置中显式指定 first/last/min/max/mean/median/majority 后重试；"
                "系统不会默认取最后一行。"
            )

        merged_values, merged_raws, merged_types = {}, {}, {}
        per_feature_strategy = {}
        for spec in config.features:
            strat = config.effective_dup_strategy(spec)
            per_feature_strategy[spec.name] = strat
            try:
                v, t, raw = _aggregate(grp, spec.name, strat)
            except LoadError as exc:
                raise LoadError(f"{table.label} 重复主键 {key} 特征 {spec.name!r}: {exc}") from exc
            merged_values[spec.name] = v
            merged_types[spec.name] = t
            merged_raws[spec.name] = raw

        dup_info.append({
            "entity_id": entity,
            "event_time": to_iso(dt),
            "row_count": len(grp),
            "strategies": per_feature_strategy,
        })
        first = grp[0]
        deduped.append(Row(
            entity_id=entity, event_dt=dt, event_raw=first.event_raw,
            values=merged_values, raws=merged_raws, types=merged_types,
        ))

    deduped.sort(key=lambda r: (r.entity_id, r.event_dt))
    table.rows = deduped
    table.duplicate_groups = dup_info
