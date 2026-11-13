# 特征一致性检查台（Feature Consistency Workbench）

导入**同一批实体**的离线特征值与线上快照（CSV），按 **实体 × 特征 × 事件时间**
比较五类差异：**缺行、类型差异、值漂移、空值差异、时间错位**。

- 纯 Python 标准库 + 原生前端，**零第三方依赖**
- 本地 HTTP 服务只绑定 `127.0.0.1`，**不连接任何线上服务、无出站请求**
- 规则可配置：数值容差（绝对/相对）、小数精度、空值含义、时间容忍度
- 重复实体按配置的主键策略处理，**绝不默认取最后一行**
- 时区不一致先提示、阻断运行，用户显式确认后才可继续
- 差异可下钻查看双方原值、解析值与生效计算规则
- 支持时间段重算、增量重算（只重算受影响特征 × 时间范围，保留上次结果）
- CSV 导出（带 BOM，Excel 直接打开）

## 目录结构

```
feature_check/
  model.py        # 配置/特征/结果数据模型与差异分类常量
  timeutil.py     # 时间解析、时区探测与不一致提示
  loader.py       # CSV 加载、类型解析、重复主键处理
  engine.py       # as-of 点时刻联结（防泄漏）与逐特征比对
  recompute.py    # 流水线、增量重算、两次运行 diff
  store.py        # 运行历史（本地 JSON，不可变快照）
  exporter.py     # CSV 导出
  server.py       # 本地 HTTP 服务（127.0.0.1）
  cli.py          # 命令行入口
  sample_data.py  # 带植入问题的样例数据生成器
  web/            # 单页控制台（index.html / app.js / styles.css）
tests/test_core.py
run.py            # 启动入口（兼容 python -I）
samples/          # 生成的样例 CSV 与配置
```

## 快速开始

```bash
# 1. 生成样例数据
python3 -I run.py 2>/dev/null; python3 -I feature_check/sample_data.py samples

# 2. 启动本地 Web 检查台
python3 -I run.py serve --port 8765
# 浏览器打开 http://127.0.0.1:8765
```

页面四步走：① 上传离线/线上 CSV → ② 配置特征与规则 → ③ 查看/筛选/下钻差异、导出 CSV
→ ④ 调整规则后增量重算，并对比两次运行的值变化。

## 命令行用法

```bash
# 完整比对（默认重复策略 fail，样例 E008 有重复主键，先用 dedup 配置）
python3 -I run.py run \
  --config samples/config_dedup.json \
  --offline samples/offline.csv \
  --online samples/online.csv \
  --store ./.feature_check_data

python3 -I run.py runs --store ./.feature_check_data      # 运行历史
python3 -I run.py show --run-id <ID> --store ...          # 差异明细
python3 -I run.py export --run-id <ID> --output out.csv   # 导出差异 CSV
python3 -I run.py diff --old-run-id <A> --new-run-id <B>  # 两次运行值变化
python3 -I run.py recompute --config new.json --parent-run-id <A> ...
```

## 核心语义

### 1. 点时刻取值与防未来数据泄漏（硬约束）

对线上快照的每个 `(实体, 事件时间 t)`，离线特征只取
`event_time <= t` 的**最新一行**（向过去看，forward fill）。
`event_time > t` 的离线行**永远不会被选中**；若不存在任何 `<= t` 的行，
结论为**整行缺失（missing_in_offline）**，而不是取未来值。
实现见 `engine.asof_lookup`（`bisect_right` 保证严格不含未来行）。

### 2. 五类差异分别统计、分别展示

| 分类 | 含义 |
| --- | --- |
| `missing_row` 整行缺失 | 一侧有该主键、另一侧无（含方向 missing_in_offline / missing_in_online） |
| `null_mismatch` 空值差异 | 空与非空相遇；strict 下两侧都空也不自动相等 |
| `type_mismatch` 类型差异 | 解析后的实际类型不同（如 bool 列出现字符串 `"VIP"`，保留原值不报错） |
| `value_drift` 值漂移 | 同类型但数值超容差/精度，或字符串/布尔不等 |
| `time_skew` 时间错位 | as-of 命中行事件时间滞后目标时间超过 `time_tolerance_seconds`（可与值漂移并存） |

### 3. 数值容差与小数精度

- 先按 `decimal_precision` 四舍五入，再比较；
- 判定阈值 = `max(绝对容差, 相对容差 × max(|离线|, |线上|))`；
- 绝对差、是否在容差内逐格记录，下钻可见完整计算过程。

### 4. 空值含义（`null_policy`）

- `strict`（默认）：空不自动相等；
- `lenient`：两侧都空视为一致，空 vs 非空仍是差异；
- `as_zero`：空按数值 0 参与比较。

### 5. 重复主键（实体 × 事件时间）

默认 `fail`：发现重复即中止并列出冲突键，**系统不会默认取最后一行**。
必须显式选择：`first` / `last` / `min` / `max` / `mean` / `median` / `majority`，
且可按特征分别配置（数值聚合用于数值列，众数用于字符串/布尔）。

### 6. 时区处理

- 内部统一转 UTC（ISO8601 存储）；
- 自动探测带偏移/朴素时间戳，朴素时间按配置时区或推断偏移解释；
- 两份数据时区不一致时**阻断运行并提示**，勾选 `accept_timezone_warning`
  （Web 上为显式复选框）后才可继续。

### 7. 增量重算与历史回溯

- 每次运行生成**不可变**快照（`runs/run_<id>.json`）；
- 重算时逐特征计算规则指纹：指纹未变且不在重算时间范围内的单元格
  **直接复用父运行结果**，其余重新计算；
- 记录复用/重算格数与受影响特征；`diff` 可点回具体实体、特征、新旧分类与值。

## 配置示例

```json
{
  "name": "用户画像特征一致性",
  "entity_column": "entity_id",
  "event_time_column": "event_time",
  "timezone": "+08:00",
  "time_tolerance_seconds": 1800,
  "default_abs_tolerance": 0.05,
  "default_rel_tolerance": 0.0,
  "default_decimal_precision": 2,
  "null_policy": "strict",
  "duplicate_strategy": "fail",
  "time_range_start": null,
  "time_range_end": null,
  "accept_timezone_warning": false,
  "features": [
    {"name": "score", "dtype": "number"},
    {"name": "city",  "dtype": "string", "duplicate_strategy": "majority"},
    {"name": "is_vip", "dtype": "bool",   "null_policy": "lenient"}
  ]
}
```

特征级字段（`abs_tolerance` / `rel_tolerance` / `decimal_precision` /
`null_policy` / `duplicate_strategy`）填 `null` 表示继承全局默认。

## 测试

```bash
python3 -I -m unittest discover -s tests -v
```

覆盖：防未来数据泄漏、as-of 命中选择、重复主键 fail/mean/last、
容差/精度、三种空值语义、类型差异、时间错位、双向缺行、时间范围过滤、
时区阻断、增量重算复用与运行 diff、CSV 导出。

## 安全边界

- 服务仅监听 `127.0.0.1`，外网网卡不可达；
- 无任何出站连接代码，不依赖线上服务；
- 上传文件名做净化，文件仅落在本机数据目录；
- 所有数据与运行历史保存在本地目录（默认 `./.feature_check_data`）。
