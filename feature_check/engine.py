"""比对引擎。

关键语义
========
1. **点时刻取值（as-of join）**：对线上快照里的每个 (实体, 事件时间 t)，
   离线特征只允许取 ``event_time <= t`` 的**最新一行**（forward fill / 向过去看）。
2. **防未来数据泄漏是硬约束**：``event_time > t`` 的离线行永远不会被选中；
   若不存在任何 ``<= t`` 的离线行，结论为整行缺失（missing_in_offline），
   而不是取未来值。
3. **时间错位**：as-of 命中行的事件时间与 t 的差超过 ``time_tolerance_seconds``
   时标记 time_skew（值仍照常比较，可与值漂移并存）。
4. 空值差异、类型差异、值漂移、整行缺失分别归类；数值容差与小数精度分开记录。
"""
from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from .loader import LoadedTable, Row
from .model import (
    CellResult, CheckConfig, FeatureSpec, RunSummary,
    DIFF_MATCH, DIFF_MISSING_ROW, DIFF_NULL_MISMATCH, DIFF_TYPE_MISMATCH,
    DIFF_VALUE_DRIFT, DIFF_TIME_SKEW,
    MISSING_IN_OFFLINE, MISSING_IN_ONLINE,
    NULL_STRICT, NULL_LENIENT, NULL_AS_ZERO,
    T_NUMBER, T_BOOL, T_STRING,
)
from .timeutil import to_iso, parse_iso


@dataclass
class AsOfHit:
    row: Row
    lag_seconds: float          # t - 离线行事件时间（恒 >= 0）


class EngineError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------

def build_asof_index(table: LoadedTable) -> dict[str, list]:
    """实体 -> 按事件时间升序排列的 Row 列表（加载阶段已排序、已去重）。"""
    index: dict[str, list[Row]] = {}
    for row in table.rows:
        index.setdefault(row.entity_id, []).append(row)
    for rows in index.values():
        rows.sort(key=lambda r: r.event_dt)
    return index


def asof_lookup(
    index: dict[str, list[Row]], entity: str, event_dt
) -> Optional[AsOfHit]:
    """点时刻取值：只向过去看。严格不含未来行（bisect_right 保证）。"""
    rows = index.get(entity)
    if not rows:
        return None
    times = [r.event_dt for r in rows]
    # bisect_right => 插入点右侧，所有下标 < pos 的行均满足 event_time <= t
    pos = bisect.bisect_right(times, event_dt)
    if pos == 0:
        # 该实体所有离线行都在 t 之后 -> 未来数据，拒绝使用
        return None
    row = rows[pos - 1]
    return AsOfHit(row=row, lag_seconds=(event_dt - row.event_dt).total_seconds())


# ---------------------------------------------------------------------------
# 规则指纹
# ---------------------------------------------------------------------------

def fingerprint(config: CheckConfig) -> str:
    return hashlib.sha256(
        json.dumps(config.rule_fingerprint(), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _cell_rule_snapshot(config: CheckConfig, f: FeatureSpec) -> dict:
    return {
        "feature": f.name,
        "dtype": f.dtype,
        "abs_tolerance": config.effective_abs_tol(f),
        "rel_tolerance": config.effective_rel_tol(f),
        "decimal_precision": config.effective_precision(f),
        "null_policy": config.effective_null_policy(f),
        "duplicate_strategy": config.effective_dup_strategy(f),
        "time_tolerance_seconds": config.time_tolerance_seconds,
        "asof_rule": "离线按事件时间向前取值，仅取 event_time <= 目标时间 的最新行；禁止未来数据",
    }


# ---------------------------------------------------------------------------
# 单特征比较
# ---------------------------------------------------------------------------

def _round(v: float, precision: Optional[int]) -> float:
    if precision is None:
        return v
    return round(v, precision)


def compare_feature(
    config: CheckConfig, spec: FeatureSpec,
    offline_row: Optional[Row], online_row: Row,
    lag_seconds: Optional[float],
) -> CellResult:
    """对单个特征产出单元格结论。offline_row 为 None 表示无 as-of 命中。"""
    base = CellResult(
        entity_id=online_row.entity_id,
        event_time=to_iso(online_row.event_dt),
        feature=spec.name,
        category=DIFF_MATCH,
        rule=_cell_rule_snapshot(config, spec),
        rule_fingerprint=fingerprint(config),
    )
    base.online_raw = online_row.raws.get(spec.name)
    base.online_value = online_row.values.get(spec.name)
    base.online_type = online_row.types.get(spec.name)
    base.online_source_time = to_iso(online_row.event_dt)

    # 时间错位（独立判定，可与值类差异并存）
    skew = (
        lag_seconds is not None
        and config.time_tolerance_seconds > 0
        and lag_seconds > config.time_tolerance_seconds
    )

    if offline_row is None:
        base.category = DIFF_MISSING_ROW
        base.detail = (
            "目标时间之前不存在该实体的离线记录（未来数据已被阻止，不会向后取值）"
        )
        return base

    base.offline_raw = offline_row.raws.get(spec.name)
    base.offline_value = offline_row.values.get(spec.name)
    base.offline_type = offline_row.types.get(spec.name)
    base.offline_source_time = to_iso(offline_row.event_dt)

    off_v, on_v = base.offline_value, base.online_value
    off_null, on_null = off_v is None, on_v is None
    policy = config.effective_null_policy(spec)

    # --- 空值处理 ---
    if off_null or on_null:
        if off_null and on_null:
            if policy == NULL_LENIENT:
                base.category = DIFF_MATCH
                base.detail = "两侧均为空，按 lenient 空值语义视为一致"
            elif policy == NULL_AS_ZERO:
                return _numeric_compare(config, spec, 0.0, 0.0, base, skew,
                                        note="两侧均为空，按 as_zero 语义按 0 比较")
            else:
                base.category = DIFF_NULL_MISMATCH
                base.detail = "两侧均为空，strict 语义下空值不自动视为相等"
            return base
        if policy == NULL_LENIENT:
            base.category = DIFF_NULL_MISMATCH
            base.detail = f"一侧为空（离线空={off_null}，线上空={on_null}）"
            return base
        if policy == NULL_AS_ZERO:
            off_num = 0.0 if off_null else off_v
            on_num = 0.0 if on_null else on_v
            # 非数值类型无法按 0 做数值比较 -> 类型差异
            if not isinstance(off_num, (int, float)) or isinstance(off_num, bool) \
               or not isinstance(on_num, (int, float)) or isinstance(on_num, bool):
                base.category = DIFF_TYPE_MISMATCH
                base.detail = (
                    f"as_zero 空值语义要求数值比较，但实际类型为 "
                    f"离线 {base.offline_type} / 线上 {base.online_type}"
                )
                return base
            return _numeric_compare(
                config, spec, float(off_num), float(on_num), base, skew,
                note=f"空值按 0 参与比较（离线空={off_null}，线上空={on_null}）",
            )
        base.category = DIFF_NULL_MISMATCH
        base.detail = f"空值差异：离线={base.offline_raw!r}，线上={base.online_raw!r}"
        return base

    # --- 类型差异 ---
    if base.offline_type != base.online_type:
        base.category = DIFF_TYPE_MISMATCH
        base.detail = (
            f"类型差异：离线 {base.offline_type}({base.offline_raw!r}) vs "
            f"线上 {base.online_type}({base.online_raw!r})"
        )
        if skew:
            base.secondary_flags.append(DIFF_TIME_SKEW)
            base.detail += _skew_suffix(lag_seconds, config)
        return base

    # --- 同类型值比较 ---
    if base.offline_type == "number":
        return _numeric_compare(config, spec, float(off_v), float(on_v), base, skew)
    if base.offline_type == "bool":
        if bool(off_v) == bool(on_v):
            base.category = DIFF_MATCH if not skew else DIFF_TIME_SKEW
            if skew:
                base.detail = "值一致，但" + _skew_suffix(lag_seconds, config)
        else:
            base.category = DIFF_VALUE_DRIFT
            base.detail = f"布尔值不一致：离线 {off_v} vs 线上 {on_v}"
            if skew:
                base.secondary_flags.append(DIFF_TIME_SKEW)
                base.detail += _skew_suffix(lag_seconds, config)
        return base
    # 字符串及其它
    if str(off_v) == str(on_v):
        base.category = DIFF_MATCH if not skew else DIFF_TIME_SKEW
        if skew:
            base.detail = "值一致，但" + _skew_suffix(lag_seconds, config)
    else:
        base.category = DIFF_VALUE_DRIFT
        base.detail = f"字符串不一致：离线 {off_v!r} vs 线上 {on_v!r}"
        if skew:
            base.secondary_flags.append(DIFF_TIME_SKEW)
            base.detail += _skew_suffix(lag_seconds, config)
    return base


def _numeric_compare(
    config: CheckConfig, spec: FeatureSpec,
    off_v: float, on_v: float, base: CellResult,
    skew: bool, note: str = "",
) -> CellResult:
    precision = config.effective_precision(spec)
    off_r = _round(off_v, precision)
    on_r = _round(on_v, precision)
    abs_tol = config.effective_abs_tol(spec)
    rel_tol = config.effective_rel_tol(spec)

    delta = abs(off_r - on_r)
    base.numeric_delta = delta
    allowed_abs = abs_tol
    allowed_rel = rel_tol * max(abs(off_r), abs(on_r))
    base.within_tolerance = delta <= max(allowed_abs, allowed_rel)

    bits = []
    if precision is not None:
        bits.append(f"按 {precision} 位小数四舍五入：离线 {off_r} vs 线上 {on_r}")
    else:
        bits.append(f"数值：离线 {off_r} vs 线上 {on_r}")
    bits.append(f"绝对差={delta:g}，容差 max(绝对 {allowed_abs:g}, 相对 {allowed_rel:g})")
    if note:
        bits.insert(0, note)

    if base.within_tolerance:
        base.category = DIFF_MATCH if not skew else DIFF_TIME_SKEW
        bits.append("在容差内，视为一致")
    else:
        base.category = DIFF_VALUE_DRIFT
        bits.append("超出容差，判定为值漂移")
        if skew:
            base.secondary_flags.append(DIFF_TIME_SKEW)
    if skew:
        bits.append(_skew_suffix(None, config))
    base.detail = "；".join(bits)
    return base


def _skew_suffix(lag_seconds, config: CheckConfig) -> str:
    lag_txt = "" if lag_seconds is None else f"，滞后 {lag_seconds:g} 秒"
    return (
        f"as-of 命中行与目标事件时间错位（容忍 {config.time_tolerance_seconds:g} 秒{lag_txt}）"
    )


# ---------------------------------------------------------------------------
# 整行比对
# ---------------------------------------------------------------------------

def _in_range(row: Row, start, end) -> bool:
    if start is not None and row.event_dt < start:
        return False
    if end is not None and row.event_dt > end:
        return False
    return True


def run_comparison(
    config: CheckConfig,
    offline: LoadedTable,
    online: LoadedTable,
) -> list[CellResult]:
    """执行完整比对，返回单元格结果列表（含缺失行与两侧独有实体）。"""
    start = parse_iso(config.time_range_start) if config.time_range_start else None
    end = parse_iso(config.time_range_end) if config.time_range_end else None

    off_index = build_asof_index(offline)
    on_index = build_asof_index(online)
    fp = fingerprint(config)
    results: list[CellResult] = []

    # 1) 以线上快照为主表，逐行做 as-of 取值
    for on_row in online.rows:
        if not _in_range(on_row, start, end):
            continue
        hit = asof_lookup(off_index, on_row.entity_id, on_row.event_dt)
        for spec in config.features:
            cell = compare_feature(
                config, spec,
                offline_row=hit.row if hit else None,
                online_row=on_row,
                lag_seconds=hit.lag_seconds if hit else None,
            )
            cell.rule_fingerprint = fp
            results.append(cell)

    # 2) 离线侧独有主键：线上整行缺失（同样受时间范围约束）
    online_keys = {(r.entity_id, r.event_dt) for r in online.rows}
    for entity, rows in off_index.items():
        for off_row in rows:
            if not _in_range(off_row, start, end):
                continue
            if (entity, off_row.event_dt) in online_keys:
                continue
            for spec in config.features:
                cell = CellResult(
                    entity_id=entity,
                    event_time=to_iso(off_row.event_dt),
                    feature=spec.name,
                    category=DIFF_MISSING_ROW,
                    offline_raw=off_row.raws.get(spec.name),
                    offline_value=off_row.values.get(spec.name),
                    offline_type=off_row.types.get(spec.name),
                    offline_source_time=to_iso(off_row.event_dt),
                    online_raw=None, online_value=None, online_type=None,
                    online_source_time=None,
                    detail="线上快照缺少该主键行（missing_in_online）",
                    rule=_cell_rule_snapshot(config, spec),
                    rule_fingerprint=fp,
                    missing_side=MISSING_IN_ONLINE,
                )
                results.append(cell)

    # 标注线上有/离线无的方向
    for cell in results:
        if cell.category == DIFF_MISSING_ROW and cell.missing_side is None:
            cell.missing_side = MISSING_IN_OFFLINE

    results.sort(key=lambda c: (c.entity_id, c.event_time, c.feature))
    return results


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def summarize(results: list[CellResult]) -> RunSummary:
    s = RunSummary()
    s.total_cells = len(results)
    entities = set()
    for c in results:
        entities.add(c.entity_id)
        # 主分类计数
        getattr_safe = {
            DIFF_MATCH: "match",
            DIFF_MISSING_ROW: "missing_row",
            DIFF_NULL_MISMATCH: "null_mismatch",
            DIFF_TYPE_MISMATCH: "type_mismatch",
            DIFF_VALUE_DRIFT: "value_drift",
            DIFF_TIME_SKEW: "time_skew",
        }
        attr = getattr_safe[c.category]
        setattr(s, attr, getattr(s, attr) + 1)
        # 并存的时间错位也计入 time_skew，并在分特征桶里体现
        if DIFF_TIME_SKEW in c.secondary_flags:
            s.time_skew += 1
        bucket = s.by_feature.setdefault(c.feature, {})
        bucket[c.category] = bucket.get(c.category, 0) + 1
        for flag in c.secondary_flags:
            bucket[flag] = bucket.get(flag, 0) + 1
    s.entities = len(entities)
    return s
