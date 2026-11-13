"""本地检查台 HTTP 服务。

安全边界：
- 只绑定 ``127.0.0.1``，不监听外网网卡；
- 不发起任何出站连接，不依赖任何线上服务；
- 上传文件仅落在本会话数据目录，文件名做净化处理。
"""
from __future__ import annotations

import json
import os
import re
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .exporter import export_csv
from .model import CheckConfig, DIFF_MATCH
from .recompute import (
    execute_pipeline, execute_incremental, diff_runs,
)
from .store import RunStore

_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class CheckServer:
    def __init__(self, data_dir: str):
        self.data_dir = os.path.abspath(data_dir)
        self.upload_dir = os.path.join(self.data_dir, "uploads")
        os.makedirs(self.upload_dir, exist_ok=True)
        self.store = RunStore(os.path.join(self.data_dir, "runs"))
        # 当前工作区的数据源路径（由上传接口登记）
        self.sources: dict[str, str] = {}

    def save_upload(self, filename: str, data: bytes) -> str:
        safe = _SAFE_NAME.sub("_", os.path.basename(filename)) or "upload.csv"
        # 避免覆盖
        base, ext = os.path.splitext(safe)
        path = os.path.join(self.upload_dir, safe)
        i = 1
        while os.path.exists(path):
            path = os.path.join(self.upload_dir, f"{base}_{i}{ext}")
            i += 1
        with open(path, "wb") as fh:
            fh.write(data)
        return path


def make_handler(app: CheckServer):

    class Handler(BaseHTTPRequestHandler):
        server_version = "FeatureCheck/1.0"

        def log_message(self, fmt, *args):  # 安静日志
            pass

        # ---------- 基础工具 ----------
        def _json(self, obj, status: int = 200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, text: str, filename: str, content_type: str):
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition",
                             f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def _parse_query(self):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            return {k: v[0] for k, v in q.items()}

        # ---------- 路由 ----------
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/health":
                    return self._json({"ok": True, "local_only": True})
                if path == "/api/runs":
                    return self._json({"runs": app.store.list_runs()})
                if path == "/api/run":
                    return self._get_run(self._parse_query())
                if path == "/api/run/diff":
                    return self._run_diff(self._parse_query())
                if path == "/api/export":
                    return self._export(self._parse_query())
                if path == "/api/sources":
                    return self._json({"sources": app.sources})
                return self._static(path)
            except KeyError as exc:
                return self._json({"error": str(exc)}, 404)
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/upload":
                    return self._upload()
                if path == "/api/run":
                    return self._start_run(self._read_json())
                if path == "/api/recompute":
                    return self._recompute(self._read_json())
                return self._json({"error": "not found"}, 404)
            except KeyError as exc:
                return self._json({"error": str(exc)}, 404)
            except Exception as exc:  # noqa: BLE001
                return self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)

        # ---------- 静态资源 ----------
        def _static(self, path: str):
            if path in ("/", "/index.html"):
                path = "/index.html"
            rel = path.lstrip("/")
            full = os.path.normpath(os.path.join(_WEB_DIR, rel))
            if not full.startswith(_WEB_DIR) or not os.path.isfile(full):
                self.send_response(404)
                self.end_headers()
                return
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
            }.get(os.path.splitext(full)[1], "application/octet-stream")
            with open(full, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        # ---------- 业务接口 ----------
        def _parse_multipart(self) -> list:
            """用标准库 email 解析 multipart/form-data，避免已弃用的 cgi 模块。

            返回 [(field_name, filename, data_bytes), ...]。
            """
            ctype = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ctype:
                raise ValueError("上传请求必须是 multipart/form-data")
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            # 构造完整的 MIME 报文交给 email 解析
            header = (
                f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n"
            ).encode("utf-8")
            msg = BytesParser(policy=default).parsebytes(header + raw)
            parts = []
            if not msg.is_multipart():
                return parts
            for part in msg.iter_parts():
                disp = part.get("Content-Disposition", "")
                if not disp:
                    continue
                name_m = re.search(r'name="([^"]*)"', disp)
                file_m = re.search(r'filename="([^"]*)"', disp)
                field_name = name_m.group(1) if name_m else ""
                filename = file_m.group(1) if file_m else None
                payload = part.get_payload(decode=True) or b""
                if filename:
                    parts.append((field_name, filename, payload))
            return parts

        def _upload(self):
            saved = {}
            for field_name, filename, data in self._parse_multipart():
                path = app.save_upload(filename, data)
                role = field_name or "file"      # offline / online
                saved[role] = path
                app.sources[role] = path
            return self._json({"saved": saved, "sources": app.sources})

        def _resolve_sources(self, payload: dict):
            offline = payload.get("offline_path") or app.sources.get("offline")
            online = payload.get("online_path") or app.sources.get("online")
            if not offline or not online:
                raise ValueError("请先上传或指定离线/线上 CSV 文件路径")
            return offline, online

        def _start_run(self, payload: dict):
            offline, online = self._resolve_sources(payload)
            config = CheckConfig.from_dict(payload["config"])
            result = execute_pipeline(config, offline, online, app.store)
            if result.record is None:
                return self._json({"blocked": True, "warnings": result.blocking_warnings}, 409)
            return self._json(self._run_payload(result.record))

        def _recompute(self, payload: dict):
            offline, online = self._resolve_sources(payload)
            config = CheckConfig.from_dict(payload["config"])
            parent_id = payload.get("parent_run_id") or app.store.latest_run_id()
            if not parent_id:
                raise ValueError("不存在可用于重算的历史运行")
            result = execute_incremental(config, offline, online, app.store, parent_id)
            if result.record is None:
                return self._json({"blocked": True, "warnings": result.blocking_warnings}, 409)
            return self._json(self._run_payload(result.record))

        def _run_payload(self, record) -> dict:
            return {
                "run": record.to_dict(),
                "summary": record.summary.to_dict(),
                "results": [c.to_dict() for c in record.results],
            }

        def _get_run(self, query: dict):
            run_id = query.get("run_id") or app.store.latest_run_id()
            if not run_id:
                return self._json({"error": "暂无运行记录"}, 404)
            record = app.store.load_run(run_id)
            payload = self._run_payload(record)
            # 可选过滤
            category = query.get("category")
            entity = query.get("entity_id")
            feature = query.get("feature")
            if category or entity or feature:
                payload["results"] = [
                    c for c in payload["results"]
                    if (not category or c["category"] == category)
                    and (not entity or c["entity_id"] == entity)
                    and (not feature or c["feature"] == feature)
                ]
            return self._json(payload)

        def _run_diff(self, query: dict):
            old = app.store.load_run(query["old_run_id"])
            new = app.store.load_run(query["new_run_id"])
            return self._json({"changes": diff_runs(old, new)})

        def _export(self, query: dict):
            run_id = query.get("run_id") or app.store.latest_run_id()
            record = app.store.load_run(run_id)
            cats = query.get("categories")
            categories = cats.split(",") if cats else None
            text = export_csv(
                record.results,
                include_matches=query.get("include_matches") == "1",
                categories=categories,
            )
            fname = f"feature_check_{run_id}.csv"
            return self._text(text, fname, "text/csv; charset=utf-8")

    return Handler


def serve(data_dir: str, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """创建并返回本地 HTTP 服务（调用方负责 serve_forever）。"""
    app = CheckServer(data_dir)
    httpd = ThreadingHTTPServer((host, port), make_handler(app))
    httpd.app = app
    return httpd
