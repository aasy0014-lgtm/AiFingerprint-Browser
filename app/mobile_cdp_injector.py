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
- iOS Chrome（CriOS 口径）：页面世界检测面补齐（__gCrWeb 全局、被改写的
  history/print/scrollTo（toString 含真机字样）、webkit.messageHandlers），
  普通页面无 window.chrome（iOS 两口径通用）
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
import math
import random
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .mobile_fingerprints import (
    _ANDROID_REMOVE_GLOBALS,
    _ANDROID_REMOVE_NAV_PROPS,
    _ANDROID_REMOVE_WORKER_GLOBALS,
    _IOS_REMOVE_GLOBALS,
    _IOS_REMOVE_NAV_PROPS,
    _IOS_REMOVE_WORKER_GLOBALS,
)

log = logging.getLogger(__name__)

# iOS Safari / Android Chrome 不存在、需从桌面 Chromium 上抹掉的 Navigator 属性。
# 清单唯一来源在指纹库（mobile_fingerprints），运行期与数据层共享，避免两处漂移。
_IOS_REMOVE_PROPS = list(_IOS_REMOVE_NAV_PROPS)
_ANDROID_REMOVE_PROPS = list(_ANDROID_REMOVE_NAV_PROPS)
# 平台 API 差集（窗口/文档/navigator 按名删除）+ Worker 全局差集；
# 清单与逐项 BCD 证据见 mobile_fingerprints 同名常量注释。
_IOS_GLOBAL_DROPS = list(_IOS_REMOVE_GLOBALS)
_ANDROID_GLOBAL_DROPS = list(_ANDROID_REMOVE_GLOBALS)
_IOS_WORKER_DROPS = list(_IOS_REMOVE_WORKER_GLOBALS)
_ANDROID_WORKER_DROPS = list(_ANDROID_REMOVE_WORKER_GLOBALS)

# ---------------- 传感器面（Android 真机口径）----------------
# 真机 Chrome Android 在注册 devicemotion/deviceorientation 监听后会收到持续
# 事件（加速度计/陀螺仪/方向传感器均可用）；桌面内核没有传感器，只在注册时
# 派发一次全 null 的哨兵事件——「有没有连续的数值事件流」是模拟器/桌面装手机
# 最直接的传感器判据之一。
#
# fp-chromium 支持 CDP 虚拟传感器（Emulation.setSensorOverrideEnabled /
# setSensorOverrideReadings，148 内核实测可用），据此按真机语义驱动：
#   devicemotion 由内核 60Hz 轮询派发（读数更新即改变事件内容）；
#   deviceorientation / deviceorientationabsolute 每个读数变化派发一次，
#   因此需要按节奏更新读数才能形成「活」的事件流。
# 读数生成见 _SensorSimulator：姿态（beta/gamma）与重力矢量保持数学一致，
# 避免「重力矢量与 deviceorientation 不符」的交叉校验命中。
_SENSOR_TICK_SEC = 0.05          # 20Hz 名义更新率（±40% 抖动，见 _sensor_loop）
_SENSOR_TICK_JITTER = 0.4        # 抖动幅度：真机传感器采样周期并非严格固定
_SENSOR_TYPES = ("accelerometer", "linear-acceleration", "gyroscope",
                 "relative-orientation", "absolute-orientation")
_GRAVITY = 9.80665               # m/s²


def _quaternion_from_euler(alpha: float, beta: float, gamma: float) -> Dict[str, float]:
    """W3C 口径的欧拉角 → 四元数（R = Rz(α)·Rx(β)·Ry(γ)）。

    与内核反解严格一致（实测：下发后事件读回的 alpha/beta/gamma 即输入值）。
    """
    a, b, g = (math.radians(v) for v in (alpha, beta, gamma))
    ca, sa = math.cos(a), math.sin(a)
    cb, sb = math.cos(b), math.sin(b)
    cg, sg = math.cos(g), math.sin(g)
    row = (
        (ca * cg - sa * sb * sg, -sa * cb, ca * sg + sa * sb * cg),
        (sa * cg + ca * sb * sg, ca * cb, sa * sg - ca * sb * cg),
        (-cb * sg, sb, cb * cg),
    )
    w = math.sqrt(max(0.0, 1 + row[0][0] + row[1][1] + row[2][2])) / 2
    if w < 1e-8:
        return {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0}
    return {"x": (row[2][1] - row[1][2]) / (4 * w),
            "y": (row[0][2] - row[2][0]) / (4 * w),
            "z": (row[1][0] - row[0][1]) / (4 * w),
            "w": w}


class _SensorSimulator:
    """生成「手持浏览姿态」的手机传感器读数（含缓慢随机游走）。

    - 姿态：beta ∈ [55°, 75°]（竖握略后倾）、gamma ∈ [-10°, 10°]、alpha 随机；
    - 重力自洽：aig = (−g·cosβ·sinγ, g·sinβ, g·cosβ·cosγ)，与 direction 事件
      的欧拉角严格同源（真机自洽性正是高级检测的交叉校验点）；
    - 抖动：加速度 ±0.2 / 线性加速度 ±0.1 级 / 陀螺仪 ±0.005 rad/s——真机在
      1dp 量化后呈 0.0/±0.1 级变化，恒定常量是模拟器特征。
    """

    def __init__(self, rng: random.Random) -> None:
        self._rng = rng
        self.alpha = rng.uniform(0.0, 360.0)
        self.beta = rng.uniform(55.0, 75.0)
        self.gamma = rng.uniform(-10.0, 10.0)
        self._linear = [0.0, 0.0, 0.0]   # 线性加速度（去重力），m/s²
        self._gyro = [0.0, 0.0, 0.0]     # 角速度，rad/s（事件里换算为 deg/s）

    def _walk(self, values: List[float], step: float, limit: float) -> None:
        for i, v in enumerate(values):
            values[i] = max(-limit, min(limit, v + self._rng.uniform(-step, step)))

    def step(self) -> Dict[str, Dict[str, float]]:
        """推进一步随机游走，返回各传感器类型的 CDP 读数参数。"""
        self.alpha = (self.alpha + self._rng.uniform(-0.6, 0.6)) % 360.0
        self.beta = max(35.0, min(85.0, self.beta + self._rng.uniform(-0.25, 0.25)))
        self.gamma = max(-25.0, min(25.0, self.gamma + self._rng.uniform(-0.25, 0.25)))
        beta = math.radians(self.beta)
        gamma = math.radians(self.gamma)
        self._walk(self._linear, 0.12, 0.45)
        self._walk(self._gyro, 0.004, 0.02)
        quaternion = _quaternion_from_euler(self.alpha, self.beta, self.gamma)
        return {
            "accelerometer": {
                "x": -_GRAVITY * math.cos(beta) * math.sin(gamma),
                "y": _GRAVITY * math.sin(beta),
                "z": _GRAVITY * math.cos(beta) * math.cos(gamma),
            },
            "linear-acceleration": {"x": self._linear[0], "y": self._linear[1],
                                    "z": self._linear[2]},
            "gyroscope": {"x": self._gyro[0], "y": self._gyro[1], "z": self._gyro[2]},
            "relative-orientation": quaternion,
            "absolute-orientation": quaternion,
        }


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


# ---- Android WebGPU（GPUAdapterInfo）架构口径 ----
# 真机 Android Chrome 的 WebGPU 由 Dawn/Vulkan 驱动，架构名来自 Dawn 内置
# 的 gpu_info 数据（PCI 设备 ID → 架构映射，见 Dawn src/dawn/gpu_info.json）。
# 依据本仓库内核（148.0.7778.215）二进制内嵌字面量实证：Qualcomm 为代际级
# "adreno-4xx".."adreno-8xx"（非具体型号）；ARM 为 "midgard"/"bifrost"/
# "valhall"/"gen-5"；Samsung 为 "rdna-1".."rdna-4"；ImgTec 为 "d-series"。
# 非库内 GPU 返回 None（不伪造口径）。
_MALI_ARCH = {
    # 5th Gen（Dawn: 0xC/0xD/0xE0 千位段）
    "G310": "gen-5", "G615": "gen-5", "G715": "gen-5",
    "G625": "gen-5", "G725": "gen-5", "G925": "gen-5",
    # Valhall（4th Gen，0x9/0xA/0xB0 段）
    "G57": "valhall", "G68": "valhall", "G77": "valhall", "G78": "valhall",
    "G510": "valhall", "G610": "valhall", "G710": "valhall",
    # Bifrost（0x6/0x70 段）
    "G31": "bifrost", "G51": "bifrost", "G52": "bifrost", "G76": "bifrost",
    # Midgard（最高位为 0）
    "G71": "midgard", "G72": "midgard",
}
_XCLIPSE_ARCH = {"530": "rdna-2", "920": "rdna-2", "940": "rdna-3"}


def _android_webgpu(renderer: str) -> Optional[Dict[str, str]]:
    """从 WebGL 渲染器字符串还原真机 Dawn 会报告的 WebGPU 适配器口径。

    WebGL↔WebGPU 跨 API 一致性是检测站常用对照点：宿主 macOS 报
    "apple"/"metal-*"，与 Android 机型声明的 GPU 直接矛盾。规则与内核
    内置的 Dawn gpu_info 映射一致（见上方表注释）。
    """
    if "Adreno" in renderer:
        m = re.search(r"Adreno\s*(?:\(TM\))?\s*(\d{3,4})", renderer)
        if m and m.group(1)[0] in "45678":
            return {"vendor": "qualcomm", "architecture": f"adreno-{m.group(1)[0]}xx"}
    if "Mali" in renderer:
        m = re.search(r"Mali-(G\d+)", renderer)
        if m and m.group(1) in _MALI_ARCH:
            return {"vendor": "arm", "architecture": _MALI_ARCH[m.group(1)]}
    if "Xclipse" in renderer:
        m = re.search(r"Xclipse\s*(\d{3,4})", renderer)
        if m and m.group(1) in _XCLIPSE_ARCH:
            return {"vendor": "samsung", "architecture": _XCLIPSE_ARCH[m.group(1)]}
    if "PowerVR" in renderer:
        if "DXT" in renderer or "D-Series" in renderer:
            return {"vendor": "img-tec", "architecture": "d-series"}
        if re.search(r"\bGE\d{4}\b|Rogue", renderer):
            return {"vendor": "img-tec", "architecture": "rogue"}
    return None


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

    # WebGPU（GPUAdapterInfo）真机 Android Chrome 口径（Dawn/Vulkan）：宿主
    # 是 Apple GPU（vendor "apple" / "metal-*"），与 Android 机型声明的 GPU
    # 直接矛盾，WebGL↔WebGPU 跨 API 对照可命中；映射见 _android_webgpu。
    webgpu = _android_webgpu(renderer) if target_os == "android" else None

    # 媒体设备枚举（enumerateDevices）：真机 Android Chrome 至少枚举前后两个
    # 摄像头（videoinput ×2）；宿主 Mac 是单摄像头（×1），数量偏差可直接被
    # 设备枚举型检测捕获。audioinput/audiooutput 真机各 1，与宿主相同。
    media_video_inputs = 2 if target_os == "android" else 0

    # iOS Chrome（CriOS）口径：mobileExtra.browser == "chrome_ios"（兼容 "chrome"）。
    # 运行期在页面世界补齐 CriOS 检测面（__gCrWeb / 改写痕迹 /
    # webkit.messageHandlers / 无 window.chrome），见 _PATCH_TEMPLATE。
    ios_chrome = (
        {"version": str(extra.get("browserVersion") or "")}
        if target_os == "ios" and str(extra.get("browser") or "") in ("chrome", "chrome_ios")
        else None
    )

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
        # 旧版指纹无该字段：按真机属性差异兜底（iOS Safari / Android Chrome）
        remove_props = (list(_IOS_REMOVE_PROPS) if target_os == "ios"
                        else list(_ANDROID_REMOVE_PROPS))
    # 平台 API 差集：窗口/文档/navigator 三处按名删除 + Worker 全局差集。
    # 同样支持指纹侧覆盖（mobileExtra.removeGlobals / removeWorkerGlobals），
    # 缺省按平台事实展开；存量指纹无需重建。
    if target_os == "ios":
        remove_globals = extra.get("removeGlobals")
        if remove_globals is None:
            remove_globals = _IOS_GLOBAL_DROPS
        remove_worker_globals = extra.get("removeWorkerGlobals")
        if remove_worker_globals is None:
            remove_worker_globals = _IOS_WORKER_DROPS
    else:
        remove_globals = extra.get("removeGlobals")
        if remove_globals is None:
            remove_globals = _ANDROID_GLOBAL_DROPS
        remove_worker_globals = extra.get("removeWorkerGlobals")
        if remove_worker_globals is None:
            remove_worker_globals = _ANDROID_WORKER_DROPS
    # plugins/mimeTypes 恒为空：Android Chrome（构建开关 enable_pdf=!is_android）
    # 与 iOS WKWebView 都没有内置 PDF 插件体系。旧 Android 指纹里的
    # emptyPlugins=False 是历史遗留标记，不改变平台事实——运行期按平台收敛，
    # 存量指纹无需重建即可获得真机 PDF 面。
    empty_plugins = True

    # NetworkInformation.type：仅 Android 实现（MDN BCD chrome_android=38；
    # 桌面 chrome 仅 ChromeOS 有）。桌面内核上 'type' in navigator.connection
    # 为 false——「桌面装 Android」的单行级证据。旧指纹无该字段时按平台兜底
    # （与 empty_plugins 同理：存量档案无需重建）。
    connection_type = extra.get("connectionType")
    if connection_type is None and target_os == "android":
        connection_type = "wifi"

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
        "removeGlobals": list(remove_globals),
        "removeWorkerGlobals": list(remove_worker_globals),
        "emptyPlugins": empty_plugins,
        "connectionType": connection_type,
        "ios": target_os == "ios",
        "iosChrome": ios_chrome,
        "webgl": webgl,
        "webgpu": webgpu,
        "mediaVideoInputs": media_video_inputs,
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
            var mask = function (fn, name, src) {
                try {
                    if (typeof fn === 'function') {
                        registry.set(fn, src || ('function ' + name + '() { [native code] }'));
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

    // 原生 DOM 属性 getter 由 C++ 实现，会校验接收者的内部槽（wrapper type）：
    // 在缺少该槽的对象上调用——典型如
    //   Object.getOwnPropertyDescriptor(Navigator.prototype, 'userAgent')
    //         .get.call(Object.create(navigator))
    // ——必须抛 TypeError: Illegal invocation。JS 模拟 getter 若忽略 this 直接返回
    // 闭包值，检测站点一眼就能看出这是人工模拟对象。
    //
    // JS 读不到内部槽，这里用原型链做等价判定：recv 合格 ⇔ proto 在其原型链上，
    // 且 recv 到 proto 之间没有任何「实例」——中间每一环都必须是原型对象。
    // （注意不能用 isPrototypeOf / instanceof：Object.create(navigator) 的原型链
    //  经过 navigator 本身，两者都会误判为真。）
    // 「X 是原型对象」⇔ X === proto，或 X.constructor.prototype === X。
    var __isProtoObj = function (x, proto) {
        if (x === proto) { return true; }
        try {
            var c = x.constructor;
            if (typeof c === 'function' && c.prototype === x) { return true; }
        } catch (e) {}
        return false;
    };
    var __slotHas = function (proto, recv) {
        if (recv === null || recv === undefined) { return false; }
        var t = typeof recv;
        if (t !== 'object' && t !== 'function') { return false; }
        var x = recv;
        for (var i = 0; i < 8; i++) {
            try { x = Object.getPrototypeOf(x); } catch (e) { return false; }
            if (x === proto) { return true; }
            if (x === null || x === undefined) { return false; }
            if (!__isProtoObj(x, proto)) { return false; }
        }
        return false;
    };
    var defineGet = function (proto, name, value) {
        try {
            var getter = __maskNative(function () {
                if (!__slotHas(proto, this)) { throw new TypeError('Illegal invocation'); }
                return value;
            }, 'get ' + name);
            Object.defineProperty(proto, name, {
                get: getter,
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
    // 与 defineFn 相同，但自定义 toString() 返回的源码文本：真机 Chrome iOS
    // 改写的 history/print/scrollTo 是非原生外观（源码含 invokeOnHost /
    // setWebViewScrollViewIsDragging 字样），不能伪装成 [native code]。
    var defineFnSrc = function (proto, name, fn, src) {
        try {
            Object.defineProperty(proto, name, {
                value: __maskNative(fn, name, src),
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
            if (!D.iosChrome) {
                // iOS Safari 专有：standalone（非独立 PWA 模式恒为 false）；
                // CriOS 是 WKWebView 壳，不暴露该属性（待真机校准，暂不定义）
                defineGet(N, 'standalone', false);
            }
            // 真机 iOS（Safari / Chrome）普通页面没有 window.chrome；桌面内核
            // 必然携带该对象且属性 configurable:false（删除/重定义均不可行）。
            // 赋值 undefined 后 typeof/truthiness 检查通过；'chrome' in window
            // 与 getOwnPropertyDescriptor 的残余暴露属内核层限制（已知边界）。
            try { window.chrome = undefined; } catch (e) {}

            // ---- iOS WebKit 与桌面 Chromium 的页面/窗口级属性差 ----
            // removeProp 只覆盖 navigator；以下两项在 Window / Performance 原型上。
            // 1) File System Access（showOpenFilePicker）：Safari 从未实现；
            try { delete Window.prototype.showOpenFilePicker; } catch (e) {}
            try { delete window.showOpenFilePicker; } catch (e) {}
            // 2) performance.memory（MemoryInfo）：WebKit 未实现；桌面内核携带，
            //    不抹除时 'memory' in performance 一行即可判「不是 Safari」。
            try { delete Performance.prototype.memory; } catch (e) {}
            try { delete performance.memory; } catch (e) {}
            if ('memory' in performance) {
                defineGet(Performance.prototype, 'memory', undefined);
            }
            // 3) iOS WebKit 专有手势事件家族：GestureEvent + ongesturestart 面。
            //    MDN BCD：chrome/chrome_android=false、safari_ios=2 —— 这是
            //    「Chromium 内核装 iOS」在事件层最直接的证据之一。按真机形态
            //    补最小完备面：构造器为 UIEvent 子类（原型链/instanceof 成立），
            //    rotation/scale 为原型访问器，处理属性挂在 Window.prototype
            //    （enumerable，与原生事件处理属性一致）。
            if (typeof window.GestureEvent === 'undefined') {
                try {
                    var __geState = new WeakMap();
                    var __GE = __maskNative(function GestureEvent(type, init) {
                        init = init || {};
                        var e = new UIEvent(type, {
                            bubbles: !!init.bubbles,
                            cancelable: !!init.cancelable,
                            view: init.view || null,
                            detail: init.detail || 0,
                        });
                        try { Object.setPrototypeOf(e, __GE.prototype); } catch (e2) {}
                        __geState.set(e, {
                            rotation: init.rotation || 0,
                            scale: init.scale === undefined ? 1 : init.scale,
                        });
                        return e;
                    }, 'GestureEvent');
                    __GE.prototype = Object.create(UIEvent.prototype, {
                        constructor: { value: __GE, writable: true, configurable: true },
                    });
                    Object.defineProperty(__GE.prototype, 'rotation', {
                        get: __maskNative(function () {
                            var s = __geState.get(this);
                            return s ? s.rotation : 0;
                        }, 'get rotation'),
                        enumerable: true, configurable: true,
                    });
                    Object.defineProperty(__GE.prototype, 'scale', {
                        get: __maskNative(function () {
                            var s = __geState.get(this);
                            return s ? s.scale : 1;
                        }, 'get scale'),
                        enumerable: true, configurable: true,
                    });
                    Object.defineProperty(window, 'GestureEvent', {
                        value: __GE, writable: true, enumerable: false, configurable: true,
                    });
                    var __geHandlers = ['ongesturestart', 'ongesturechange', 'ongestureend'];
                    var __geDefineHandler = function (__hn) {
                        try {
                            Object.defineProperty(Window.prototype, __hn, {
                                get: __maskNative(function () {
                                    return null;
                                }, 'get ' + __hn),
                                set: __maskNative(function (v) {
                                }, 'set ' + __hn),
                                enumerable: true, configurable: true,
                            });
                        } catch (e3) {}
                    };
                    for (var __gi = 0; __gi < __geHandlers.length; __gi++) {
                        __geDefineHandler(__geHandlers[__gi]);
                    }
                } catch (e) {}
            }
            // 4) 传感器权限面（iOS 14.5+）：DeviceMotionEvent/DeviceOrientationEvent
            //    的 requestPermission 静态方法。MDN BCD：chrome=false、
            //    chrome_android=false、safari_ios=14.5（Chromium 到 152 才实现，
            //    148 内核与 Chrome Android 口径都不该出现）——一条 typeof 判断
            //    即可判「不是 iOS」。真机语义：无瞬时激活 → reject
            //    NotAllowedError（WebKit 原文案）；有激活 → 弹系统授权框，
            //    Promise 保持 pending 直到用户作答。仿真不弹系统框（无法伪造
            //    用户决定），保持 pending 与「用户未作答」一致，不伪造
            //    granted/denied；未授权时真机本就不派发传感器事件。
            var __sensorRequestPermission = function () {
                return new Promise(function (resolve, reject) {
                    var act = navigator.userActivation;
                    if (!(act && act.isActive)) {
                        reject(new DOMException('Requesting device orientation '
                            + 'or motion access requires a user gesture',
                            'NotAllowedError'));
                    }
                });
            };
            var __defineSensorRequestPermission = function (iface) {
                if (typeof iface === 'undefined') {
                    return;
                }
                try {
                    Object.defineProperty(iface, 'requestPermission', {
                        value: __maskNative(__sensorRequestPermission, 'requestPermission'),
                        writable: true, enumerable: true, configurable: true,
                    });
                } catch (e) {}
            };
            __defineSensorRequestPermission(window.DeviceMotionEvent);
            __defineSensorRequestPermission(window.DeviceOrientationEvent);
            // 5) absolute：iOS Safari 的 DeviceOrientationEvent 无 absolute 属性
            //    （MDN BCD：safari_ios=false，桌面 Safari 17 才有）——真机一行
            //    `'absolute' in DeviceOrientationEvent.prototype` 即可判内核。
            try {
                if (typeof DeviceOrientationEvent !== 'undefined') {
                    delete DeviceOrientationEvent.prototype.absolute;
                    if ('absolute' in DeviceOrientationEvent.prototype) {
                        defineGet(DeviceOrientationEvent.prototype, 'absolute', undefined);
                    }
                }
            } catch (e) {}
            // 6) deviceorientationabsolute：WebKit 全平台无该事件（caniuse：
            //    Safari/Safari iOS 不支持，Chrome 50+ 支持）——处理属性一并抹除。
            try { delete Window.prototype.ondeviceorientationabsolute; } catch (e) {}
            try { delete window.ondeviceorientationabsolute; } catch (e) {}
            if ('ondeviceorientationabsolute' in window) {
                defineGet(Window.prototype, 'ondeviceorientationabsolute', undefined);
            }
            // 7) 未授权时真机为零事件：桌面内核没有传感器，注册监听会立刻收到
            //    一次全 null 的哨兵 devicemotion/deviceorientation（实测各 1 次）。
            //    真机 iOS 未授权时连这一次都没有，「监听 1.5s 计数 > 0」即可
            //    判非 iOS。init 脚本先于页面脚本执行，捕获阶段监听先注册先执行，
            //    用 stopImmediatePropagation 抹掉内核派发；只有 isTrusted
            //    （内核派发）的事件被拦，脚本自派发保持原语义。
            try {
                var __killSensorSentinel = function (ev) {
                    if (ev.isTrusted) {
                        ev.stopImmediatePropagation();
                    }
                };
                window.addEventListener('devicemotion', __killSensorSentinel, true);
                window.addEventListener('deviceorientation', __killSensorSentinel, true);
                // WebKit 全平台没有该事件（6) 已抹除处理属性）；桌面内核注册
                // 监听时同样会派发一次该事件的哨兵，不抑制时「doa 事件计数 1」
                // 与真机（0）可辨。
                window.addEventListener('deviceorientationabsolute',
                                        __killSensorSentinel, true);
            } catch (e) {}

            // 8) 元素级全屏 API：iPhone 上 WebKit 整块缺席。MDN BCD
            //    （Element.requestFullscreen / Document.fullscreenEnabled，
            //    safari_ios 16.4）注「Only available on iPad, not on iPhone」；
            //    WebKit 源码 Element+Fullscreen.idl / Document+Fullscreen.idl /
            //    DocumentOrShadowRoot+Fullscreen.idl 整块由
            //    EnabledBySettingOrQuirk=FullScreenEnabled|… +
            //    DisabledByQuirk=shouldDisableElementFullscreen 门控，iPhone 上
            //    标准版、webkit/Mozilla 前缀版与 onfullscreen* 处理属性全部不存在
            //    （2026-08 真机实测报告一致：元素级 requestFullscreen 与
            //    webkitRequestFullscreen 均 undefined，仅 <video> 有全屏通道）。
            //    真机一行 `'requestFullscreen' in document.documentElement` 判内核。
            //    注：iPad Safari 元素级全屏自 16.4 起可用 → 义表型设备保留现状。
            var __isIPad = /iPad/.test(String(D.platform || '')) || /iPad/.test(String(D.ua || ''));
            if (!__isIPad) {
                var __fsKill = function (obj, names) {
                    if (!obj) { return; }
                    for (var __fi = 0; __fi < names.length; __fi++) {
                        try { delete obj[names[__fi]]; } catch (e) {}
                    }
                };
                __fsKill(Element.prototype, [
                    'requestFullscreen', 'webkitRequestFullscreen', 'webkitRequestFullScreen',
                    'onfullscreenchange', 'onfullscreenerror',
                    'onwebkitfullscreenchange', 'onwebkitfullscreenerror']);
                __fsKill(Document.prototype, [
                    'fullscreenEnabled', 'fullscreen', 'fullscreenElement', 'exitFullscreen',
                    'webkitFullscreenEnabled', 'webkitFullscreenElement', 'webkitExitFullscreen',
                    'webkitCancelFullScreen', 'webkitIsFullScreen',
                    'webkitFullScreenKeyboardInputAllowed', 'webkitCurrentFullScreenElement',
                    'onfullscreenchange', 'onfullscreenerror',
                    'onwebkitfullscreenchange', 'onwebkitfullscreenerror']);
                if (typeof ShadowRoot !== 'undefined') {
                    __fsKill(ShadowRoot.prototype, ['fullscreenElement']);
                }
            }

            // 9) <video> 的 webkit 全屏家族：iPhone/iPad 全有
            //    （HTMLVideoElement.idl 的 webkitSupportsFullscreen /
            //    webkitDisplayingFullscreen / webkitEnterFullscreen /
            //    webkitExitFullscreen 及其 FullScreen 大写拼写别名均无平台守卫），
            //    也是 iPhone 上唯一的元素级全屏通道。视频站点普遍用
            //    `video.webkitEnterFullscreen` 做 iOS 能力探测，桌面内核带
            //    HTMLVideoElement 但全部缺失 → 一行判假。
            //    语义：webkitSupportsFullscreen=元素已有媒体源（真机无源 false）；
            //    webkitDisplayingFullscreen 跟随 enter/exit 状态机（仿真无真实
            //    全屏，只对齐可观察状态，不打扰视频播放）。
            if (typeof HTMLVideoElement !== 'undefined') {
                try {
                    var __vfProto = HTMLVideoElement.prototype;
                    if (!('webkitEnterFullscreen' in __vfProto)) {
                        var __vfState = new WeakSet();
                        var __vfEnterOf = function () {
                            return function () {
                                try { __vfState.add(this); } catch (e) {}
                            };
                        };
                        var __vfExitOf = function () {
                            return function () {
                                try { __vfState.delete(this); } catch (e) {}
                            };
                        };
                        defineFn(__vfProto, 'webkitEnterFullscreen', __vfEnterOf());
                        defineFn(__vfProto, 'webkitEnterFullScreen', __vfEnterOf());
                        defineFn(__vfProto, 'webkitExitFullscreen', __vfExitOf());
                        defineFn(__vfProto, 'webkitExitFullScreen', __vfExitOf());
                        Object.defineProperty(__vfProto, 'webkitSupportsFullscreen', {
                            get: __maskNative(function () {
                                try {
                                    return !!(this.currentSrc || this.src);
                                } catch (e) { return false; }
                            }, 'get webkitSupportsFullscreen'),
                            enumerable: true, configurable: true,
                        });
                        Object.defineProperty(__vfProto, 'webkitDisplayingFullscreen', {
                            get: __maskNative(function () {
                                try { return __vfState.has(this); } catch (e) { return false; }
                            }, 'get webkitDisplayingFullscreen'),
                            enumerable: true, configurable: true,
                        });
                    }
                } catch (e) {}
            }

            // 10) 传感器构造器不可构造：WebKit 的 DeviceMotionEvent /
            //     DeviceOrientationEvent 没有构造器操作——DeviceMotionEvent.idl
            //     原文「FIXME: Add support for DeviceMotionEvent constructor.」；
            //     MDN BCD constructible：safari/safari_ios=false、chrome=59。
            //     真机 `new DeviceMotionEvent('devicemotion')` 一行 throw 判内核。
            //     用 Proxy 包装原生构造器（apply/construct 均抛 TypeError
            //     「Illegal constructor」，与 WebKit 默认文案一致），原型链、
            //     instanceof、toString 外观、prototype.constructor 指向与 window
            //     属性描述符全部保持原生形态（Proxy 无 JS 可观测品牌）。
            var __sensorNatives = {};
            var __noCtor = function (name) {
                try {
                    var __Native = window[name];
                    if (typeof __Native !== 'function') {
                        return;
                    }
                    __sensorNatives[name] = __Native;
                    if (typeof Proxy === 'undefined') {
                        return;
                    }
                    var __throwCtor = function () {
                        throw new TypeError('Illegal constructor');
                    };
                    var __proxy = new Proxy(__Native, { apply: __throwCtor, construct: __throwCtor });
                    var __wdesc = Object.getOwnPropertyDescriptor(window, name);
                    Object.defineProperty(window, name, {
                        value: __proxy,
                        writable: __wdesc ? !!__wdesc.writable : true,
                        enumerable: __wdesc ? !!__wdesc.enumerable : false,
                        configurable: __wdesc ? !!__wdesc.configurable : true,
                    });
                    if (__Native.prototype) {
                        Object.defineProperty(__Native.prototype, 'constructor', {
                            value: __proxy, writable: true, enumerable: false, configurable: true,
                        });
                    }
                } catch (e) {}
            };
            __noCtor('DeviceMotionEvent');
            __noCtor('DeviceOrientationEvent');

            // 11) iOS 传感器事件的完整形态面（罗盘属性 + legacy 初始化器 + createEvent）：
            //     a) WebKit 的 DeviceOrientationEvent 在 iOS 构建下没有 absolute，
            //        取而代之 webkitCompassHeading / webkitCompassAccuracy
            //        （DeviceOrientationEvent.idl 的 #if WTF_PLATFORM_IOS_FAMILY
            //        分支；非 iOS 构建才有 absolute）。真机一行
            //        `'webkitCompassHeading' in DeviceOrientationEvent.prototype` 判 iOS；
            //        无罗盘数据时为 null。
            //     b) legacy 初始化器：WebKit 的 DeviceMotionEvent /
            //        DeviceOrientationEvent 都有 initDeviceMotionEvent /
            //        initDeviceOrientationEvent（iOS 版 DOE 的第 7/8 参数是
            //        compassHeading/compassAccuracy，非 absolute），Chromium 148
            //        已移除；真机 `'initDeviceMotionEvent' in
            //        DeviceMotionEvent.prototype` 一行判假。补齐后 alpha/beta/gamma
            //        （与 acceleration / accelerationIncludingGravity /
            //        rotationRate / interval）需回读一致，故以 WeakMap 存值 +
            //        覆盖原型 getter（引擎派发的事件不在存储里，仍走原生 getter，
            //        行为不变）；字典参数按 WebIDL 转换（缺键/非对象 → null 字段，
            //        每次读取返回新对象，与真机字典返回值一致）。
            //     c) document.createEvent('DeviceMotionEvent'/'DeviceOrientationEvent')：
            //        WebKit 的 Document::createEvent 明确支持这两个名字
            //        （Document.cpp 的 ENABLE(DEVICE_ORIENTATION) 分支），Chromium
            //        148 抛 NotSupportedError；真机一行判假，故整链补齐（返回值用
            //        原生构造器造未初始化实例：type=''，与 createForBindings 同形态）。
            var __dblOrNull = function (v) {
                if (v === undefined || v === null) { return null; }
                return Number(v);
            };
            var __dictOrNull = function (v, keys) {
                if (v === undefined || v === null) { return null; }
                var o = Object(v);
                var out = {};
                for (var __ki = 0; __ki < keys.length; __ki++) {
                    out[keys[__ki]] = __dblOrNull(o[keys[__ki]]);
                }
                return out;
            };
            var __overrideSensorGet = function (proto, prop, read) {
                try {
                    var d = Object.getOwnPropertyDescriptor(proto, prop);
                    if (!d || typeof d.get !== 'function') { return; }
                    var __nativeGet = d.get;
                    var desc = {
                        get: __maskNative(function () { return read(this, __nativeGet); },
                            'get ' + prop),
                        enumerable: d.enumerable, configurable: true,
                    };
                    if (d.set) { desc.set = d.set; }
                    Object.defineProperty(proto, prop, desc);
                } catch (e) {}
            };
            var __legacyInit = function (proto, name, store, values) {
                try {
                    if (typeof proto[name] === 'function') { return; }
                    var fn = function () {
                        var args = arguments;
                        Event.prototype.initEvent.call(this, args[0], !!args[1], !!args[2]);
                        try { store.set(this, values.call(this, args)); } catch (e) {}
                    };
                    try {
                        Object.defineProperty(fn, 'length', { value: 0, configurable: true });
                    } catch (e) {}
                    defineFn(proto, name, fn);
                } catch (e) {}
            };
            if (typeof DeviceOrientationEvent !== 'undefined') {
                try {
                    var __compass = new WeakMap();
                    var __legacyOrient = new WeakMap();
                    var __compassOf = function (ev) {
                        try { return __compass.get(ev) || null; } catch (e) { return null; }
                    };
                    Object.defineProperty(DeviceOrientationEvent.prototype, 'webkitCompassHeading', {
                        get: __maskNative(function () {
                            var s = __compassOf(this);
                            return s ? s.heading : null;
                        }, 'get webkitCompassHeading'),
                        enumerable: true, configurable: true,
                    });
                    Object.defineProperty(DeviceOrientationEvent.prototype, 'webkitCompassAccuracy', {
                        get: __maskNative(function () {
                            var s = __compassOf(this);
                            return s ? s.accuracy : null;
                        }, 'get webkitCompassAccuracy'),
                        enumerable: true, configurable: true,
                    });
                    var __orientProps = ['alpha', 'beta', 'gamma'];
                    for (var __oi = 0; __oi < __orientProps.length; __oi++) {
                        (function (prop) {
                            __overrideSensorGet(DeviceOrientationEvent.prototype, prop,
                                function (ev, nativeGet) {
                                    var s = __legacyOrient.get(ev);
                                    if (s && s[prop] !== undefined) { return s[prop]; }
                                    return nativeGet.call(ev);
                                });
                        })(__orientProps[__oi]);
                    }
                    __legacyInit(DeviceOrientationEvent.prototype, 'initDeviceOrientationEvent',
                        __legacyOrient,
                        function (args) {
                            var out = {
                                alpha: __dblOrNull(args[3]),
                                beta: __dblOrNull(args[4]),
                                gamma: __dblOrNull(args[5]),
                            };
                            try {
                                __compass.set(this, {
                                    heading: __dblOrNull(args[6]),
                                    accuracy: __dblOrNull(args[7]),
                                });
                            } catch (e) {}
                            return out;
                        });
                } catch (e) {}
            }
            if (typeof DeviceMotionEvent !== 'undefined') {
                try {
                    var __legacyMotion = new WeakMap();
                    var __motionDicts = {
                        acceleration: ['x', 'y', 'z'],
                        accelerationIncludingGravity: ['x', 'y', 'z'],
                        rotationRate: ['alpha', 'beta', 'gamma'],
                    };
                    var __motionProps = ['acceleration', 'accelerationIncludingGravity',
                                         'rotationRate', 'interval'];
                    for (var __mi = 0; __mi < __motionProps.length; __mi++) {
                        (function (prop) {
                            var keys = __motionDicts[prop] || null;
                            __overrideSensorGet(DeviceMotionEvent.prototype, prop,
                                function (ev, nativeGet) {
                                    var s = __legacyMotion.get(ev);
                                    if (!s || s[prop] === undefined) {
                                        return nativeGet.call(ev);
                                    }
                                    if (!keys || s[prop] === null) { return s[prop]; }
                                    var out = {};
                                    for (var __ki = 0; __ki < keys.length; __ki++) {
                                        out[keys[__ki]] = s[prop][keys[__ki]];
                                    }
                                    return out;
                                });
                        })(__motionProps[__mi]);
                    }
                    __legacyInit(DeviceMotionEvent.prototype, 'initDeviceMotionEvent',
                        __legacyMotion,
                        function (args) {
                            return {
                                acceleration: __dictOrNull(args[3], ['x', 'y', 'z']),
                                accelerationIncludingGravity:
                                    __dictOrNull(args[4], ['x', 'y', 'z']),
                                rotationRate: __dictOrNull(args[5], ['alpha', 'beta', 'gamma']),
                                interval: __dblOrNull(args[6]),
                            };
                        });
                } catch (e) {}
            }
            try {
                var __origCreateEvent = Document.prototype.createEvent;
                var __sensorEventNames = {
                    devicemotionevent: 'DeviceMotionEvent',
                    deviceorientationevent: 'DeviceOrientationEvent',
                };
                var __createEvent = function (type) {
                    var iface = __sensorEventNames[String(type).toLowerCase()];
                    if (iface) {
                        var native = __sensorNatives[iface];
                        if (typeof native === 'function') {
                            return new native('');
                        }
                    }
                    return __origCreateEvent.apply(this, arguments);
                };
                try {
                    Object.defineProperty(__createEvent, 'length', {
                        value: __origCreateEvent.length, configurable: true,
                    });
                } catch (e) {}
                defineFn(Document.prototype, 'createEvent', __createEvent);
            } catch (e) {}

            // 12) WebKit 专属 CSS 面：-webkit-touch-callout（WebKit CSSProperties.json
            //     非标准属性，值 default|none；MDN BCD 仅 safari_ios=2，chrome /
            //     chrome_android=false）与 -webkit-overflow-scrolling（同源非标准，
            //     值 auto|touch；Chromium 从未实现）。iOS 检测脚本的经典组合正是
            //     `CSS.supports('-webkit-touch-callout', 'none')` /
            //     `'webkitTouchCallout' in el.style`——桌面内核装 iOS 一行判假。
            //     真机语义：CSS.supports 两参（含值合法性）与单参条件文本（含复合
            //     条件）都按键值合法性作答；驼峰属性是 CSS 属性访问器，读写与
            //     getPropertyValue / setProperty / removeProperty 同源；非法值静默
            //     忽略（不覆盖旧值）。访问器 enumerable:false：真机 WebKit 的 CSS
            //     属性是不可枚举的具名属性（`for...in` 不列出）。
            //     已知边界：getComputedStyle 侧无级联/used value（引擎级能力，无法
            //     从 JS 补齐）——返回内联存储值或 ''；cssText 写入不识别这两项。
            try {
                var __wf = Object.create(null);
                __wf['-webkit-touch-callout'] = {
                    camel: 'webkitTouchCallout', values: ['default', 'none'],
                };
                __wf['-webkit-overflow-scrolling'] = {
                    camel: 'webkitOverflowScrolling', values: ['auto', 'touch'],
                };
                var __cssStore = new WeakMap();
                var __wfInfo = function (name) {
                    if (typeof name !== 'string') { return null; }
                    var key = name.trim().toLowerCase();
                    var info = __wf[key];
                    return info ? { key: key, info: info } : null;
                };
                var __wfValueOk = function (info, value) {
                    if (value === undefined || value === null) { return false; }
                    var v = String(value).trim().toLowerCase();
                    for (var __vi = 0; __vi < info.values.length; __vi++) {
                        if (info.values[__vi] === v) { return true; }
                    }
                    return false;
                };
                var __wfStore = function (decl) {
                    var s = __cssStore.get(decl);
                    if (!s) {
                        s = Object.create(null);
                        __cssStore.set(decl, s);
                    }
                    return s;
                };
                for (var __wk in __wf) {
                    (function (key, info) {
                        try {
                            if (Object.getOwnPropertyDescriptor(
                                    CSSStyleDeclaration.prototype, info.camel)) {
                                return;
                            }
                            Object.defineProperty(CSSStyleDeclaration.prototype, info.camel, {
                                configurable: true,
                                enumerable: false,
                                get: __maskNative(function () {
                                    var s = __cssStore.get(this);
                                    return (s && s[key] !== undefined) ? s[key] : '';
                                }, 'get ' + info.camel),
                                set: __maskNative(function (v) {
                                    if (__wfValueOk(info, v)) {
                                        __wfStore(this)[key] = String(v).trim().toLowerCase();
                                    }
                                }, 'set ' + info.camel),
                            });
                        } catch (e) {}
                    })(__wk, __wf[__wk]);
                }
                var __matchLen = function (fn, ref) {
                    try {
                        Object.defineProperty(fn, 'length', {
                            value: ref.length, configurable: true,
                        });
                    } catch (e) {}
                    return fn;
                };
                var __csProto = CSSStyleDeclaration.prototype;
                var __origSetProperty = __csProto.setProperty;
                var __origGetPropertyValue = __csProto.getPropertyValue;
                var __origRemoveProperty = __csProto.removeProperty;
                __csProto.setProperty = __maskNative(__matchLen(function (property, value, priority) {
                    var hit = __wfInfo(property);
                    if (hit) {
                        if (__wfValueOk(hit.info, value)) {
                            __wfStore(this)[hit.key] = String(value).trim().toLowerCase();
                        }
                        return;
                    }
                    return __origSetProperty.apply(this, arguments);
                }, __origSetProperty), 'setProperty');
                __csProto.getPropertyValue = __maskNative(__matchLen(function (property) {
                    var hit = __wfInfo(property);
                    if (hit) {
                        var s = __cssStore.get(this);
                        return (s && s[hit.key] !== undefined) ? s[hit.key] : '';
                    }
                    return __origGetPropertyValue.apply(this, arguments);
                }, __origGetPropertyValue), 'getPropertyValue');
                __csProto.removeProperty = __maskNative(__matchLen(function (property) {
                    var hit = __wfInfo(property);
                    if (hit) {
                        var s = __cssStore.get(this);
                        var prev = (s && s[hit.key] !== undefined) ? s[hit.key] : '';
                        if (s) { delete s[hit.key]; }
                        return prev;
                    }
                    return __origRemoveProperty.apply(this, arguments);
                }, __origRemoveProperty), 'removeProperty');
                var __origSupports = (typeof CSS !== 'undefined' && CSS
                    && typeof CSS.supports === 'function') ? CSS.supports : null;
                if (__origSupports) {
                    var __fakeSupports = function (a, b) {
                        try {
                            if (arguments.length >= 2) {
                                var hit2 = __wfInfo(a);
                                if (hit2) { return __wfValueOk(hit2.info, b); }
                            } else if (arguments.length === 1) {
                                var text = String(a);
                                if (/webkit-(touch-callout|overflow-scrolling)/i.test(text)) {
                                    var rewritten = text.replace(
                                        /-webkit-(touch-callout|overflow-scrolling)\s*:\s*([^)]*)/gi,
                                        function (m, prop, val) {
                                            var info = __wf['-webkit-' + prop.toLowerCase()];
                                            return __wfValueOk(info, val)
                                                ? '(color: red)'
                                                : '(color: __fp_invalid_color_0x9)';
                                        });
                                    return __origSupports.call(CSS, rewritten);
                                }
                            }
                        } catch (e) {}
                        return __origSupports.apply(CSS, arguments);
                    };
                    __matchLen(__fakeSupports, __origSupports);
                    Object.defineProperty(CSS, 'supports', {
                        value: __maskNative(__fakeSupports, 'supports'),
                        writable: true, enumerable: true, configurable: true,
                    });
                }
            } catch (e) {}

            // 13) Safari 全局面（WebKit main / iOS 27 口径）：
            //     a) WebKitPoint + webkitConvertPointFromPageToNode /
            //        webkitConvertPointFromNodeToPage。DOMWindow.idl:120-122 原文
            //        注「Blink has already dropped these」；MDN BCD：
            //        chrome/chrome_android/webview_android 移除于 39、edge 79、
            //        opera/samsung 同期，safari 4 / safari_ios 3 / webview_ios 3
            //        保留 → 桌面内核一行
            //        `typeof window.webkitConvertPointFromNodeToPage === 'function'`
            //        判「不是 Safari」。真机语义（WebKit Node::convertToPage /
            //        convertFromPage，均 MapCoordinatesMode::UseTransforms）：
            //        node 无 layout box 时沿 parentElement 上溯，上溯到头则原样
            //        返回；本实现以 border box（getBoundingClientRect + 滚动偏移）
            //        换算——无变换元素与真机一致（layout 测试的 (0,0)/(5,40) 往返
            //        口径），rotate/scale/skew 场景退化为 AABB（文档边界）。
            //        WebIDL 转换口径（LayoutTests/fast/dom/Window/
            //        webkitConvertPoint.html）：node 非 Node、p 非 WebKitPoint
            //        且非 null/undefined → TypeError；node/p 任一 null → null。
            //     b) webkitCancelRequestAnimationFrame：DOMWindow.idl:110
            //        [ImplementedAs=cancelAnimationFrame]（webkitRequestAnimationFrame
            //        / webkitCancelAnimationFrame 内核本就携带）。
            //     c) WebKitNamespace / UserMessageHandlersNamespace /
            //        UserMessageHandler 三个接口对象：WebKit 的 IDL 均无
            //        LegacyNoInterfaceObject、Conditional=USER_MESSAGE_HANDLERS
            //        （Cocoa 编译开启，PlatformEnableCocoa.h）→ 真机页面世界接口
            //        对象存在且不可构造（IDL 无 constructor）；实例 window.webkit
            //        仅当宿主为「页面世界」注册过消息处理器时出现
            //        （JSDOMWindowCustom.cpp:227 → LocalDOMWindow::
            //        shouldHaveWebKitNamespaceForWorld：页面世界
            //        hasUserMessageHandler 才为 true）——Safari 无、CriOS 有。
            //        原型成员按 world 门控：evaluateScript(isNonNormal) /
            //        createJSHandle / createNodeSnapshot 在页面世界缺席，
            //        messageHandlers / buffers 保留（WebKitNamespace.idl）。
            //        CriOS 页面世界注册的通道：crwebinvoke（注入桥 core，§9.4
            //        源码级确认）+ POST 两通道（POST 导航 workaround，B1-7 待
            //        按导航态条件注入）；真机 window.webkit 为 DontDelete+ReadOnly
            //        （JSC slot 属性：writable:false / enumerable:true /
            //        configurable:false），postMessage 返回 Promise 且宿主未启用
            //        回执时保持 pending（java_script_content_world.mm 页面世界
            //        分支 CHECK 无回执）。
            try {
                var __wkFround = (typeof Math.fround === 'function')
                    ? Math.fround : function (v) { return v; };
                var __wkPtStore = new WeakMap();
                var __wkPtRec = function (recv) {
                    var s = null;
                    try { s = __wkPtStore.get(recv); } catch (e) {}
                    if (!s) { throw new TypeError('Illegal invocation'); }
                    return s;
                };
                if (typeof window.WebKitPoint === 'undefined') {
                    var __WKP = __maskNative(function WebKitPoint(x, y) {
                        if (!(this instanceof __WKP)) {
                            throw new TypeError('Illegal constructor');
                        }
                        var px = (arguments.length > 0 && x !== undefined) ? x : 0;
                        var py = (arguments.length > 1 && y !== undefined) ? y : 0;
                        __wkPtStore.set(this, { x: __wkFround(px), y: __wkFround(py) });
                    }, 'WebKitPoint');
                    try {
                        Object.defineProperty(__WKP, 'length', { value: 0, configurable: true });
                    } catch (e) {}
                    try {
                        // 接口对象 prototype 属性 = ReadOnly|DontEnum|DontDelete
                        Object.defineProperty(__WKP, 'prototype', { writable: false });
                    } catch (e) {}
                    var __wkPtMember = function (name) {
                        Object.defineProperty(__WKP.prototype, name, {
                            configurable: true, enumerable: true,
                            get: __maskNative(function () {
                                return __wkPtRec(this)[name];
                            }, 'get ' + name),
                            set: __maskNative(function (v) {
                                __wkPtRec(this)[name] = __wkFround(v);
                            }, 'set ' + name),
                        });
                    };
                    __wkPtMember('x');
                    __wkPtMember('y');
                    Object.defineProperty(__WKP.prototype, 'constructor', {
                        value: __WKP, writable: true, enumerable: false, configurable: true,
                    });
                    try {
                        Object.defineProperty(__WKP.prototype, Symbol.toStringTag, {
                            value: 'WebKitPoint', writable: false,
                            enumerable: false, configurable: true,
                        });
                    } catch (e) {}
                    Object.defineProperty(window, 'WebKitPoint', {
                        value: __WKP, writable: true, enumerable: false, configurable: true,
                    });
                }
                // WebIDL Node? 品牌校验：借原生 nodeType 访问器（跨 realm 可用）
                var __wkNodeTypeGet = null;
                try {
                    __wkNodeTypeGet = Object.getOwnPropertyDescriptor(
                        Node.prototype, 'nodeType').get;
                } catch (e) {}
                var __wkIsNode = function (v) {
                    if (!v || typeof v !== 'object') { return false; }
                    if (__wkNodeTypeGet) {
                        try { __wkNodeTypeGet.call(v); return true; } catch (e) { return false; }
                    }
                    return v instanceof Node;
                };
                var __wkAnchorOf = function (node) {
                    var n = node;
                    while (n) {
                        if (n.nodeType === 9) {          // Document：LayoutView 原点 == 文档原点
                            n = n.documentElement;
                            continue;
                        }
                        if (n.nodeType !== 1) {          // 文本/注释：真机用其 renderer，仿真取父元素
                            n = n.parentElement;
                            continue;
                        }
                        var rects = null;
                        try { rects = n.getClientRects(); } catch (e) { rects = null; }
                        if (rects && rects.length > 0) { return n; }
                        n = n.parentElement;
                    }
                    return null;
                };
                var __wkDefineConvert = function (toPage) {
                    var name = toPage
                        ? 'webkitConvertPointFromNodeToPage'
                        : 'webkitConvertPointFromPageToNode';
                    if (typeof window[name] === 'function') { return; }
                    var fn = __maskNative(function (node, p) {
                        var n = node;
                        if (n === undefined || n === null) { return null; }
                        if (!__wkIsNode(n)) {
                            throw new TypeError('Argument 1 (node) to Window.' + name
                                + ' must be an instance of Node');
                        }
                        var pt = p;
                        if (pt === undefined || pt === null) { return null; }
                        if (!(pt instanceof __WKP)) {
                            throw new TypeError('Argument 2 (p) to Window.' + name
                                + ' must be an instance of WebKitPoint');
                        }
                        var el = __wkAnchorOf(n);
                        if (!el) { return new __WKP(pt.x, pt.y); }
                        var r = el.getBoundingClientRect();
                        var ox = r.left + (window.pageXOffset || 0);
                        var oy = r.top + (window.pageYOffset || 0);
                        return toPage
                            ? new __WKP(pt.x + ox, pt.y + oy)
                            : new __WKP(pt.x - ox, pt.y - oy);
                    }, name);
                    try {
                        Object.defineProperty(fn, 'length', { value: 0, configurable: true });
                    } catch (e) {}
                    Object.defineProperty(window, name, {
                        value: fn, writable: true, enumerable: true, configurable: true,
                    });
                };
                __wkDefineConvert(true);
                __wkDefineConvert(false);
                if (typeof window.webkitCancelRequestAnimationFrame === 'undefined'
                        && typeof window.cancelAnimationFrame === 'function') {
                    var __wkCraf = __maskNative(function (id) {
                        return window.cancelAnimationFrame(id);
                    }, 'webkitCancelRequestAnimationFrame');
                    try {
                        Object.defineProperty(__wkCraf, 'length', { value: 1, configurable: true });
                    } catch (e) {}
                    Object.defineProperty(window, 'webkitCancelRequestAnimationFrame', {
                        value: __wkCraf, writable: true, enumerable: true, configurable: true,
                    });
                }
                // 不可构造的接口对象（apply/construct 均抛 TypeError，与 WebKit
                // 「Illegal constructor」同文案；Proxy 无 JS 可观测品牌）
                var __wkIfaceOf = function (name, fillProto) {
                    var target = __maskNative(function () {
                        throw new TypeError('Illegal constructor');
                    }, name);
                    try {
                        Object.defineProperty(target, 'length', { value: 0, configurable: true });
                    } catch (e) {}
                    var proto = target.prototype;
                    try {
                        // WebKit 生成器：接口对象 prototype 属性 = ReadOnly|DontEnum|DontDelete
                        Object.defineProperty(target, 'prototype', { writable: false });
                    } catch (e) {}
                    if (fillProto) { fillProto(proto); }
                    var out = target;
                    if (typeof Proxy !== 'undefined') {
                        var __throwCtor = function () {
                            throw new TypeError('Illegal constructor');
                        };
                        out = new Proxy(target, { apply: __throwCtor, construct: __throwCtor });
                    }
                    Object.defineProperty(proto, 'constructor', {
                        value: out, writable: true, enumerable: false, configurable: true,
                    });
                    try {
                        Object.defineProperty(proto, Symbol.toStringTag, {
                            value: name, writable: false, enumerable: false, configurable: true,
                        });
                    } catch (e) {}
                    return out;
                };
                var __wkNsSlots = new WeakMap();
                var WebKitNamespace = __wkIfaceOf('WebKitNamespace', function (proto) {
                    var mkMember = function (name, pick) {
                        Object.defineProperty(proto, name, {
                            configurable: true, enumerable: true,
                            get: __maskNative(function () {
                                var s = null;
                                try { s = __wkNsSlots.get(this); } catch (e) {}
                                if (!s) { throw new TypeError('Illegal invocation'); }
                                return pick(s);
                            }, 'get ' + name),
                        });
                    };
                    mkMember('messageHandlers', function (s) { return s.handlers; });
                    mkMember('buffers', function (s) { return s.buffers; });
                });
                var UserMessageHandlersNamespace = __wkIfaceOf('UserMessageHandlersNamespace');
                var UserMessageHandler = __wkIfaceOf('UserMessageHandler', function (proto) {
                    defineFn(proto, 'postMessage', function (message) {
                        // 真机为原生 binding：返回 Promise，宿主未启用回执时永不落定
                        try {
                            return new Promise(function () {});
                        } catch (e) { return undefined; }
                    });
                });
                var __wkWinIface = function (name, iface) {
                    if (typeof window[name] !== 'undefined') { return; }
                    Object.defineProperty(window, name, {
                        value: iface, writable: true, enumerable: false, configurable: true,
                    });
                };
                __wkWinIface('WebKitNamespace', WebKitNamespace);
                __wkWinIface('UserMessageHandlersNamespace', UserMessageHandlersNamespace);
                __wkWinIface('UserMessageHandler', UserMessageHandler);
                // CriOS：页面世界有注册过的消息处理器 → window.webkit 实例出现
                // （Safari 页面世界无处理器 → 保持 undefined，见 P1-2 裁定）。
                if (D.iosChrome && typeof window.webkit === 'undefined') {
                    // UserMessageHandlersNamespace 是「只具名 getter」接口：
                    // supportedPropertyNames() 恒为空（UserMessageHandlersNamespace.cpp
                    // 的 FIXME 未实现枚举），具名属性的可见性改由 namedItem 返回
                    // 非 null 判定（JSDOMAbstractOperations.h accessVisibleNamedProperty
                    // 步骤 1）。真机因此「可读不可枚举」：Object.keys 与
                    // getOwnPropertyNames 都是 []，而 'crwebinvoke' in handlers 为
                    // true、handlers.crwebinvoke 可取值、未知名 undefined；
                    // [[GetOwnProperty]] 侧仍返回 Writable|Enumerable|Configurable
                    // 描述符（CodeGeneratorJS.pm GenerateNamedGetter + slot.setValue）。
                    var __wkHandlersOf = function (names) {
                        var map = {};
                        for (var __hi = 0; __hi < names.length; __hi++) {
                            map[names[__hi]] = Object.create(UserMessageHandler.prototype);
                        }
                        var __hasOwn = function (k) {
                            return Object.prototype.hasOwnProperty.call(map, k);
                        };
                        var target = Object.create(UserMessageHandlersNamespace.prototype);
                        if (typeof Proxy === 'undefined') { return target; }
                        return new Proxy(target, {
                            get: function (t, k, r) {
                                if (typeof k === 'string' && __hasOwn(k)) { return map[k]; }
                                return Reflect.get(t, k, r);
                            },
                            has: function (t, k) {
                                if (typeof k === 'string' && __hasOwn(k)) { return true; }
                                return Reflect.has(t, k);
                            },
                            ownKeys: function (t) { return Reflect.ownKeys(t); },
                            getOwnPropertyDescriptor: function (t, k) {
                                if (typeof k === 'string' && __hasOwn(k)) {
                                    return { value: map[k], writable: true,
                                             enumerable: true, configurable: true };
                                }
                                return Reflect.getOwnPropertyDescriptor(t, k);
                            },
                        });
                    };
                    // WebKitBufferNamespace 同为只具名 getter 接口，且
                    // LegacyNoInterfaceObject——全局没有 WebKitBufferNamespace，
                    // 构造器只挂在原型上。页面世界没注册 buffer，所有名字不可见：
                    // ownKeys [] / in false / 取值 undefined（空对象 + 正确原型即等价）。
                    var __wkBufCtor = __maskNative(function () {
                        throw new TypeError('Illegal constructor');
                    }, 'WebKitBufferNamespace');
                    var __wkBufProto = {};
                    Object.defineProperty(__wkBufCtor, 'prototype', {
                        value: __wkBufProto, writable: false,
                        enumerable: false, configurable: false,
                    });
                    Object.defineProperty(__wkBufProto, 'constructor', {
                        value: __wkBufCtor, writable: true,
                        enumerable: false, configurable: true,
                    });
                    try {
                        Object.defineProperty(__wkBufProto, Symbol.toStringTag, {
                            value: 'WebKitBufferNamespace',
                            writable: false, enumerable: false, configurable: true,
                        });
                    } catch (e) {}
                    var __wkNsObj = Object.create(WebKitNamespace.prototype);
                    __wkNsSlots.set(__wkNsObj, {
                        handlers: __wkHandlersOf(['crwebinvoke', 'POSTSuccessHandler',
                                                  'POSTErrorHandler']),
                        buffers: Object.create(__wkBufProto),
                    });
                    Object.defineProperty(window, 'webkit', {
                        value: __wkNsObj, writable: false,
                        enumerable: true, configurable: false,
                    });
                }
            } catch (e) {}
        }

        // ---- 抹掉该平台不存在的桌面 Chromium 属性（如 iOS 的 connection）----
        var drops = D.removeProps || [];
        for (var i = 0; i < drops.length; i++) {
            removeProp(drops[i]);
        }

        // ---- 平台 API 差集：窗口/文档/navigator 三处按名删除 ----
        // 桌面内核暴露、所属平台真机没有的整族 API（iOS 的通用传感器/
        // 隐私沙盒/JS 专有面，Android 的桌面独有面）。逐项 BCD 证据见
        // app/mobile_fingerprints.py 的 _*_REMOVE_GLOBALS；删除点覆盖
        // window / Window.prototype / Document.prototype / document /
        // Navigator.prototype / navigator，仍残留时按类兜底 getter=undefined。
        var dropsG = D.removeGlobals || [];
        for (var gi = 0; gi < dropsG.length; gi++) {
            var gn = dropsG[gi];
            try { delete window[gn]; } catch (e) {}
            try { delete Window.prototype[gn]; } catch (e) {}
            try { delete Document.prototype[gn]; } catch (e) {}
            try { delete document[gn]; } catch (e) {}
            try { delete Navigator.prototype[gn]; } catch (e) {}
            try { delete navigator[gn]; } catch (e) {}
            try {
                if (gn in window) { defineGet(Window.prototype, gn, undefined); }
            } catch (e) {}
            try {
                if (gn in document) { defineGet(Document.prototype, gn, undefined); }
            } catch (e) {}
            try {
                if (gn in navigator) { defineGet(Navigator.prototype, gn, undefined); }
            } catch (e) {}
        }

        // ---- 无内置 PDF 插件的平台（iOS WKWebView / Android Chrome）----
        // iOS：Safari 的 plugins/mimeTypes 恒为空（无水印 PDF 插件体系）。
        // Android：Chrome 构建开关 enable_pdf=!is_android，同样为空。
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

        // ---- Android Chrome：PDF 面（构建口径 enable_pdf=!is_android）----
        // 真机 Android Chrome：plugins/mimeTypes 为空（上方已统一置空），
        // pdfViewerEnabled 属性存在且恒为 false。与 iOS 的「属性不存在」形态
        // 不同（桌面内核是 5 个 PDF 插件 + true，单行即可判「桌面装手机」）。
        if (D.emptyPlugins && !D.ios) {
            defineGet(N, 'pdfViewerEnabled', false);
        }

        // ---- Android Chrome：NetworkInformation.type（仅 Android 实现）----
        // MDN BCD：chrome_android=38 有该属性；桌面 chrome 仅 ChromeOS 有——
        // 桌面内核上 'type' in navigator.connection 为 false，是「桌面装
        // Android」的单行级证据。真机取值为枚举（wifi/cellular/ethernet/…），
        // 与其它信号无交叉约束，按环境出网方式取 'wifi'。
        if (D.connectionType && !D.ios && typeof NetworkInformation !== 'undefined') {
            defineGet(NetworkInformation.prototype, 'type', D.connectionType);
        }

        // ---- iOS Chrome（CriOS）：页面世界检测面 ----
        // 真机 Chrome iOS 是 WKWebView 壳：web_bundle.js 等用户脚本注入页面
        // 世界，网站 JS 可见 window.__gCrWeb（约 30 成员）、被改写的 history/
        // print/scrollTo（toString 可见 "invokeOnHost"、"setWebViewScrollView
        // IsDragging" 字样）、window.webkit.messageHandlers（装配见 13)）。
        // 桌面内核均缺失
        // 这些特征，按 Chrome61-IPA 分析报告 §4.4 补齐；成员全集与文本细节以
        // 真机抓取二次校准（P2）。worker 内真机同样没有这些对象，故本段仅
        // 存在于页面模板。
        if (D.iosChrome) {
            try {
                var mkFn = function (name) {
                    var f = function () {};
                    try { Object.defineProperty(f, 'name', { value: name, configurable: true }); } catch (e) {}
                    return f;
                };
                var mkNS = function (names) {
                    var o = {};
                    for (var ni = 0; ni < names.length; ni++) { o[names[ni]] = mkFn(names[ni]); }
                    return o;
                };
                // windowId：每窗口唯一（真机为宿主填充的会话标识，格式待校准）
                var winId = '';
                try {
                    var rnd = new Uint8Array(16);
                    crypto.getRandomValues(rnd);
                    for (var bi = 0; bi < rnd.length; bi++) {
                        winId += ('0' + rnd[bi].toString(16)).slice(-2);
                    }
                } catch (e) {
                    winId = 'w' + Date.now().toString(16) + Math.random().toString(16).slice(2);
                }
                var gcr = {
                    windowId: winId,
                    common: mkNS([]),
                    message: mkNS(['invokeOnHost']),
                    console: mkNS([]),
                    autofill: mkNS([]),
                    suggestion: mkNS(['getNextElementInTabOrder']),
                    contextualSearch: mkNS(['mutationEvent', 'selectionChanged']),
                    languageDetection: mkNS(['getTextContent']),
                    translate: mkNS([]),
                    findInPage: mkNS([]),
                    plugin: mkNS([])
                };
                var gcrFnNames = ['stringify', 'getElementFromPoint',
                    'findPasswordForms', 'fillPasswordForm',
                    'fillPasswordFormWithGeneratedPassword', 'findMatchingPasswordForms',
                    'isAutofilled', 'setAutofilled', 'clearAutofilledPasswords',
                    'getPasswordForm', 'getPasswordFormData', 'getPasswordFormDataList',
                    'getElementByNameWithParent', 'selectText',
                    'dispatchPopstateEvent', 'dispatchHashchangeEvent', 'replaceWebViewURL'];
                for (var gi = 0; gi < gcrFnNames.length; gi++) {
                    gcr[gcrFnNames[gi]] = mkFn(gcrFnNames[gi]);
                }
                Object.defineProperty(window, '__gCrWeb', {
                    value: gcr, writable: true, enumerable: true, configurable: true,
                });

                // history.* 改写：行为透传原生；toString 呈真机的 JS 源码文本
                var H = (typeof History !== 'undefined') ? History.prototype : null;
                var wrapHistory = function (name, arity, src) {
                    if (!H || typeof H[name] !== 'function') { return; }
                    var orig = H[name];
                    var fn = arity === 3
                        ? function (state, title, url) { return orig.apply(this, arguments); }
                        : (arity === 1
                            ? function (delta) { return orig.apply(this, arguments); }
                            : function () { return orig.apply(this, arguments); });
                    defineFnSrc(H, name, fn, src);
                };
                wrapHistory('pushState', 3,
                    "function pushState(state, title, url) { window.__gCrWeb.message.invokeOnHost('window.history.willChangeState', String(url)); }");
                wrapHistory('replaceState', 3,
                    "function replaceState(state, title, url) { window.__gCrWeb.message.invokeOnHost('window.history.willChangeState', String(url)); }");
                wrapHistory('back', 0,
                    "function back() { window.__gCrWeb.message.invokeOnHost('window.history.back'); }");
                wrapHistory('forward', 0,
                    "function forward() { window.__gCrWeb.message.invokeOnHost('window.history.forward'); }");
                wrapHistory('go', 1,
                    "function go(delta) { window.__gCrWeb.message.invokeOnHost('window.history.go', String(delta)); }");

                // window.print / window.scrollTo 改写（同上报文形态）
                var origPrint = window.print;
                if (typeof origPrint === 'function') {
                    defineFnSrc(window, 'print', function () { return origPrint.apply(this, arguments); },
                        "function print() { window.__gCrWeb.message.invokeOnHost('print.window.print'); }");
                }
                var origScrollTo = window.scrollTo;
                if (typeof origScrollTo === 'function') {
                    defineFnSrc(window, 'scrollTo', function (x, y) { return origScrollTo.apply(this, arguments); },
                        "function scrollTo(x, y) { setWebViewScrollViewIsDragging(false); window.__gCrWeb.message.invokeOnHost('window.scrollTo', x, y); }");
                }

                // WKWebView 痕迹 window.webkit 已移入上方 13)（WebKitNamespace
                // 实例装配：通道 crwebinvoke/POST*、原型访问器 messageHandlers/
                // buffers、postMessage 返回 pending Promise）。
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

        // ---- 媒体设备枚举：真机 Android Chrome 枚举前后两个摄像头 ----
        // 宿主 Mac 单摄像头 → videoinput ×1；真机为 ×2（前/后），未授权时
        // label/deviceId/groupId 全为空串（Chromium 语义，宿主亦然）。只调整
        // 类别分组与 videoinput 数量；合成条目走 MediaDeviceInfo 原型链
        // （instanceof 成立、自身无 own property，字段 getter 在中间原型上）。
        if (D.mediaVideoInputs && typeof MediaDevices !== 'undefined'
                && typeof MediaDeviceInfo !== 'undefined' && MediaDevices.prototype
                && MediaDevices.prototype.enumerateDevices) {
            try {
                var mdProto = MediaDevices.prototype;
                var origEnum = mdProto.enumerateDevices;
                var makeDevice = function (kind) {
                    var proto = Object.create(MediaDeviceInfo.prototype);
                    defineGet(proto, 'kind', kind);
                    defineGet(proto, 'deviceId', '');
                    defineGet(proto, 'groupId', '');
                    defineGet(proto, 'label', '');
                    return Object.create(proto);
                };
                defineFn(mdProto, 'enumerateDevices', function () {
                    return origEnum.apply(this, arguments).then(function (list) {
                        var ai = [], vi = [], ao = [], other = [];
                        for (var i = 0; i < (list ? list.length : 0); i++) {
                            var d = list[i];
                            if (d.kind === 'audioinput') { ai.push(d); }
                            else if (d.kind === 'videoinput') { vi.push(d); }
                            else if (d.kind === 'audiooutput') { ao.push(d); }
                            else { other.push(d); }
                        }
                        var out = ai.length ? [ai[0]] : [makeDevice('audioinput')];
                        var vs = vi.length ? [vi[0]] : [];
                        while (vs.length < D.mediaVideoInputs) { vs.push(makeDevice('videoinput')); }
                        out = out.concat(vs);
                        out = out.concat(ao.length ? [ao[0]] : [makeDevice('audiooutput')]);
                        return out.concat(other);
                    });
                });
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

        // ---- WebGPU：适配器信息（GPUAdapterInfo）必须与移动 GPU 口径一致 ----
        // 宿主是 Apple GPU（vendor "apple" / architecture "metal-*"），而 WebGL
        // 已声明 Android 机型的 GPU；真机 Android Chrome（Dawn/Vulkan）报内核
        // Dawn gpu_info 数据映射的代际级架构名（qualcomm/adreno-Nxx、arm/
        // valhall|gen-5、samsung/rdna-N、img-tec/d-series，见 _android_webgpu），
        // 跨 API 对照可直接命中（GPUAdapterInfo 属性在原型上，实例无 own
        // property；device/description 宿主本就为空串，与真机相同，无需处理）。
        if (D.webgpu && typeof GPUAdapterInfo !== 'undefined' && GPUAdapterInfo.prototype) {
            try {
                defineGet(GPUAdapterInfo.prototype, 'vendor', D.webgpu.vendor);
                defineGet(GPUAdapterInfo.prototype, 'architecture', D.webgpu.architecture);
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
        "removeGlobals": list(spec.get("removeGlobals") or []),
        "removeWorkerGlobals": list(spec.get("removeWorkerGlobals") or []),
        "emptyPlugins": bool(spec.get("emptyPlugins")),
        "connectionType": str(spec.get("connectionType") or ""),
        "ios": bool(spec.get("ios")),
        "iosChrome": spec.get("iosChrome"),
        "webgl": spec.get("webgl"),
        "webgpu": spec.get("webgpu"),
        "mediaVideoInputs": int(spec.get("mediaVideoInputs") or 0),
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
            var mask = function (fn, name, src) {
                try {
                    if (typeof fn === 'function') {
                        registry.set(fn, src || ('function ' + name + '() { [native code] }'));
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
    var __slotHas = function (proto, recv) {
        if (recv === null || recv === undefined) { return false; }
        var t = typeof recv;
        if (t !== 'object' && t !== 'function') { return false; }
        try { return proto.isPrototypeOf(recv) === true; } catch (e) { return false; }
    };
    var defineGet = function (proto, name, value) {
        try {
            var getter = __maskNative(function () {
                if (!__slotHas(proto, this)) { throw new TypeError('Illegal invocation'); }
                return value;
            }, 'get ' + name);
            Object.defineProperty(proto, name, {
                get: getter,
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

        // ---- 平台 API 差集（Worker 全局）：IdleDetector / WebSocketStream /
        // ImageDecoder / FileSystemObserver / Scheduler 等内核 Worker 侧同样
        // 暴露、但所属平台真机没有的构造器与命名空间对象 ----
        // 实测（探针）：暂停态注入时全局对象上还没有这些接口对象，它们是在
        // Worker 首脚本开始执行时才安装的——所以这里的删除只对「已运行后才
        // 挂载」的 Worker（晚期挂载）生效；首脚本前的抹除由 _attach_worker 的
        // beforeScriptExecution 插桩断点 + build_worker_drop_patch 完成。
        var dropsW = D.removeWorkerGlobals || [];
        var wsP = null;
        try {
            wsP = (typeof WorkerGlobalScope !== 'undefined' && WorkerGlobalScope.prototype)
                ? WorkerGlobalScope.prototype : null;
        } catch (e) {}
        for (var wi = 0; wi < dropsW.length; wi++) {
            var wn = dropsW[wi];
            try { delete self[wn]; } catch (e) {}
            if (wsP) { try { delete wsP[wn]; } catch (e) {} }
            try {
                if (wn in self) {
                    Object.defineProperty(self, wn, {
                        value: undefined, writable: true,
                        enumerable: false, configurable: true,
                    });
                }
            } catch (e) {}
        }

        // ---- iOS Safari：Worker 侧同样没有 performance.memory ----
        // （WebKit 未实现 MemoryInfo；内核在 Worker 的 performance 上同样暴露）
        if (D.ios) {
            try {
                if (typeof Performance !== 'undefined' && Performance.prototype) {
                    delete Performance.prototype.memory;
                    if ('memory' in performance) {
                        defineGet(Performance.prototype, 'memory', undefined);
                    }
                }
            } catch (e) {}
        }

        if (D.platform) { defineGet(N, 'platform', D.platform); }
        if (D.ua) {
            defineGet(N, 'userAgent', D.ua);
            defineGet(N, 'appVersion', D.ua.replace(/^Mozilla\//, ''));
        }
        if (D.deviceMemory !== null && D.deviceMemory !== undefined) {
            defineGet(N, 'deviceMemory', D.deviceMemory);
        }

        // ---- Android Chrome：Worker 内 NetworkInformation.type 同样存在 ----
        // （MDN BCD worker_support chrome_android=38；桌面 WorkerNavigator 有
        //   connection 但没有 type 属性）
        if (D.connectionType && !D.ios && typeof NetworkInformation !== 'undefined') {
            defineGet(NetworkInformation.prototype, 'type', D.connectionType);
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

        // ---- WebGPU：Worker 内 GPUAdapterInfo 与主文档同口径 ----
        // （Dedicated Worker 可用 navigator.gpu；宿主 Apple 适配器信息在这里
        //   同样泄漏，真机 Android Worker 报与主文档相同的 Dawn 代际口径）
        if (D.webgpu && typeof GPUAdapterInfo !== 'undefined' && GPUAdapterInfo.prototype) {
            defineGet(GPUAdapterInfo.prototype, 'vendor', D.webgpu.vendor);
            defineGet(GPUAdapterInfo.prototype, 'architecture', D.webgpu.architecture);
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
        "removeGlobals": list(spec.get("removeGlobals") or []),
        "removeWorkerGlobals": list(spec.get("removeWorkerGlobals") or []),
        "connectionType": str(spec.get("connectionType") or ""),
        "ios": bool(spec.get("ios")),
        "webgpu": spec.get("webgpu"),
    }
    return _WORKER_PATCH_TEMPLATE.replace("__SPEC__", json.dumps(payload, ensure_ascii=False))


def build_worker_drop_patch(drops: List[str]) -> str:
    """Worker 全局差集的补删脚本（在「首脚本执行前」的插桩断点处执行）。

    实测（探针 worker_global_probe.py）：Worker 全局对象上的接口对象
    （FileSystemObserver / IdleDetector / WebSocketStream / ImageDecoder /
    Scheduler / scheduler）在「暂停态注入」与「注入侧定时器」阶段都还不存在
    —— 它们是在 Worker 首脚本开始执行时才安装的，此刻删除会落空、随后被
    装回。因此：暂停态注入后挂 beforeScriptExecution 插桩断点，断点命中时
    用本脚本补删（此刻接口已安装、脚本首行未执行），再放行。
    """
    return (
        "(function () {\n"
        "    var names = " + json.dumps(list(drops), ensure_ascii=False) + ";\n"
        "    var wsP = null;\n"
        "    try {\n"
        "        wsP = (typeof WorkerGlobalScope !== 'undefined' && WorkerGlobalScope.prototype)\n"
        "            ? WorkerGlobalScope.prototype : null;\n"
        "    } catch (e) {}\n"
        "    for (var i = 0; i < names.length; i++) {\n"
        "        var n = names[i];\n"
        "        try { delete self[n]; } catch (e) {}\n"
        "        if (wsP) { try { delete wsP[n]; } catch (e) {} }\n"
        "        try {\n"
        "            if (n in self) {\n"
        "                Object.defineProperty(self, n, {\n"
        "                    value: undefined, writable: true,\n"
        "                    enumerable: false, configurable: true,\n"
        "                });\n"
        "            }\n"
        "        } catch (e) {}\n"
        "    }\n"
        "})();\n"
    )


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
                "battery": None,
                # 与 derive_spec 同口径：平台事实不依赖调用方式（移动仿真会话
                # 只服务 Android/iOS，PDF 面与桌面独有 API 都要收敛）
                "removeProps": (list(_IOS_REMOVE_PROPS) if target_os == "ios"
                                else list(_ANDROID_REMOVE_PROPS)),
                "removeGlobals": (list(_IOS_GLOBAL_DROPS) if target_os == "ios"
                                  else list(_ANDROID_GLOBAL_DROPS)),
                "removeWorkerGlobals": (list(_IOS_WORKER_DROPS) if target_os == "ios"
                                        else list(_ANDROID_WORKER_DROPS)),
                "emptyPlugins": True,
                "connectionType": "wifi" if target_os == "android" else None,
                "ios": target_os == "ios", "iosChrome": None, "webgl": None, "webgpu": None,
                "mediaVideoInputs": 0,
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
        # Worker 全局差集补删：sessionId → 待补删名单（客户端问 beforeScriptExecution
        # 断点命中后消费）。见 _attach_worker / _worker_drop_apply。
        self._worker_drop_waiters: dict[str, list] = {}
        self._attach_tasks: dict[str, asyncio.Task] = {}
        self._first_page_session: Optional[str] = None
        # Accept-CH：CDP session → {源: 该源已声明的高熵提示}（Fetch 域回送）
        self._accept_ch: dict[str, dict[str, set]] = {}
        # 传感器仿真（Android）：虚拟传感器是浏览器进程级全局对象，全会话
        # 一条读数循环即可（详见 _setup_sensors）；supported=None 表示未探测
        self._sensor_supported: Optional[bool] = None
        self._sensor_task: Optional[asyncio.Task] = None
        self._sensor_session: Optional[str] = None
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
        # 传感器噪声任务（每 50ms 一次 CDP 下发）：先取消并回收，避免
        # 「Task was destroyed but it is pending」与关闭竞态
        if self._sensor_task is not None:
            self._sensor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sensor_task
            self._sensor_task = None
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
                elif msg.get("method") == "Debugger.paused":
                    # Worker 全局差集补删的插桩断点（脚本执行前）：
                    # 只消费我们自己下的 beforeScriptExecution；
                    # 其它来源的暂停（用户断点）一概不介入。
                    params = msg.get("params") or {}
                    sid_evt = msg.get("sessionId") or ""
                    drops = self._worker_drop_waiters.get(sid_evt)
                    if drops is not None and params.get("reason") == "instrumentation":
                        self._worker_drop_waiters.pop(sid_evt, None)
                        asyncio.create_task(self._worker_drop_apply(sid_evt, drops))
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
            # 不启用 Emulation.setEmitTouchEventsForMouse：实测（fp-chromium 148）
            # 该转换会吞掉 mousePressed/Released 的 CDP 应答 → Playwright 点击
            # 永久挂起，且产出序列缺兼容鼠标事件与 click。移动端自动化应改用
            # Input.dispatchTouchEvent 派发（见 docs/移动端大厂检测对抗优化方案.md
            # §事件层）。
            # OOPIF 目标不单独启用传感器：虚拟传感器是浏览器级全局对象，
            # 页面目标的覆盖对所有帧一并生效；跨域子帧能否收到由 Permissions
            # Policy 决定（未声明 allow 的跨域子帧真机同样零事件）。
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

        平台 API 差集（removeWorkerGlobals）另需一条链路：Worker 全局对象上
        的接口对象在「首脚本开始执行」时才安装（暂停态与注入侧定时器阶段实测
        都读不到），暂停态删除会落空、随后被装回。因此额外挂
        beforeScriptExecution 插桩断点，断点在脚本首行前命中时补删（见
        build_worker_drop_patch），补删完成后立即关闭 Debugger 并放行。
        """
        if not target_id or not session_id:
            return
        if target_id in self._applied_targets:
            await self._resume(session_id)
            return
        self._applied_targets.add(target_id)
        patch = build_worker_patch(self.spec)
        drops = list(self.spec.get("removeWorkerGlobals") or [])
        injected = False
        try:
            await self._send("Runtime.evaluate", {"expression": patch},
                             session_id=session_id, timeout=3.0)
            injected = True
        except (CDPError, asyncio.TimeoutError):
            pass
        if drops and injected:
            # 插桩断点：命中「脚本执行前」再补删一次全局差集。
            try:
                await self._send("Debugger.enable", {},
                                 session_id=session_id, timeout=3.0)
                await self._send("Debugger.setInstrumentationBreakpoint",
                                 {"instrumentation": "beforeScriptExecution"},
                                 session_id=session_id, timeout=3.0)
                self._worker_drop_waiters[session_id] = drops
            except (CDPError, asyncio.TimeoutError):
                # 断点链路不可用（老内核）：退回暂停态补删（已在上面的补丁里执行）。
                # 极小竞态窗口（首行可能读到未抹除的构造器）记入边界。
                with contextlib.suppress(CDPError, asyncio.TimeoutError):
                    await self._send("Debugger.disable", {},
                                     session_id=session_id, timeout=3.0)
        await self._resume(session_id)
        if not injected:
            # 暂停态不受理 Runtime 命令时（理论上不会）：放行后补一次，
            # 极小竞态窗口（Worker 首行可能已读取原生值）
            with contextlib.suppress(CDPError, asyncio.TimeoutError):
                await self._send("Runtime.evaluate", {"expression": patch},
                                 session_id=session_id, timeout=3.0)

    async def _worker_drop_apply(self, session_id: str, drops: list) -> None:
        """插桩断点命中（Worker 首脚本执行前）时补删全局差集，然后放行脚本。"""
        with contextlib.suppress(CDPError, asyncio.TimeoutError):
            await self._send("Runtime.evaluate",
                             {"expression": build_worker_drop_patch(drops)},
                             session_id=session_id, timeout=3.0)
        # 关掉 Debugger（释放暂停）后不再介入该 Worker 的后续脚本。
        with contextlib.suppress(CDPError, asyncio.TimeoutError):
            await self._send("Debugger.disable", {}, session_id=session_id, timeout=3.0)
        with contextlib.suppress(CDPError, asyncio.TimeoutError):
            await self._send("Debugger.resume", {}, session_id=session_id, timeout=3.0)

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
        # 不启用 setEmitTouchEventsForMouse（会吞鼠标命令应答，见主路径注释）

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

        # 7. Android：传感器事件流。真机 Chrome Android 持续派发
        #    devicemotion/deviceorientation（60Hz 级数值流）；桌面内核没有
        #    传感器，只在注册监听时派发一次全 null 的哨兵事件——「有没有
        #    连续数值流」是最直接的模拟器/桌面判据。必须紧随目标挂载下发：
        #    传感器请求一旦在「无传感器」状态下定型，之后启用覆盖也不会再
        #    派发事件（实测）。iOS 不启用（真机需 requestPermission 授权，
        #    未授权时真机为零事件，模板层只按 WebKit 语义伪造静态方法）。
        await self._setup_sensors(sid)

    # ---------------- 传感器仿真（Android）----------------

    async def _setup_sensors(self, sid: str) -> None:
        """启用 CDP 虚拟传感器并启动读数噪声循环（仅 Android，会话内一次）。

        虚拟传感器是浏览器进程级全局对象（实测：其他目标再次 enable 返回
        "The specified sensor type is already overridden"，而推送的读数对
        所有目标生效）——与真机语义一致：同一设备的所有页面/iframe 共享
        同一份传感器数据。因此只在首个页面目标上启用并运行一条循环。
        跨域 iframe 的可用性由 Permissions Policy 决定（devicemotion 需要
        accelerometer/gyroscope 特性，默认 allowlist=self；未声明 allow 的
        跨域子帧真机同样拿不到事件，内核行为与真机一致，无需额外处理）。
        """
        if self.target_os != "android" or self._sensor_supported is not None:
            return
        try:
            await self._send("Emulation.setSensorOverrideEnabled",
                             {"enabled": True, "type": _SENSOR_TYPES[0]},
                             session_id=sid, timeout=5.0)
        except CDPError as e:
            log.debug("内核不支持 CDP 虚拟传感器，跳过传感器仿真: %s", e)
            self._sensor_supported = False
            return
        self._sensor_supported = True
        self._sensor_session = sid
        for sensor_type in _SENSOR_TYPES[1:]:
            with contextlib.suppress(CDPError):
                await self._send("Emulation.setSensorOverrideEnabled",
                                 {"enabled": True, "type": sensor_type},
                                 session_id=sid, timeout=5.0)
        simulator = _SensorSimulator(random.Random())
        with contextlib.suppress(CDPError):
            await self._push_sensor_readings(sid, simulator.step())
        if self._sensor_task is not None:
            self._sensor_task.cancel()
        self._sensor_task = asyncio.create_task(self._sensor_loop(simulator))

    async def _push_sensor_readings(self, sid: str,
                                    readings: Dict[str, Dict[str, float]]) -> None:
        """下发一步读数（xyz 用于加速度/角速度，四元数用于方向）。

        读数写入全局虚拟传感器，对所有页面/iframe 一并生效。
        """
        for sensor_type, values in readings.items():
            if sensor_type.endswith("orientation"):
                reading = {"quaternion": values}
            else:
                reading = {"xyz": values}
            await self._send("Emulation.setSensorOverrideReadings",
                             {"type": sensor_type, "reading": reading},
                             session_id=sid, timeout=5.0)

    async def _sensor_loop(self, simulator: _SensorSimulator) -> None:
        """按真机节奏持续更新读数（事件流随读数变化保持「活」）。"""
        failures = 0
        try:
            while not self._closed:
                # 更新节奏带抖动：严格固定周期会让「事件内容变化」呈精确的
                # 周期性（如连续 3 样本相同再跳变），真机传感器采样周期不固定
                jitter = 1.0 + _SENSOR_TICK_JITTER * (random.random() * 2 - 1)
                await asyncio.sleep(_SENSOR_TICK_SEC * jitter)
                sid = self._sensor_session
                if sid is None:
                    return
                try:
                    await self._push_sensor_readings(sid, simulator.step())
                    failures = 0
                except CDPError as e:
                    # 推送通道（首个页面）失效时改绑仍存活的页面会话；
                    # 连续失败即停止仿真（浏览器随之退出/会话关闭）
                    failures += 1
                    if failures >= 5:
                        candidate = self._first_page_session
                        if candidate and candidate != sid:
                            self._sensor_session = candidate
                            failures = 0
                            continue
                        log.debug("传感器读数推送连续失败，停止仿真: %s", e)
                        return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("传感器仿真循环退出: %s", e)
        finally:
            if self._sensor_task is asyncio.current_task():
                self._sensor_task = None

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
