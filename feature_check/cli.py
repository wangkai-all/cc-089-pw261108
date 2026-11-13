"""命令行入口：run / recompute / runs / show / export。"""
from __future__ import annotations

import argparse
import json
import sys

from .exporter import export_csv
from .model import CheckConfig, DIFF_LABELS
from .recompute import execute_pipeline, execute_incremental, diff_runs
from .store import RunStore


def _load_config(path: str) -> CheckConfig:
    with open(path, "r", encoding="utf-8") as fh:
        return CheckConfig.from_dict(json.load(fh))


def _print_blocking(warnings: list) -> int:
    print("运行被阻止，需要先处理以下提示：", file=sys.stderr)
    for w in warnings:
        print(f"  - {w}", file=sys.stderr)
    return 2


def cmd_run(args) -> int:
    config = _load_config(args.config)
    store = RunStore(args.store)
    result = execute_pipeline(config, args.offline, args.online, store)
    if result.record is None:
        return _print_blocking(result.blocking_warnings)
    rec = result.record
    print(f"运行完成: run_id={rec.run_id}")
    print(_summary_text(rec))
    return 0


def cmd_recompute(args) -> int:
    config = _load_config(args.config)
    store = RunStore(args.store)
    result = execute_incremental(
        config, args.offline, args.online, store, args.parent_run_id
    )
    if result.record is None:
        return _print_blocking(result.blocking_warnings)
    rec = result.record
    print(f"增量重算完成: run_id={rec.run_id} (父运行 {args.parent_run_id})")
    print(rec.recompute_note)
    print(_summary_text(rec))
    return 0


def cmd_runs(args) -> int:
    store = RunStore(args.store)
    for meta in store.list_runs():
        print(
            f"{meta['run_id']}  {meta['created_at']}  {meta['name']}  "
            f"单元格={meta['total_cells']} 差异={meta['diff_cells']}  "
            f"父运行={meta.get('parent_run_id') or '-'}"
        )
    return 0


def cmd_show(args) -> int:
    store = RunStore(args.store)
    rec = store.load_run(args.run_id)
    print(_summary_text(rec))
    print("\n差异明细（前 %d 条）：" % args.limit)
    shown = 0
    for c in rec.results:
        if c.category == "match":
            continue
        print(
            f"  [{DIFF_LABELS[c.category]}] {c.entity_id} @ {c.event_time} "
            f"{c.feature}: 离线={c.offline_raw!r} 线上={c.online_raw!r}  {c.detail}"
        )
        shown += 1
        if shown >= args.limit:
            break
    if shown == 0:
        print("  无差异")
    return 0


def cmd_export(args) -> int:
    store = RunStore(args.store)
    rec = store.load_run(args.run_id)
    cats = args.categories.split(",") if args.categories else None
    text = export_csv(rec.results, include_matches=args.include_matches,
                      categories=cats)
    with open(args.output, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    print(f"已导出 {args.output}")
    return 0


def cmd_diff(args) -> int:
    store = RunStore(args.store)
    old = store.load_run(args.old_run_id)
    new = store.load_run(args.new_run_id)
    changes = diff_runs(old, new)
    print(f"两次运行共 {len(changes)} 个单元格发生变化：")
    for ch in changes[: args.limit]:
        old_cat = ch["old"]["category"] if ch["old"] else "-"
        new_cat = ch["new"]["category"] if ch["new"] else "-"
        print(
            f"  {ch['change_type']:9s} {ch['entity_id']} @ {ch['event_time']} "
            f"{ch['feature']}: {old_cat} -> {new_cat}"
        )
    return 0


def _summary_text(rec) -> str:
    s = rec.summary
    lines = [
        f"实体数={s.entities} 单元格总数={s.total_cells}",
        "  ".join([
            f"一致={s.match}", f"整行缺失={s.missing_row}", f"空值差异={s.null_mismatch}",
            f"类型差异={s.type_mismatch}", f"值漂移={s.value_drift}", f"时间错位={s.time_skew}",
        ]),
    ]
    if rec.timezone_warnings:
        lines.append("时区提示: " + " | ".join(rec.timezone_warnings))
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="feature-check",
                                description="特征一致性检查台（纯本地，不连接线上服务）")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_store(sp):
        sp.add_argument("--store", default="./.feature_check_data", help="运行历史目录")

    sp = sub.add_parser("run", help="执行一次完整比对")
    sp.add_argument("--config", required=True)
    sp.add_argument("--offline", required=True)
    sp.add_argument("--online", required=True)
    add_store(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("recompute", help="基于父运行增量重算")
    sp.add_argument("--config", required=True)
    sp.add_argument("--offline", required=True)
    sp.add_argument("--online", required=True)
    sp.add_argument("--parent-run-id", required=True)
    add_store(sp)
    sp.set_defaults(func=cmd_recompute)

    sp = sub.add_parser("runs", help="列出运行历史")
    add_store(sp)
    sp.set_defaults(func=cmd_runs)

    sp = sub.add_parser("show", help="查看某次运行的差异明细")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--limit", type=int, default=50)
    add_store(sp)
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("export", help="导出 CSV")
    sp.add_argument("--run-id", required=True)
    sp.add_argument("--output", required=True)
    sp.add_argument("--categories", help="逗号分隔的差异分类")
    sp.add_argument("--include-matches", action="store_true")
    add_store(sp)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("diff", help="对比两次运行的值变化")
    sp.add_argument("--old-run-id", required=True)
    sp.add_argument("--new-run-id", required=True)
    sp.add_argument("--limit", type=int, default=50)
    add_store(sp)
    sp.set_defaults(func=cmd_diff)

    sp = sub.add_parser("serve", help="启动本地 Web 检查台")
    sp.add_argument("--data-dir", default="./.feature_check_data")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_serve)
    return p


def cmd_serve(args) -> int:
    from .server import serve
    httpd = serve(args.data_dir, args.host, args.port)
    print(f"特征一致性检查台已启动（仅本地）: http://{args.host}:{args.port}")
    print("按 Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
