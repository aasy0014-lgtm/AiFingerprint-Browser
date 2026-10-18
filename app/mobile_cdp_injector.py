"""移动端 CDP 仿真会话（持久连接）

为 fp-chromium（桌面 Chromium 内核）建立移动设备仿真，使页面呈现
真实移动端环境（Android/iOS）：

- Viewport / screen.width/height / DevicePixelRatio（CDP 设备仿真）
- Touch Emulation（多点触控，maxTouchPoints）
- User-Agent / navigator.platform 覆盖
- navigator.mobile、userAgentData.mobile 等 JS 层字段（init 脚本）

关键设计：**仿真设置随 CDP 客户端断开会被浏览器还原**（与 DevTools
设备窗口关闭后恢复同理）。因此本模块维护一条常驻的轻量原生 WebSocket
连接（不引入 Playwright/Node 进程），并把仿真应用到：

1. 连接时已存在的页面 target；
2. 监听 Target 自动挂载事件，后续新开的标签页同样应用仿真。

环境停止时调用 close() 显式断开。

像素语义：指纹库中的 screen 是物理分辨率（如 1440x3120），
浏览器可见的 screen.width/innerWidth 是 CSS 逻辑像素（物理 / DPR），
setDeviceMetricsOverride 的参数按 CSS 逻辑像素传。
"""
import asyncio
import contextlib
import itertools
import json
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


class CDPError(RuntimeError):
    """CDP 协议返回的 error 对象。"""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


def _css_size(physical: Optional[dict], dpr: float) -> tuple[int, int]:
    """物理分辨率 → CSS 逻辑像素（如 1440x3120 @2.6 → 554x1200）。"""
    width = (physical or {}).get("width", 0) or 0
    height = (physical or {}).get("height", 0) or 0
    scale = dpr if dpr and dpr > 0 else 1.0
    return round(width / scale), round(height / scale)


def js_mobile_patch(max_touch_points: int, platform: str) -> str:
    """JS 层字段补丁：改 Navigator.prototype（实例上不留 own property）。

    platform 在该内核的原生伪装里只有桌面取值（Linux x86_64 / MacIntel 等），
    移动端取值必须在这里覆盖；maxTouchPoints 同样兜底。
    """
    platform_js = "null" if not platform else json.dumps(platform)
    return f"""
    (function() {{
        try {{
            var def = function (proto, name, value) {{
                Object.defineProperty(proto, name, {{
                    get: function () {{ return value; }},
                    configurable: true,
                }});
            }};
            def(Navigator.prototype, 'mobile', true);
            def(Navigator.prototype, 'maxTouchPoints', {max_touch_points});
            if ({platform_js}) {{
                def(Navigator.prototype, 'platform', {platform_js});
            }}
            if (navigator.userAgentData && navigator.userAgentData.mobile !== true) {{
                try {{ def(Object.getPrototypeOf(navigator.userAgentData), 'mobile', true); }}
                catch (e) {{}}
            }}
        }} catch (e) {{}}
    }})();
    """


class MobileEmulationSession:
    """一条常驻 CDP 连接上的移动端设备仿真。

    用法：
        session = MobileEmulationSession(ws_endpoint, viewport=..., dpr=...,
                                         ua=..., platform=...)
        await session.start()   # 连接 + 应用仿真 + 导航到 start_url
        ...
        await session.close()   # 环境停止时
    """

    def __init__(
        self,
        ws_endpoint: str,
        *,
        viewport: Optional[dict] = None,
        dpr: float = 1.0,
        ua: str = "",
        platform: str = "",
        max_touch_points: int = 10,
        locale: str = "",
        start_url: str = "",
    ) -> None:
        self.ws_endpoint = ws_endpoint
        self.viewport = viewport or {}
        self.dpr = dpr if dpr and dpr > 0 else 1.0
        self.ua = ua
        self.platform = platform
        self.max_touch_points = int(max_touch_points or 10)
        self.locale = locale
        self.start_url = start_url or ""

        self._ws = None
        self._reader: Optional[asyncio.Task] = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._applied_targets: set[str] = set()
        self._attach_tasks: dict[str, asyncio.Task] = {}
        self._first_page_session: Optional[str] = None
        self._closed = False

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        """连接浏览器调试端口并应用仿真；失败会自行清理。"""
        import websockets

        css_w, css_h = _css_size(self.viewport, self.dpr)
        if not css_w or not css_h:
            raise ValueError("移动端仿真缺少有效屏幕尺寸")

        self._ws = await websockets.connect(
            self.ws_endpoint, max_size=64 * 1024 * 1024, ping_interval=None,
        )
        self._reader = asyncio.create_task(self._read_loop())

        # 监听后续新开的标签页 / 页面
        await self._send("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": False,
            "flatten": True,
        })

        # 已存在的页面
        targets = (await self._send("Target.getTargets")).get("targetInfos", [])
        for info in targets:
            if info.get("type") == "page":
                await self._attach_page(info.get("targetId", ""))

        # 应用完成后导航到目标页（首个真实页面加载前指纹已生效）
        if self.start_url and self.start_url != "about:blank":
            await self._navigate_start_url()

    async def close(self) -> None:
        self._closed = True
        if self._reader:
            self._reader.cancel()
            # CancelledError 是 BaseException：等待被 cancel 的读取任务
            # 必须显式压制，否则会从 close() 冒泡出去把停止流程打断
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        if self._ws is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ws.close()
            self._ws = None

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._closed

    # ---------------- CDP 传输层 ----------------

    async def _send(self, method: str, params: Optional[dict] = None,
                    session_id: Optional[str] = None, timeout: float = 15.0) -> dict:
        if self._ws is None or self._closed:
            raise CDPError(-1, "CDP 会话已关闭")
        mid = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        msg: dict[str, Any] = {"id": mid, "method": method}
        if params:
            msg["params"] = params
        if session_id:
            msg["sessionId"] = session_id
        await self._ws.send(json.dumps(msg))
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(mid, None)

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                mid = msg.get("id")
                if mid is not None:
                    fut = self._pending.pop(mid, None)
                    if fut and not fut.done():
                        if "error" in msg:
                            err = msg["error"] or {}
                            fut.set_exception(CDPError(
                                err.get("code", -1), err.get("message", "unknown"),
                                err.get("data")))
                        else:
                            fut.set_result(msg.get("result") or {})
                elif msg.get("method") == "Target.attachedToTarget":
                    params = msg.get("params") or {}
                    info = params.get("targetInfo") or {}
                    sid = params.get("sessionId")
                    if not sid:
                        continue
                    if info.get("type") == "page":
                        asyncio.create_task(self._attach_page(
                            info.get("targetId", ""), session_id=sid))
                    elif info.get("type") == "tab":
                        # 新标签页（page 的父 target）：递归挂载其下的页面
                        asyncio.create_task(self._autoattach_tab(sid))
        except Exception:
            pass  # 连接关闭/取消
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CDPError(-1, "CDP 连接已断开"))
            self._pending.clear()

    # ---------------- 仿真应用 ----------------

    async def _autoattach_tab(self, tab_session_id: str) -> None:
        with contextlib.suppress(Exception):
            await self._send("Target.setAutoAttach", {
                "autoAttach": True,
                "waitForDebuggerOnStart": False,
                "flatten": True,
            }, session_id=tab_session_id)

    async def _attach_page(self, target_id: str, session_id: Optional[str] = None) -> None:
        if not target_id:
            return
        # 同一页面可能被并发挂载（自动挂载事件 vs start() 的初始遍历）：
        # 后到者必须等待先到者的仿真应用完成，否则 start() 可能在
        # addScriptToEvaluateOnNewDocument / setUserAgentOverride 等步骤
        # 尚未执行完时就发起 start_url 导航，导致新文档丢失移动端补丁。
        existing = self._attach_tasks.get(target_id)
        if existing is not None and existing is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await existing
            return
        if target_id in self._applied_targets:
            return
        current = asyncio.current_task()
        self._attach_tasks[target_id] = current
        self._applied_targets.add(target_id)
        try:
            if not session_id:
                res = await self._send("Target.attachToTarget",
                                       {"targetId": target_id, "flatten": True})
                session_id = res.get("sessionId")
            if not session_id:
                self._applied_targets.discard(target_id)
                return
            if self._first_page_session is None:
                self._first_page_session = session_id
            await self._apply_device_emulation(session_id)
        except Exception as e:
            log.debug("移动端仿真挂载页面失败: %s", e)
            self._applied_targets.discard(target_id)
        finally:
            if current is not None and self._attach_tasks.get(target_id) is current:
                self._attach_tasks.pop(target_id, None)

    async def _apply_device_emulation(self, sid: str) -> None:
        css_w, css_h = _css_size(self.viewport, self.dpr)

        # 0. 启用 Page 域：未启用时 addScriptToEvaluateOnNewDocument 虽然返回
        #    identifier，但 init 脚本不会真正挂载到后续文档（导航后的新文档
        #    丢失 JS 补丁：platform/mobile 等字段仍是内核原生值）
        with contextlib.suppress(CDPError):
            await self._send("Page.enable", {}, session_id=sid)

        # 1. 设备度量：viewport + screen + DPR + 移动布局（CSS 逻辑像素）
        full: dict[str, Any] = {
            "width": css_w,
            "height": css_h,
            "deviceScaleFactor": self.dpr,
            "mobile": True,
            "screenWidth": css_w,
            "screenHeight": css_h,
            "screenOrientation": {"type": "portraitPrimary", "angle": 0},
        }
        for drop in (None, "screenOrientation", ("screenWidth", "screenHeight")):
            params = dict(full)
            if drop:
                for key in ((drop,) if isinstance(drop, str) else drop):
                    params.pop(key, None)
            try:
                await self._send("Emulation.setDeviceMetricsOverride", params,
                                 session_id=sid)
                break
            except CDPError:
                continue

        # 2. 触控仿真（原生影响 navigator.maxTouchPoints 与 pointer 事件）
        with contextlib.suppress(CDPError):
            await self._send("Emulation.setTouchEmulationEnabled", {
                "enabled": True,
                "maxTouchPoints": self.max_touch_points,
            }, session_id=sid)

        # 3. UA 覆盖（内核层只会按桌面平台拼 UA，必须显式覆盖为移动端 UA）
        if self.ua:
            ua_params: dict[str, Any] = {"userAgent": self.ua}
            if self.platform:
                ua_params["platform"] = self.platform
            if self.locale:
                ua_params["acceptLanguage"] = self.locale
            try:
                await self._send("Emulation.setUserAgentOverride", ua_params,
                                 session_id=sid)
            except CDPError:
                with contextlib.suppress(CDPError):
                    await self._send("Emulation.setUserAgentOverride",
                                     {"userAgent": self.ua}, session_id=sid)

        # 4. JS 层字段：init 脚本对后续导航生效；当前文档立即执行一次
        patch = js_mobile_patch(self.max_touch_points, self.platform)
        with contextlib.suppress(CDPError):
            await self._send("Page.addScriptToEvaluateOnNewDocument",
                             {"source": patch}, session_id=sid)
        with contextlib.suppress(CDPError):
            await self._send("Runtime.evaluate", {"expression": patch},
                             session_id=sid)

    async def _navigate_start_url(self) -> None:
        sid = self._first_page_session
        if not sid:
            return
        try:
            await self._send("Page.navigate", {"url": self.start_url}, session_id=sid)
        except CDPError as e:
            log.debug("移动端 start_url 导航失败: %s", e)
