"""HTTP 黑盒客户端：被测系统的唯一造数/契约验证通道。

设计要点：
- 只依赖公开 HTTP API，不 import 任何业务代码；
- 统一封装鉴权头、JSON 信封、点路径取值；
- 调用方（runner）负责变量替换与断言，本模块只做忠实的请求/响应建模。
"""
from __future__ import annotations

import json as _json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

MISSING = object()


def json_path(data: Any, path: str) -> Any:
    """按点路径取值，支持数组下标：data.items.0.id。取不到返回 MISSING。"""
    cur = data
    for part in path.split("."):
        if cur is None:
            return MISSING
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
                continue
            except (ValueError, IndexError):
                return MISSING
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return MISSING
    return cur


@dataclass
class HttpResponse:
    status: int
    headers: dict
    json: Any
    text: str
    elapsed_ms: int
    request_desc: str
    error: str | None = None

    def pick(self, path: str) -> Any:
        return json_path(self.json, path)


@dataclass
class ApiClient:
    base_url: str
    token: str | None = None
    # 鉴权头模板，如 {"Authorization": "Bearer ${token}"}；None 表示不加
    auth_header: dict | None = field(default_factory=lambda: {"Authorization": "Bearer ${token}"})
    timeout: int = 30
    verify_ssl: bool = True

    def __post_init__(self) -> None:
        self.session = requests.Session()

    def _headers(self, extra: dict | None, use_auth: bool = True) -> dict:
        h = dict(extra or {})
        if use_auth and self.auth_header and self.token:
            for k, v in self.auth_header.items():
                h[k] = str(v).replace("${token}", self.token)
        return h

    def call(self, spec: dict) -> HttpResponse:
        method = spec.get("method", "GET").upper()
        url = self.base_url.rstrip("/") + spec["path"]
        desc = f"{method} {spec['path']}"
        use_auth = not spec.get("no_auth", False)
        kwargs: dict[str, Any] = {
            "headers": self._headers(spec.get("headers"), use_auth),
            "params": spec.get("params"),
            "timeout": spec.get("timeout", self.timeout),
            "verify": self.verify_ssl,
        }
        if "json" in spec:
            kwargs["json"] = spec["json"]

        # multipart 上传：files=[{field, path}]，form 为普通表单字段
        file_specs = spec.get("files")
        if file_specs:
            opened = []
            files = []
            try:
                for f in file_specs:
                    p = Path(f["path"])
                    fh = p.open("rb")
                    opened.append(fh)
                    files.append((f["field"], (p.name, fh)))
                kwargs["files"] = files
                kwargs["data"] = spec.get("form")
                return self._send(method, url, desc, **kwargs)
            finally:
                for fh in opened:
                    fh.close()
        if spec.get("form"):
            kwargs["data"] = spec["form"]

        return self._send(method, url, desc, **kwargs)

    def _send(self, method: str, url: str, desc: str, **kwargs) -> HttpResponse:
        started = time.time()
        try:
            resp = self.session.request(method, url, **kwargs)
            body = self._parse_json(resp.text)
            return HttpResponse(
                status=resp.status_code,
                headers=dict(resp.headers),
                json=body,
                text=resp.text[:4000],
                elapsed_ms=int((time.time() - started) * 1000),
                request_desc=desc,
            )
        except requests.RequestException as exc:
            return HttpResponse(
                status=0,
                headers={},
                json=None,
                text="",
                elapsed_ms=int((time.time() - started) * 1000),
                request_desc=desc,
                error=f"{type(exc).__name__}: {exc}",
            )

    @staticmethod
    def _parse_json(text: str) -> Any:
        try:
            return _json.loads(text)
        except (ValueError, TypeError):
            return None
