"""移动端输入层（真机语义）：触摸点击与触摸滚动。

真机（Android/iOS）上不存在桌面鼠标输入：页面点击来自触摸（touchStart →
touchEnd，随后是兼容鼠标事件与 pointerType=touch 的 click），滚动来自触摸
拖拽（touchmove 序列）。桌面内核的 Playwright 鼠标 API（click / wheel）在
移动端环境里会产出 pointerdown:mouse / mousedown / wheel 等桌面序列，是行为型
风控（Cloudflare/DataDome/极验等）最直接的「手机壳桌面输入」证据。

实现路径：CDP `Input.dispatchTouchEvent`（Playwright 的 page.touchscreen 在
connect_over_cdp 的默认上下文不可用——上下文无 hasTouch）。

不启用 `Emulation.setEmitTouchEventsForMouse`：实测（fp-chromium 148）该转换
会吞掉 mousePressed/Released 的 CDP 应答，Playwright 鼠标点击永久挂起；且转换
产出的序列缺兼容鼠标事件与 click。详见 docs/移动端大厂检测对抗优化方案.md。

边界：触摸拖拽不含系统级惯性（fling）与压力曲线；对常规风控的行为面
（事件类型/序列/坐标轨迹）已与真机同构。
"""
from __future__ import annotations

import asyncio
import weakref
from typing import Any, Optional

MOBILE_TARGETS = ("android", "ios")


def is_mobile_target(target_os: Optional[str]) -> bool:
    """target_os 是否为移动端（Android/iOS，fp-chromium 仿真口径）。"""
    return str(target_os or "") in MOBILE_TARGETS


def is_mobile_profile(profile: Optional[dict[str, Any]]) -> bool:
    """环境档案是否为移动端（按 target_os 判定）。"""
    return bool(profile) and is_mobile_target(profile.get("target_os"))


class TouchInput:
    """按 page 复用一条 CDP 会话的触摸派发器（惰性建立连接）。"""

    def __init__(self, page) -> None:
        self._page = page
        self._cdp = None

    async def _session(self):
        if self._cdp is None:
            self._cdp = await self._page.context.new_cdp_session(self._page)
        return self._cdp

    @staticmethod
    def _point(x: float, y: float) -> dict:
        return {"x": float(x), "y": float(y),
                "radiusX": 1, "radiusY": 1, "force": 1, "id": 1}

    async def tap(self, x: float, y: float, hold_ms: int = 60) -> None:
        """单指点击：touchStart →（短暂按住）→ touchEnd。

        Chromium 会按真机语义补齐兼容鼠标事件（mousemove/mousedown/mouseup）
        与 pointerType=touch 的 click。
        """
        cdp = await self._session()
        await cdp.send("Input.dispatchTouchEvent", {
            "type": "touchStart", "touchPoints": [self._point(x, y)]})
        await asyncio.sleep(max(int(hold_ms), 10) / 1000)
        await cdp.send("Input.dispatchTouchEvent", {
            "type": "touchEnd", "touchPoints": []})

    async def scroll(self, x: float, y: float, dy: float,
                     steps: int = 8, step_delay: float = 0.012) -> None:
        """触摸拖拽滚动：页面向下滚 dy>0 像素（手指上滑）。"""
        cdp = await self._session()
        view = await self._page.evaluate("() => ({w: innerWidth, h: innerHeight})")
        width = float(view.get("w") or 360)
        height = float(view.get("h") or 640)
        start_x = min(max(float(x), 10.0), max(width - 10.0, 10.0))
        start_y = min(max(float(y), 10.0), max(height - 10.0, 10.0))
        # 拖拽终点限制在视口内（触控不能拖出屏幕；超长距离由调用方分多次滚动）
        end_y = min(max(start_y - float(dy), 10.0), max(height - 10.0, 10.0))
        steps = max(int(steps), 2)
        await cdp.send("Input.dispatchTouchEvent", {
            "type": "touchStart", "touchPoints": [self._point(start_x, start_y)]})
        for i in range(1, steps + 1):
            ny = start_y + (end_y - start_y) * i / steps
            await cdp.send("Input.dispatchTouchEvent", {
                "type": "touchMove", "touchPoints": [self._point(start_x, ny)]})
            if step_delay:
                await asyncio.sleep(float(step_delay))
        await cdp.send("Input.dispatchTouchEvent", {
            "type": "touchEnd", "touchPoints": []})


_TOUCH_INPUTS: "weakref.WeakKeyDictionary[Any, TouchInput]" = weakref.WeakKeyDictionary()


def touch_input(page) -> TouchInput:
    """取该 page 的 TouchInput（按 page 复用 CDP 会话）。"""
    ti = _TOUCH_INPUTS.get(page)
    if ti is None:
        ti = TouchInput(page)
        _TOUCH_INPUTS[page] = ti
    return ti


async def tap_locator(page, locator, timeout: Optional[float] = None) -> None:
    """触摸点击一个元素（真机语义；图形中心点，越界时钳制在视口内）。"""
    await locator.scroll_into_view_if_needed(timeout=timeout)
    box = await locator.bounding_box()
    if not box:
        raise RuntimeError("元素无可点击区域（bounding_box 为空）")
    view = await page.evaluate("() => ({w: innerWidth, h: innerHeight})")
    width = float(view.get("w") or 360)
    height = float(view.get("h") or 640)
    x = float(box["x"]) + float(box["width"]) / 2
    y = float(box["y"]) + float(box["height"]) / 2
    x = min(max(x, 8.0), max(width - 8.0, 8.0))
    y = min(max(y, 8.0), max(height - 8.0, 8.0))
    await touch_input(page).tap(x, y)


async def touch_scroll(page, amount: float,
                       x: Optional[float] = None,
                       y: Optional[float] = None) -> None:
    """触摸滚动页面 amount 像素（正数向下；默认从视口下半部起手）。"""
    if x is None or y is None:
        view = await page.evaluate("() => ({w: innerWidth, h: innerHeight})")
        x = (float(view.get("w") or 360)) / 2 if x is None else x
        y = (float(view.get("h") or 640)) * 0.72 if y is None else y
    await touch_input(page).scroll(x, y, amount)
