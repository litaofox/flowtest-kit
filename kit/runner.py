"""流程执行器：把一份 .flow.yaml 演成真实业务，并做三层断言。

用法：
    python kit/runner.py flows/主链路.flow.yaml
退出码：全部通过 0；任何硬断言失败/步骤异常 1（可直接作 CI 门禁）。

步骤类型：api（含上传）/ poll（异步任务轮询）/ ui（真实浏览器）/ sql（只读落库断言）。
设计约束见 docs/方案设计说明书.md 第五章（断言失败永不重试、重置双开关、SQL 默认只读）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from api import MISSING, ApiClient, HttpResponse, json_path  # noqa: E402

VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")

# 网络层可重试的场景（仅这些；断言失败绝不重试）
RETRIABLE_HTTP = {429, 500, 502, 503, 504}


def render(value: Any, variables: dict) -> Any:
    """递归把字符串中的 ${VAR} 替换为变量/环境变量。整串为单变量时保留原类型。"""
    if isinstance(value, str):
        single = VAR_PATTERN.fullmatch(value.strip())
        if single:
            name = single.group(1)
            if name in variables:
                return variables[name]
            if name in os.environ:
                return os.environ[name]
            raise KeyError(f"变量未定义: {name}")

        def repl(m: re.Match) -> str:
            name = m.group(1)
            if name in variables:
                return str(variables[name])
            if name in os.environ:
                return os.environ[name]
            raise KeyError(f"变量未定义: {name}")

        return VAR_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [render(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: render(v, variables) for k, v in value.items()}
    return value


# ---------------------------------------------------------------- 断言引擎
def _op_check(actual: Any, op: str, expected: Any) -> tuple[bool, str]:
    if op in ("exists", "not_exists"):
        present = actual is not MISSING and actual is not None
        ok = present if op == "exists" else not present
        return ok, f"期望 {op}，实际 {'存在=' + repr(actual) if present else '缺失'}"
    if actual is MISSING:
        return False, "字段缺失"
    try:
        checks = {
            "eq": lambda: actual == expected,
            "ne": lambda: actual != expected,
            "gt": lambda: actual > expected,
            "gte": lambda: actual >= expected,
            "lt": lambda: actual < expected,
            "lte": lambda: actual <= expected,
            "contains": lambda: expected in actual,
            "not_contains": lambda: expected not in actual,
            "in": lambda: actual in expected,
            "regex": lambda: re.search(str(expected), str(actual)) is not None,
        }
        ok = checks[op]()
        return ok, f"期望 {op} {expected!r}，实际 {actual!r}"
    except TypeError as exc:
        return False, f"类型不可比: {exc}"


def _normalize(rule: Any) -> tuple[str, str, Any]:
    """规则两种写法：短写 {path: value}（eq）；长写 {path: {op, value}}。"""
    path, body = rule
    if isinstance(body, dict) and "op" in body:
        return path, body["op"], body.get("value")
    return path, "eq", body


def eval_json_rules(resp: HttpResponse, rules: dict) -> list[str]:
    failures = []
    for raw_path, raw_rule in rules.items():
        if raw_path == "http_status":
            ok, detail = _op_check(resp.status, "eq", raw_rule)
        elif raw_path == "http_status_in":
            ok, detail = _op_check(resp.status, "in", raw_rule)
        else:
            path, op, expected = _normalize((raw_path, raw_rule))
            ok, detail = _op_check(resp.pick(path), op, expected)
        if not ok:
            failures.append(f"{raw_path}: {detail}")
    return failures


class FlowRunner:
    def __init__(self, flow_path: str | Path, output_root: str | Path = "test-output") -> None:
        self.flow_path = Path(flow_path)
        self.flow = yaml.safe_load(self.flow_path.read_text(encoding="utf-8"))
        name = self.flow.get("meta", {}).get("name", self.flow_path.stem)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.out_dir = Path(output_root) / f"{name}-{ts}"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        # 内置变量 RUN_ID（可用环境变量覆盖），供业务唯一键实现幂等
        self.variables: dict[str, Any] = {"RUN_ID": os.getenv("RUN_ID", datetime.now().strftime("%Y%m%d%H%M%S"))}
        self.results: list[dict] = []
        self.api: ApiClient | None = None
        self.browser = None

    # ------------------------------------------------------------- 主流程
    def run(self) -> dict:
        meta = self.flow.get("meta", {})
        summary = {"flow": meta.get("name"), "role": meta.get("role"), "started_at": datetime.now().isoformat(timespec="seconds")}
        try:
            self._maybe_reset()
            self._healthcheck()
            self._login()
            for step in self.flow.get("steps", []):
                self._run_step(step)
                if self.results[-1]["status"] == "FAILED" and not step.get("continue_on_failure"):
                    break
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # 环境级致命错误（起不来/登录不了）单独记录
            self.results.append({"id": "_fatal", "type": "setup", "status": "FAILED",
                                 "error": f"{type(exc).__name__}: {exc}", "elapsed_ms": 0, "evidence": []})
        finally:
            if self.browser:
                self.browser.close()

        failed = [r for r in self.results if r["status"] == "FAILED"]
        skipped = [r for r in self.results if r["status"] == "SKIPPED"]
        summary.update(
            total=len(self.results), passed=len(self.results) - len(failed) - len(skipped),
            failed=len(failed), skipped=len(skipped),
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
        self._write_report(summary)
        return summary

    # ------------------------------------------------------------- 环境
    def _maybe_reset(self) -> None:
        reset = self.flow.get("reset")
        if not reset:
            return
        if os.getenv("ALLOW_RESET") != "1":
            raise RuntimeError("流程声明了 reset，但未设置 ALLOW_RESET=1（防止误删测试库）")
        for cmd in reset["commands"]:
            proc = subprocess.run(cmd, shell=True, cwd=reset.get("cwd"), capture_output=True, text=True, timeout=900)
            if proc.returncode != 0:
                raise RuntimeError(f"重置命令失败: {cmd}\n{proc.stderr[-800:]}")

    def _healthcheck(self) -> None:
        hc = self.flow.get("healthcheck")
        if not hc:
            return
        probe = ApiClient(render(hc.get("base_url", self.flow["env"]["base_url"]), self.variables),
                          auth_header=None)
        deadline = time.time() + hc.get("timeout", 180)
        interval = hc.get("interval", 5)
        last = "未发起探测"
        while time.time() < deadline:
            resp = probe.call({"method": "GET", "path": hc["path"]})
            if resp.error:
                last = resp.error
            else:
                cond = resp.status == hc.get("expect_status", 200)
                rules = hc.get("expect_json")
                if cond and rules:
                    cond = not eval_json_rules(resp, rules)
                if cond:
                    return
                last = f"status={resp.status} body={resp.text[:200]}"
            time.sleep(interval)
        raise RuntimeError(f"健康检查超时: {last}")

    def _login(self) -> None:
        auth = self.flow.get("auth")
        env = self.flow.get("env", {})
        # 鉴权头模板保持原样：${token} 由 ApiClient 在每次请求时替换（此刻尚未登录）
        self.api = ApiClient(
            render(env["base_url"], self.variables),
            auth_header=auth.get("header_template", {"Authorization": "Bearer ${token}"}) if auth else None,
        )
        if not auth:
            return
        login_spec = render(auth["login"], self.variables)
        token_path = login_spec.pop("token_path")        # 与请求字段同层声明，发送前摘除
        login_captures = login_spec.pop("captures", {})
        resp = self.api.call(login_spec)
        if resp.status >= 400 or resp.error:
            raise RuntimeError(f"登录失败: status={resp.status} {resp.error or resp.text[:300]}")
        self.api.token = str(resp.pick(token_path))
        if self.api.token == str(MISSING):
            raise RuntimeError(f"登录响应中找不到令牌路径 {token_path}")
        self.variables["token"] = self.api.token
        for var, path in login_captures.items():
            self.variables[var] = resp.pick(path)
        # 浏览器注入配置（值在首次打开页面前随变量渲染）
        self._inject_spec = render(auth.get("inject"), self.variables) if auth.get("inject") else None

    # ------------------------------------------------------------- 步骤
    def _run_step(self, step: dict) -> None:
        sid, stype = step["id"], step.get("type", "api")
        started = time.time()
        record = {"id": sid, "type": stype, "title": step.get("title", sid), "status": "PASSED",
                  "elapsed_ms": 0, "assertions": [], "evidence": [], "error": None}
        required_env = step.get("required_env")
        if required_env and not os.getenv(required_env):
            record["status"] = "SKIPPED"
            record["assertions"].append(f"缺少环境变量 {required_env}，按用例设计跳过本步骤")
            record["elapsed_ms"] = int((time.time() - started) * 1000)
            self.results.append(record)
            return
        try:
            if stype in ("api", "poll"):
                self._do_api_step(step, record)
            elif stype == "ui":
                self._do_ui_step(step, record)
            elif stype == "sql":
                self._do_sql_step(step, record)
            else:
                raise ValueError(f"未知步骤类型: {stype}")
        except Exception as exc:
            record["status"] = "FAILED"
            record["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            record["elapsed_ms"] = int((time.time() - started) * 1000)
            self.results.append(record)

    def _do_api_step(self, step: dict, record: dict) -> None:
        request_spec = render(step["request"], self.variables)
        retries = int(step.get("retries", 2))
        if step["type"] == "poll":
            resp = self._poll(request_spec, step, record)
        else:
            resp = self._call_with_retry(request_spec, retries)
        record["evidence"].append({"response_excerpt": resp.text[:1000], "http_status": resp.status,
                                   "elapsed_ms": resp.elapsed_ms})

        expect_failure = step.get("expect_failure", False)
        rules = render(step.get("assert", {}), self.variables)
        if expect_failure:
            rejected = resp.status >= 400 or (resp.json is not None and resp.pick("code") not in (MISSING, 0))
            if not rejected:
                raise AssertionError(f"预期失败用例却成功了: status={resp.status} body={resp.text[:200]}")
            record["assertions"].append("预期拒绝已发生")
        failures = eval_json_rules(resp, rules) if rules else []
        record["assertions"] += [f"FAIL: {f}" for f in failures]
        if failures:
            raise AssertionError("; ".join(failures))

        for var, path in step.get("capture", {}).items():
            value = resp.pick(path)
            if value is MISSING:
                raise AssertionError(f"capture 失败：响应中无路径 {path}")
            self.variables[var] = value

    def _call_with_retry(self, spec: dict, retries: int) -> HttpResponse:
        last = None
        for attempt in range(retries + 1):
            resp = self.api.call(spec)
            last = resp
            if not resp.error and resp.status not in RETRIABLE_HTTP:
                return resp
            time.sleep(1.5 * (attempt + 1))
        return last

    def _poll(self, spec: dict, step: dict, record: dict) -> HttpResponse:
        until = render(step["until"], self.variables)
        terminate = render(step.get("terminate"), self.variables)
        timeout = step.get("timeout", 180)
        interval = step.get("interval", 5)
        deadline = time.time() + timeout
        resp = None
        rounds = 0
        while time.time() < deadline:
            rounds += 1
            resp = self._call_with_retry(spec, retries=0)
            if not eval_json_rules(resp, until):
                record["assertions"].append(f"轮询 {rounds} 轮后满足条件")
                return resp
            if terminate and not eval_json_rules(resp, terminate):
                raise AssertionError(f"轮询命中终止条件: {terminal}; body={resp.text[:300]}")
            time.sleep(interval)
        raise AssertionError(f"轮询超时（{timeout}s/{rounds} 轮）: body={resp.text[:300] if resp else '无响应'}")

    def _do_ui_step(self, step: dict, record: dict) -> None:
        from browser import BrowserSession, DomCheckError

        if self.browser is None:
            self.browser = BrowserSession(
                self.api.base_url, inject=self._inject_spec, output_dir=self.out_dir,
                headless=os.getenv("HEADFUL") != "1",
            )
            self.browser.start()
        route = render(step["route"], self.variables)
        wait = render(step.get("wait", {}), self.variables)
        if float(wait.get("seconds", 0)) > 2:
            raise ValueError("渲染宽限 wait.seconds 不得超过 2s（请改用 selector/networkidle 条件等待）")
        self.browser.open(route, wait)

        failures = self.browser.check_dom(render(step.get("dom", []), self.variables))
        record["assertions"] += [f"DOM FAIL: {f}" for f in failures]
        if failures:
            shot = self.browser.screenshot(f"{step['id']}-FAILED.png")
            record["evidence"].append(shot)
            raise AssertionError("; ".join(failures))
        record["assertions"].append(f"DOM 断言 {len(step.get('dom', []))} 项通过")

        if step.get("screenshot"):
            evidence = self.browser.screenshot(
                step["screenshot"], mask=step.get("mask"), visual=render(step.get("visual"), self.variables)
            )
            record["evidence"].append(evidence)

    def _do_sql_step(self, step: dict, record: dict) -> None:
        import psycopg2  # 延迟导入：不用 DB 断言的项目无需安装驱动

        dsn = os.environ[step["dsn_env"]]
        query = render(step["query"], self.variables).strip()
        if not step.get("allow_write") and not re.match(r"(SELECT|WITH)\b", query, re.I):
            raise PermissionError("SQL 步骤默认只允许 SELECT/WITH；写操作需显式 allow_write: true")
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(query)
            if cur.description is None:
                return
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        expect = step.get("assert", {})
        failures = []
        if "row_count" in expect:
            op, val = _normalize(("row_count", expect["row_count"]))[1:]
            ok, detail = _op_check(len(rows), op, val)
            if not ok:
                failures.append(f"row_count: {detail}")
        for col, expected in expect.get("first_row", {}).items():
            actual = rows[0].get(col) if rows else MISSING
            ok, detail = _op_check(actual, "eq", expected)
            if not ok:
                failures.append(f"first_row.{col}: {detail}")
        for col in expect.get("non_null", []):
            if any(r.get(col) in (None, MISSING) for r in rows):
                failures.append(f"non_null: 列 {col} 存在空值")
        record["assertions"].append(f"SQL 返回 {len(rows)} 行")
        if failures:
            raise AssertionError("; ".join(failures))

    # ------------------------------------------------------------- 报告
    def _write_report(self, summary: dict) -> None:
        (self.out_dir / "result.json").write_text(
            json.dumps({"summary": summary, "steps": self.results}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        lines = [
            f"# 业务流程测试报告：{summary['flow']}",
            "",
            f"- 角色：{summary.get('role')}　开始：{summary['started_at']}　结束：{summary['finished_at']}",
            f"- 结果：**{summary['passed']}/{summary['total']} 通过，{summary['failed']} 失败**",
            "",
            "| # | 步骤 | 类型 | 耗时ms | 状态 | 断言/错误 |",
            "|---|---|---|---|---|---|",
        ]
        for i, r in enumerate(self.results, 1):
            detail = r.get("error") or "；".join(a for a in r["assertions"] if not a.startswith("FAIL"))
            lines.append(f"| {i} | {r['id']} | {r['type']} | {r['elapsed_ms']} | {r['status']} | {detail or ''} |")
        if summary["failed"]:
            lines += ["", "## 失败证据", ""]
            for r in self.results:
                if r["status"] != "FAILED":
                    continue
                lines.append(f"### {r['id']}")
                lines.append(f"- 错误：{r['error']}")
                for ev in r["evidence"]:
                    if "screenshot" in ev:
                        lines.append(f"- 截图：{ev['screenshot']}")
                    if "response_excerpt" in ev:
                        lines.append(f"- 响应：`{ev['response_excerpt']}`")
        (self.out_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="运行业务流程 .flow.yaml")
    ap.add_argument("flow", help=".flow.yaml 文件路径")
    ap.add_argument("--output", default="test-output", help="产物根目录")
    args = ap.parse_args()
    summary = FlowRunner(args.flow, args.output).run()
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"报告目录: {Path(args.output)}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
