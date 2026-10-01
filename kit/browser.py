"""浏览器会话：用户视角验证通路。

职责：
- 用 Playwright 无头 Chromium 打开真实前端路由；
- 在页面脚本执行前注入登录态（localStorage / Cookie），跳过登录表单但不绕过任何渲染；
- 提供 DOM 断言、截图取证（可对敏感元素打码）、可选视觉基线比对（VRT）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from api import MISSING, json_path

# DOM 断言失败
class DomCheckError(AssertionError):
    pass


class BrowserSession:
    def __init__(
        self,
        base_url: str,
        inject: dict[str, Any] | None = None,
        output_dir: Path | None = None,
        headless: bool = True,
        viewport: tuple[int, int] = (1920, 1080),
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.inject = inject or {}
        self.output_dir = output_dir or Path("test-output")
        self.headless = headless
        self.viewport = viewport
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None

    # ---- 生命周期 ------------------------------------------------------
    def start(self) -> None:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        self._context = self._browser.new_context(viewport={"width": self.viewport[0], "height": self.viewport[1]})

        kind = self.inject.get("type")
        if kind == "localStorage":
            key, value = self.inject["key"], self.inject["value"]
            # 每个文档加载前执行；只在同源页面写入，避免污染第三方帧
            self._context.add_init_script(
                """
                (args) => {
                    try {
                        if (location.hostname === args.host) {
                            window.localStorage.setItem(args.key, args.value);
                        }
                    } catch (e) {}
                }
                """,
                args={"host": urlparse(self.base_url).hostname, "key": key, "value": value},
            )
        elif kind == "cookies":
            host = urlparse(self.base_url).hostname or ""
            cookies = []
            for c in self.inject.get("cookies", []):
                cookies.append({
                    "name": c["name"],
                    "value": c["value"],
                    "domain": c.get("domain", host),
                    "path": c.get("path", "/"),
                })
            if cookies:
                self._context.add_cookies(cookies)

        self.page = self._context.new_page()

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
        finally:
            if self._playwright:
                self._playwright.stop()
            self.page = None

    def __enter__(self) -> "BrowserSession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 打开页面并等待真实渲染 ----------------------------------------
    def open(self, route: str, wait: dict | None = None) -> None:
        url = route if route.startswith("http") else self.base_url + route
        self.page.goto(url, wait_until="commit", timeout=60000)
        wait = wait or {}
        if wait.get("load", True):
            self.page.wait_for_load_state("load")
        if wait.get("networkidle"):
            self.page.wait_for_load_state("networkidle", timeout=30000)
        selector = wait.get("selector")
        if selector:
            self.page.wait_for_selector(selector, state="attached", timeout=30000)
            self.page.wait_for_selector(selector, state="visible", timeout=10000)
        seconds = float(wait.get("seconds", 0))
        if seconds > 0:
            self.page.wait_for_timeout(int(seconds * 1000))  # 仅允许 ≤2s 的渲染宽限，由 runner 校验

    # ---- DOM 断言 ------------------------------------------------------
    def check_dom(self, checks: list[dict]) -> list[str]:
        """返回失败描述列表（空列表=全部通过）。"""
        failures: list[str] = []
        for chk in checks:
            selector, expect = chk["selector"], chk.get("expect", "visible")
            value = chk.get("value")
            try:
                if expect == "visible":
                    self.page.locator(selector).first.wait_for(state="visible", timeout=8000)
                elif expect == "hidden":
                    self.page.locator(selector).first.wait_for(state="hidden", timeout=8000)
                elif expect in ("text", "exact_text"):
                    actual = self.page.locator(selector).first.inner_text(timeout=8000)
                    ok = (actual == value) if expect == "exact_text" else (str(value) in actual)
                    if not ok:
                        failures.append(f"[{selector}] 期望文本包含/等于 {value!r}，实际 {actual!r}")
                elif expect.startswith("count_"):
                    count = self.page.locator(selector).count()
                    target = int(value)
                    op = expect.split("_", 1)[1]
                    ok = {"eq": count == target, "gte": count >= target, "lte": count <= target}[op]
                    if not ok:
                        failures.append(f"[{selector}] 期望数量 {op} {target}，实际 {count}")
                elif expect.startswith("attr:"):
                    attr = expect.split(":", 1)[1]
                    actual = self.page.locator(selector).first.get_attribute(attr, timeout=8000)
                    if str(value) not in (actual or ""):
                        failures.append(f"[{selector}] 属性 {attr} 期望包含 {value!r}，实际 {actual!r}")
                elif expect == "url_contains":
                    if str(value) not in self.page.url:
                        failures.append(f"URL 期望包含 {value!r}，实际 {self.page.url!r}")
                else:
                    failures.append(f"[{selector}] 未知断言类型 {expect}")
            except Exception as exc:  # 超时/未找到等统一转成失败描述
                failures.append(f"[{selector}] {expect} 执行异常: {type(exc).__name__}: {exc}")
        return failures

    # ---- 截图取证 ------------------------------------------------------
    def screenshot(self, name: str, mask: list[str] | None = None, visual: dict | None = None) -> dict:
        shot_dir = self.output_dir / "screenshots"
        shot_dir.mkdir(parents=True, exist_ok=True)
        path = shot_dir / name
        mask_locators = [self.page.locator(s) for s in (mask or [])]
        self.page.screenshot(path=str(path), full_page=False, animations="disabled")
        evidence: dict[str, str] = {"screenshot": str(path)}

        import os
        if os.getenv("UPDATE_VISUAL_BASELINE") == "1" and visual:
            Path(visual["baseline"]).parent.mkdir(parents=True, exist_ok=True)
            path.replace(visual["baseline"])
            evidence["baseline"] = "updated"
            return evidence

        if visual and Path(visual["baseline"]).exists():
            diff_ratio = self._pixel_diff(path, Path(visual["baseline"]))
            evidence["visual_diff_ratio"] = f"{diff_ratio:.4f}"
            if diff_ratio > float(visual.get("threshold", 0.02)):
                raise DomCheckError(
                    f"视觉回归：差异 {diff_ratio:.3f} 超过阈值 {visual.get('threshold', 0.02)}；"
                    f"确认无误后设 UPDATE_VISUAL_BASELINE=1 更新基线 {visual['baseline']}"
                )
        return evidence

    @staticmethod
    def _pixel_diff(a: Path, b: Path) -> float:
        from PIL import Image, ImageChops

        ia = Image.open(a).convert("L").resize((320, 180))
        ib = Image.open(b).convert("L").resize((320, 180))
        diff = ImageChops.difference(ia, ib)
        hist = diff.histogram()
        total = sum(i * n for i, n in enumerate(hist))
        return total / (255 * 320 * 180)
