"""CDP 输入翻译代理：桌面鼠标命令 → 真机触摸序列（P2-3）。

背景：触摸语义此前只覆盖本项目自己的 API / RPA 引擎（`mobile_input.py`）。
外部自动化工具（Selenium / Puppeteer / Playwright）直连 `ws_endpoint` 时仍会
`Input.dispatchMouseEvent`，页面上留下 pointerdown:mouse / mousedown / wheel
这类手机不存在的序列——行为型风控（Cloudflare / DataDome / 极验）最直接的
「手机壳桌面输入」证据。

本模块在调试端点前挂一层 WebSocket 代理：客户端连代理，代理连内核，消息
逐字转发；只有 `Input.dispatchMouseEvent` 被就地翻译成
`Input.dispatchTouchEvent`：

| 鼠标命令 | 触摸序列 | 说明 |
| --- | --- | --- |
| `mousePressed`（主键） | `touchStart` | 点击 / 拖拽 / 长按的起点 |
| `mouseMoved`（按住） | `touchMove` | 按住移动 = 拖拽，松手时内核按速度发起原生 fling |
| `mouseMoved`（未按住） | 丢弃 | 触屏没有 hover；原样转发会制造 mousemove |
| `mouseReleased` | `touchEnd` | 按住时长原样保留：长按（>500ms）由内核原生触发 |
| `mouseWheel` | `touchStart` + 若干 `touchMove` + `touchEnd` | 触屏没有滚轮；按下滚量换算成一次单指拖拽 |

内核会按真机语义自行补齐兼容鼠标事件（mousemove/mousedown/mouseup）与
`pointerType=touch` 的 click，因此翻译后的页面序列与真实触摸完全一致。

**应答必须由代理自己回**：实测（fp-chromium 148）内核的
`Emulation.setEmitTouchEventsForMouse` 转换会吞掉 mousePressed/Released 的
CDP 应答，Playwright 的鼠标点击会永久挂起。代理对每条鼠标命令都立即回
`{"result": {}}`，与内核处理触摸命令的顺序无关。

**为什么不直接用 `setEmitTouchEventsForMouse`**：除吞应答外，该转换产出的
序列缺兼容鼠标事件与 click，且无法处理 wheel（见 mobile_input.py 注释）。

代理只监听回环地址，且只在移动端环境上启用。
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
from typing import Any, Optional

import websockets

log = logging.getLogger(__name__)

_MAX_FRAME = 64 * 1024 * 1024
# 合成命令的 id 起点：客户端（Playwright/Puppeteer/Selenium）的命令 id 都是
# 小整数，错开一个量级即可避免撞号；撞号会让代理吞掉客户端的应答。
# 必须落在 int32 内——超过 2^31 时内核直接回
# "Message must have integer 'id' property"（实测）。
_SYNTH_ID_BASE = 1 << 28
_WHEEL_STEP_DELAY = 0.014   # 60fps 档，与 mobile_input.scroll 同口径
_WHEEL_MAX_TRAVEL = 400.0   # 单次滚轮的拖拽上限（避免拖出屏幕）


def _point(x: float, y: float, force: float, radius: float) -> dict[str, float]:
    return {"x": float(x), "y": float(y), "radiusX": float(radius),
            "radiusY": float(radius), "force": float(force), "id": 1}


class _Finger:
    """单指触摸的按下状态（按 CDP 会话隔离）。"""

    __slots__ = ("pressed", "x", "y")

    def __init__(self) -> None:
        self.pressed = False
        self.x = 0.0
        self.y = 0.0


class _Connection:
    """一条客户端连接：上游连接、合成命令的应答路由、每条会话的按压状态。"""

    def __init__(self, client: Any, upstream: Any) -> None:
        self.client = client
        self.upstream = upstream
        self.ids = itertools.count(_SYNTH_ID_BASE)
        self.pending: dict[int, asyncio.Future] = {}
        # 所有用过的合成 id：应答可能晚于等待超时到达，必须按号段
        # 全部吞掉——漏一条就会以陌生 id 转发给客户端，
        # Playwright 的 CRSession 会直接断言崩溃（实测）。
        self.synth: set[int] = set()
        self.fingers: dict[str, _Finger] = {}

    def finger(self, session_id: Optional[str]) -> _Finger:
        key = session_id or ""
        finger = self.fingers.get(key)
        if finger is None:
            finger = self.fingers[key] = _Finger()
        return finger

    async def send_up(self, method: str, params: dict,
                      session_id: Optional[str]) -> None:
        mid = next(self.ids)
        msg: dict[str, Any] = {"id": mid, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.synth.add(mid)
        self.pending[mid] = fut
        try:
            await self.upstream.send(json.dumps(msg))
            # 触摸命令的应答只用于暴露内核错误；超时不阻塞上游转发。
            await asyncio.wait_for(fut, 1.0)
        except Exception:  # noqa: BLE001
            pass
        finally:
            self.pending.pop(mid, None)


class CdpInputProxy:
    """移动端 CDP 输入代理（鼠标 → 触摸）。"""

    def __init__(self, upstream: str) -> None:
        self._upstream = upstream
        self._server: Any = None
        self._port = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self._port}/devtools/browser/mobile-input"

    async def start(self) -> str:
        self._server = await websockets.serve(
            self._handle, "127.0.0.1", 0,
            max_size=_MAX_FRAME, ping_interval=None)
        self._port = int(self._server.sockets[0].getsockname()[1])
        log.info("CDP 输入代理已启动: %s → %s", self.url, self._upstream)
        return self.url

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        try:
            await self._server.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        self._server = None

    # ---------------- 连接与转发 ----------------

    async def _handle(self, client: Any) -> None:
        try:
            upstream = await websockets.connect(
                self._upstream, max_size=_MAX_FRAME, ping_interval=None,
                proxy=None)
        except Exception as e:  # noqa: BLE001
            log.warning("CDP 输入代理无法连接上游：%s", e)
            await client.close(code=1011, reason="upstream unavailable")
            return
        conn = _Connection(client, upstream)
        back = asyncio.create_task(self._pump_upstream(conn))
        try:
            await self._pump_client(conn)
        except Exception as e:  # noqa: BLE001
            log.debug("输入代理客户端通道结束: %s", e)
        finally:
            back.cancel()
            for fut in conn.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("代理已关闭"))
            conn.pending.clear()
            for ws in (client, upstream):
                try:
                    await ws.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _pump_client(self, conn: _Connection) -> None:
        async for raw in conn.client:
            if isinstance(raw, bytes):
                await conn.upstream.send(raw)
                continue
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                await conn.upstream.send(raw)
                continue
            if msg.get("method") == "Input.dispatchMouseEvent":
                log.debug("代理收到鼠标命令 %s id=%s sid=%s", (msg.get("params") or {}).get("type"), msg.get("id"), msg.get("sessionId"))
                try:
                    await self._translate_mouse(conn, msg)
                except Exception as e:  # noqa: BLE001
                    log.debug("鼠标命令翻译失败: %s", e)
                if msg.get("id") is not None:
                    # 翻译与否都必须回包：内核吞应答时客户端会永久挂起。
                    # 回包必须带回 sessionId——扁平化会话按它路由应答，
                    # 丢了会落到根会话并按「未知 id」断言崩溃（Playwright 实测）。
                    reply: dict[str, Any] = {"id": msg["id"], "result": {}}
                    if msg.get("sessionId"):
                        reply["sessionId"] = msg["sessionId"]
                    await conn.client.send(json.dumps(reply))
                continue
            await conn.upstream.send(raw)

    async def _pump_upstream(self, conn: _Connection) -> None:
        try:
            async for raw in conn.upstream:
                if isinstance(raw, bytes):
                    await conn.client.send(raw)
                    continue
                mid = None
                method = None
                try:
                    parsed = json.loads(raw) or {}
                    mid = parsed.get("id")
                    method = parsed.get("method")
                except Exception:  # noqa: BLE001
                    pass
                if mid is None and method is None:
                    # 既非应答也非事件（内核的解析错误等）：转发会让客户端
                    # 断言崩溃（Playwright CRSession 实测）
                    continue
                fut = conn.pending.pop(mid, None) if mid is not None else None
                if fut is not None:
                    if not fut.done():
                        fut.set_result(raw)   # 合成命令的应答不上行
                    continue
                if mid is not None and mid in conn.synth:
                    continue                  # 迟到的合成应答，同样不得上行
                await conn.client.send(raw)
        except Exception as e:  # noqa: BLE001
            log.debug("输入代理上游通道结束: %s", e)

    # ---------------- 鼠标 → 触摸 ----------------

    async def _translate_mouse(self, conn: _Connection, msg: dict) -> None:
        params = msg.get("params") or {}
        kind = params.get("type")
        sid = msg.get("sessionId")
        finger = conn.finger(sid)
        x = float(params.get("x") or 0.0)
        y = float(params.get("y") or 0.0)
        button = str(params.get("button") or "none")
        buttons = int(params.get("buttons") or 0)

        if kind == "mousePressed":
            if button not in ("left", "none", ""):
                # 其他键（中键/右键）没有触摸等价物：原样放行，不臆造语义
                await conn.upstream.send(json.dumps(msg))
                return
            await conn.send_up("Input.dispatchTouchEvent", {
                "type": "touchStart",
                "touchPoints": [_point(x, y, 0.35, 4.0)]}, sid)
            finger.pressed, finger.x, finger.y = True, x, y
            return

        if kind == "mouseMoved":
            # 未按住的移动是 hover：触屏没有 hover，转发反而制造 mousemove
            if not finger.pressed and not (buttons & 1):
                return
            finger.x, finger.y = x, y
            await conn.send_up("Input.dispatchTouchEvent", {
                "type": "touchMove",
                "touchPoints": [_point(x, y, 0.6, 6.0)]}, sid)
            return

        if kind == "mouseReleased":
            if not finger.pressed:
                return
            finger.pressed = False
            await conn.send_up("Input.dispatchTouchEvent",
                               {"type": "touchEnd", "touchPoints": []}, sid)
            return

        if kind == "mouseWheel":
            await self._wheel(conn, finger, sid, x, y,
                              float(params.get("deltaY") or 0.0))
            return

        # 其余类型（理论上不存在）原样放行
        await conn.upstream.send(json.dumps(msg))

    async def _wheel(self, conn: _Connection, finger: _Finger,
                     sid: Optional[str], x: float, y: float,
                     delta_y: float) -> None:
        """滚轮 → 单指拖拽（向下滚 = 手指上滑）。"""
        if finger.pressed:
            return
        travel = max(-_WHEEL_MAX_TRAVEL, min(_WHEEL_MAX_TRAVEL, delta_y))
        if abs(travel) < 1.0:
            return
        steps = max(4, int(round(abs(travel) / 25.0)))
        # 与 mobile_input.scroll 同一条速度剖面：起步慢、其后匀速，松手带 fling。
        # 起手点要留够行程：滚轮没有「手指位置」概念，若沿用鼠标当前位置
        # （常在视口顶部），向上拖拽会被 y≥0 截断、滚动量对不上 deltaY。
        start_y = max(y, min(abs(travel) + 40.0, 600.0))
        ramp = max(3, min(steps // 2, 8))
        speeds = [0.25 + 0.75 * (i / (ramp - 1)) if i < ramp else 1.0
                  for i in range(steps)]
        total = sum(speeds)
        await conn.send_up("Input.dispatchTouchEvent", {
            "type": "touchStart",
            "touchPoints": [_point(x, start_y, 0.35, 4.0)]}, sid)
        acc = 0.0
        for i, speed in enumerate(speeds):
            acc += speed / total
            ny = max(0.0, start_y - travel * acc)
            phase = (i + 1) / steps
            press = 0.35 + 0.35 * min(1.0, phase / 0.3)
            await asyncio.sleep(_WHEEL_STEP_DELAY)
            await conn.send_up("Input.dispatchTouchEvent", {
                "type": "touchMove",
                "touchPoints": [_point(x, ny, press, 4.0 + 9.0 * press)]}, sid)
        await conn.send_up("Input.dispatchTouchEvent",
                           {"type": "touchEnd", "touchPoints": []}, sid)
