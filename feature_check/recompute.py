"""比对流水线与增量重算。

重算规则（满足用户“只重算受影响的特征与时间范围、保留上次结果”的要求）：

1. 数据未变、仅规则变化时，逐特征计算规则指纹；指纹未变的特征，
   且单元格不在新的重算时间范围内，则直接复用上一次运行的结果。
2. 指纹变化的特征、或落在重算时间范围内的单元格重新计算。
3. 每次重算都生成**新的不可变运行记录**，并记录父运行、复用/重算单元格数；
   通过 ``diff_runs`` 可点回具体实体与值的变化。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .engine import run_comparison, summarize, fingerprint
from .loader import load_csv, LoadedTable
from .model import CheckConfig, CellResult
from .store import RunRecord, RunStore
from .timeutil import compare_timezone, parse_iso, to_iso


@dataclass
class PipelineResult:
    record: RunRecord
    blocking_warnings: list


def feature_rule_hash(config: CheckConfig, feature_name: str) -> str:
    """单个特征实际生效的规则指纹。"""
    spec = config.feature_map()[feature_name]
    payload = {
        "dtype": spec.dtype,
        "abs_tolerance": config.effective_abs_tol(spec),
        "rel_tolerance": config.effective_rel_tol(spec),
        "decimal_precision": config.effective_precision(spec),
        "null_policy": config.effective_null_policy(spec),
        "duplicate_strategy": config.effective_dup_strategy(spec),
        "time_tolerance_seconds": config.time_tolerance_seconds,
        "timezone": config.timezone,
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _expected_tz_offset(config: CheckConfig):
    if not config.timezone:
        return None
    # 支持固定偏移写法 "+08:00"
    tz = config.timezone.strip()
    if tz.startswith(("+", "-")) and ":" in tz:
        sign = 1 if tz[0] == "+" else -1
        hh, mm = tz[1:].split(":")
        return sign * (int(hh) * 60 + int(mm))
    # 时区名（如 Asia/Shanghai）：用 zoneinfo，缺失时回退 UTC 并告警
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        offset = datetime.now(ZoneInfo(tz)).utcoffset()
        return int(offset.total_seconds() // 60)
    except Exception:
        return None


def execute_pipeline(
    config: CheckConfig,
    offline_path: str,
    online_path: str,
    store: RunStore,
    parent_run_id: str | None = None,
    recompute_note: str = "",
) -> PipelineResult:
    """加载数据 -> 时区检查 -> 去重 -> 比对 -> 落盘。"""
    tz_offset = _expected_tz_offset(config)
    offline = load_csv(offline_path, config, "offline", tz_offset)
    online = load_csv(online_path, config, "online", tz_offset)

    tz_warnings = compare_timezone(offline.timezone, online.timezone)
    blocking = list(tz_warnings)
    if tz_warnings and not config.accept_timezone_warning:
        # 时区不一致：先提示，不产出比对结果
        blocking.append("已阻止运行：请确认时区处理方式，并在配置中勾选 accept_timezone_warning 后重试。")
        return PipelineResult(record=None, blocking_warnings=blocking)

    results = run_comparison(config, offline, online)
    summary = summarize(results)

    record = RunRecord(
        run_id=store.new_run_id(),
        created_at=to_iso(datetime.now(timezone.utc)),
        config=config,
        summary=summary,
        results=results,
        rule_fingerprint=fingerprint(config),
        timezone_warnings=tz_warnings,
        duplicate_info={
            "offline": offline.duplicate_groups,
            "online": online.duplicate_groups,
        },
        source_files={"offline": offline_path, "online": online_path},
        parent_run_id=parent_run_id,
        recompute_note=recompute_note,
    )
    store.save_run(record)
    return PipelineResult(record=record, blocking_warnings=blocking)


def execute_incremental(
    config: CheckConfig,
    offline_path: str,
    online_path: str,
    store: RunStore,
    parent_run_id: str,
) -> PipelineResult:
    """增量重算：未受影响的单元格直接复用父运行结果。"""
    parent = store.load_run(parent_run_id)

    # 1) 先完整跑一遍新规则（数据需重新加载，去重策略可能已变）
    tz_offset = _expected_tz_offset(config)
    offline = load_csv(offline_path, config, "offline", tz_offset)
    online = load_csv(online_path, config, "online", tz_offset)
    tz_warnings = compare_timezone(offline.timezone, online.timezone)
    if tz_warnings and not config.accept_timezone_warning:
        return PipelineResult(
            record=None,
            blocking_warnings=tz_warnings + [
                "已阻止运行：时区不一致，请先确认并勾选 accept_timezone_warning。"
            ],
        )

    fresh = run_comparison(config, offline, online)

    # 2) 计算每个特征的新旧规则指纹
    new_hashes = {f.name: feature_rule_hash(config, f.name) for f in config.features}
    old_hashes = {}
    for c in parent.results:
        old_hashes[c.feature] = c.rule_fingerprint
    # 父运行的 rule_fingerprint 是整配置哈希；特征级需要从父配置重算
    old_hashes = {
        f.name: feature_rule_hash(parent.config, f.name) for f in parent.config.features
    }

    range_start = parse_iso(config.time_range_start) if config.time_range_start else None
    range_end = parse_iso(config.time_range_end) if config.time_range_end else None

    def in_recompute_range(cell: CellResult) -> bool:
        if range_start is None and range_end is None:
            return False
        dt = parse_iso(cell.event_time)
        if range_start is not None and dt < range_start:
            return False
        if range_end is not None and dt > range_end:
            return False
        return True

    parent_index = {
        (c.entity_id, c.event_time, c.feature): c for c in parent.results
    }

    merged: list[CellResult] = []
    reused, recomputed = 0, 0
    affected_features = {
        name for name, h in new_hashes.items() if old_hashes.get(name) != h
    }
    for cell in fresh:
        key = (cell.entity_id, cell.event_time, cell.feature)
        old = parent_index.get(key)
        rule_unchanged = new_hashes[cell.feature] == old_hashes.get(cell.feature)
        if (
            old is not None
            and rule_unchanged
            and not in_recompute_range(cell)
        ):
            merged.append(old)          # 完整保留上次结果
            reused += 1
        else:
            merged.append(cell)         # 受影响特征或时间范围内 -> 重算
            recomputed += 1

    summary = summarize(merged)
    note = (
        f"增量重算：受影响特征 {sorted(affected_features) or '无'}；"
        f"时间范围 {config.time_range_start or '-'} ~ {config.time_range_end or '-'}；"
        f"重算 {recomputed} 格，复用 {reused} 格。"
    )
    record = RunRecord(
        run_id=store.new_run_id(),
        created_at=to_iso(datetime.now(timezone.utc)),
        config=config,
        summary=summary,
        results=merged,
        rule_fingerprint=fingerprint(config),
        timezone_warnings=tz_warnings,
        duplicate_info={
            "offline": offline.duplicate_groups,
            "online": online.duplicate_groups,
        },
        source_files={"offline": offline_path, "online": online_path},
        parent_run_id=parent_run_id,
        recompute_note=note,
        reused_cells=reused,
        recomputed_cells=recomputed,
    )
    store.save_run(record)
    return PipelineResult(record=record, blocking_warnings=tz_warnings)


def diff_runs(old: RunRecord, new: RunRecord) -> list[dict]:
    """对比两次运行，返回发生变化的单元格（可点回具体实体和值变化）。"""
    old_index = {(c.entity_id, c.event_time, c.feature): c for c in old.results}
    changes = []
    for c in new.results:
        key = (c.entity_id, c.event_time, c.feature)
        prev = old_index.get(key)
        if prev is None:
            changes.append({
                "entity_id": c.entity_id, "event_time": c.event_time,
                "feature": c.feature, "change_type": "new_cell",
                "old": None, "new": c.to_dict(),
            })
        elif (prev.category, prev.offline_raw, prev.online_raw,
              prev.numeric_delta, prev.within_tolerance) != (
            c.category, c.offline_raw, c.online_raw,
            c.numeric_delta, c.within_tolerance
        ):
            changes.append({
                "entity_id": c.entity_id, "event_time": c.event_time,
                "feature": c.feature,
                "change_type": "changed",
                "old": prev.to_dict(), "new": c.to_dict(),
            })
    new_keys = {(c.entity_id, c.event_time, c.feature) for c in new.results}
    for key, prev in old_index.items():
        if key not in new_keys:
            changes.append({
                "entity_id": key[0], "event_time": key[1], "feature": key[2],
                "change_type": "removed", "old": prev.to_dict(), "new": None,
            })
    changes.sort(key=lambda x: (x["entity_id"], x["event_time"], x["feature"]))
    return changes
