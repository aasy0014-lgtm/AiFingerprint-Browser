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

拖拽剖面（速度/压力曲线）：真机触控拖拽是「起步慢 → 加速 → 匀速 → 松手」，
touchmove 的帧间隔与位移都随手指速度变化，接触面积（radiusX/Y）与压力
（force）也随按压过程起伏。固定等距等时的拖拽（每帧同位移、同 12ms）在
时间戳与 force 序列上是完全平坦的，是行为型风控的高级模型（速度剖面）能
直接读出的自动化痕迹。`scroll()` 因此按 S 形速度曲线分配每帧位移、按压力
曲线给出 force/radius，并叠加少量帧间抖动。

松手惯性（fling）：不需要仿真——CDP 派发的 touchmove 带真实时间戳，内核
按松手瞬间的速度自己发起原生 fling 动画。实测（fp-chromium 148，8 帧 300px）：
帧间隔 50ms → 松手后 0px；20ms → +219px；12ms → +205px；4ms → +84px。
本模块的速度曲线刻意落在 10~20ms/帧，与真机拖拽同档，松手即带惯性。
"""
from __future__ import annotations

import asyncio
import random
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
    def _point(x: float, y: float, force: float = 1.0,
               radius: float = 1.0) -> dict:
        return {"x": float(x), "y": float(y),
                "radiusX": float(radius), "radiusY": float(radius),
                "force": float(force), "id": 1}

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
                     steps: Optional[int] = None,
                     step_delay: float = 0.014,
                     force_peak: float = 0.85,
                     jitter: bool = True) -> None:
        """触摸拖拽滚动：页面向下滚 dy>0 像素（手指上滑）。

        每帧位移按「起步加速 → 匀速」的速度剖面分配（松手时仍在峰值速度，
        内核据此发起原生 fling）；force 与 radiusX/radiusY 按「按到峰值 →
        抬指前放松」的压力曲线起伏。

        step_delay 是帧间隔基准：内核把 touchmove 的时间戳按发送时刻记录，
        实测 0.014 时 event.timeStamp 间隔在 14~19ms 自然抖动，与真机采样
        同档；再大就会退到「每两帧一条」的 33ms 档。

        steps 缺省按位移自动取（约 20px/帧），步数越多剖面越接近真机采样。
        """
        cdp = await self._session()
        view = await self._page.evaluate("() => ({w: innerWidth, h: innerHeight})")
        width = float(view.get("w") or 360)
        height = float(view.get("h") or 640)
        start_x = min(max(float(x), 10.0), max(width - 10.0, 10.0))
        start_y = min(max(float(y), 10.0), max(height - 10.0, 10.0))
        # 拖拽终点限制在视口内（触控不能拖出屏幕；超长距离由调用方分多次滚动）
        end_y = min(max(start_y - float(dy), 10.0), max(height - 10.0, 10.0))
        travel = end_y - start_y
        if steps is None:
            steps = int(round(abs(travel) / 20.0))
        steps = max(int(steps), 3)
        rnd = random.Random()

        # 速度剖面：前一半帧线性加速，其后匀速——松手瞬间仍带速度。
        # 斜坡必须落在「绝对像素」上且足够长：内核按帧合并 touchmove，
        # 亚像素级的起步帧会被合并掉，剖面在页面上就变成一条平线（实测
        # 二次曲线在 40 帧时前 13 帧都 <1px，合并后首帧只比匀速段小 15%）。
        # 5 帧斜坡实测只留下两级台阶（合并后），拉到 10 帧才读得出完整的
        # 加速段：dy ≈ 0.5 → 1.0 倍匀速位移，逐帧递增。
        ramp = max(4, min(int(round(steps * 0.5)), 12))
        speeds = [0.15 + 0.85 * (i / (ramp - 1)) if i < ramp else 1.0
                  for i in range(steps)]
        total = sum(speeds)
        fracs, acc = [], 0.0
        for sp in speeds:
            acc += sp / total
            fracs.append(acc)
        fracs[-1] = 1.0
        mean_dy = abs(travel) / steps

        await cdp.send("Input.dispatchTouchEvent", {
            "type": "touchStart",
            "touchPoints": [self._point(start_x, start_y, 0.35, 4.0)]})
        loop = asyncio.get_running_loop()
        base = loop.time()
        for i, frac in enumerate(fracs):
            ny = start_y + travel * frac
            phase = (i + 1) / steps
            press = 0.35 + (force_peak - 0.35) * min(1.0, phase / 0.3)
            if phase > 0.75:  # 抬指前放松
                press -= (force_peak - 0.45) * ((phase - 0.75) / 0.25)
            radius = 4.0 + 9.0 * press
            px = start_x
            if jitter:
                press = min(1.0, max(0.1, press + rnd.uniform(-0.03, 0.03)))
                radius = max(2.0, radius + rnd.uniform(-0.6, 0.6))
                # 位移抖动与当前帧速同量级，但不能盖过起步段的差异
                ny += rnd.uniform(-0.35, 0.35) * min(1.0, mean_dy / 20.0)
                px += rnd.uniform(-0.6, 0.6)
            if step_delay:
                # 按序号排程而非「距上次发送 + 抖动」：抖动量只作用于位移/
                # 压力，不加到时间轴上，否则相位会一路漂移。
                due = base + (i + 1) * float(step_delay)
                await asyncio.sleep(max(0.0, due - loop.time()))
            await cdp.send("Input.dispatchTouchEvent", {
                "type": "touchMove",
                "touchPoints": [self._point(px, ny, press, radius)]})
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
                       y: Optional[float] = None,
                       steps: Optional[int] = None) -> None:
    """触摸滚动页面 amount 像素（正数向下；默认从视口下半部起手）。"""
    if x is None or y is None:
        view = await page.evaluate("() => ({w: innerWidth, h: innerHeight})")
        x = (float(view.get("w") or 360)) / 2 if x is None else x
        y = (float(view.get("h") or 640)) * 0.72 if y is None else y
    await touch_input(page).scroll(x, y, amount, steps=steps)
