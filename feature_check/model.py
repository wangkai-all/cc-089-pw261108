"""核心数据模型：特征定义、规则配置、比对结果。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# ---------------------------------------------------------------------------
# 枚举型常量
# ---------------------------------------------------------------------------

# 重复实体主键的处理策略（绝不默认取最后一行）
DUP_FAIL = "fail"            # 发现重复即报错中止（默认）
DUP_FIRST = "first"          # 保留首行
DUP_LAST = "last"            # 显式选择保留末行
DUP_MIN = "min"              # 数值取最小
DUP_MAX = "max"              # 数值取最大
DUP_MEAN = "mean"            # 数值取均值
DUP_MEDIAN = "median"        # 数值取中位
DUP_MAJORITY = "majority"    # 众数（含字符串）

DUP_STRATEGIES = {
    DUP_FAIL, DUP_FIRST, DUP_LAST, DUP_MIN, DUP_MAX,
    DUP_MEAN, DUP_MEDIAN, DUP_MAJORITY,
}

# 空值语义
NULL_STRICT = "strict"       # None 与任何值都不同（默认）
NULL_LENIENT = "lenient"     # 两侧都空视为一致；空 vs 非空仍是差异
NULL_AS_ZERO = "as_zero"     # 空值按数值 0 参与比较

# 特征数据类型
T_NUMBER = "number"
T_STRING = "string"
T_BOOL = "bool"
T_AUTO = "auto"

# 差异分类
DIFF_MISSING_ROW = "missing_row"        # 整行缺失（一侧有、另一侧无）
DIFF_NULL_MISMATCH = "null_mismatch"    # 空值差异
DIFF_TYPE_MISMATCH = "type_mismatch"    # 类型差异
DIFF_VALUE_DRIFT = "value_drift"        # 值漂移（数值超容差/精度，或字符串不等）
DIFF_TIME_SKEW = "time_skew"            # 时间错位（as-of 命中行事件时间超容忍度）
DIFF_MATCH = "match"                    # 一致

DIFF_LABELS = {
    DIFF_MISSING_ROW: "整行缺失",
    DIFF_NULL_MISMATCH: "空值差异",
    DIFF_TYPE_MISMATCH: "类型差异",
    DIFF_VALUE_DRIFT: "值漂移",
    DIFF_TIME_SKEW: "时间错位",
    DIFF_MATCH: "一致",
}

# 缺失行方向
MISSING_IN_OFFLINE = "missing_in_offline"   # 线上有、离线无
MISSING_IN_ONLINE = "missing_in_online"     # 离线有、线上无


# ---------------------------------------------------------------------------
# 配置模型
# ---------------------------------------------------------------------------

@dataclass
class FeatureSpec:
    """单个特征的定义。"""
    name: str
    dtype: str = T_AUTO                 # number | string | bool | auto
    abs_tolerance: Optional[float] = None
    rel_tolerance: Optional[float] = None
    decimal_precision: Optional[int] = None   # 小数位精度，先四舍五入再比较
    null_policy: Optional[str] = None         # 覆盖全局空值语义
    duplicate_strategy: Optional[str] = None  # 覆盖全局重复处理
    nullable: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FeatureSpec":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class CheckConfig:
    """一次比对运行的完整规则。"""
    name: str = "未命名检查"
    entity_column: str = "entity_id"
    event_time_column: str = "event_time"
    features: list[FeatureSpec] = field(default_factory=list)
    # 时间
    time_tolerance_seconds: float = 0.0
    timezone: Optional[str] = None          # 期望时区名（如 Asia/Shanghai），空=按数据推断
    # 全局默认（特征级可覆盖）
    default_abs_tolerance: float = 0.0
    default_rel_tolerance: float = 0.0
    default_decimal_precision: Optional[int] = None
    null_policy: str = NULL_STRICT
    duplicate_strategy: str = DUP_FAIL
    # 重算范围
    time_range_start: Optional[str] = None  # ISO8601，含
    time_range_end: Optional[str] = None    # ISO8601，含
    accept_timezone_warning: bool = False   # 时区不一致时用户显式确认后才置 true

    # --- 便捷派生 ---
    def feature_map(self) -> dict[str, FeatureSpec]:
        return {f.name: f for f in self.features}

    def effective_abs_tol(self, f: FeatureSpec) -> float:
        return self.default_abs_tolerance if f.abs_tolerance is None else f.abs_tolerance

    def effective_rel_tol(self, f: FeatureSpec) -> float:
        return self.default_rel_tolerance if f.rel_tolerance is None else f.rel_tolerance

    def effective_precision(self, f: FeatureSpec) -> Optional[int]:
        return self.default_decimal_precision if f.decimal_precision is None else f.decimal_precision

    def effective_null_policy(self, f: FeatureSpec) -> str:
        return self.null_policy if f.null_policy is None else f.null_policy

    def effective_dup_strategy(self, f: FeatureSpec) -> str:
        return self.duplicate_strategy if f.duplicate_strategy is None else f.duplicate_strategy

    def rule_fingerprint(self) -> dict:
        """参与逐单元格判定的规则指纹（重算时据此决定哪些单元格受影响）。"""
        return {
            "time_tolerance_seconds": self.time_tolerance_seconds,
            "timezone": self.timezone,
            "null_policy": self.null_policy,
            "duplicate_strategy": self.duplicate_strategy,
            "default_abs_tolerance": self.default_abs_tolerance,
            "default_rel_tolerance": self.default_rel_tolerance,
            "default_decimal_precision": self.default_decimal_precision,
            "features": {
                f.name: {
                    "dtype": f.dtype,
                    "abs_tolerance": f.abs_tolerance,
                    "rel_tolerance": f.rel_tolerance,
                    "decimal_precision": f.decimal_precision,
                    "null_policy": f.null_policy,
                    "duplicate_strategy": f.duplicate_strategy,
                }
                for f in self.features
            },
        }

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "CheckConfig":
        d = dict(d)
        d["features"] = [FeatureSpec.from_dict(f) for f in d.get("features", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------

@dataclass
class CellResult:
    """一个 (实体, 事件时间, 特征) 单元格的比对结论。"""
    entity_id: str
    event_time: str                     # 规范 ISO8601 UTC
    feature: str
    category: str                       # DIFF_*
    offline_raw: Any = None             # 原始字符串/None
    online_raw: Any = None
    offline_value: Any = None           # 按 dtype 解析后的值
    online_value: Any = None
    offline_type: Optional[str] = None
    online_type: Optional[str] = None
    offline_source_time: Optional[str] = None   # as-of 实际命中的离线行事件时间
    online_source_time: Optional[str] = None
    detail: str = ""                    # 人类可读说明
    rule: dict = field(default_factory=dict)    # 本单元格生效的计算规则快照
    numeric_delta: Optional[float] = None
    within_tolerance: Optional[bool] = None
    rule_fingerprint: str = ""          # 对应 CheckConfig.rule_fingerprint 的哈希
    missing_side: Optional[str] = None  # missing_in_offline | missing_in_online
    secondary_flags: list = field(default_factory=list)  # 并存的次要分类（如 time_skew）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class RunSummary:
    total_cells: int = 0
    match: int = 0
    missing_row: int = 0
    null_mismatch: int = 0
    type_mismatch: int = 0
    value_drift: int = 0
    time_skew: int = 0
    entities: int = 0
    by_feature: dict = field(default_factory=dict)   # feature -> {category: count}

    def to_dict(self) -> dict:
        return asdict(self)
