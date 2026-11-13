"""核心语义单元测试。运行：python -m unittest discover -s tests -v"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

# 兼容 python -I（隔离模式不自动把 cwd 加入 sys.path）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from feature_check.model import (
    CheckConfig, FeatureSpec,
    DUP_FAIL, DUP_MEAN, DUP_LAST,
    NULL_LENIENT, NULL_AS_ZERO,
    DIFF_MATCH, DIFF_MISSING_ROW, DIFF_NULL_MISMATCH,
    DIFF_TYPE_MISMATCH, DIFF_VALUE_DRIFT, DIFF_TIME_SKEW,
    MISSING_IN_OFFLINE, MISSING_IN_ONLINE,
)
from feature_check.loader import load_csv, LoadError
from feature_check.engine import (
    build_asof_index, asof_lookup, run_comparison, summarize,
)
from feature_check.timeutil import parse_timestamp, detect_timezone, compare_timezone
from feature_check.recompute import (
    execute_pipeline, execute_incremental, diff_runs, feature_rule_hash,
)
from feature_check.store import RunStore
from feature_check.exporter import export_csv

HEADER = "entity_id,event_time,score,city,is_vip\n"


def write(path, content):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(content)


def base_config(**kw):
    cfg = CheckConfig(
        name="t", entity_column="entity_id", event_time_column="event_time",
        timezone="+08:00", accept_timezone_warning=True,
        default_abs_tolerance=0.05, default_decimal_precision=2,
        features=[
            FeatureSpec("score", "number"),
            # 字符串/布尔特征显式覆盖数值容差，全局容差调整不应改变它们的规则指纹
            FeatureSpec("city", "string", abs_tolerance=0.0, rel_tolerance=0.0),
            FeatureSpec("is_vip", "bool", abs_tolerance=0.0, rel_tolerance=0.0),
        ],
    )
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class TimeUtilTests(unittest.TestCase):
    def test_aware_and_naive(self):
        dt, aware = parse_timestamp("2026-11-01T10:00:00+08:00")
        self.assertTrue(aware)
        self.assertEqual(dt.hour, 2)  # 转 UTC
        dt2, aware2 = parse_timestamp("2026-11-01 10:00:00", assumed_offset_minutes=480)
        self.assertFalse(aware2)
        self.assertEqual(dt, dt2)

    def test_z_suffix(self):
        dt, aware = parse_timestamp("2026-11-01T02:00:00Z")
        self.assertTrue(aware)
        self.assertEqual(dt.hour, 2)

    def test_timezone_mismatch_detected(self):
        off = detect_timezone(["2026-11-01T10:00:00+08:00"])
        on = detect_timezone(["2026-11-01T10:00:00+00:00"])
        warnings = compare_timezone(off, on)
        self.assertTrue(warnings)

    def test_naive_assumed_utc_warning(self):
        info = detect_timezone(["2026-11-01 10:00:00"])
        self.assertTrue(info.has_naive)
        self.assertTrue(any("UTC" in w for w in info.warnings))


class LeakageTests(unittest.TestCase):
    """未来数据泄漏防护是最高优先级约束。"""

    def test_future_row_never_selected(self):
        with tempfile.TemporaryDirectory() as d:
            off = os.path.join(d, "off.csv")
            on = os.path.join(d, "on.csv")
            # 离线只有 12:00（未来），线上目标 11:00
            write(off, HEADER + "E1,2026-11-01T12:00:00+08:00,55,Chengdu,false\n")
            write(on, HEADER + "E1,2026-11-01T11:00:00+08:00,55,Chengdu,false\n")
            cfg = base_config()
            offline = load_csv(off, cfg, "offline", 480)
            online = load_csv(on, cfg, "online", 480)
            idx = build_asof_index(offline)
            hit = asof_lookup(idx, "E1", online.rows[0].event_dt)
            self.assertIsNone(hit)  # 绝不允许取未来行

            results = run_comparison(cfg, offline, online)
            cats = {c.feature: c.category for c in results}
            self.assertTrue(all(v == DIFF_MISSING_ROW for v in cats.values()))
            self.assertEqual(results[0].missing_side, MISSING_IN_OFFLINE)

    def test_asof_picks_latest_past_row(self):
        with tempfile.TemporaryDirectory() as d:
            off = os.path.join(d, "off.csv")
            on = os.path.join(d, "on.csv")
            write(off, HEADER
                  + "E1,2026-11-01T08:00:00+08:00,70,H,true\n"
                  + "E1,2026-11-01T09:30:00+08:00,71,H,true\n"
                  + "E1,2026-11-01T10:30:00+08:00,99,H,true\n")  # 未来行
            write(on, HEADER + "E1,2026-11-01T10:00:00+08:00,71,H,true\n")
            cfg = base_config()
            offline = load_csv(off, cfg, "offline", 480)
            online = load_csv(on, cfg, "online", 480)
            idx = build_asof_index(offline)
            hit = asof_lookup(idx, "E1", online.rows[0].event_dt)
            self.assertEqual(hit.row.values["score"], 71.0)  # 命中 09:30 而非 10:30


class DuplicateTests(unittest.TestCase):
    def test_duplicate_fails_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            off = os.path.join(d, "off.csv")
            write(off, HEADER
                  + "E1,2026-11-01T10:00:00+08:00,40,W,true\n"
                  + "E1,2026-11-01T10:00:00+08:00,60,W,true\n")
            cfg = base_config(duplicate_strategy=DUP_FAIL)
            with self.assertRaises(LoadError) as ctx:
                load_csv(off, cfg, "offline", 480)
            self.assertIn("不会默认取最后一行", str(ctx.exception))

    def test_duplicate_mean_and_explicit_last(self):
        with tempfile.TemporaryDirectory() as d:
            off = os.path.join(d, "off.csv")
            write(off, HEADER
                  + "E1,2026-11-01T10:00:00+08:00,40,W,true\n"
                  + "E1,2026-11-01T10:00:00+08:00,60,X,false\n")
            # 数值特征 mean，字符串/布尔特征按众数
            cfg = base_config(duplicate_strategy=DUP_MEAN)
            for f in cfg.features:
                if f.name in ("city", "is_vip"):
                    f.duplicate_strategy = "majority"
            table = load_csv(off, cfg, "offline", 480)
            self.assertEqual(len(table.rows), 1)
            self.assertEqual(table.rows[0].values["score"], 50.0)
            self.assertEqual(len(table.duplicate_groups), 1)

            cfg2 = base_config(duplicate_strategy=DUP_LAST)
            table2 = load_csv(off, cfg2, "offline", 480)
            self.assertEqual(table2.rows[0].values["score"], 60.0)
            self.assertEqual(table2.rows[0].values["city"], "X")


class ComparisonTests(unittest.TestCase):
    def _load(self, d, off_rows, on_rows, cfg=None):
        off = os.path.join(d, "off.csv")
        on = os.path.join(d, "on.csv")
        write(off, HEADER + off_rows)
        write(on, HEADER + on_rows)
        cfg = cfg or base_config()
        offline = load_csv(off, cfg, "offline", 480)
        online = load_csv(on, cfg, "online", 480)
        return cfg, offline, online

    def test_value_drift_vs_tolerance(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, off, on = self._load(
                d,
                "E1,2026-11-01T10:00:00+08:00,90.0,B,true\n",
                "E1,2026-11-01T10:00:00+08:00,92.5,B,true\n",
            )
            res = {c.feature: c for c in run_comparison(cfg, off, on)}
            self.assertEqual(res["score"].category, DIFF_VALUE_DRIFT)
            self.assertFalse(res["score"].within_tolerance)

    def test_delta_within_tolerance_is_match(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, off, on = self._load(
                d,
                "E1,2026-11-01T10:00:00+08:00,90.00,B,true\n",
                "E1,2026-11-01T10:00:00+08:00,90.03,B,true\n",
            )
            res = {c.feature: c for c in run_comparison(cfg, off, on)}
            self.assertEqual(res["score"].category, DIFF_MATCH)
            self.assertTrue(res["score"].within_tolerance)

    def test_decimal_precision_rounding(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = base_config(default_abs_tolerance=0.0, default_decimal_precision=1)
            off_f = os.path.join(d, "off.csv")
            on_f = os.path.join(d, "on.csv")
            write(off_f, HEADER + "E1,2026-11-01T10:00:00+08:00,1.24,B,true\n")
            write(on_f, HEADER + "E1,2026-11-01T10:00:00+08:00,1.26,B,true\n")
            offline = load_csv(off_f, cfg, "offline", 480)
            online = load_csv(on_f, cfg, "online", 480)
            res = {c.feature: c for c in run_comparison(cfg, offline, online)}
            # 1.24 -> 1.2, 1.26 -> 1.3 => 漂移
            self.assertEqual(res["score"].category, DIFF_VALUE_DRIFT)

    def test_type_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, off, on = self._load(
                d,
                "E1,2026-11-01T10:00:00+08:00,75.5,S,false\n",
                "E1,2026-11-01T10:00:00+08:00,75.5,S,VIP\n",
            )
            res = {c.feature: c for c in run_comparison(cfg, off, on)}
            self.assertEqual(res["is_vip"].category, DIFF_TYPE_MISMATCH)

    def test_null_policies(self):
        with tempfile.TemporaryDirectory() as d:
            # strict：两侧都空也算空值差异
            cfg = base_config(null_policy="strict")
            _, off, on = self._load(
                d,
                "E1,2026-11-01T10:00:00+08:00,60,,true\n",
                "E1,2026-11-01T10:00:00+08:00,60,,true\n",
                cfg,
            )
            res = {c.feature: c for c in run_comparison(cfg, off, on)}
            self.assertEqual(res["city"].category, DIFF_NULL_MISMATCH)

            # lenient：两侧都空视为一致
            cfg2 = base_config(null_policy=NULL_LENIENT)
            off2 = load_csv(off.path if hasattr(off, "path") else os.path.join(d, "off.csv"),
                            cfg2, "offline", 480)
            on2 = load_csv(os.path.join(d, "on.csv"), cfg2, "online", 480)
            res2 = {c.feature: c for c in run_comparison(cfg2, off2, on2)}
            self.assertEqual(res2["city"].category, DIFF_MATCH)

            # as_zero：空按 0；两侧都空 -> 0==0 一致
            cfg3 = base_config(null_policy=NULL_AS_ZERO)
            off3 = load_csv(os.path.join(d, "off.csv"), cfg3, "offline", 480)
            on3 = load_csv(os.path.join(d, "on.csv"), cfg3, "online", 480)
            res3 = {c.feature: c for c in run_comparison(cfg3, off3, on3)}
            self.assertEqual(res3["city"].category, DIFF_MATCH)

    def test_as_zero_with_non_numeric_other_side(self):
        """空按 0，但另一侧是字符串时不得崩溃，应判类型差异。"""
        with tempfile.TemporaryDirectory() as d:
            off = os.path.join(d, "off.csv")
            on = os.path.join(d, "on.csv")
            # city 为 string 列：离线空，线上有字符串
            write(off, HEADER + "E1,2026-11-01T10:00:00+08:00,60,,true\n")
            write(on, HEADER + "E1,2026-11-01T10:00:00+08:00,60,GZ,true\n")
            cfg = base_config(null_policy=NULL_AS_ZERO)
            offline = load_csv(off, cfg, "offline", 480)
            online = load_csv(on, cfg, "online", 480)
            res = {c.feature: c for c in run_comparison(cfg, offline, online)}
            self.assertEqual(res["city"].category, DIFF_TYPE_MISMATCH)

    def test_time_skew(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = base_config(time_tolerance_seconds=1800)
            _, off, on = self._load(
                d,
                "E1,2026-11-01T08:00:00+08:00,70,H,true\n",
                "E1,2026-11-01T10:00:00+08:00,70,H,true\n",
                cfg,
            )
            res = {c.feature: c for c in run_comparison(cfg, off, on)}
            self.assertEqual(res["score"].category, DIFF_TIME_SKEW)
            self.assertIn("错位", res["score"].detail)

    def test_drift_and_skew_coexist(self):
        """值漂移与时间错位并存：主分类为漂移，secondary_flags 记录错位。"""
        with tempfile.TemporaryDirectory() as d:
            cfg = base_config(time_tolerance_seconds=1800)
            _, off, on = self._load(
                d,
                "E1,2026-11-01T08:00:00+08:00,70,H,true\n",
                "E1,2026-11-01T10:00:00+08:00,99,H,true\n",
                cfg,
            )
            res = {c.feature: c for c in run_comparison(cfg, off, on)}
            cell = res["score"]
            self.assertEqual(cell.category, DIFF_VALUE_DRIFT)
            self.assertIn(DIFF_TIME_SKEW, cell.secondary_flags)
            s = summarize(list(res.values()))
            self.assertEqual(s.value_drift, 1)
            # 三个特征都滞后 2 小时；score 漂移为主分类并带错位标记，其余两个主分类即错位
            self.assertEqual(s.time_skew, 3)

    def test_missing_in_online(self):
        with tempfile.TemporaryDirectory() as d:
            cfg, off, on = self._load(
                d,
                "E5,2026-11-01T10:00:00+08:00,88,S,true\n",
                "E6,2026-11-01T10:00:00+08:00,88,S,true\n",
            )
            results = run_comparison(cfg, off, on)
            missing = [c for c in results if c.category == DIFF_MISSING_ROW]
            sides = {c.entity_id: c.missing_side for c in missing}
            self.assertEqual(sides["E5"], MISSING_IN_ONLINE)
            self.assertEqual(sides["E6"], MISSING_IN_OFFLINE)


class TimeRangeTests(unittest.TestCase):
    def test_range_filters_comparison(self):
        with tempfile.TemporaryDirectory() as d:
            off = os.path.join(d, "off.csv")
            on = os.path.join(d, "on.csv")
            write(off, HEADER
                  + "E1,2026-11-01T10:00:00+08:00,1,B,true\n"
                  + "E1,2026-11-02T10:00:00+08:00,2,B,true\n")
            write(on, HEADER
                  + "E1,2026-11-01T10:00:00+08:00,1,B,true\n"
                  + "E1,2026-11-02T10:00:00+08:00,9,B,true\n")
            cfg = base_config(
                time_range_start="2026-11-02T00:00:00+08:00",
                time_range_end="2026-11-02T23:59:59+08:00",
            )
            offline = load_csv(off, cfg, "offline", 480)
            online = load_csv(on, cfg, "online", 480)
            results = run_comparison(cfg, offline, online)
            self.assertEqual(len(results), 3)  # 仅 11-02 的 3 个特征
            self.assertTrue(all("2026-11-02" in c.event_time for c in results))


class PipelineIncrementalTests(unittest.TestCase):
    def _setup(self, d):
        off = os.path.join(d, "off.csv")
        on = os.path.join(d, "on.csv")
        write(off, HEADER
              + "E1,2026-11-01T10:00:00+08:00,90.0,B,true\n"
              + "E2,2026-11-01T10:00:00+08:00,75.5,S,false\n")
        write(on, HEADER
              + "E1,2026-11-01T10:00:00+08:00,92.5,B,true\n"
              + "E2,2026-11-01T10:00:00+08:00,75.5,S,VIP\n")
        return off, on

    def test_full_pipeline_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            off, on = self._setup(d)
            store = RunStore(os.path.join(d, "store"))
            cfg = base_config()
            pr = execute_pipeline(cfg, off, on, store)
            self.assertEqual(pr.blocking_warnings, [])
            s = pr.record.summary
            self.assertEqual(s.value_drift, 1)
            self.assertEqual(s.type_mismatch, 1)
            self.assertEqual(s.match, 4)
            self.assertEqual(s.entities, 2)

    def test_timezone_blocks_until_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            off = os.path.join(d, "off.csv")
            on = os.path.join(d, "on.csv")
            write(off, "entity_id,event_time,score,city,is_vip\n"
                       "E1,2026-11-01T10:00:00+08:00,90,B,true\n")
            write(on, "entity_id,event_time,score,city,is_vip\n"
                      "E1,2026-11-01T10:00:00+00:00,90,B,true\n")
            store = RunStore(os.path.join(d, "store"))
            cfg = base_config(accept_timezone_warning=False, timezone=None)
            pr = execute_pipeline(cfg, off, on, store)
            self.assertIsNone(pr.record)
            self.assertTrue(pr.blocking_warnings)
            # 显式确认后可运行
            cfg.accept_timezone_warning = True
            pr2 = execute_pipeline(cfg, off, on, store)
            self.assertIsNotNone(pr2.record)

    def test_incremental_reuses_unchanged_feature(self):
        with tempfile.TemporaryDirectory() as d:
            off, on = self._setup(d)
            store = RunStore(os.path.join(d, "store"))
            cfg1 = base_config(default_abs_tolerance=0.05)
            r1 = execute_pipeline(cfg1, off, on, store).record

            # 只放宽 score 的容差；city/is_vip 规则指纹不变，应被复用
            cfg2 = base_config(default_abs_tolerance=5.0)
            r2 = execute_incremental(cfg2, off, on, store, r1.run_id).record

            self.assertEqual(r2.parent_run_id, r1.run_id)
            # score 2 个单元格重算（E1/E2），其余 4 个复用
            self.assertEqual(r2.recomputed_cells, 2)
            self.assertEqual(r2.reused_cells, 4)
            # 放宽容差后 E1 score 变为一致
            e1_score = next(c for c in r2.results
                            if c.entity_id == "E1" and c.feature == "score")
            self.assertEqual(e1_score.category, DIFF_MATCH)
            # 历史运行不可变：r1 仍是漂移
            old = store.load_run(r1.run_id)
            old_e1 = next(c for c in old.results
                          if c.entity_id == "E1" and c.feature == "score")
            self.assertEqual(old_e1.category, DIFF_VALUE_DRIFT)

    def test_diff_runs_points_to_specific_change(self):
        with tempfile.TemporaryDirectory() as d:
            off, on = self._setup(d)
            store = RunStore(os.path.join(d, "store"))
            r1 = execute_pipeline(base_config(default_abs_tolerance=0.05),
                                  off, on, store).record
            r2 = execute_incremental(base_config(default_abs_tolerance=5.0),
                                     off, on, store, r1.run_id).record
            changes = diff_runs(r1, r2)
            self.assertTrue(any(
                ch["entity_id"] == "E1" and ch["feature"] == "score"
                and ch["old"]["category"] == DIFF_VALUE_DRIFT
                and ch["new"]["category"] == DIFF_MATCH
                for ch in changes
            ))

    def test_feature_rule_hash_changes(self):
        cfg1 = base_config(default_abs_tolerance=0.05)
        cfg2 = base_config(default_abs_tolerance=5.0)
        self.assertNotEqual(
            feature_rule_hash(cfg1, "score"), feature_rule_hash(cfg2, "score"))
        self.assertEqual(
            feature_rule_hash(cfg1, "city"), feature_rule_hash(cfg2, "city"))


class ExportTests(unittest.TestCase):
    def test_csv_export_filters(self):
        with tempfile.TemporaryDirectory() as d:
            off = os.path.join(d, "off.csv")
            on = os.path.join(d, "on.csv")
            write(off, HEADER + "E1,2026-11-01T10:00:00+08:00,90,B,true\n")
            write(on, HEADER + "E1,2026-11-01T10:00:00+08:00,99,B,true\n")
            cfg = base_config()
            store = RunStore(os.path.join(d, "store"))
            rec = execute_pipeline(cfg, off, on, store).record
            csv_text = export_csv(rec.results)
            self.assertNotIn("一致", csv_text)          # 默认不含 match
            self.assertIn("值漂移", csv_text)
            full = export_csv(rec.results, include_matches=True)
            self.assertIn("一致", full)
            self.assertTrue(full.startswith("﻿"))       # BOM


if __name__ == "__main__":
    unittest.main()
