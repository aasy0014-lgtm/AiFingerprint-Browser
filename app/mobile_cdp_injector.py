"""移动端 CDP 仿真会话（持久连接）

为 fp-chromium（桌面 Chromium 内核）建立移动设备仿真，使页面呈现
真实移动端环境（Android/iOS）。所有取值来自指纹库生成的 fingerprint
数据（app/mobile_fingerprints.py），保证「存储的指纹 = 运行期实际值」：

- 视口 / screen.width/height / DevicePixelRatio（CDP 设备仿真）
  首屏视口高度 < 屏幕高度（真机浏览器 UI 占位，innerHeight == screen.height
  是移动端仿真最常见的穿帮点）
- Touch Emulation（多点触控，maxTouchPoints）
- User-Agent / navigator.platform 覆盖
- 时区（Emulation.setTimezoneOverride，与内核 --timezone 参数对齐 geoip 出口时区）
- WebGL 非遮蔽渲染器/厂商（内核原生 GPU 伪装不支持 Android/iOS 平台，
  不覆盖会直接暴露宿主机的 GPU 字符串）
- 补丁函数原生伪装：Function.prototype.toString 对补丁函数返回
  "[native code]" 外观（否则检测站按「函数被篡改」直接扣分）
- Android：navigator.userAgentData（UA-CH，含机型/系统版本等高熵值）、
  navigator.deviceMemory、Battery API、Sec-CH-UA* 请求头
- iOS：抹掉 Safari 不存在的属性（navigator.connection、pdfViewerEnabled、
  userAgentData），plugins/mimeTypes 置空，navigator.vendor=Apple
- screen.colorDepth/pixelDepth = 24（宿主 macOS 报告 30，是明显的穿帮点）

关键设计：**仿真设置随 CDP 客户端断开会被浏览器还原**（与 DevTools
设备窗口关闭后恢复同理）。因此本模块维护一条常驻的轻量原生 WebSocket
连接（不引入 Playwright/Node 进程），并把仿真应用到：

1. 连接时已存在的页面 target；
2. 监听 Target 自动挂载事件，后续新开的标签页同样应用仿真——新目标以
   waitForDebuggerOnStart 暂停，等补丁就绪后再 runIfWaitingForDebugger
   放行，杜绝「首个文档在补丁生效前执行页面脚本」的竞态（UA 是移动端、
   屏幕/GPU/触控全泄露宿主值的典型穿帮）。

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
from urllib.parse import urlsplit

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
    if target_os == "android" and ua_ch:
        # Chromium 源码事实：Android 手机端 UA-CH 的 architecture/bitness 恒为空字符串
        # （components/embedder_support/user_agent_utils.cc：GetCpuArchitecture /
        # GetCpuBitness 在 IS_ANDROID 且非桌面/XR 设备上返回 ""）。
        # 旧版指纹数据曾填 "arm"，此处运行期归一化，避免「UA 是安卓、高熵值却
        # 带桌面架构」被服务端 OS 一致性判定命中。
        ua_ch = dict(ua_ch)
        ua_ch["architecture"] = ""
        ua_ch["bitness"] = ""

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


# 高熵提示：仅当站点在响应里回 Accept-CH 声明后才发送（真机 Chrome 行为）。
# 内核既不发送低熵头也不处理 Accept-CH，本层在 Fetch 域里按「源」精确回送。
_HIGH_ENTROPY_HINTS = frozenset({
    "sec-ch-ua-model", "sec-ch-ua-platform-version", "sec-ch-ua-arch",
    "sec-ch-ua-bitness", "sec-ch-ua-full-version", "sec-ch-ua-full-version-list",
    "sec-ch-ua-wow64",
})


def high_entropy_headers(ua_ch: Dict[str, Any], hints: set) -> Dict[str, str]:
    """按站点声明的高熵提示构造头值（与 JS UA-CH 同一数据源，保证头/JS 一致）。"""
    out: Dict[str, str] = {}
    if "sec-ch-ua-model" in hints:
        out["Sec-CH-UA-Model"] = f'"{ua_ch.get("model") or ""}"'
    if "sec-ch-ua-platform-version" in hints:
        out["Sec-CH-UA-Platform-Version"] = f'"{ua_ch.get("platformVersion") or ""}"'
    if "sec-ch-ua-arch" in hints:
        out["Sec-CH-UA-Arch"] = f'"{ua_ch.get("architecture") or ""}"'
    if "sec-ch-ua-bitness" in hints:
        out["Sec-CH-UA-Bitness"] = f'"{ua_ch.get("bitness") or ""}"'
    if "sec-ch-ua-full-version" in hints:
        out["Sec-CH-UA-Full-Version"] = f'"{ua_ch.get("fullVersion") or ""}"'
    if "sec-ch-ua-full-version-list" in hints:
        parts = [f'"{b.get("brand")}";v="{b.get("version")}"'
                 for b in (ua_ch.get("fullVersionList") or [])]
        out["Sec-CH-UA-Full-Version-List"] = ", ".join(parts)
    if "sec-ch-ua-wow64" in hints:
        out["Sec-CH-UA-WoW64"] = "?1" if ua_ch.get("wow64") else "?0"
    return out


def url_origin(url: str) -> str:
    """URL 的 scheme://host:port（Accept-CH 的存储粒度是 origin）。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return ""


# ------------------------------------------------------------------ JS 补丁
# 检测站点（如 BrowserScan）会做函数完整性检查：被 JS 包装过的函数
# Function.prototype.toString() 不再是 "[native code]" → 直接标记
# "Canvas related functions have been artificially modified"。
# __maskNative 把补丁函数登记进注册表，并接管 Function.prototype.toString
# 使其对补丁函数返回原生样式的字符串（含函数名）。
_PATCH_TEMPLATE = r"""
(function () {
    var D;
    try { D = __SPEC__; } catch (e) { return; }

    // ---- 原生函数伪装：toString() 一律返回 [native code] 外观 ----
    var __maskNative = (function () {
        try {
            var origToString = Function.prototype.toString;
            if (typeof WeakMap === 'undefined') { return function (fn) { return fn; }; }
            var registry = new WeakMap();
            var faked = function toString() {
                var v = registry.get(this);
                return v !== undefined ? v : origToString.call(this);
            };
            var mask = function (fn, name) {
                try {
                    if (typeof fn === 'function') {
                        registry.set(fn, 'function ' + name + '() { [native code] }');
                        try { Object.defineProperty(fn, 'name', { value: name, configurable: true }); } catch (e2) {}
                    }
                } catch (e) {}
                return fn;
            };
            mask(faked, 'toString');
            Object.defineProperty(Function.prototype, 'toString', {
                value: faked, writable: true, configurable: true,
            });
            return mask;
        } catch (e) {
            return function (fn) { return fn; };
        }
    })();

    var defineGet = function (proto, name, value) {
        try {
            Object.defineProperty(proto, name, {
                get: __maskNative(function () { return value; }, 'get ' + name),
                enumerable: true,
                configurable: true,
            });
            return true;
        } catch (e) { return false; }
    };
    var defineFn = function (proto, name, fn) {
        try {
            Object.defineProperty(proto, name, {
                value: __maskNative(fn, name),
                writable: true, enumerable: true, configurable: true,
            });
            return true;
        } catch (e) { return false; }
    };
    // window 上的 [Replaceable] 属性（devicePixelRatio/outerWidth 等）：
    // 原生是「get/set 均存在、赋值时自我替换为数据属性」，保持同样形态。
    var replaceable = function (obj, name, value) {
        if (!obj) { return false; }
        try {
            var getter = __maskNative(function () { return value; }, 'get ' + name);
            var setter = __maskNative(function (v) {
                try {
                    Object.defineProperty(obj, name, {
                        value: v, writable: true, enumerable: true, configurable: true,
                    });
                } catch (e) {}
            }, 'set ' + name);
            Object.defineProperty(obj, name, {
                get: getter, set: setter, enumerable: true, configurable: true,
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
        // ---- UA 文档起点覆盖 ----
        // CDP Emulation.setUserAgentOverride 是渲染器侧命令：新标签页首文档
        // 偶发在其落达前已提交（竞态实测），此时 UA 会漏成宿主桌面值。
        // 文档起点脚本先于一切页面脚本，这里兜底保证 UA/appVersion 恒为移动端；
        // 与 CDP 覆盖同值，不冲突（appVersion = UA 去掉 "Mozilla/" 前缀）。
        if (D.ua) {
            defineGet(N, 'userAgent', D.ua);
            defineGet(N, 'appVersion', D.ua.replace(/^Mozilla\//, ''));
        }
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
                defineFn(emptyPlugins, 'item', function () { return null; });
                defineFn(emptyPlugins, 'namedItem', function () { return null; });
                defineFn(emptyPlugins, 'refresh', function () {});
                defineGet(N, 'plugins', emptyPlugins);
            } catch (e) {}
            try {
                if (typeof MimeTypeArray !== 'undefined') {
                    var emptyMimes = Object.create(MimeTypeArray.prototype);
                    Object.defineProperty(emptyMimes, 'length', { value: 0 });
                    defineFn(emptyMimes, 'item', function () { return null; });
                    defineFn(emptyMimes, 'namedItem', function () { return null; });
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
                var baseOut = function () {
                    return { brands: base.brands, mobile: base.mobile, platform: base.platform };
                };
                var hev = function (hints) {
                    var out = baseOut();
                    (hints || []).forEach(function (h) {
                        if (Object.prototype.hasOwnProperty.call(he, h)) { out[h] = he[h]; }
                    });
                    return Promise.resolve(out);
                };
                var uaProto = (typeof NavigatorUAData !== 'undefined') ? NavigatorUAData.prototype : null;
                var uaData;
                if (uaProto) {
                    // 属性挂到原生原型上：实例自身无 own property（与真机一致）
                    defineGet(uaProto, 'brands', base.brands);
                    defineGet(uaProto, 'mobile', base.mobile);
                    defineGet(uaProto, 'platform', base.platform);
                    defineFn(uaProto, 'getHighEntropyValues', hev);
                    defineFn(uaProto, 'toJSON', baseOut);
                    uaData = Object.create(uaProto);
                } else {
                    // 兜底：内核不暴露 NavigatorUAData 接口时退回实例属性
                    uaData = {};
                    Object.defineProperty(uaData, 'brands', { get: __maskNative(function () { return base.brands; }, 'get brands') });
                    Object.defineProperty(uaData, 'mobile', { get: __maskNative(function () { return base.mobile; }, 'get mobile') });
                    Object.defineProperty(uaData, 'platform', { get: __maskNative(function () { return base.platform; }, 'get platform') });
                    Object.defineProperty(uaData, 'getHighEntropyValues', { value: __maskNative(hev, 'getHighEntropyValues') });
                    Object.defineProperty(uaData, 'toJSON', { value: __maskNative(baseOut, 'toJSON') });
                }
                defineGet(N, 'userAgentData', uaData);
            } catch (e) {}
        }

        // ---- Android Chrome：Battery API ----
        if (D.battery) {
            try {
                var B = D.battery;
                if (typeof BatteryManager !== 'undefined') {
                    // 属性/方法整体挂原生原型：实例自身无 own property（与真机一致）
                    var bProto = BatteryManager.prototype;
                    defineGet(bProto, 'charging', !!B.charging);
                    defineGet(bProto, 'chargingTime', B.chargingTime);
                    defineGet(bProto, 'dischargingTime', B.dischargingTime);
                    defineGet(bProto, 'level', B.level);
                    // 真实 EventTarget 方法要求内部槽位，直接调用会 Illegal invocation；
                    // 覆盖为无副作用实现（电量取值路径不依赖事件）
                    defineFn(bProto, 'addEventListener', function () {});
                    defineFn(bProto, 'removeEventListener', function () {});
                    defineFn(bProto, 'dispatchEvent', function () { return true; });
                    var bm = Object.create(bProto);
                    defineFn(N, 'getBattery', function () { return Promise.resolve(bm); });
                }
            } catch (e) {}
        }

        // ---- 色深：真机移动端 24 位（宿主 macOS 报告 30 位）----
        if (D.colorDepth && typeof Screen !== 'undefined' && Screen.prototype) {
            defineGet(Screen.prototype, 'colorDepth', D.colorDepth);
            defineGet(Screen.prototype, 'pixelDepth', D.colorDepth);
        }

        // ---- 屏幕尺寸 / DPR / outer / 朝向：跨域 iframe（OOPIF）没有页面级
        //      CDP 度量仿真，这些字段全是宿主值（macOS 1440x900/DPR2/横向）；
        //      真机上 iframe 内与主文档完全相同 → 镜像主文档保持一致 ----
        if (D.cssWidth && D.cssHeight && typeof Screen !== 'undefined' && Screen.prototype) {
            defineGet(Screen.prototype, 'width', D.cssWidth);
            defineGet(Screen.prototype, 'height', D.cssHeight);
            defineGet(Screen.prototype, 'availWidth', D.cssWidth);
            defineGet(Screen.prototype, 'availHeight', D.cssHeight);
        }
        if (D.dpr) {
            replaceable(window, 'devicePixelRatio', D.dpr);
            if (D.viewportHeight) { replaceable(window, 'outerHeight', D.viewportHeight); }
            replaceable(window, 'outerWidth', D.cssWidth);
        }
        if (typeof ScreenOrientation !== 'undefined' && ScreenOrientation.prototype) {
            defineGet(ScreenOrientation.prototype, 'type', 'portrait-primary');
            defineGet(ScreenOrientation.prototype, 'angle', 0);
        }

        // ---- SpeechSynthesis：移动端语音列表必须为空 ----
        // Android Chrome 不提供语音枚举（getVoices() 恒为 []）；宿主 macOS 会
        // 暴露上百个语音（婷婷/Albert/Samantha…），检测站点把这个数量直接作为
        // OS 判定输入（BrowserScan 的 "Different operating systems" 即来源于
        // macOS 语音数 180 vs 安卓期望 0）。iOS Safari 在无用户手势的检测场景
        // 同样返回空列表，故两端统一清空。
        try {
            var SS = (typeof SpeechSynthesis !== 'undefined') ? SpeechSynthesis.prototype : null;
            if (SS && SS.getVoices) {
                defineFn(SS, 'getVoices', function () { return []; });
            } else if (window.speechSynthesis) {
                defineFn(window.speechSynthesis, 'getVoices', function () { return []; });
            }
        } catch (e) {}

        // ---- WebGL：非遮蔽厂商/渲染器（内核原生 GPU 伪装不含移动平台）----
        if (D.webgl && D.webgl.renderer) {
            try {
                var patchGL = function (Ctor) {
                    if (!Ctor || !Ctor.prototype || !Ctor.prototype.getParameter) { return; }
                    var orig = Ctor.prototype.getParameter;
                    var wrapped = __maskNative(function (p) {
                        if (p === 37445 || p === 37446) {
                            // 耗时对齐原生：真实查询仍走一遍原生路径（结果丢弃后返回伪造值）。
                            // 若纯 JS 直接返回闭包字符串，吞吐量可达每毫秒数万次；
                            // BrowserScan 的 E() 计时器在 5ms 内压测
                            // getParameter(UNMASKED_RENDERER_WEBGL) 调用数，Z = 调用数/毫秒，
                            // Z > 100 即判定「WebGL 被伪造」→ "WebGL exception" -5%。
                            // 原生路径约 0.5ms/次（Z≈2-3），与真机同量级。
                            try { orig.apply(this, arguments); } catch (e) {}
                            return p === 37445 ? D.webgl.vendor : D.webgl.renderer;
                        }
                        return orig.apply(this, arguments);
                    }, 'getParameter');
                    Object.defineProperty(Ctor.prototype, 'getParameter', {
                        value: wrapped, writable: true, enumerable: true, configurable: true,
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
        "ua": str(spec.get("ua") or ""),
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
        # 跨域 iframe（OOPIF）无页面级 CDP 度量仿真：screen/DPR/outer 镜像主文档
        "cssWidth": int(spec.get("cssWidth") or 0),
        "cssHeight": int(spec.get("cssHeight") or 0),
        "viewportHeight": int(spec.get("viewportHeight") or 0),
        "dpr": float(spec.get("dpr") or 0),
    }
    return _PATCH_TEMPLATE.replace("__SPEC__", json.dumps(payload, ensure_ascii=False))


# ------------------------------------------------------------------ Worker 补丁
# Dedicated/Shared/Service Worker 是独立 JS 全局，不继承页面的 init 脚本：
# WorkerNavigator 的 platform / deviceMemory / userAgentData（安全上下文下
# 内核会给出一份桌面 Linux 元数据）都会暴露宿主值。Worker 无法使用
# Page.addScriptToEvaluateOnNewDocument，由 _attach_worker 在目标被门控
# 暂停时 Runtime.evaluate 注入（实测暂停态 1-2ms 响应，先于首行代码）。
_WORKER_PATCH_TEMPLATE = r"""
(function () {
    var D;
    try { D = __SPEC__; } catch (e) { return; }

    // ---- 原生函数伪装（与页面补丁同一实现）----
    var __maskNative = (function () {
        try {
            var origToString = Function.prototype.toString;
            if (typeof WeakMap === 'undefined') { return function (fn) { return fn; }; }
            var registry = new WeakMap();
            var faked = function toString() {
                var v = registry.get(this);
                return v !== undefined ? v : origToString.call(this);
            };
            var mask = function (fn, name) {
                try {
                    if (typeof fn === 'function') {
                        registry.set(fn, 'function ' + name + '() { [native code] }');
                        try { Object.defineProperty(fn, 'name', { value: name, configurable: true }); } catch (e2) {}
                    }
                } catch (e) {}
                return fn;
            };
            mask(faked, 'toString');
            Object.defineProperty(Function.prototype, 'toString', {
                value: faked, writable: true, configurable: true,
            });
            return mask;
        } catch (e) {
            return function (fn) { return fn; };
        }
    })();
    var defineGet = function (proto, name, value) {
        try {
            Object.defineProperty(proto, name, {
                get: __maskNative(function () { return value; }, 'get ' + name),
                enumerable: true,
                configurable: true,
            });
            return true;
        } catch (e) { return false; }
    };
    var defineFn = function (proto, name, fn) {
        try {
            Object.defineProperty(proto, name, {
                value: __maskNative(fn, name),
                writable: true, enumerable: true, configurable: true,
            });
            return true;
        } catch (e) { return false; }
    };

    try {
        var N = null;
        try { N = (typeof WorkerNavigator !== 'undefined') ? WorkerNavigator.prototype : null; } catch (e) {}
        if (!N && typeof navigator !== 'undefined') {
            N = Object.getPrototypeOf(navigator);
        }
        if (!N) { return; }

        // ---- 抹掉该平台不存在的属性（iOS 的 deviceMemory / userAgentData 等）----
        var drops = D.removeProps || [];
        for (var i = 0; i < drops.length; i++) {
            try { delete N[drops[i]]; } catch (e) {}
            try { delete navigator[drops[i]]; } catch (e) {}
            if (drops[i] in navigator) { defineGet(N, drops[i], undefined); }
        }

        if (D.platform) { defineGet(N, 'platform', D.platform); }
        if (D.ua) {
            defineGet(N, 'userAgent', D.ua);
            defineGet(N, 'appVersion', D.ua.replace(/^Mozilla\//, ''));
        }
        if (D.deviceMemory !== null && D.deviceMemory !== undefined) {
            defineGet(N, 'deviceMemory', D.deviceMemory);
        }

        // ---- UA-CH：Worker 内 navigator.userAgentData 与主文档一致 ----
        // （安全上下文下内核原生提供一份桌面 Linux 元数据：platform "Linux"、
        //   mobile false；真机 Android Worker 的 UA-CH 与主文档相同）
        if (D.uaCh) {
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
            var baseOut = function () {
                return { brands: base.brands, mobile: base.mobile, platform: base.platform };
            };
            var hev = function (hints) {
                var out = baseOut();
                (hints || []).forEach(function (h) {
                    if (Object.prototype.hasOwnProperty.call(he, h)) { out[h] = he[h]; }
                });
                return Promise.resolve(out);
            };
            var uaProto = null;
            try { uaProto = (typeof NavigatorUAData !== 'undefined') ? NavigatorUAData.prototype : null; } catch (e) {}
            if (!uaProto && navigator.userAgentData) {
                try { uaProto = Object.getPrototypeOf(navigator.userAgentData); } catch (e) {}
            }
            var uaData;
            if (uaProto) {
                defineGet(uaProto, 'brands', base.brands);
                defineGet(uaProto, 'mobile', base.mobile);
                defineGet(uaProto, 'platform', base.platform);
                defineFn(uaProto, 'getHighEntropyValues', hev);
                defineFn(uaProto, 'toJSON', baseOut);
                uaData = Object.create(uaProto);
            } else {
                uaData = {};
                Object.defineProperty(uaData, 'brands', { get: __maskNative(function () { return base.brands; }, 'get brands') });
                Object.defineProperty(uaData, 'mobile', { get: __maskNative(function () { return base.mobile; }, 'get mobile') });
                Object.defineProperty(uaData, 'platform', { get: __maskNative(function () { return base.platform; }, 'get platform') });
                Object.defineProperty(uaData, 'getHighEntropyValues', { value: __maskNative(hev, 'getHighEntropyValues') });
                Object.defineProperty(uaData, 'toJSON', { value: __maskNative(baseOut, 'toJSON') });
            }
            defineGet(N, 'userAgentData', uaData);
        }
    } catch (e) {}
})();
"""


def build_worker_patch(spec: Dict[str, Any]) -> str:
    """Worker 目标补丁：WorkerNavigator 与主文档同源（platform/UA-CH/属性抹除）。"""
    payload = {
        "ua": str(spec.get("ua") or ""),
        "platform": spec.get("platform") or "",
        "deviceMemory": spec.get("deviceMemory"),
        "uaCh": spec.get("uaCh"),
        "removeProps": list(spec.get("removeProps") or []),
        "ios": bool(spec.get("ios")),
    }
    return _WORKER_PATCH_TEMPLATE.replace("__SPEC__", json.dumps(payload, ensure_ascii=False))


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
        timezone: str = "",
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
        self.timezone = (timezone or "").strip()
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
        # Accept-CH：CDP session → {源: 该源已声明的高熵提示}（Fetch 域回送）
        self._accept_ch: dict[str, dict[str, set]] = {}
        self._closed = False

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        """连接浏览器调试端口并应用仿真；失败会自行清理。"""
        import websockets

        self._ws = await websockets.connect(
            self.ws_endpoint, max_size=64 * 1024 * 1024, ping_interval=None,
        )
        self._reader = asyncio.create_task(self._read_loop())

        # 监听后续新开的标签页 / 页面。
        # waitForDebuggerOnStart：新目标先在调试器处暂停，等本会话把
        # 设备仿真与 JS 补丁挂上后再放行（Runtime.runIfWaitingForDebugger）。
        # 否则「新标签页打开检测站」会出现首个文档在补丁生效前就执行页面
        # 脚本的竞态：UA 是移动端、其余全泄露宿主值。
        await self._send("Target.setAutoAttach", {
            "autoAttach": True,
            "waitForDebuggerOnStart": True,
            "flatten": True,
        })

        # 目标发现：跨域 iframe（OOPIF）的 auto-attach 只会投递给单个客户端
        # （实测：多客户端时仅先配置者收到），Playwright 等工具同时连接会抢走
        # 投递。开启发现后用显式 attachToTarget 兜底，保证 OOPIF 覆盖率与
        # 客户端数量无关。
        with contextlib.suppress(CDPError):
            await self._send("Target.setDiscoverTargets", {"discover": True})

        # 已存在的页面
        targets = (await self._send("Target.getTargets")).get("targetInfos", [])
        for info in targets:
            if info.get("type") == "page":
                await self._attach_page(info.get("targetId", ""))
            elif info.get("type") == "iframe":
                asyncio.create_task(self._attach_frame(info.get("targetId", "")))

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
                    ttype = info.get("type")
                    if ttype == "page":
                        asyncio.create_task(self._attach_page(
                            info.get("targetId", ""), session_id=sid, paused=True))
                    elif ttype == "tab":
                        # 新标签页（page 的父 target）：递归设定其子页面挂载策略
                        asyncio.create_task(self._attach_tab(sid))
                    elif ttype == "iframe":
                        # 跨域 iframe（OOPIF）：独立进程/目标，需与主文档同一套信号
                        asyncio.create_task(self._attach_frame(
                            info.get("targetId", ""), session_id=sid,
                            paused=bool(params.get("waitingForDebugger"))))
                    elif ttype in ("worker", "shared_worker", "service_worker"):
                        # 独立 JS 全局：WorkerNavigator 补丁后放行
                        asyncio.create_task(self._attach_worker(
                            info.get("targetId", ""), session_id=sid))
                    else:
                        # 其他被暂停的目标（auction worklet 等）：本层不处理，直接放行
                        asyncio.create_task(self._resume(sid))
                elif msg.get("method") == "Target.targetCreated":
                    # 目标发现兜底：OOPIF 的 auto-attach 投递存在客户端竞争，
                    # 这里对任何新出现的 iframe 显式挂载补丁（去重后无副作用）
                    info = (msg.get("params") or {}).get("targetInfo") or {}
                    if info.get("type") == "iframe" and info.get("targetId"):
                        asyncio.create_task(self._attach_frame(info.get("targetId", "")))
                elif msg.get("method") == "Fetch.requestPaused":
                    # Accept-CH：Document 响应解析声明；已声明源的请求回送高熵头
                    asyncio.create_task(self._on_fetch_paused(msg))
        except Exception:
            pass  # 连接关闭/取消
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(CDPError(-1, "CDP 连接已断开"))
            self._pending.clear()

    # ---------------- 仿真应用 ----------------

    async def _resume(self, sid: str) -> None:
        """放行被 waitForDebuggerOnStart 暂停的目标（非暂停目标上是无害空操作）。"""
        with contextlib.suppress(CDPError):
            await self._send("Runtime.runIfWaitingForDebugger", {}, session_id=sid,
                             timeout=5.0)

    async def _attach_tab(self, tab_session_id: str) -> None:
        """新标签页（page 的父 target）：设定其子页面自动挂载策略并放行自身。"""
        with contextlib.suppress(Exception):
            await self._send("Target.setAutoAttach", {
                "autoAttach": True,
                "waitForDebuggerOnStart": True,
                "flatten": True,
            }, session_id=tab_session_id)
        await self._resume(tab_session_id)

    async def _attach_page(self, target_id: str, session_id: Optional[str] = None,
                           *, paused: bool = False) -> None:
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
            # 极端场景（同 target 重复挂载且处于暂停态）：确保不会卡死
            if paused and session_id:
                await self._resume(session_id)
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
            # 子目标（Worker / 跨域 iframe）自动挂载：两者被门控暂停后先注入
            # 补丁再放行。OOPIF 在 Chromium 中同样受 waitForDebuggerOnStart
            # 门控（暂停于上下文创建前，故暂停态不能 evaluate、只需 addScript）；
            # 若该投递被其他客户端抢占，Target.targetCreated 兜底路径仍会覆盖。
            with contextlib.suppress(CDPError):
                await self._send("Target.setAutoAttach", {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": True,
                    "flatten": True,
                }, session_id=session_id)
        except Exception as e:
            log.debug("移动端仿真挂载页面失败: %s", e)
            self._applied_targets.discard(target_id)
        finally:
            # 先放行再补丁当前文档：Runtime 命令在暂停态下可能不受理，
            # 而 addScriptToEvaluateOnNewDocument 已在暂停期间挂好，
            # 恢复后的首个真实文档必定带补丁。
            if paused and session_id:
                await self._resume(session_id)
            if session_id and target_id in self._applied_targets:
                await self._apply_patch_now(session_id)
            if current is not None and self._attach_tasks.get(target_id) is current:
                self._attach_tasks.pop(target_id, None)

    async def _attach_frame(self, target_id: str, session_id: Optional[str] = None,
                            *, paused: bool = False) -> None:
        """跨域 iframe（OOPIF）补丁：与主文档保持同一套移动端信号。

        OOPIF 是独立进程/目标，不继承页面的 CDP 仿真与 JS 补丁：platform、
        screen/DPR（宿主 1440x900/2/横向）、触控、WebGL、UA-CH 全部穿帮。
        两条挂载路径共用本方法：
          1. 页面级 auto-attach 投递（waitForDebuggerOnStart 暂停于上下文
             创建前 → addScript 挂好后放行，首个文档即带补丁，无竞态）；
          2. Target.targetCreated 发现兜底 + 显式 attachToTarget（auto-attach
             投递被其他客户端抢占时仍生效；目标已在运行，则对当前文档
             立即补丁一次，尽力而为）。
        """
        if not target_id:
            return
        existing = self._attach_tasks.get(target_id)
        if existing is not None and existing is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await existing
            # 门控暂停是会话级的：其他会话的 runIfWaitingForDebugger 不解
            # 本会话的暂停（实测）。等待先到者完成补丁注册后，必须由本会话放行。
            if paused and session_id:
                await self._resume(session_id)
            return
        if target_id in self._applied_targets:
            if paused and session_id:
                await self._resume(session_id)
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
            # 嵌套跨域 iframe / iframe 内 Worker：继续覆盖其子目标
            with contextlib.suppress(CDPError):
                await self._send("Target.setAutoAttach", {
                    "autoAttach": True,
                    "waitForDebuggerOnStart": True,
                    "flatten": True,
                }, session_id=session_id)
            # 关键路径：先挂 init 脚本（暂停态下执行；被门控的目标放行后的
            # 首个真实文档因此必定带补丁）
            patch = build_mobile_patch(self.spec)
            with contextlib.suppress(CDPError):
                await self._send("Page.enable", {}, session_id=session_id)
            with contextlib.suppress(CDPError):
                await self._send("Page.addScriptToEvaluateOnNewDocument",
                                 {"source": patch}, session_id=session_id)
            # 触控仿真：pointer/hover 媒体查询与触控事件（JS 补丁只覆盖
            # maxTouchPoints 数值，媒体查询来自目标的触控开关）
            with contextlib.suppress(CDPError):
                await self._send("Emulation.setTouchEmulationEnabled", {
                    "enabled": True,
                    "maxTouchPoints": self.max_touch_points,
                }, session_id=session_id)
            # UA-CH 低熵请求头：iframe 发出的请求与主文档一致
            # （真机 Chrome 对全站发送；内核不发送任何 Sec-CH-UA* 头）
            ua_ch = self.spec.get("uaCh")
            if ua_ch:
                with contextlib.suppress(CDPError):
                    await self._send("Network.enable", {}, session_id=session_id)
                with contextlib.suppress(CDPError):
                    await self._send("Network.setExtraHTTPHeaders",
                                     {"headers": sec_ch_headers(ua_ch)},
                                     session_id=session_id)
        except Exception as e:
            log.debug("移动端仿真挂载 iframe 失败: %s", e)
            self._applied_targets.discard(target_id)
        finally:
            # 与页面路径同一顺序：先放行（创建执行上下文），再补丁当前文档。
            # 目标未被门控时两步都是无害的空操作/仅覆盖当前文档。
            if session_id:
                await self._resume(session_id)
                await self._apply_patch_now(session_id)
            if current is not None and self._attach_tasks.get(target_id) is current:
                self._attach_tasks.pop(target_id, None)

    async def _attach_worker(self, target_id: str, session_id: str) -> None:
        """Dedicated/Shared/Service Worker 补丁：WorkerNavigator 与主文档同源。

        Worker 是独立 JS 全局：platform（Linux x86_64）、deviceMemory（宿主
        内存桶）、userAgentData（桌面 Linux 元数据）全部穿帮。Worker 无法
        使用 Page.addScriptToEvaluateOnNewDocument；目标正被门控暂停时，
        Runtime.evaluate 即为「首行代码执行前」的注入点（实测 1-2ms）。
        """
        if not target_id or not session_id:
            return
        if target_id in self._applied_targets:
            await self._resume(session_id)
            return
        self._applied_targets.add(target_id)
        patch = build_worker_patch(self.spec)
        injected = False
        try:
            await self._send("Runtime.evaluate", {"expression": patch},
                             session_id=session_id, timeout=3.0)
            injected = True
        except (CDPError, asyncio.TimeoutError):
            pass
        finally:
            await self._resume(session_id)
        if not injected:
            # 暂停态不受理 Runtime 命令时（理论上不会）：放行后补一次，
            # 极小竞态窗口（Worker 首行可能已读取原生值）
            with contextlib.suppress(CDPError, asyncio.TimeoutError):
                await self._send("Runtime.evaluate", {"expression": patch},
                                 session_id=session_id, timeout=3.0)

    # ---------------- Accept-CH 高熵头回送 ----------------

    def _accept_ch_patterns(self, sid: str) -> list[dict]:
        """Fetch 拦截规则：所有 Document 响应（解析 Accept-CH）+ 已声明源的请求。"""
        patterns = [{
            "urlPattern": "*",
            "requestStage": "Response",
            "resourceType": "Document",
        }]
        for origin in self._accept_ch.get(sid, {}):
            patterns.append({"urlPattern": origin + "/*", "requestStage": "Request"})
        return patterns

    async def _enable_accept_ch(self, sid: str) -> None:
        """启用 Accept-CH 高熵头回送（仅页面级会话）。

        内核不处理 Accept-CH：站点声明高熵提示后，真机 Chrome 会对该源的后续
        请求附上 Sec-CH-UA-Model 等头，本层用 Fetch 域按源精确补齐。跨源 iframe
        不启用——高熵头受 Permissions-Policy（默认为 self）约束，真机在第三方
        上下文同样不回送。
        """
        if self.spec.get("ios") or not self.spec.get("uaCh"):
            return
        self._accept_ch.setdefault(sid, {})
        with contextlib.suppress(CDPError):
            await self._send("Fetch.enable", {"patterns": self._accept_ch_patterns(sid)},
                             session_id=sid)

    async def _on_fetch_paused(self, msg: Dict[str, Any]) -> None:
        """Fetch 域事件：Document 响应解析 Accept-CH；命中源注入高熵头。"""
        sid = msg.get("sessionId") or ""
        params = msg.get("params") or {}
        rid = params.get("requestId") or ""
        if not sid or not rid:
            return
        is_response = ("responseStatusCode" in params
                       or "responseErrorReason" in params)
        request = params.get("request") or {}
        if is_response:
            hints: set = set()
            for h in params.get("responseHeaders") or []:
                if (h.get("name") or "").lower() == "accept-ch":
                    for token in (h.get("value") or "").split(","):
                        name = token.split(";")[0].strip().lower()
                        if name in _HIGH_ENTROPY_HINTS:
                            hints.add(name)
            origin = url_origin(request.get("url") or "")
            known = self._accept_ch.setdefault(sid, {})
            if origin and hints and known.get(origin) != hints:
                # 新声明或集合变化：更新该源的提示并刷新拦截规则
                known[origin] = hints
                with contextlib.suppress(CDPError):
                    await self._send("Fetch.enable",
                                     {"patterns": self._accept_ch_patterns(sid)},
                                     session_id=sid)
            await self._continue_paused(sid, rid, is_response=True)
            return
        # Request 阶段：只有已声明源的请求会走到这里
        origin = url_origin(request.get("url") or "")
        hints = (self._accept_ch.get(sid) or {}).get(origin)
        if not hints:
            await self._continue_paused(sid, rid)
            return
        ua_ch = self.spec.get("uaCh") or {}
        headers = dict(request.get("headers") or {})
        # 拦截层看到的请求头未必包含 CDP extra-headers/UA 覆盖注入的项：
        # 缺失时补齐（已有值保持——浏览器视角的最终值优先）
        for name, value in sec_ch_headers(ua_ch).items():
            headers.setdefault(name, value)
        if self.ua:
            headers.setdefault("User-Agent", self.ua)
        headers.update(high_entropy_headers(ua_ch, hints))
        await self._continue_paused(sid, rid, headers=headers)

    async def _continue_paused(self, sid: str, rid: str, *,
                               headers: Optional[Dict[str, str]] = None,
                               is_response: bool = False) -> None:
        """放行 Fetch 暂停的请求（响应阶段优先 continueResponse，回退旧方法）。"""
        if is_response:
            try:
                await self._send("Fetch.continueResponse", {"requestId": rid},
                                 session_id=sid)
                return
            except CDPError:
                with contextlib.suppress(CDPError):
                    await self._send("Fetch.continueRequest", {"requestId": rid},
                                     session_id=sid)
                return
        params: Dict[str, Any] = {"requestId": rid}
        if headers is not None:
            params["headers"] = [{"name": k, "value": v}
                                 for k, v in headers.items()]
        try:
            await self._send("Fetch.continueRequest", params, session_id=sid)
        except CDPError:
            # 参数被拒（如头格式）时至少保证请求被放行，避免卡死页面
            with contextlib.suppress(CDPError):
                await self._send("Fetch.continueRequest", {"requestId": rid},
                                 session_id=sid)

    async def _apply_device_emulation(self, sid: str) -> None:
        # 0. 启用 Page 域：未启用时 addScriptToEvaluateOnNewDocument 虽然返回
        #    identifier，但 init 脚本不会真正挂载到后续文档（导航后的新文档
        #    丢失 JS 补丁：platform/userAgentData 等字段仍是内核原生值）
        with contextlib.suppress(CDPError):
            await self._send("Page.enable", {}, session_id=sid)

        # 0.5 JS 补丁 init 脚本：紧跟 Page.enable 挂载（关键路径最短化）。
        #     新目标被 waitForDebuggerOnStart 门控时，本脚本必须在放行前
        #     到达，否则首个文档仍会以原生指纹执行页面脚本。
        patch = build_mobile_patch(self.spec)
        with contextlib.suppress(CDPError):
            await self._send("Page.addScriptToEvaluateOnNewDocument",
                             {"source": patch}, session_id=sid)

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

        # 5. 时区：geoip 解析的出口时区（与内核 --timezone 参数同时生效，
        #    覆盖 JS Intl/Date；内核参数覆盖不到的场景由此兜底）
        if self.timezone:
            with contextlib.suppress(CDPError):
                await self._send("Emulation.setTimezoneOverride",
                                 {"timezoneId": self.timezone}, session_id=sid)

        # 6. Accept-CH：站点声明高熵提示后，对其后续请求回送 Sec-CH-UA-* 高熵头
        #    （内核完全不处理 Accept-CH；真机 Chrome 全量支持，缺回送是明显破绽）
        await self._enable_accept_ch(sid)

    async def _apply_patch_now(self, sid: str) -> None:
        """对已存在的当前文档立即执行一次 JS 补丁。

        新挂载的页面（waitForDebuggerOnStart 释放前）当前文档通常是空白页；
        需要立即补丁的主要是「会话连接前就存在的页面」与自检场景。
        """
        patch = build_mobile_patch(self.spec)
        with contextlib.suppress(CDPError):
            await self._send("Runtime.evaluate", {"expression": patch},
                             session_id=sid, timeout=5.0)

    async def _navigate_start_url(self) -> None:
        sid = self._first_page_session
        if not sid:
            return
        try:
            await self._send("Page.navigate", {"url": self.start_url}, session_id=sid)
        except CDPError as e:
            log.debug("移动端 start_url 导航失败: %s", e)
