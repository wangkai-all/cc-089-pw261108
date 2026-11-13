"""时间解析与时区探测。

设计原则：
- 所有时间内部统一为带时区的 ``datetime``（UTC 存储，ISO8601 展示）。
- 不带时区的时间戳按“数据中推断出的默认时区”解释，并明确记录该假设。
- 两个数据源时区不一致时返回告警，由上层决定是否阻断（用户确认后才可继续）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

# 常见的“朴素时间”显式格式（无 UTC 偏移）
_NAIVE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
)

# 形如 2026-11-13T08:00:00+08:00 / ...Z / ... +0800
_OFFSET_RE = re.compile(
    r"(Z|[+-]\d{2}:?\d{2})$"
)


@dataclass
class TimezoneInfo:
    """一份数据的时区探测结果。"""
    has_tz_aware: bool = False          # 是否存在带时区偏移的时间戳
    has_naive: bool = False             # 是否存在不带时区的时间戳
    observed_offsets: set = None        # 观察到的 UTC 偏移（分钟）
    assumed_tz_offset_minutes: int = 0  # 朴素时间假定的偏移
    warnings: list = None

    def __post_init__(self):
        if self.observed_offsets is None:
            self.observed_offsets = set()
        if self.warnings is None:
            self.warnings = []

    def to_dict(self) -> dict:
        return {
            "has_tz_aware": self.has_tz_aware,
            "has_naive": self.has_naive,
            "observed_offsets": sorted(self.observed_offsets),
            "assumed_tz_offset_minutes": self.assumed_tz_offset_minutes,
            "warnings": self.warnings,
        }


def parse_timestamp(raw: str, assumed_offset_minutes: int = 0) -> tuple[datetime, bool]:
    """解析单个时间戳。

    返回 ``(带时区 datetime, 是否原本带时区)``。
    朴素时间按 ``assumed_offset_minutes`` 解释。
    """
    s = raw.strip()
    if not s:
        raise ValueError("空时间戳")

    m = _OFFSET_RE.search(s)
    aware = m is not None
    if aware:
        iso = s.replace("Z", "+00:00")
        # 兼容 +0800 无冒号写法
        iso = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", iso)
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:  # pragma: no cover - 正则已保证
            raise ValueError(f"无法解析时区偏移: {raw}")
        return dt.astimezone(timezone.utc), True

    # 朴素时间
    dt = None
    for fmt in _NAIVE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        # 最后尝试 fromisoformat（兼容更多本地写法）
        try:
            dt = datetime.fromisoformat(s)
        except ValueError as exc:
            raise ValueError(f"无法解析时间戳: {raw!r}") from exc
    if dt.tzinfo is not None:  # pragma: no cover
        return dt.astimezone(timezone.utc), True
    tz = timezone(timedelta(minutes=assumed_offset_minutes))
    return dt.replace(tzinfo=tz).astimezone(timezone.utc), False


def detect_timezone(values: list[str], expected_offset_minutes: int | None = None) -> TimezoneInfo:
    """扫描一批时间戳，探测时区情况。"""
    info = TimezoneInfo()
    for raw in values:
        if raw is None or not str(raw).strip():
            continue
        s = str(raw)
        m = _OFFSET_RE.search(s.strip())
        if m:
            info.has_tz_aware = True
            token = m.group(1)
            if token == "Z":
                info.observed_offsets.add(0)
            else:
                sign = 1 if token[0] == "+" else -1
                token = token[1:].replace(":", "")
                off = sign * (int(token[:2]) * 60 + int(token[2:4]))
                info.observed_offsets.add(off)
        else:
            info.has_naive = True

    # 假定偏移：用户配置优先；否则取观察到的唯一偏移；再否则 UTC
    if expected_offset_minutes is not None:
        info.assumed_tz_offset_minutes = expected_offset_minutes
    elif len(info.observed_offsets) == 1:
        info.assumed_tz_offset_minutes = next(iter(info.observed_offsets))
    else:
        info.assumed_tz_offset_minutes = 0
        if info.has_naive and not info.observed_offsets:
            info.warnings.append(
                "时间戳均无时区信息且未配置时区，已按 UTC(+00:00) 解释，请确认是否正确。"
            )

    if info.has_naive and info.observed_offsets and (
        expected_offset_minutes is None
        or expected_offset_minutes not in info.observed_offsets
    ):
        info.warnings.append(
            "同一数据中混用带时区与不带时区的时间戳，朴素时间按假定偏移 "
            f"{info.assumed_tz_offset_minutes} 分钟解释。"
        )
    return info


def compare_timezone(offline: TimezoneInfo, online: TimezoneInfo) -> list[str]:
    """比较两份数据的时区，返回阻塞/提示信息列表。"""
    warnings: list[str] = []
    off_offsets = offline.observed_offsets or {offline.assumed_tz_offset_minutes}
    on_offsets = online.observed_offsets or {online.assumed_tz_offset_minutes}
    if off_offsets != on_offsets or (
        offline.assumed_tz_offset_minutes != online.assumed_tz_offset_minutes
    ):
        warnings.append(
            "时区不一致：离线数据偏移(分钟)="
            f"{sorted(off_offsets)}，线上快照偏移(分钟)={sorted(on_offsets)}。"
            "请先统一时区或确认解释规则后再运行比对。"
        )
    warnings.extend(offline.warnings)
    warnings.extend(online.warnings)
    return warnings


def to_iso(dt: datetime) -> str:
    """规范 ISO8601 UTC 字符串。"""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(s: str) -> datetime:
    """解析 to_iso 输出或任意 ISO8601。"""
    dt, _ = parse_timestamp(s.replace("Z", "+00:00"))
    return dt
