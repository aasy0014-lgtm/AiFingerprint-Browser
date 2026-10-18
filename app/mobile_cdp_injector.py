"""移动端 CDP 仿真会话（持久连接）

为 fp-chromium（桌面 Chromium 内核）建立移动设备仿真，使页面呈现
真实移动端环境（Android/iOS）。所有取值来自指纹库生成的 fingerprint
数据（app/mobile_fingerprints.py），保证「存储的指纹 = 运行期实际值」：

- 视口 / screen.width/height / DevicePixelRatio（CDP 设备仿真）
  首屏视口高度 < 屏幕高度（真机浏览器 UI 占位，innerHeight == screen.height
  是移动端仿真最常见的穿帮点）
- Touch Emulation（多点触控，maxTouchPoints）
- User-Agent / navigator.platform 覆盖
- WebGL 非遮蔽渲染器/厂商（内核原生 GPU 伪装不支持 Android/iOS 平台，
  不覆盖会直接暴露宿主机的 GPU 字符串）
- Android：navigator.userAgentData（UA-CH，含机型/系统版本等高熵值）、
  navigator.deviceMemory、Battery API、Sec-CH-UA* 请求头
- iOS：抹掉 Safari 不存在的属性（navigator.connection、pdfViewerEnabled、
  userAgentData），plugins/mimeTypes 置空，navigator.vendor=Apple
- screen.colorDepth/pixelDepth = 24（宿主 macOS 报告 30，是明显的穿帮点）

关键设计：**仿真设置随 CDP 客户端断开会被浏览器还原**（与 DevTools
设备窗口关闭后恢复同理）。因此本模块维护一条常驻的轻量原生 WebSocket
连接（不引入 Playwright/Node 进程），并把仿真应用到：

1. 连接时已存在的页面 target；
2. 监听 Target 自动挂载事件，后续新开的标签页同样应用仿真。

环境停止时调用 close() 显式断开。

像素语义：指纹库 screen 里 width/height 是物理分辨率（如 1440x3120），
cssWidth/cssHeight 是浏览器可见的 CSS 逻辑像素（物理 / DPR），
cssViewportHeight 是首屏视口高度；setDeviceMetricsOverride 的
width/height 按视口传、screenWidth/screenHeight 按屏幕传。
"""
import asyncio
import contextlib
import itertools
import json
import logging
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# iOS Safari 不存在、需从桌面 Chromium 上抹掉的 Navigator 属性
# （getBattery/deviceMemory 是桌面 Chrome 的原生 API，Safari 两者都没有）
_IOS_REMOVE_PROPS = ["connection", "pdfViewerEnabled", "userAgentData", "getBattery", "deviceMemory"]


class CDPError(RuntimeError):
    """CDP 协议返回的 error 对象。"""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


# ------------------------------------------------------------------ 规格提取
def _memory_bucket(value: Any) -> Optional[int]:
    """规范化 deviceMemory 上报值（Chrome 只报 1/2/4/8 这类桶）。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    for cap in (8, 4, 2, 1):
        if n >= cap:
            return cap
    return 1


def derive_spec(fingerprint: dict, target_os: str) -> Dict[str, Any]:
    """从存储的移动端 fingerprint 提取运行期仿真规格（兼容旧版结构）。"""
    fp = fingerprint or {}
    nav = fp.get("navigator") or {}
    screen = fp.get("screen") or {}
    extra = fp.get("mobileExtra") or {}

    dpr = float(screen.get("devicePixelRatio") or 1.0) or 1.0
    phys_w = int(screen.get("width") or 0)
    phys_h = int(screen.get("height") or 0)
    # 新结构直接读 CSS 字段；旧结构按物理 / DPR 推算
    css_w = int(screen.get("cssWidth") or (round(phys_w / dpr) if phys_w else 0))
    css_h = int(screen.get("cssHeight") or (round(phys_h / dpr) if phys_h else 0))
    if not (css_w and css_h):
        raise ValueError("移动端仿真缺少有效屏幕尺寸")
    vp_h = int(screen.get("cssViewportHeight") or css_h)
    if not 0.5 * css_h <= vp_h <= css_h:
        vp_h = css_h

    vc = fp.get("videoCard") or {}
    renderer = str(vc.get("renderer") or "")
    webgl = {"vendor": str(vc.get("vendor") or ""), "renderer": renderer} if renderer else None

    ua_ch = extra.get("uaCh") or None

    battery = fp.get("battery") if (target_os == "android" and fp.get("battery")) else None

    device_memory = None
    if target_os == "android":
        device_memory = _memory_bucket(extra.get("memoryClass") or nav.get("deviceMemory"))

    remove_props = extra.get("removeNavigatorProps")
    if remove_props is None:
        # 旧版 iOS 指纹无该字段：按 iOS Safari 的真实属性差异兜底
        remove_props = list(_IOS_REMOVE_PROPS) if target_os == "ios" else []
    empty_plugins = bool(extra.get("emptyPlugins", target_os == "ios"))

    color_depth = screen.get("colorDepth")
    try:
        color_depth = int(color_depth)
    except (TypeError, ValueError):
        color_depth = 0

    return {
        "ua": str(nav.get("userAgent") or ""),
        "platform": str(nav.get("platform") or ""),
        "vendor": str(nav.get("vendor") or ""),
        "maxTouchPoints": int(nav.get("maxTouchPoints") or 5),
        "deviceMemory": device_memory,
        "colorDepth": color_depth or None,
        "uaCh": ua_ch,
        "battery": battery,
        "removeProps": list(remove_props),
        "emptyPlugins": empty_plugins,
        "ios": target_os == "ios",
        "webgl": webgl,
        "cssWidth": css_w,
        "cssHeight": css_h,
        "viewportHeight": vp_h,
        "dpr": dpr,
    }


def sec_ch_headers(ua_ch: Dict[str, Any]) -> Dict[str, str]:
    """UA-CH 低熵请求头（Chrome 默认对所有站点发送的三个提示）。"""
    brands = ", ".join(f'"{b["brand"]}";v="{b["version"]}"' for b in (ua_ch.get("brands") or []))
    return {
        "Sec-CH-UA": brands,
        "Sec-CH-UA-Mobile": "?1" if ua_ch.get("mobile") else "?0",
        "Sec-CH-UA-Platform": f'"{ua_ch.get("platform") or "Android"}"',
    }


# ------------------------------------------------------------------ JS 补丁
_PATCH_TEMPLATE = r"""
(function () {
    var D;
    try { D = __SPEC__; } catch (e) { return; }
    var defineGet = function (proto, name, value) {
        try {
            Object.defineProperty(proto, name, {
                get: function () { return value; },
                configurable: true,
            });
            return true;
        } catch (e) { return false; }
    };
    var defineFn = function (proto, name, fn) {
        try {
            Object.defineProperty(proto, name, {
                value: fn, writable: true, configurable: true,
            });
            return true;
        } catch (e) { return false; }
    };
    var removeProp = function (name) {
        try { delete Navigator.prototype[name]; } catch (e) {}
        try { delete navigator[name]; } catch (e) {}
        if (name in navigator) {
            defineGet(Navigator.prototype, name, undefined);
        }
    };

    try {
        var N = Navigator.prototype;

        // ---- navigator 基础字段（原型级定义，实例上不留 own property）----
        defineGet(N, 'maxTouchPoints', D.maxTouchPoints);
        if (D.platform) { defineGet(N, 'platform', D.platform); }
        if (D.vendor) { defineGet(N, 'vendor', D.vendor); }
        if (D.deviceMemory !== null && D.deviceMemory !== undefined) {
            defineGet(N, 'deviceMemory', D.deviceMemory);
        }
        if (D.ios) {
            // iOS Safari 专有：standalone（非独立 PWA 模式恒为 false）
            defineGet(N, 'standalone', false);
        }

        // ---- 抹掉该平台不存在的桌面 Chromium 属性（如 iOS 的 connection）----
        var drops = D.removeProps || [];
        for (var i = 0; i < drops.length; i++) {
            removeProp(drops[i]);
        }

        // ---- iOS Safari：plugins / mimeTypes 为空 ----
        if (D.emptyPlugins && typeof PluginArray !== 'undefined') {
            try {
                var emptyPlugins = Object.create(PluginArray.prototype);
                Object.defineProperty(emptyPlugins, 'length', { value: 0 });
                Object.defineProperty(emptyPlugins, 'item', { value: function () { return null; } });
                Object.defineProperty(emptyPlugins, 'namedItem', { value: function () { return null; } });
                Object.defineProperty(emptyPlugins, 'refresh', { value: function () {} });
                defineGet(N, 'plugins', emptyPlugins);
            } catch (e) {}
            try {
                if (typeof MimeTypeArray !== 'undefined') {
                    var emptyMimes = Object.create(MimeTypeArray.prototype);
                    Object.defineProperty(emptyMimes, 'length', { value: 0 });
                    Object.defineProperty(emptyMimes, 'item', { value: function () { return null; } });
                    Object.defineProperty(emptyMimes, 'namedItem', { value: function () { return null; } });
                    defineGet(N, 'mimeTypes', emptyMimes);
                }
            } catch (e) {}
        }

        // ---- Android Chrome：UA-CH（navigator.userAgentData）----
        if (D.uaCh) {
            try {
                var ch = D.uaCh;
                var he = {
                    architecture: ch.architecture,
                    bitness: ch.bitness,
                    fullVersionList: ch.fullVersionList,
                    model: ch.model,
                    platformVersion: ch.platformVersion,
                    uaFullVersion: ch.fullVersion,
                    wow64: ch.wow64
                };
                var base = { brands: ch.brands, mobile: ch.mobile, platform: ch.platform };
                var proto = (typeof NavigatorUAData !== 'undefined') ? NavigatorUAData.prototype : null;
                var uaData = proto ? Object.create(proto) : {};
                Object.defineProperty(uaData, 'brands', { get: function () { return base.brands; } });
                Object.defineProperty(uaData, 'mobile', { get: function () { return base.mobile; } });
                Object.defineProperty(uaData, 'platform', { get: function () { return base.platform; } });
                Object.defineProperty(uaData, 'getHighEntropyValues', {
                    value: function (hints) {
                        var out = { brands: base.brands, mobile: base.mobile, platform: base.platform };
                        (hints || []).forEach(function (h) {
                            if (Object.prototype.hasOwnProperty.call(he, h)) { out[h] = he[h]; }
                        });
                        return Promise.resolve(out);
                    }
                });
                Object.defineProperty(uaData, 'toJSON', {
                    value: function () { return { brands: base.brands, mobile: base.mobile, platform: base.platform }; }
                });
                defineGet(N, 'userAgentData', uaData);
            } catch (e) {}
        }

        // ---- Android Chrome：Battery API ----
        if (D.battery) {
            try {
                var B = D.battery;
                var bProto = (typeof BatteryManager !== 'undefined') ? BatteryManager.prototype : EventTarget.prototype;
                var bm = Object.create(bProto);
                var bdef = function (name, value) {
                    try { Object.defineProperty(bm, name, { get: function () { return value; }, configurable: true }); } catch (e) {}
                };
                bdef('charging', !!B.charging);
                bdef('chargingTime', B.chargingTime);
                bdef('dischargingTime', B.dischargingTime);
                bdef('level', B.level);
                var noop = function () {};
                Object.defineProperty(bm, 'addEventListener', { value: noop, writable: true, configurable: true });
                Object.defineProperty(bm, 'removeEventListener', { value: noop, writable: true, configurable: true });
                Object.defineProperty(bm, 'dispatchEvent', { value: function () { return true; }, writable: true, configurable: true });
                Object.defineProperty(bm, 'onchargingchange', { value: null, writable: true });
                Object.defineProperty(bm, 'onchargingtimechange', { value: null, writable: true });
                Object.defineProperty(bm, 'ondischargingtimechange', { value: null, writable: true });
                Object.defineProperty(bm, 'onlevelchange', { value: null, writable: true });
                defineFn(N, 'getBattery', function () { return Promise.resolve(bm); });
            } catch (e) {}
        }

        // ---- 色深：真机移动端 24 位（宿主 macOS 报告 30 位）----
        if (D.colorDepth && typeof Screen !== 'undefined' && Screen.prototype) {
            defineGet(Screen.prototype, 'colorDepth', D.colorDepth);
            defineGet(Screen.prototype, 'pixelDepth', D.colorDepth);
        }

        // ---- WebGL：非遮蔽厂商/渲染器（内核原生 GPU 伪装不含移动平台）----
        if (D.webgl && D.webgl.renderer) {
            try {
                var patchGL = function (Ctor) {
                    if (!Ctor || !Ctor.prototype || !Ctor.prototype.getParameter) { return; }
                    var orig = Ctor.prototype.getParameter;
                    var wrapped = function (p) {
                        if (p === 37445) { return D.webgl.vendor; }
                        if (p === 37446) { return D.webgl.renderer; }
                        return orig.apply(this, arguments);
                    };
                    Object.defineProperty(Ctor.prototype, 'getParameter', {
                        value: wrapped, writable: true, configurable: true,
                    });
                };
                patchGL(window.WebGLRenderingContext);
                patchGL(window.WebGL2RenderingContext);
            } catch (e) {}
        }
    } catch (e) {}
})();
"""


def build_mobile_patch(spec: Dict[str, Any]) -> str:
    """把仿真规格编译成 init 脚本（页面脚本运行前生效）。"""
    payload = {
        "platform": spec.get("platform") or "",
        "maxTouchPoints": int(spec.get("maxTouchPoints") or 5),
        "vendor": spec.get("vendor") or "",
        "deviceMemory": spec.get("deviceMemory"),
        "colorDepth": spec.get("colorDepth"),
        "uaCh": spec.get("uaCh"),
        "battery": spec.get("battery"),
        "removeProps": list(spec.get("removeProps") or []),
        "emptyPlugins": bool(spec.get("emptyPlugins")),
        "ios": bool(spec.get("ios")),
        "webgl": spec.get("webgl"),
    }
    return _PATCH_TEMPLATE.replace("__SPEC__", json.dumps(payload, ensure_ascii=False))


class MobileEmulationSession:
    """一条常驻 CDP 连接上的移动端设备仿真。

    用法：
        session = MobileEmulationSession(ws_endpoint,
                                         fingerprint=fingerprint_data["fingerprint"],
                                         target_os="android", start_url=...)
        await session.start()   # 连接 + 应用仿真 + 导航到 start_url
        ...
        await session.close()   # 环境停止时
    """

    def __init__(
        self,
        ws_endpoint: str,
        *,
        fingerprint: Optional[dict] = None,
        target_os: str = "android",
        locale: str = "",
        start_url: str = "",
        # 兼容旧调用方式（直接传标量；优先级低于 fingerprint）
        viewport: Optional[dict] = None,
        dpr: float = 1.0,
        ua: str = "",
        platform: str = "",
        max_touch_points: int = 5,
    ) -> None:
        self.ws_endpoint = ws_endpoint
        self.target_os = target_os
        self.locale = locale
        self.start_url = start_url or ""

        if fingerprint:
            spec = derive_spec(fingerprint, target_os)
        else:
            # 旧调用方式：仅视口 / UA / platform
            phys = viewport or {}
            scale = dpr if dpr and dpr > 0 else 1.0
            css_w = round((phys.get("width") or 0) / scale)
            css_h = round((phys.get("height") or 0) / scale)
            if not (css_w and css_h):
                raise ValueError("移动端仿真缺少有效屏幕尺寸")
            spec = {
                "ua": ua, "platform": platform, "vendor": "",
                "maxTouchPoints": int(max_touch_points or 5),
                "deviceMemory": None, "colorDepth": None, "uaCh": None,
                "battery": None, "removeProps": [], "emptyPlugins": False,
                "ios": target_os == "ios", "webgl": None,
                "cssWidth": css_w, "cssHeight": css_h,
                "viewportHeight": css_h, "dpr": scale,
            }

        self.spec = spec
        self.ua = spec["ua"]
        self.platform = spec["platform"]
        self.max_touch_points = spec["maxTouchPoints"]
        self.css_w = spec["cssWidth"]
        self.css_h = spec["cssHeight"]
        self.viewport_h = spec["viewportHeight"]
        self.dpr = spec["dpr"]

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
        # 0. 启用 Page 域：未启用时 addScriptToEvaluateOnNewDocument 虽然返回
        #    identifier，但 init 脚本不会真正挂载到后续文档（导航后的新文档
        #    丢失 JS 补丁：platform/userAgentData 等字段仍是内核原生值）
        with contextlib.suppress(CDPError):
            await self._send("Page.enable", {}, session_id=sid)

        # 1. 设备度量：视口 + 屏幕 + DPR + 移动布局（CSS 逻辑像素）
        #    真机首屏 innerHeight < screen.height（浏览器 UI 占位），
        #    innerHeight == screen.height 是最典型的仿真穿帮点。
        full: dict[str, Any] = {
            "width": self.css_w,
            "height": self.viewport_h,
            "deviceScaleFactor": self.dpr,
            "mobile": True,
            "screenWidth": self.css_w,
            "screenHeight": self.css_h,
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

        # 4. Android：UA-CH 低熵请求头（该内核不发送任何 Sec-CH-UA* 头，
        #    真实 Chrome 默认对全站发送这三个提示）
        ua_ch = self.spec.get("uaCh")
        if ua_ch:
            with contextlib.suppress(CDPError):
                await self._send("Network.enable", {}, session_id=sid)
            with contextlib.suppress(CDPError):
                await self._send("Network.setExtraHTTPHeaders",
                                 {"headers": sec_ch_headers(ua_ch)}, session_id=sid)

        # 5. JS 层字段：init 脚本对后续导航生效；当前文档立即执行一次
        patch = build_mobile_patch(self.spec)
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
