"""运行历史存储（纯本地 JSON 文件，无任何外部服务）。

目录结构::

    <store_dir>/
      manifest.json            # 运行索引
      runs/run_<id>.json       # 每次运行的完整快照（不可变）
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from .model import CheckConfig, CellResult, RunSummary
from .timeutil import to_iso


@dataclass
class RunRecord:
    run_id: str
    created_at: str
    config: CheckConfig
    summary: RunSummary
    results: list[CellResult]
    rule_fingerprint: str
    timezone_warnings: list
    duplicate_info: dict
    source_files: dict
    parent_run_id: str | None = None
    recompute_note: str = ""
    reused_cells: int = 0
    recomputed_cells: int = 0

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "config": self.config.to_dict(),
            "summary": self.summary.to_dict(),
            "results": [c.to_dict() for c in self.results],
            "rule_fingerprint": self.rule_fingerprint,
            "timezone_warnings": self.timezone_warnings,
            "duplicate_info": self.duplicate_info,
            "source_files": self.source_files,
            "parent_run_id": self.parent_run_id,
            "recompute_note": self.recompute_note,
            "reused_cells": self.reused_cells,
            "recomputed_cells": self.recomputed_cells,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RunRecord":
        return cls(
            run_id=d["run_id"],
            created_at=d["created_at"],
            config=CheckConfig.from_dict(d["config"]),
            summary=RunSummary(**d["summary"]),
            results=[CellResult(**c) for c in d["results"]],
            rule_fingerprint=d["rule_fingerprint"],
            timezone_warnings=d.get("timezone_warnings", []),
            duplicate_info=d.get("duplicate_info", {}),
            source_files=d.get("source_files", {}),
            parent_run_id=d.get("parent_run_id"),
            recompute_note=d.get("recompute_note", ""),
            reused_cells=d.get("reused_cells", 0),
            recomputed_cells=d.get("recomputed_cells", 0),
        )


class RunStore:
    """线程安全的运行历史存储。"""

    def __init__(self, store_dir: str):
        self.dir = os.path.abspath(store_dir)
        self.runs_dir = os.path.join(self.dir, "runs")
        os.makedirs(self.runs_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._manifest_path = os.path.join(self.dir, "manifest.json")
        if not os.path.exists(self._manifest_path):
            self._write_manifest({"runs": [], "seq": 0})

    # ---- 底层读写 ----
    def _read_manifest(self) -> dict:
        with open(self._manifest_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _write_manifest(self, data: dict) -> None:
        tmp = self._manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._manifest_path)

    def save_run(self, record: RunRecord) -> None:
        with self._lock:
            path = os.path.join(self.runs_dir, f"run_{record.run_id}.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(record.to_dict(), fh, ensure_ascii=False)
            manifest = self._read_manifest()
            manifest["runs"].append({
                "run_id": record.run_id,
                "created_at": record.created_at,
                "name": record.config.name,
                "rule_fingerprint": record.rule_fingerprint,
                "parent_run_id": record.parent_run_id,
                "total_cells": record.summary.total_cells,
                "diff_cells": sum(1 for c in record.results if c.category != "match"),
            })
            self._write_manifest(manifest)

    def new_run_id(self) -> str:
        with self._lock:
            manifest = self._read_manifest()
            manifest["seq"] += 1
            seq = manifest["seq"]
            self._write_manifest(manifest)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"{stamp}_{seq:04d}"

    def list_runs(self) -> list[dict]:
        return list(reversed(self._read_manifest()["runs"]))

    def load_run(self, run_id: str) -> RunRecord:
        path = os.path.join(self.runs_dir, f"run_{run_id}.json")
        if not os.path.exists(path):
            raise KeyError(f"运行不存在: {run_id}")
        with open(path, "r", encoding="utf-8") as fh:
            return RunRecord.from_dict(json.load(fh))

    def latest_run_id(self) -> str | None:
        runs = self._read_manifest()["runs"]
        return runs[-1]["run_id"] if runs else None
