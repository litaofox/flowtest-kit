"""pytest 入口：发现并执行业务流程文件。

项目内使用：
    # 单条流程
    set FLOW_FILE=flows/主链路.flow.yaml   (PowerShell: $env:FLOW_FILE="...")
    # 或批量
    set FLOW_GLOB=flows/*.flow.yaml
    pytest /path/to/flowtest-kit/tests/test_flow.py

另含一个零依赖自检（本地起 HTTP 桩），任何环境都可验证执行器本身可用：
    pytest /path/to/flowtest-kit/tests/test_flow.py -k selfcheck
"""
from __future__ import annotations

import glob
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

KIT_DIR = Path(__file__).resolve().parents[1] / "kit"
sys.path.insert(0, str(KIT_DIR))

from runner import FlowRunner  # noqa: E402


# ----------------------------------------------------------- 业务流程发现
def _discover_flows() -> list[str]:
    single = os.getenv("FLOW_FILE")
    if single:
        return [single]
    pattern = os.getenv("FLOW_GLOB", "flows/*.flow.yaml")
    return sorted(glob.glob(pattern))


FLOWS = _discover_flows()


@pytest.mark.skipif(not FLOWS, reason="未发现流程文件（设置 FLOW_FILE 或 FLOW_GLOB）")
@pytest.mark.parametrize("flow_path", FLOWS)
def test_business_flow(flow_path: str, tmp_path) -> None:
    summary = FlowRunner(flow_path, output_root=str(tmp_path / "output")).run()
    report_dirs = list((tmp_path / "output").glob("*"))
    assert report_dirs, "未生成报告目录"
    result = json.loads((report_dirs[0] / "result.json").read_text(encoding="utf-8"))
    assert summary["failed"] == 0, (
        f"流程 {summary['flow']} 有 {summary['failed']} 个失败步骤，"
        f"报告：{report_dirs[0] / 'report.md'}"
    )
    assert result["summary"]["total"] > 0


# ----------------------------------------------------------- 执行器自检
class _StubHandler(BaseHTTPRequestHandler):
    """最小业务桩：登录发 token、订单可查、越权 403。"""

    def log_message(self, *args):  # 静默
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/api/auth/login":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            if data.get("username") == "admin" and data.get("password") == "pw":
                self._send(200, {"code": 0, "message": "ok",
                                 "data": {"accessToken": "tok-123", "user": {"id": 7, "name": "admin"}}})
            else:
                self._send(401, {"code": 40100, "message": "bad credentials", "data": None})
        else:
            self._send(404, {"code": 404, "message": "not found", "data": None})

    def do_GET(self):
        if self.path == "/api/orders/1":
            if self.headers.get("Authorization") != "Bearer tok-123":
                self._send(403, {"code": 40300, "message": "forbidden", "data": None})
            else:
                self._send(200, {"code": 0, "message": "ok",
                                 "data": {"id": 1, "status": "PAID", "amount": 99.5}})
        elif self.path == "/api/forbidden":
            self._send(403, {"code": 40300, "message": "forbidden", "data": None})
        elif self.path == "/health":
            self._send(200, {"code": 0, "message": "ok", "data": {"status": "UP"}})
        else:
            self._send(404, {"code": 404, "message": "not found", "data": None})


SELFCHECK_FLOW = """
meta: {name: selfcheck, role: admin}
env: {base_url: "http://127.0.0.1:${PORT}"}
healthcheck:
  path: /health
  timeout: 30
  interval: 1
auth:
  login:
    method: POST
    path: /api/auth/login
    json: {username: "${ADMIN_USER}", password: "${ADMIN_PASS}"}
    token_path: data.accessToken
    captures: {uid: data.user.id}
steps:
  - id: order_paid
    title: 已支付订单可查
    type: api
    request: {method: GET, path: /api/orders/1}
    assert:
      code: 0
      data.status: PAID
      data.amount: {op: gte, value: 0}
    capture: {orderStatus: data.status}
  - id: low_role_forbidden
    title: 越权访问应被拒绝（反面路径）
    type: api
    request: {method: GET, path: /api/forbidden}
    expect_failure: true
    assert:
      http_status: 403
      code: 40300
  - id: capture_works
    title: 步骤间变量与环境变量正确传递
    type: api
    request: {method: GET, path: /api/orders/1}
    assert:
      data.status: "${orderStatus}"
"""


def test_runner_selfcheck(tmp_path, monkeypatch) -> None:
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("PORT", str(port))
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASS", "pw")
    try:
        flow_file = tmp_path / "selfcheck.flow.yaml"
        flow_file.write_text(SELFCHECK_FLOW, encoding="utf-8")
        summary = FlowRunner(flow_file, output_root=str(tmp_path / "out")).run()
        assert summary["failed"] == 0, f"自检不应有失败步骤: {summary}"
        assert summary["total"] == 3
        report_dir = next((tmp_path / "out").glob("selfcheck-*"))
        result = json.loads((report_dir / "result.json").read_text(encoding="utf-8"))
        # 反面路径必须被记录为 PASS（预期拒绝）
        titles = {s["id"]: s["status"] for s in result["steps"]}
        assert titles["low_role_forbidden"] == "PASSED"
        assert (report_dir / "report.md").exists()
    finally:
        server.shutdown()
