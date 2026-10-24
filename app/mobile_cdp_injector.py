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
- iOS：抹掉 Safari 不存在的属性（navigator.connection、userAgentData），
  plugins/mimeTypes 置空、pdfViewerEnabled 置 true，navigator.vendor=Apple
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
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

from .mobile_fingerprints import (
    _ANDROID_REMOVE_GLOBALS,
    _ANDROID_REMOVE_NAV_PROPS,
    _ANDROID_REMOVE_WORKER_GLOBALS,
    _IOS_REMOVE_GLOBALS,
    _IOS_REMOVE_NAV_PROPS,
    _IOS_REMOVE_WORKER_GLOBALS,
    _IOS_WORKER_ADD_INTERFACES,
    _IOS_WORKER_GLOBAL_DROPS,
    _IOS_WORKER_NAV_PROTO,
    _ANDROID_DOWNLINK_MAX_MBPS,
    _android_tts_voices,
    _codec_capability,
    _add_members,
    _drop_proto_members,
    _generate_mobile_font_subset,
    _ios_av1_capable,
    _ios_permissions,
    _webgl_profile,
    _worker_add_members,
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
_SENSOR_TICK_SEC = 1.0 / 60.0    # 60Hz 名义更新率（与真机传感器采样率同档）
_SENSOR_TICK_JITTER = 0.2        # 抖动幅度：真机采样周期并非严格定频
# 每步随机游走幅度按 tick 缩放：20Hz 时代的步长 ÷ 3 保持同样的物理漂移速率
# （°/s、m/s²/s），换更新频率不改变「手持姿态」的统计特征。
_SENSOR_STEP_SCALE = _SENSOR_TICK_SEC / 0.05
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
      单步幅度随更新周期缩放（见 _SENSOR_STEP_SCALE）：60Hz 下每步更小、
      每秒漂移量不变。
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
        # 步长按更新周期缩放：60Hz 下每秒步数 ×3，单步幅度 ÷3，
        # 姿态与线速度的每秒漂移量与 20Hz 时代一致（真机静止手持的统计特征）
        s = _SENSOR_STEP_SCALE
        self.alpha = (self.alpha + self._rng.uniform(-0.6 * s, 0.6 * s)) % 360.0
        self.beta = max(35.0, min(85.0, self.beta + self._rng.uniform(-0.25 * s, 0.25 * s)))
        self.gamma = max(-25.0, min(25.0, self.gamma + self._rng.uniform(-0.25 * s, 0.25 * s)))
        beta = math.radians(self.beta)
        gamma = math.radians(self.gamma)
        self._walk(self._linear, 0.12 * s, 0.45)
        self._walk(self._gyro, 0.004 * s, 0.02)
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


# ---- iOS 安全区（env(safe-area-inset-*)）口径 ----
# Safari 竖屏 + viewport-fit=cover 时，页面铺到状态栏/Home Indicator 之下，
# env() 返回的正是被系统 UI 占据的那条边（CSS px）：
#   刘海机型状态栏 47、灵动岛 59、6.3"/6.9" 的 16 Pro/17 系 62；
#   Home Indicator 34；SE（无刘海、无指示条）全 0；
#   iPadOS 状态栏 24、Home Indicator 20。
# 数值按 CSS 宽度分组（宽度是屏幕的确定属性，机型名会随指纹库增删漂移）。
# Android Chrome 不暴露安全区：显示开孔不作为 env() 下发，恒为 0，故不覆盖。
_IOS_SAFE_AREA_TOP = {
    375: 44,   # iPhone X / XS / 11 Pro / 12 mini / 13 mini
    390: 47,   # iPhone 12/13/14/16e（刘海）
    393: 59,   # iPhone 15/16（灵动岛）
    402: 62,   # iPhone 16 Pro / 17
    414: 47,   # iPhone XR / 11 / XS Max / 11 Pro Max
    420: 59,   # iPhone Air
    428: 47,   # iPhone 12/13 Pro Max
    430: 59,   # iPhone 14 Pro Max / 15 Pro Max
    440: 62,   # iPhone 16 Pro Max / 17 Pro Max
}


def _ios_safe_area(css_w: int, css_h: int, device_type: str = "") -> Optional[Dict[str, int]]:
    """真机竖屏安全区（Safari 非壳、viewport-fit=cover 口径）。"""
    if device_type == "tablet" or css_w >= 700:
        return {"top": 24, "bottom": 20}
    if css_w <= 375 and css_h <= 700:
        # SE 2/3：4.7" 屏、实体 Home 键，无刘海也无指示条
        return {"top": 0, "bottom": 0}
    return {"top": _IOS_SAFE_AREA_TOP.get(css_w, 47), "bottom": 34}


# HDR 屏机型（(dynamic-range: high) 报 true）。iPhone X / XS / 11 Pro 起
# 采用 OLED HDR 屏，iPhone 12 以后全系；SE 系列（LCD）与 iPhone 8 及更早
# 无 HDR。按机型名裁决，库外机型按「非 SE 的现代 iPhone 有 HDR」兜底。
_IOS_HDR_EXCLUDE = ("SE",)


def _ios_hdr_screen(model: str, market_name: str = "") -> bool:
    """真机 (dynamic-range: high) 口径：HDR 屏机型 true，SE 系列 false。"""
    name = f"{model} {market_name}"
    return not any(tok in name for tok in _IOS_HDR_EXCLUDE)


# 读取主文档的 viewport meta（只读、无副作用；不在页面留下任何全局）
_SAFE_AREA_META_JS = (
    "(() => { const m = document.querySelector('meta[name=\"viewport\" i]');"
    " return m ? String(m.getAttribute('content') || '') : ''; })()"
)


# ---- F-73：特性 × iOS 版本门控 ----
# 设备库 iOS 版本跨度 13→27。下面这些名字在**低于**表中版本的真机上不存在，
# 但桌面内核一律暴露（或被无条件补建），所以只在低版本 profile 上删除。
# 方向说明：表里是「最低支持版本」，不是「删除到哪个版本」。
_IOS_MIN_VERSIONS = {
    # 26+：Digital Credentials
    "DigitalCredential": (26,),
    # 26+：WebGPU 整族（F-82）。BCD 里每个接口自身都是 safari_ios 26，
    # 而基线 WebKit 27.2 ≈ Safari 26——差集普查永远看不到「低版本该删」，
    # 18.6 profile 上 41 名全暴露。逐名列出而不是前缀匹配：`GPUError` 在
    # Chromium 上独立存在且早已在 _IOS_GLOBAL_DROPS，按 `GPU` 前缀整族删会
    # 把它一起卷进来（结果相同但语义混乱）。navigator 成员按裸名 `gpu`
    # 给——删除器对 removeProps 走 Navigator.prototype 的键名。
    "gpu": (26,),
    "GPU": (26,), "GPUAdapter": (26,), "GPUAdapterInfo": (26,),
    "GPUBindGroup": (26,), "GPUBindGroupLayout": (26,), "GPUBuffer": (26,),
    "GPUBufferUsage": (26,), "GPUCanvasContext": (26,), "GPUColorWrite": (26,),
    "GPUCommandBuffer": (26,), "GPUCommandEncoder": (26,),
    "GPUCompilationInfo": (26,), "GPUCompilationMessage": (26,),
    "GPUComputePassEncoder": (26,), "GPUComputePipeline": (26,),
    "GPUDevice": (26,), "GPUDeviceLostInfo": (26,), "GPUExternalTexture": (26,),
    "GPUInternalError": (26,), "GPUMapMode": (26,), "GPUOutOfMemoryError": (26,),
    "GPUPipelineError": (26,), "GPUPipelineLayout": (26,), "GPUQuerySet": (26,),
    "GPUQueue": (26,), "GPURenderBundle": (26,), "GPURenderBundleEncoder": (26,),
    "GPURenderPassEncoder": (26,), "GPURenderPipeline": (26,),
    "GPUSampler": (26,), "GPUShaderModule": (26,), "GPUShaderStage": (26,),
    "GPUSupportedFeatures": (26,), "GPUSupportedLimits": (26,),
    "GPUTexture": (26,), "GPUTextureUsage": (26,), "GPUTextureView": (26,),
    "GPUUncapturedErrorEvent": (26,), "GPUValidationError": (26,),
    "WGSLLanguageFeatures": (26,),
    # 27+：ARIA Notifications
    "ariaNotify": (27,),
    # 27+ 但**默认关闭**：WebKit `UnifiedWebPreferences.yaml` 的
    # `DevicePostureAPIEnabled` 是 `status: testable` + `defaultValue: false`
    # ——出货版 Safari 不开该偏好，普通浏览上下文里恒缺席，与版本无关。
    # 放这里会随版本门控在 27+ 档漏出（F-139），故归入无条件删除表。
    # 同族：BCD `api/DevicePosture.json` 的 safari_ios 条目本身就是
    # `flags: [{type: preference, name: DevicePostureEnabled}]`。
    # 26.x：F-85 普查余量（BCD 接口自身 safari_ios，逐族列明）。这些族
    # 在 WebKit 27.2 基线里同样存在，差集普查看不见，只能按版本门控。
    #   WebTransport 族 26.4 / Navigation API 族 26.2 / Streams BYOB 26.4
    #   （BYOB 三名的接口自身条目即 26.4，不存在「16.4 早期形态」——本轮
    #   按 BCD 精确版本复核，见 /tmp/bcd_full 的 api/*.json）；
    #   Trusted Types / WebCodecs 音频侧 / CSS Position-Try / ScrollTimeline /
    #   ViewTimeline / URLPattern / FileSystemWritableFileStream 为 26.0；
    #   CaretPosition / CommandEvent / EventCounts / LargestContentfulPaint /
    #   PerformanceEventTiming 为 26.2；Origin 为 26.5。
    "WebTransport": (26, 4), "WebTransportBidirectionalStream": (26, 4),
    "WebTransportDatagramDuplexStream": (26, 4), "WebTransportError": (26, 4),
    "ReadableByteStreamController": (26, 4),
    "ReadableStreamBYOBReader": (26, 4), "ReadableStreamBYOBRequest": (26, 4),
    "Navigation": (26, 2), "NavigateEvent": (26, 2),
    "NavigationActivation": (26, 2),
    "NavigationCurrentEntryChangeEvent": (26, 2),
    "NavigationDestination": (26, 2), "NavigationHistoryEntry": (26, 2),
    "NavigationTransition": (26, 2), "navigation": (26, 2),
    "CaretPosition": (26, 2), "CommandEvent": (26, 2), "EventCounts": (26, 2),
    "LargestContentfulPaint": (26, 2), "PerformanceEventTiming": (26, 2),
    "Origin": (26, 5),
    "TrustedHTML": (26,), "TrustedScript": (26,), "TrustedScriptURL": (26,),
    "TrustedTypePolicy": (26,), "TrustedTypePolicyFactory": (26,),
    "trustedTypes": (26,),
    "AudioData": (26,), "AudioDecoder": (26,), "AudioEncoder": (26,),
    "EncodedAudioChunk": (26,),
    "CSSPositionTryDescriptors": (26,), "CSSPositionTryRule": (26,),
    "FileSystemWritableFileStream": (26,),
    "ScrollTimeline": (26,), "ViewTimeline": (26,), "URLPattern": (26,),
    "HTMLSelectedContentElement": (27,),
    # 16.4+：Home Screen Web App 推送 / 通知 / 徽标 / 音频会话 / 报表
    "Notification": (16, 4), "PushManager": (16, 4),
    "PushSubscription": (16, 4), "PushSubscriptionOptions": (16, 4),
    "setAppBadge": (16, 4), "clearAppBadge": (16, 4),
    "Report": (16, 4), "AudioSession": (16, 4),
    # 标准全屏族 5 名（webkit 前缀别名 8 名无条件保留，不进表）
    "fullscreen": (16, 4), "exitFullscreen": (16, 4),
    "fullscreenEnabled": (16, 4), "onfullscreenchange": (16, 4),
    "onfullscreenerror": (16, 4),
    # 17+ / 18+ / 14.1+ / 11.1+
    "BarcodeDetector": (17,), "MediaStreamTrackProcessor": (18,),
    "SpeechRecognition": (14, 1), "PaymentRequest": (11, 1),
}


# ---- F-89：CSS.supports 白名单的版本门控 ----
# allow/deny 两张表按属性名静态生效，低版本 profile 上会误报「新属性已支持」。
# 值语义见 _PATCH_TEMPLATE 的 __cssAllow（F-88）。表里是最低支持版本，
# 与 _IOS_MIN_VERSIONS 同向：低于该版本的真机 CSS.supports 必须 false。
# BCD：margin-trim safari/iOS 16.4、text-autospace 18.4（初始值 no-autospace）、
# backdrop-filter 18.0（-webkit- 是 CSSProperties.json 里的 cascade-alias，
# 无 settings-flag，随主属性同生同灭）。-webkit-hyphens 反而**不门控**：
# BCD css.properties.hyphens.safari_ios 的前缀条目自 4.2 起就在（无前缀才 17）。
# -apple-pay-button-style 随 ENABLE_APPLE_PAY 恒开，同样不进门控。
_CSS_MIN_VERSIONS = {
    "margin-trim": (16, 4),
    "text-autospace": (18, 4),
    "-webkit-backdrop-filter": (18,),
}


def _css_version_gated(os_version: Any) -> List[str]:
    """低于 profile 版本的 CSS 属性名：注入层对其一律按「不支持」作答。"""
    version = _parse_ios_version(os_version)
    if version is None:
        return []
    return [name for name, minimum in _CSS_MIN_VERSIONS.items()
            if version < tuple(minimum)]


def _parse_ios_version(value: Any) -> Optional[tuple]:
    """'18.6' / '16.4.1' / '17' / 缺省 → 版本元组；无法解析返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    parts = []
    for chunk in text.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else None


_UA_OS_VERSION = re.compile(
    r"CPU (?:iPhone )?OS (\d+(?:_\d+)*) like Mac OS X|CPU OS (\d+(?:_\d+)*) ")


def _ua_ios_version(ua: Any) -> Optional[tuple]:
    """从 UA 的 `CPU iPhone OS 26_6 like Mac OS X` / `CPU OS 27_0` 取版本。

    门控读 `mobileExtra.osVersion`，UA 读 `navigator.userAgent`——两处独立
    取数，存量档案迁移或 DB 手工编辑后可以脱钩（F-140）：UA 声明 18_6 却
    在 26+ 面暴露 WebCodecs 音频侧，一行判假。这里以 UA 为准回推。
    """
    m = _UA_OS_VERSION.search(str(ua or ""))
    if not m:
        return None
    raw = m.group(1) or m.group(2) or ""
    return _parse_ios_version(raw.replace("_", "."))


def _ios_version_gated(remove_globals: List[str], remove_props: List[str],
                       os_version: Any) -> tuple:
    """按 profile 的 iOS 版本补上门控项（低于最低支持版本才删）。"""
    version = _parse_ios_version(os_version)
    if version is None:
        return remove_globals, remove_props
    dropped = [name for name, minimum in _IOS_MIN_VERSIONS.items()
               if version < tuple(minimum)]
    if not dropped:
        return remove_globals, remove_props
    return (list(remove_globals) + [n for n in dropped if n not in remove_globals],
            list(remove_props) + [n for n in dropped if n not in remove_props])


def _ios_add_members_gated(add: Dict[str, Any], os_version: Any) -> Dict[str, Any]:
    """从页面补建表剔除低于 profile 版本的门控接口（F-138）。

    页面补丁是单模板，删除段在补建段之前执行——低版本 profile 上同一个名字
    既在删除清单（门控）又在补建清单（无条件补建）时，补建会赢。
    `AudioSession`/`Report`（16.4）实测在 16.0 档被补了回来。Worker 侧早有
    同构剔除（见 worker_add 段），页面侧一直缺。
    """
    version = _parse_ios_version(os_version)
    if version is None or not add:
        return add
    drop = {name for name, minimum in _IOS_MIN_VERSIONS.items()
            if version < tuple(minimum)}
    if not drop:
        return add
    for key in ("protos", "statics", "constants", "handlers"):
        rows = add.get(key)
        if rows:
            add[key] = [r for r in rows if r.get("ctor") not in drop]
    ifaces = add.get("interfaces")
    if ifaces:
        add["interfaces"] = {k: v for k, v in ifaces.items() if k not in drop}
    globals_ = add.get("globals")
    if globals_:
        add["globals"] = {k: v for k, v in globals_.items() if k not in drop}
    return add


def derive_spec(fingerprint: dict, target_os: str,
                locale: str = "") -> Dict[str, Any]:
    """从存储的移动端 fingerprint 提取运行期仿真规格（兼容旧版结构）。

    `locale` 只影响 TTS 默认语音的语族（F-161）：真机 Android 的默认语音
    跟随系统语言，而系统语言由 geoip 在运行期决定，存储指纹里没有。
    """
    fp = fingerprint or {}
    nav = fp.get("navigator") or {}
    screen = fp.get("screen") or {}
    extra = dict(fp.get("mobileExtra") or {})

    # F-140：UA 与 osVersion 脱钩时以 UA 为准。产品生成流内部一致，脱钩只
    # 发生在生成之后的变更路径（存量档案迁移、DB 手工编辑）；不改存档，
    # 只在运行期把门控基准换成 UA 声明的版本。
    if target_os == "ios":
        _ua_ver = _ua_ios_version(nav.get("userAgent"))
        if _ua_ver and _parse_ios_version(extra.get("osVersion")) != _ua_ver:
            extra["osVersion"] = ".".join(str(p) for p in _ua_ver)

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
        if not ua_ch.get("formFactors"):
            # F-97：存量指纹的 uaCh 没有 formFactors；按机型类型运行期补齐，
            # 与 JS 高熵值、Sec-CH-UA-Form-Factors 头共用同一数据源。
            ua_ch["formFactors"] = (
                ["Tablet"] if str(extra.get("deviceType") or "") == "tablet" else ["Mobile"]
            )

    battery = fp.get("battery") if (target_os == "android" and fp.get("battery")) else None

    device_memory = None
    if target_os == "android":
        device_memory = _memory_bucket(extra.get("memoryClass") or nav.get("deviceMemory"))

    remove_props = extra.get("removeNavigatorProps")
    if remove_props is None:
        # 旧版指纹无该字段：按真机属性差异兜底（iOS Safari / Android Chrome）
        remove_props = (list(_IOS_REMOVE_PROPS) if target_os == "ios"
                        else list(_ANDROID_REMOVE_PROPS))
    elif target_os == "ios":
        # F-03：pdfViewerEnabled 是 Safari 16.4+ 的真实能力位（真机恒 true），
        # 早期版本误列为删除项且已写进存量档案，运行期归一化剔除。
        remove_props = [p for p in remove_props if p != "pdfViewerEnabled"]
    if target_os == "android":
        # F-96：存量指纹的 removeNavigatorProps 是生成时快照，新补的平台
        # 事实（windowControlsOverlay 等）不会自动出现；运行期与基线求并集，
        # 不要求重建指纹，也不改存量 JSON。
        remove_props = list(dict.fromkeys([*_ANDROID_REMOVE_PROPS, *remove_props]))
    # 平台 API 差集：窗口/文档/navigator 三处按名删除 + Worker 全局差集。
    # 同样支持指纹侧覆盖（mobileExtra.removeGlobals / removeWorkerGlobals），
    # 缺省按平台事实展开；存量指纹无需重建。
    if target_os == "ios":
        base_globals = list(_IOS_GLOBAL_DROPS)
        remove_globals = extra.get("removeGlobals")
        if remove_globals is None:
            remove_globals = list(base_globals)
        remove_worker_globals = extra.get("removeWorkerGlobals")
        if remove_worker_globals is None:
            remove_worker_globals = _IOS_WORKER_DROPS
    else:
        base_globals = list(_ANDROID_GLOBAL_DROPS)
        remove_globals = extra.get("removeGlobals")
        if remove_globals is None:
            remove_globals = list(base_globals)
        remove_worker_globals = extra.get("removeWorkerGlobals")
        if remove_worker_globals is None:
            remove_worker_globals = _ANDROID_WORKER_DROPS
    # F-73：特性 × iOS 版本门控。放在指纹侧覆盖归一化**之后**，保证存量
    # 指纹里显式写死的 removeGlobals 仍然优先；缺省路径（含 base_globals）
    # 已展开，这里只做追加。
    if target_os == "ios":
        remove_globals, remove_props = _ios_version_gated(
            remove_globals, remove_props, extra.get("osVersion"))
        # Worker 侧同样按版本门控：Worker 是独立全局，页面删除到不了；
        # WebTransport / Trusted Types / Navigation API / WebCodecs 音频侧
        # 在 Chromium 的 Worker 全局里同样存在，低版本 profile 不删即
        # 「页面没有、Worker 有」的跨上下文矛盾。
        _, remove_worker_globals = _ios_version_gated(
            [], remove_worker_globals, extra.get("osVersion"))

    # F-40：getDisplayMedia 的删除项同样按版本门控（iOS 27 起真机有）。
    # 只摘这一名——MediaDevices 上还有 F-103 的 setCaptureHandleConfig，
    # 整键 pop 会把它一起放回去。
    drop_members = _drop_proto_members(target_os, extra.get("osVersion"))
    _ios_ver = _parse_ios_version(extra.get("osVersion"))
    if target_os == "ios" and _ios_ver is not None and _ios_ver >= (27,):
        _md = [m for m in drop_members.get("MediaDevices", [])
               if m != "getDisplayMedia"]
        if _md:
            drop_members["MediaDevices"] = _md
        else:
            drop_members.pop("MediaDevices", None)

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

    # NetworkInformation.downlinkMax：与 type 同属 NetInfoDownlinkMax 开关，
    # 仅 Android 实现（BCD chrome_android=38；桌面仅 ChromeOS）。旧指纹无该
    # 字段时按平台事实兜底，存量档案无需重建。
    downlink_max = extra.get("downlinkMax")
    if downlink_max is None and target_os == "android":
        downlink_max = _ANDROID_DOWNLINK_MAX_MBPS

    # canPlayType 能力表：iOS 目标必须关掉内核 FFmpeg 的 ogg/webm 面（真机
    # AVFoundation 无这两个容器解码器），并补齐 hvc1。旧指纹无该字段时按平台
    # 事实兜底，存量档案无需重建（与 empty_plugins / connection_type 同理）。
    # F-19：maybe/probably 的档位也在这里校准（容器白名单 + 机型能力位）。
    av1_capable = (
        target_os == "ios"
        and _ios_av1_capable(str(extra.get("model") or ""),
                             str(extra.get("marketName") or ""))
    )
    codec_capability = extra.get("codecCapability")
    if not isinstance(codec_capability, dict) or not codec_capability:
        codec_capability = _codec_capability(target_os, av1=av1_capable)
    elif target_os == "ios":
        # 存量档案里的表只覆盖 ogg/webm/hvc1；平台基线补齐 F-19 新行，
        # 档案侧自定义值仍优先。
        merged = _codec_capability(target_os, av1=av1_capable)
        merged.update({str(k).lower(): v for k, v in codec_capability.items()})
        # 机型能力位由运行期决定，不能被档案里的旧 '' 覆盖回去（存量档案
        # 生成时未带 av1 分支）。
        if av1_capable:
            merged['video/mp4; codecs="av01.0.05m.08"'] = "probably"
        codec_capability = merged

    # WebGL 参数上限 / 扩展集：宿主是 Apple M 系列（16384 上限 + 桌面专属
    # 纹理扩展），与声明的 Adreno/Mali/Apple GPU 三元组不符。旧指纹无该字段
    # 时按平台兜底，存量档案无需重建。
    webgl_profile = extra.get("webglProfile")
    if not isinstance(webgl_profile, dict) or not webgl_profile:
        webgl_profile = _webgl_profile(target_os, renderer)

    color_depth = screen.get("colorDepth")
    try:
        color_depth = int(color_depth)
    except (TypeError, ValueError):
        color_depth = 0

    # Worker 补建表里有两项受 iOS 版本门控（AudioDecoder / AudioEncoder 是
    # Safari 26 才有的 WebCodecs 音频侧）：低版本 profile 上它们既在删除清单
    # （F-85 门控）又在补建清单，补建在后执行会赢——所以按版本从补建表剔除，
    # 让「页面没有、Worker 也没有」保持一致。
    worker_add = _worker_add_members(target_os)
    if target_os == "ios" and worker_add.get("interfaces"):
        _wv = _parse_ios_version(extra.get("osVersion"))
        if _wv is not None:
            for _wn in ("AudioDecoder", "AudioEncoder"):
                _min = _IOS_MIN_VERSIONS.get(_wn)
                if _min and _wv < tuple(_min):
                    worker_add["interfaces"].pop(_wn, None)

    # 屏幕自然方向（F-44）：WebKit `naturalScreenOrientationType()` 在 iOS
    # 上是「iPad → landscape-primary，其余 → portrait-primary」，桌面/电视
    # 一律 landscape-primary（Source/WebCore/page/ScreenOrientationType.h）。
    # Android 与 iOS 同构：手机自然竖屏、平板自然横屏（F-92）。
    # ScreenOrientation::angle() 按「当前方向 + 自然方向是否竖屏」查表，
    # 所以 iPhone/Android 手机竖屏 angle 是 0、iPad/Android 平板竖屏是 90——
    # 不能按平台写死一个数。
    _device_type = str(extra.get("deviceType") or "")
    natural_landscape = (
        target_os not in ("ios", "android")
        or _device_type == "tablet"
    )

    # iOS 安全区：非 cover 页面在运行期置 0（见 _sync_safe_area）
    safe_area = None
    if target_os == "ios":
        safe_area = _ios_safe_area(css_w, css_h, str(extra.get("deviceType") or ""))

    # HDR 屏：iPhone X / XS / 11 Pro 及 iPhone 12 以后全系为 HDR 屏，
    # (dynamic-range: high) 报 true；SE 系列无 HDR，报 false。CDP 的
    # setEmulatedMedia 不支持该特性（实测仍 false），只能 JS 改 matchMedia。
    dynamic_range_high = False
    if target_os == "ios":
        dynamic_range_high = _ios_hdr_screen(
            str(extra.get("model") or ""), str(extra.get("marketName") or ""))

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
        "baseGlobals": base_globals,
        "removeWorkerGlobals": list(remove_worker_globals),
        "workerAdd": worker_add,
        "emptyPlugins": empty_plugins,
        "connectionType": connection_type,
        "downlinkMax": downlink_max,
        "codecCapability": codec_capability,
        "webglProfile": webgl_profile,
        "ios": target_os == "ios",
        "android": target_os == "android",
        # F-45：Android 的 TTS 语音表（iOS 侧为空表 → getVoices() 返回 []）
        # F-161：默认语音按 locale 选语族，避免与 Intl 的 locale 冲突
        "androidVoices": _android_tts_voices(target_os, locale),
        "iosChrome": ios_chrome,
        "webgl": webgl,
        "webgpu": webgpu,
        "mediaVideoInputs": media_video_inputs,
        "cssWidth": css_w,
        "cssHeight": css_h,
        "viewportHeight": vp_h,
        "dpr": dpr,
        "safeArea": safe_area,
        "dynamicRangeHigh": dynamic_range_high,
        # F-44：自然方向是否横屏（决定 portrait-primary 的 angle 是 90 还是 0）
        "naturalLandscape": natural_landscape,
        # F-26/F-27：接口原型级成员差集（删除清单）
        "dropMembers": drop_members,
        # F-20：permissions.query 的真机口径表
        "permissions": _ios_permissions(target_os),
        # F-25/F-36：补成员清单（接口对象 / 原型成员 / 常量 / 事件处理器）
        # F-25/F-36：补成员清单（接口对象 / 原型成员 / 常量 / 事件处理器）
        "addMembers": _ios_add_members_gated(
            _add_members(target_os, extra.get("osVersion")),
            extra.get("osVersion") if target_os == "ios" else None),
        "av1": av1_capable,
        # F-140：机型类别显式下发。元素级全屏的 iPad 保留路径原先只读
        # platform/UA，与 deviceType 脱钩时（存量档案迁移、DB 手工编辑）
        # 会把 iPad 误判成 iPhone 而整块抹掉。
        "deviceType": _device_type,
        # F-142：MSE 真值表的两个运行期开关。`ios` 决定 WebM 的 VP9/ogg 分支
        # （iOS 只有硬件 VP9），`av1` 决定 av01-in-webm 的机型门控；其余分支
        # 与平台无关。不能复用 codecCapability——那是 canPlayType 表。
        "mseTypes": {"ios": target_os == "ios", "av1": av1_capable},
        # F-89：CSS 白名单的版本门控（低于 profile 版本的属性一律 false）
        "cssGate": (_css_version_gated(extra.get("osVersion"))
                    if target_os == "ios" else []),
        # F-163：设备声明的字体子集。宿主 macOS 的本地字体（Helvetica Neue /
        # Luminari / Galvji / InaiMathi Bold / PingFang HK Light）会经
        # `new FontFace(n, 'local("<n>")').load()` 解析成功，creepjs 的
        # isFontOSBad 据此判定「Android UA 却是 Apple 字体」记 bold。
        # 注入器据此白名单裁剪 local() 源。
        "fonts": [str(f) for f in (fp.get("fonts") or []) if str(f)],
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
    "sec-ch-ua-wow64", "sec-ch-ua-form-factors",
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
    if "sec-ch-ua-form-factors" in hints:
        ff = ua_ch.get("formFactors") or []
        if ff:
            out["Sec-CH-UA-Form-Factors"] = ", ".join(f'"{v}"' for v in ff)
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


# iOS Chrome（CriOS）：POST 导航重放面（B1-7）
# 依据 Chrome 61 同 commit 源码：
#   ios/web/web_state/js/crw_js_post_request_loader.mm 在 POST 导航时用
#   loadHTMLString 造一个合成文档，内联执行 post_request.js 并调用
#   __crPostRequestWorkaround.runPostRequest(url, headers, base64Body, contentType)；
#   post_request.js 把该全局定义为**命名空间对象**（不是函数）。同一时刻
#   CRWWKScriptMessageRouter 才临时注册 POSTSuccessHandler / POSTErrorHandler
#   两个通道，重放完成（成功/错误回执）后立即 removeScriptMessageHandlerForName。
# 所以普通 GET 文档上这三者都不存在——无条件注入反而多出一个可检测面。
# 内核本身原生支持 POST 导航、不需要这个 workaround，本层只补可观测面：
# 主文档由 POST 请求产生时，在文档起点装配同名命名空间（含真实实现与源码
# 形态的 toString），GET 文档一律不补。
#
# 注入时机的难点：Page.addScriptToEvaluateOnNewDocument 的注册只对「注册之后
# 发起」的导航生效（实测在 requestWillBeSent 里注册会漏掉这次 POST 文档、并
# 泄漏给下一次 GET），而 Fetch 暂停期间 Page 域命令会被这次导航卡死。所以
# 脚本**常驻注册**，POST 与否用一个不进网络请求的标记传给文档：Fetch 响应级
# 暂停时（请求已发出，服务端看不到）用 Network.setCookie 种一个一次性随机
# cookie，脚本在文档起点读到后立刻删除——页面脚本看到的 document.cookie 与
# 真机一致，服务端请求头也不含该 cookie。
#
# 本脚本必须排在主补丁**之前**：它只负责清标记并把结论交给主补丁，通道本身
# 由主补丁的具名 getter 机制装配（与 crwebinvoke 同一路径，实例上可读不可
# 枚举、原型上不留自有属性）。交接用的全局属性在主补丁执行的同一段同步代码
# 里删除，页面脚本永远看不到。
_CRIOS_POST_REQUEST_JS = r"""
(function () {
    'use strict';
    // 真机该脚本只作用于宿主自己造的合成文档，不进子帧。
    if (window.top !== window) { return; }
    var __marker = null;
    try {
        var __raw = String(document.cookie || '');
        var __parts = __raw ? __raw.split(';') : [];
        for (var __i = 0; __i < __parts.length; __i++) {
            var __kv = __parts[__i].replace(/^\s+|\s+$/g, '');
            if (__kv.indexOf('__fpwb_pn_') === 0) {
                __marker = __kv.split('=')[0];
                break;
            }
        }
        if (!__marker) { return; }
        document.cookie = __marker + '=; path=/; max-age=0';
    } catch (e) { return; }
    try {
        Object.defineProperty(window, '__fpwbPostNav', {
            value: true, writable: true, enumerable: false, configurable: true,
        });
    } catch (e) {}
})();
"""

# runPostRequest 的**原文**（Chrome 61 同 commit post_request.js，逐字节拷贝，
# 仅去掉赋值号与结尾分号）。真机上页面读到的是这段源码——检测站常把
# `String(fn)` 里有没有 document.write / 同步 XHR 当成宿主痕迹，所以实现虽用
# 普通函数写，toString 统一登记为这段原文（与真机逐字节一致）。
_CRIOS_POST_REQUEST_FN_SRC = r"""function(
    url, headers, body, contentType) {

  /**
   * Converts a Base64-encoded string to a blob.
   * @param {string} byteCharacters Base64-encoded data string.
   * @param {string} contentType Corresponding content type.
   * @return {Blob} Binary representation of byteCharacters.
   */
  var base64ToBlob = function(byteCharacters, contentType) {
    contentType = contentType || '';
    var sliceSize = 512;
    var byteArrays = [];
    var byteCount = byteCharacters.length;
    for (var offset = 0; offset < byteCount; offset += sliceSize) {
      var slice = byteCharacters.slice(offset, offset + sliceSize);
      var byteNumbers = new Array(slice.length);
      for (var i = 0; i < slice.length; i++) {
        byteNumbers[i] = slice.charCodeAt(i);
      }
      var byteArray = new Uint8Array(byteNumbers);
      byteArrays.push(byteArray);
    }
    return new Blob(byteArrays, {type: contentType});
  }

  /**
   * Creates and executes a POST request.
   * @param {string} url The url of the request.
   * @param {Object<string,string>} headers Request headers to include in POST.
   * Each header value must be expressed as a key-value pair in this object.
   * @param {string} body Request body encoded with Base64.
   * @param {string} contentType Content-Type header value.
   */
  var createAndSendPostRequest = function(url, headers, body, contentType) {
    var request = new XMLHttpRequest();
    request.open('POST', url, false);
    for (var key in headers) {
      if (headers.hasOwnProperty(key)) {
        request.setRequestHeader(key, headers[key]);
      }
    }
    var blob = base64ToBlob(atob(body), contentType);
    request.send(blob);
    if (request.status != 200) {
      throw request.status;
    }
    return request.responseText;
  }

  document.open();
  try {
    document.write(createAndSendPostRequest(url, headers, body, contentType));
    window.webkit.messageHandlers['POSTSuccessHandler'].postMessage("");
  } catch(error) {
    window.webkit.messageHandlers['POSTErrorHandler'].postMessage(error);
  }
  document.close();
}"""

# ------------------------------------------------------------------ JS 补丁
# 检测站点（如 BrowserScan）会做函数完整性检查：被 JS 包装过的函数
# Function.prototype.toString() 不再是 "[native code]" → 直接标记
# "Canvas related functions have been artificially modified"。
# __maskNative 把补丁函数登记进注册表，并接管 Function.prototype.toString
# 使其对补丁函数返回原生样式的字符串（含函数名）。
# ---------------------------------------------------------------------------
# F-142：MSE 独立真值表（页面与 Worker 共用同一份源码，见 __MSE_SRC__）
# ---------------------------------------------------------------------------
# `MediaSource.isTypeSupported` 在 WebKit 里走
# MediaSource::isTypeSupported → MediaPlayer::supportsType →
# bestMediaEngineForSupportParameters → MediaPlayerPrivateMediaSourceAVFObjC::
# supportsTypeAndCodecs → SourceBufferParser::isContentTypeSupported，即
# 「WebM 解析器优先、否则 AVStreamDataParser」两条独立分支；与
# HTMLMediaElement.canPlayType 的 MIMETypeCache 路径**不同源**。实测反例：
# `audio/mp4; codecs="flac"` canPlayType=probably 而 MSE=false；`video/mp2t`
# canPlayType='' 而 MSE=true。所以本表按 WebKit 源码逐条复刻，既不能从
# `_CODEC_CAPABILITY_IOS` 外推，也不能把本表外推到 canPlayType。
#
# WebM 分支（SourceBufferParserWebM::isContentTypeSupported）：
#   容器 audio/webm|video/webm（忽略大小写），audio/webm 还要求至少一个音频
#   解码器可用；codecs 为空 → MayBeSupported（true）。
#   vp09*/vp9/vp9.0 → isVP9DecoderAvailable()：iOS 只有硬件 VP9（Apple 芯片
#     无 VP9 硬解）→ false；桌面有 SW 回退 → true。
#   vp08*/vp8/vp8.0 → isVP8DecoderAvailable() → VideoDecoder::isVPXSupported()
#     → WebRTCProvider::webRTCAvailable()，iOS 上无条件 true。
#   opus/vorbis → isOpus/VorbisDecoderAvailable()：桌面 dlopen 到
#     AudioCodecs.component → true；iOS 无该组件，回落运行期种子位，源码不可
#     判（真机采集单待补）——按基线锚点取 true。
#   av01.* → av1HardwareDecoderAvailable()（A17 Pro 起）。
#   其余（avc1.* / pcm 仅在 limitedMatroska 下放行）→ 不支持。
# AVFoundation 分支（SourceBufferParserAVFObjC::isContentTypeSupported）：
#   codecs 先经 outputMIMECodecParameterForInputMIMECodecParameter 归一化
#   （丢弃空项），再查 AVStreamDataParserMIMETypeCache。
#
# 已对 Playwright WebKit（macOS 锚点）做过 45000+ 条差分对拍，零偏差；
# ios / av1 两个分支按源码取证得出，macOS 锚点上无法差分。
_MSE_SUPPORT_SRC = r"""function __mseSupports(raw, opt) {
  opt = opt || {};
  var ios = !!opt.ios, av1dev = !!opt.av1;
  var WS = ' \t';
  function trim(s) {
    var a = 0, b = s.length;
    while (a < b && WS.indexOf(s.charAt(a)) !== -1) { a++; }
    while (b > a && WS.indexOf(s.charAt(b - 1)) !== -1) { b--; }
    return s.slice(a, b);
  }
  function lower(s) { return s.replace(/[A-Z]+/g, function (m) { return m.toLowerCase(); }); }
  function containerOf(raw) {
    var i = raw.indexOf(';');
    return trim(i === -1 ? raw : raw.slice(0, i));
  }
  function paramOf(raw, name) {
    var semi = raw.indexOf(';');
    if (semi === -1) { return { value: '', quoted: false }; }
    var at = lower(raw).indexOf(name, semi + 1);
    if (at === -1) { return { value: '', quoted: false }; }
    var eq = raw.indexOf('=', at + name.length);
    if (eq === -1) { return { value: '', quoted: false }; }
    var q1 = raw.indexOf('"', eq + 1);
    var q2 = q1 === -1 ? -1 : raw.indexOf('"', q1 + 1);
    if (q1 !== -1 && q2 !== -1) {
      return { value: trim(raw.slice(q1 + 1, q2)), quoted: true };
    }
    var end = raw.indexOf(';', eq + 1);
    return { value: trim(raw.slice(eq + 1, end === -1 ? raw.length : end)),
             quoted: false };
  }
  // ---- AVFoundation codec predicates (case-sensitive) ----
  function allDigits(s) {
    if (!s.length) { return false; }
    for (var i = 0; i < s.length; i++) {
      var c = s.charAt(i);
      if (c < '0' || c > '9') { return false; }
    }
    return true;
  }
  // avc1 / avc3：四字符码之后只能是结尾、'.' 或十六进制串
  function avcOk(c) {
    if (c.slice(0, 4) !== 'avc1' && c.slice(0, 4) !== 'avc3') { return false; }
    if (c.length === 4) { return true; }
    if (c.charAt(4) !== '.') { return false; }
    var rest = c.slice(5);
    if (!rest.length) { return true; }
    return !/^[ \t]/.test(rest);
  }
  // hvc1 / hev1：裸四字符码，或 P.T.L<n>[.尾缀]，profile ∈ {1,2,3,4}
  function hevcOk(c) {
    if (c === 'hvc1' || c === 'hev1') { return true; }
    var p = c.slice(0, 5);
    if (p !== 'hvc1.' && p !== 'hev1.') { return false; }
    var parts = c.slice(5).split('.');
    if (parts.length < 3) { return false; }
    var prof = parts[0];
    if (prof !== '1' && prof !== '2' && prof !== '3' && prof !== '4') { return false; }
    if (!allDigits(parts[1])) { return false; }
    var lv = parts[2];
    if (lv.length < 2) { return false; }
    if (lv.charAt(0) !== 'L' && lv.charAt(0) !== 'H') { return false; }
    var num = lv.slice(1);
    if (!allDigits(num)) { return false; }
    return parseInt(num, 10) <= 186;
  }
  // dvh1 / dvhe：裸四字符码，或两个两位字段，profile ∈ {05,08}、level 01..09
  function doviOk(c) {
    if (c === 'dvh1' || c === 'dvhe') { return true; }
    var p = c.slice(0, 5);
    if (p !== 'dvh1.' && p !== 'dvhe.') { return false; }
    var parts = c.slice(5).split('.');
    if (parts.length !== 2) { return false; }
    if (parts[0] !== '05' && parts[0] !== '08') { return false; }
    if (parts[1].length !== 2) { return false; }
    var lv = parts[1];
    return lv.charAt(0) === '0' && lv.charAt(1) >= '1' && lv.charAt(1) <= '9';
  }
  // mp4 内的 vp09：裸四字符码，或至少 3 个数字开头字段
  function vp9Mp4Ok(c) {
    if (c === 'vp09') { return true; }
    if (c.slice(0, 5) !== 'vp09.') { return false; }
    var parts = c.slice(5).split('.');
    if (parts.length < 3) { return false; }
    return allDigits(parts[0]) && allDigits(parts[1]) && /^\d/.test(parts[2])
        && parts[2].indexOf(' ') === -1 && parts[2].indexOf('\t') === -1;
  }
  // mp4a.40.<OTI> / mp4a.67
  function mp4aOk(c) {
    if (c.slice(0, 5) !== 'mp4a.') { return false; }
    var rest = c.slice(5);
    var dot = rest.indexOf('.');
    if (dot === -1) {
      return rest.length >= 1 && allDigits(rest) && parseInt(rest, 10) === 67;
    }
    var oti = rest.slice(0, dot);
    if (!allDigits(oti) || parseInt(oti, 10) !== 40) { return false; }
    var sub = rest.slice(dot + 1);
    if (!allDigits(sub)) { return false; }
    var v = parseInt(sub, 10);
    return v === 2 || v === 5 || v === 29 || v === 32 || v === 33 || v === 42;
  }
  var MP4_FAMILY = {
    'video/mp4': 1, 'audio/mp4': 1, 'application/mp4': 1, 'audio/x-m4a': 1,
    'video/x-m4v': 1, 'video/3gpp': 1, 'video/3gpp2': 1, 'audio/3gpp': 1
  };
  function mp4FamilyCodecOk(c) {
    if (avcOk(c) || hevcOk(c) || doviOk(c) || vp9Mp4Ok(c) || mp4aOk(c)) { return true; }
    // F-123：MSE 的 mp4 分支最终落到 AVStreamDataParser（SourceBufferParser
    // AVFObjC → AVStreamDataParserMIMETypeCache::canDecodeExtendedType），
    // 与 WebM 的 av1HardwareDecoderAvailable() 同受 VideoToolbox 硬解门控
    // （AV1UtilitiesCocoa.mm:198-205）→ 机型能力位决定，非恒 false。
    if (c === 'av01' || c.indexOf('av01.') === 0) { return av1dev; }
    return c === 'alac' || c === 'ac-3' || c === 'ec-3' || c === 'Opus'
        || c === 'fLaC' || c === 'ulaw' || c === 'alaw' || c === 'samr' || c === 'sawb';
  }
  function quicktimeCodecOk(c) {
    if (avcOk(c) || hevcOk(c) || doviOk(c) || vp9Mp4Ok(c) || mp4aOk(c)) { return true; }
    return c === 'alac' || c === 'ac-3' || c === 'ec-3' || c === 'opus'
        || c === 'flac' || c === 'lpcm' || c === 'ulaw' || c === 'alaw'
        || c === 'samr' || c === 'sawb';
  }
  function mp2tCodecOk(c) {
    if (c.slice(0, 4) === 'avc1' || c.slice(0, 4) === 'avc3') {
      if (c.length === 4) { return true; }
      if (c.charAt(4) !== '.') { return false; }
      var rest = c.slice(5);
      return !rest.length || !/^[ \t]/.test(rest);
    }
    if (mp4aOk(c)) { return true; }
    return c === 'ac-3' || c === 'ec-3';
  }
  function aacCodecOk(c) { return mp4aOk(c); }
  function mpegAudioCodecOk(c) { return c === 'mp3'; }
  // ---- WebM（容器忽略大小写，codec 区分大小写）----
  function parseVP(c) {
    var isVP9 = c.indexOf('vp09') === 0 || c === 'vp9' || c === 'vp9.0';
    var isVP8 = c.indexOf('vp08') === 0 || c === 'vp8' || c === 'vp8.0';
    if (!isVP9 && !isVP8) { return null; }
    if (isVP8) {
      if (c === 'vp8' || c === 'vp8.0') { return { vp8: true, legacy: true }; }
      if (c !== 'vp8.0' && c.indexOf('vp08') !== 0) { return null; }
      if (c === 'vp08') { return null; }
      if (c.charAt(4) !== '.') { return null; }
      var p8 = c.slice(5).split('.');
      if (p8.length !== 3) { return null; }
      for (var i = 0; i < 3; i++) { if (!allDigits(p8[i])) { return null; } }
      if (parseInt(p8[0], 10) !== 0) { return null; }
      var bd8 = parseInt(p8[2], 10);
      if (bd8 !== 8) { return null; }
      return { vp8: true, ok: true };
    }
    if (c === 'vp9' || c === 'vp9.0') { return { vp9: true, legacy: true }; }
    if (c === 'vp09') { return null; }
    if (c.charAt(4) !== '.') { return null; }
    var p = c.slice(5).split('.');
    if (p.length !== 3 && p.length !== 8) { return null; }
    for (var j = 0; j < p.length; j++) { if (!allDigits(p[j])) { return null; } }
    var prof = parseInt(p[0], 10);
    if (prof !== 0 && prof !== 2) { return null; }
    var lv = parseInt(p[1], 10);
    if (lv > 60) { return null; }
    var bd = parseInt(p[2], 10);
    if (p.length === 3) {
      if (bd === 8) { if (lv < 10) { return null; } }
      else if (bd !== 10) { return null; }
    } else if (bd !== 8 && bd !== 10) { return null; }
    if (p.length === 8) {
      var chroma = parseInt(p[3], 10), cp = parseInt(p[4], 10), tc = parseInt(p[5], 10);
      var mc = parseInt(p[6], 10), fr = parseInt(p[7], 10);
      if (chroma > 3 || [1,2,4,5,6,7,8,9,10,11,12,22].indexOf(cp) === -1) { return null; }
      if ([1,2,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18].indexOf(tc) === -1) { return null; }
      if ([0,1,2,4,5,6,7,8,9,10,11,12,13,14].indexOf(mc) === -1) { return null; }
      if (mc === 0 && chroma !== 3) { return null; }
      if (fr > 1) { return null; }
    }
    return { vp9: true, ok: true };
  }
  function webmCodecOk(c) {
    var vp = parseVP(c);
    if (vp) {
      // 解码器可用性判定在 parseVPCodecParameters **之前**，legacy 拼写
      // （vp9 / vp9.0）同样受 !isVP9DecoderAvailable() 拦截。
      if (vp.vp8) { return true; }
      return !ios;
    }
    if (c === 'opus' || c === 'vorbis') { return true; }
    if (c.indexOf('av01.') === 0) { return av1dev; }
    return false;
  }
  // ---- 顶层 ----
  if (typeof raw === 'symbol') { throw new TypeError('Cannot convert a symbol to a string'); }
  var s = String(raw);
  var container = containerOf(s);
  if (container === '') { return false; }
  var codecInfo = paramOf(s, 'codecs');
  var codecParam = codecInfo.value;
  if (!codecInfo.quoted) {
    // 未加引号的取值里出现引号会截断 codec 列表（WebKit 的解析顺序）
    var q = codecParam.indexOf('"');
    if (q > 0) { codecParam = trim(codecParam.slice(0, q)); }
  }
  var isAVF = Object.prototype.hasOwnProperty.call(MP4_FAMILY, container)
      || container === 'video/quicktime' || container === 'audio/aac'
      || container === 'audio/aacp' || container === 'audio/mpeg'
      || container === 'audio/mp3' || container === 'audio/mpeg3'
      || container === 'audio/mpg' || container === 'video/mp2t';
  var lc = lower(container);
  var isWebm = lc === 'video/webm' || lc === 'audio/webm';
  var isOgg = lc === 'audio/ogg' || lc === 'application/ogg';
  if (!isAVF && !isWebm && !isOgg) { return false; }
  if (isOgg) {
    if (opt.ios) { return false; }
    var oc = codecParam === '' ? [] : codecParam.split(',').map(trim);
    if (!oc.length) { return true; }
    // audio/ogg 才列 codec；application/ogg 不列
    if (lc === 'application/ogg') { return false; }
    var oggReal = false;
    for (var oi = 0; oi < oc.length; oi++) {
      if (oc[oi] === '') { continue; }
      oggReal = true;
      if (oc[oi] !== 'vorbis' && oc[oi] !== 'opus' && oc[oi] !== 'flac') { return false; }
    }
    return oggReal;
  }
  var codecs;
  var emptyOk = false;
  if (codecParam === '') { codecs = []; emptyOk = true; }
  else {
    codecs = codecParam.split(',').map(trim);
    // AVFoundation 丢弃空项，但保留一组分隔符
    var first = codecParam.indexOf(',');
    if (first !== -1 && codecParam.length > first + 1) {
      emptyOk = true;
    }
  }
  if (isWebm) {
    // WebM 分支保留 AVF 归一化会丢掉的空项
    if (!codecs.length) { return true; }
    var sawReal = false;
    for (var i = 0; i < codecs.length; i++) {
      if (codecs[i] === '') { continue; }
      sawReal = true;
      if (!webmCodecOk(codecs[i])) { return false; }
    }
    return sawReal;
  }
  // AVFoundation 分支：丢弃空项（outputMIMECodecParameterForInput...）
  var real = [];
  for (var k = 0; k < codecs.length; k++) { if (codecs[k] !== '') { real.push(codecs[k]); } }
  if (!real.length) { return emptyOk; }
  var pred;
  if (container === 'video/quicktime') { pred = quicktimeCodecOk; }
  else if (container === 'audio/aac') { pred = aacCodecOk; }
  else if (container === 'audio/aacp') { return false; }
  else if (container === 'audio/mpeg' || container === 'audio/mp3'
           || container === 'audio/mpeg3' || container === 'audio/mpg') {
    pred = mpegAudioCodecOk;
  } else if (container === 'video/mp2t') { pred = mp2tCodecOk; }
  else { pred = mp4FamilyCodecOk; }
  for (var m = 0; m < real.length; m++) { if (!pred(real[m])) { return false; } }
  return true;
}"""


# F-143：canPlayType 兜底——从「表外透传宿主 Blink」改成「按 WebKit 容器
# 白名单算法自行判定」。
#
# 旧语义的漏洞：`_CODEC_CAPABILITY_IOS` 是稀疏覆盖表，未命中的串直接透传
# 宿主 Chromium 的 FFmpeg 口径。于是 `audio/mp4; codecs="opus"` 得到
# "probably"——而 WebKit 明确不认（AVAssetMIMETypeCache::canDecodeExtendedType
# 有 opus-in-mp4 的免责分支），一行 canPlayType 即可判假。同类还有
# `video/x-matroska`（F-126）、版本化 codecs 串（F-124）。
#
# 本函数逐行复刻 MIMETypeCache::canDecodeType
# （Source/WebCore/platform/graphics/MIMETypeCache.cpp:71-105）：
#   1. 空串 → ''；容器不在白名单 → ''（supportsContainerType）
#   2. codecs 为空 → "maybe"（MayBeSupported：容器命中即可，不需要解码器背书）
#   3. shouldOverrideExtendedType（audio/mpeg + 单个 "mp3"，:163-175）
#      → "probably"
#   4. canDecodeExtendedType（AVFoundation 扩展查询）→ "probably" / ''
#
# 白名单 = AVAssetMIMETypeCache::isStaticContainerType 的 30 项
# （AVAssetMIMETypeCache.mm:143-179，**无平台分支**）＋ iOS 的
# audiovisualMIMETypes 动态项（audio/flac、audio/x-caf、video/mp2t）。
#
# 为什么不能把 macOS 基线整表照抄：macOS 的 AVFoundation 自带 webm/ogg/avi
# 解码器，同一份 WebKit 在 macOS 上给 "maybe"/"probably"，iOS 的容器表里
# 没有这些项（清单 F-125 排除表：`video/avi` 必须是 ''）。故白名单按 iOS
# 口径写死，macOS 基线只当「算法复刻正确性」的差分锚点（263 条矩阵里
# 34 条差异全部是已知的 iOS/macOS 平台差，无一条意外）。
#
# codecs 谓词（ACCEPT）同样按 iOS 解码器族写死：MP4 族认 avc1/hvc1/dvh1/
# vp09/av01/mp4a/alac/ac-3/ec-3/Opus/fLaC/samr/ulaw；quicktime 另有
# opus/flac/lpcm；mpegts 只认 avc1/mp4a/ac-3/ec-3；纯音频容器按解码器族
# （mp3 / mp4a / PCM '1'）收敛。未列 codec 一律 ''——这正是「白名单语义」。
#
# 与 _MSE_SUPPORT_SRC 的关系：WebKit 内部 canPlayType（MIMETypeCache）与
# MediaSource::isTypeSupported（SourceBufferParser）是两条独立门控，实测
# 互相矛盾（flac-in-mp4：cpt probably / MSE false；mp2t：cpt '' / MSE true）
# ——两张表各自维护，禁止互推（清单 §49 F-144 要点）。
_CPT_IOS_SRC = r"""function __cptIos(raw, opt) {
  opt = opt || {};
  var WS = ' \t';
  function trim(s) {
    var a = 0, b = s.length;
    while (a < b && WS.indexOf(s.charAt(a)) !== -1) { a++; }
    while (b > a && WS.indexOf(s.charAt(b - 1)) !== -1) { b--; }
    return s.slice(a, b);
  }
  var MP4_CODECS = ['avc1', 'hvc1', 'dvh1', 'vp09', 'av01', 'mp4a', 'alac',
                    'ac-3', 'ec-3', 'Opus', 'fLaC', 'samr', 'ulaw'];
  var STATIC = {
    'application/vnd.apple.mpegurl': 1, 'application/x-mpegurl': 1,
    'audio/3gpp': 1, 'audio/aac': 1, 'audio/aacp': 1, 'audio/aiff': 1,
    'audio/basic': 1, 'audio/mp3': 1, 'audio/mp4': 1, 'audio/mpeg': 1,
    'audio/mpeg3': 1, 'audio/mpegurl': 1, 'audio/mpg': 1, 'audio/vnd.wave': 1,
    'audio/wav': 1, 'audio/wave': 1, 'audio/x-aac': 1, 'audio/x-aiff': 1,
    'audio/x-m4a': 1, 'audio/x-mpegurl': 1, 'audio/x-wav': 1,
    'video/3gpp': 1, 'video/3gpp2': 1, 'video/mp4': 1, 'video/mpeg': 1,
    'video/mpeg2': 1, 'video/mpg': 1, 'video/quicktime': 1, 'video/x-m4v': 1,
    'video/x-mpeg': 1, 'video/x-mpg': 1
  };
  var DYNAMIC = { 'audio/flac': 1, 'audio/x-caf': 1, 'video/mp2t': 1 };
  var ACCEPT = {
    'video/mp4': MP4_CODECS, 'audio/mp4': MP4_CODECS, 'video/x-m4v': MP4_CODECS,
    'video/3gpp': MP4_CODECS, 'video/3gpp2': MP4_CODECS,
    'audio/3gpp': MP4_CODECS, 'audio/x-m4a': MP4_CODECS,
    'video/quicktime': ['avc1', 'hvc1', 'mp4a', 'alac', 'ac-3', 'ec-3',
                        'opus', 'fLaC', 'flac', 'lpcm'],
    'video/mp2t': ['avc1', 'mp4a', 'ac-3', 'ec-3'],
    'audio/mpeg': ['mp3'], 'audio/mp3': ['mp3'], 'audio/mpeg3': ['mp3'],
    'audio/mpg': ['mp3'],
    'audio/aac': ['mp4a'], 'audio/aacp': ['mp4a'], 'audio/x-aac': ['mp4a'],
    'audio/wav': ['1'], 'audio/wave': ['1'], 'audio/x-wav': ['1'],
    'audio/vnd.wave': ['1']
  };
  var s = trim(String(raw));
  if (!s) { return ''; }
  var parts = s.split(';');
  var container = trim(parts[0]).toLowerCase();
  if (!container) { return ''; }
  if (!STATIC[container] && !DYNAMIC[container]) { return ''; }
  var codecs = null;
  for (var i = 1; i < parts.length; i++) {
    var eq = parts[i].indexOf('=');
    if (eq < 0) { continue; }
    if (trim(parts[i].slice(0, eq)).toLowerCase() !== 'codecs') { continue; }
    var v = trim(parts[i].slice(eq + 1));
    if (v.length >= 2 && v.charAt(0) === v.charAt(v.length - 1)
        && (v.charAt(0) === '"' || v.charAt(0) === "'")) {
      v = v.slice(1, -1);
    }
    var bits = v.split(',');
    codecs = [];
    for (var j = 0; j < bits.length; j++) {
      var t = trim(bits[j]);
      if (t) { codecs.push(t); }
    }
    break;
  }
  // ContentType::codecs() 为空 → MayBeSupported（MIMETypeCache.cpp:84-88）
  if (!codecs || !codecs.length) { return 'maybe'; }
  // shouldOverrideExtendedType：Modernizr 惯用的 audio/mpeg; codecs="mp3"
  if (container === 'audio/mpeg' && codecs.length === 1 && codecs[0] === 'mp3') {
    return 'probably';
  }
  var accept = ACCEPT[container];
  if (!accept) { return ''; }
  for (var k = 0; k < codecs.length; k++) {
    var c = codecs[k];
    // AVAssetMIMETypeCache::canDecodeExtendedType（:108-112）：MP4 容器内的
    // 小写 'flac' 先改写成 MP4RA 注册的 'fLaC' 再问 AVFoundation —— 所以
    // mp4 认 flac，而 audio/flac 容器带 codecs="flac" 反而不认。
    if ((container === 'video/mp4' || container === 'audio/mp4')
        && c.indexOf('flac') !== -1) {
      c = c.split('flac').join('fLaC');
    }
    var ok = false;
    for (var m = 0; m < accept.length; m++) {
      var b = accept[m];
      if (c === b || c.indexOf(b + '.') === 0) {
        // av01 由机型能力位门控（A17 Pro 起才有硬解）
        ok = (b === 'av01') ? !!opt.av1 : true;
        break;
      }
    }
    if (!ok) { return ''; }
  }
  return 'probably';
}"""


_WEBCODECS_ICS_SRC = r"""// ---- 编解码器能力判定（WebKit main 源码口径，F-133）----
// 视频：WebCodecsVideoDecoder/Encoder.cpp isSupported*Codec——
//   vp8* / avc1.* / vp09.*（profile 0 + 8bit）/ hev1.* / hvc1.*
//   （WebCodecsHEVCEnabled，COCOA 默认 true）/ av01.0*
//   （WebCodecsAV1Enabled，COCOA 默认 false、status: preview →
//   出货版 Safari 不开，与机型有无 AV1 硬解无关）。
// 音频：AudioDecoderCocoa::isCodecSupported 用前缀/精确匹配；
//   AudioEncoder 的 mp4a 是精确串表 + 采样率区间。
var __wcVp9Ok = function (c) {
    // vp09.<profile>.<level>.<bitDepth>
    var p = c.split('.');
    if (p.length < 4) { return false; }
    var profile = parseInt(p[1], 10);
    var depth = parseInt(p[3], 10);
    // profile 2（10bit）另受 webRTCVP9Profile2CodecEnabled 门控，
    // 出货版 WebCodecs 走不到 → 按 false。
    return profile === 0 && depth === 8;
};
var __wcPcmOk = function (c) {
    var p = c.split('-');
    return p.length === 2 && /^(u8|s16|s24|s32|f32)$/.test(p[1]);
};
var __wcCodecOk = function (codec, kind, cfg) {
    var c = String(codec || '');
    var lc = c.toLowerCase();
    if (!c) { return false; }
    if (kind === 'vd' || kind === 've') {
        return lc.indexOf('vp8') === 0
            || lc.indexOf('avc1.') === 0
            || (lc.indexOf('vp09.') === 0 && __wcVp9Ok(lc))
            || lc.indexOf('hev1.') === 0
            || lc.indexOf('hvc1.') === 0;
    }
    if (kind === 'ad') {
        return /^mp4a\.40\.(5|23|29|39)/.test(lc)
            || lc.indexOf('mp4a') === 0
            || lc === 'mp3' || lc === 'opus' || lc === 'alaw'
            || lc === 'ulaw' || lc === 'flac' || lc === 'vorbis'
            || (lc.indexOf('pcm-') === 0 && __wcPcmOk(lc));
    }
    // ae：mp4a 是精确串表（不吃 mp4a.40.6 之类）
    var exact = /^(mp4a\.40\.(2|02|5|05|29|42)|mp3|opus|alaw|ulaw|flac|vorbis)$/
        .test(lc);
    if (!exact && !(lc.indexOf('pcm-') === 0 && __wcPcmOk(lc))) { return false; }
    var sr = (cfg && cfg.sampleRate) ? Number(cfg.sampleRate) : 0;
    return sr >= 3000 && sr <= 384000;
};
var __wcSorted = function (o) {
    var out = {};
    Object.keys(o).sort().forEach(function (k) { out[k] = o[k]; });
    return out;
};
var __wcSupportResult = function (cfg, supported, fill) {
    var out = {};
    if (cfg && typeof cfg === 'object') {
        for (var k in cfg) {
            if (Object.prototype.hasOwnProperty.call(cfg, k)) { out[k] = cfg[k]; }
        }
    }
    (fill || []).forEach(function (pair) {
        if (!(pair[0] in out)) { out[pair[0]] = pair[1]; }
    });
    return Promise.resolve({ config: __wcSorted(out), supported: !!supported });
};
// 真机必填项缺失时抛 WebKit 文案的 TypeError（实测
// `Member WebCodecsAudioDecoderConfig.numberOfChannels is required and
// must be an instance of unsigned long`）。
var __wcReq = function (cfg, iface, member, type) {
    if (cfg && cfg[member] !== undefined && cfg[member] !== null) { return; }
    throw new TypeError('Member ' + iface + '.' + member
                        + ' is required and must be an instance of ' + type);
};
var __wcIcsImpl = function (name, kind) {
    return function (t, args) {
        var cfg = args[0];
        if (cfg === undefined || cfg === null) {
            throw new TypeError('Not enough arguments');
        }
        if (typeof cfg !== 'object') { throw new TypeError('Type error'); }
        var iface = 'WebCodecs' + name + 'Config';
        __wcReq(cfg, iface, 'codec', 'DOMString');
        if (kind === 'ad' || kind === 'ae') {
            // 实测校验顺序：codec → numberOfChannels → sampleRate
            __wcReq(cfg, iface, 'numberOfChannels', 'unsigned long');
            __wcReq(cfg, iface, 'sampleRate', 'unsigned long');
            if (!cfg.sampleRate) { throw new TypeError('Config is not valid'); }
            return __wcSupportResult(cfg, __wcCodecOk(cfg.codec, kind, cfg),
                (kind === 'ae') ? [['bitrateMode', 'variable']] : []);
        }
        if (kind === 've') {
            __wcReq(cfg, iface, 'width', 'unsigned long');
            __wcReq(cfg, iface, 'height', 'unsigned long');
            return __wcSupportResult(cfg, __wcCodecOk(cfg.codec, kind, cfg),
                [['alpha', 'discard'], ['bitrateMode', 'variable'],
                 ['hardwareAcceleration', 'no-preference'],
                 ['latencyMode', 'quality']]);
        }
        return __wcSupportResult(cfg, __wcCodecOk(cfg.codec, kind, cfg),
            [['hardwareAcceleration', 'no-preference']]);
    };
};
var __wcIcsKind = function (name) {
    return (name === 'AudioDecoder') ? 'ad'
         : (name === 'AudioEncoder') ? 'ae'
         : (name === 'VideoDecoder') ? 'vd' : 've';
};
"""


# F-135：navigator.mediaCapabilities 口径。
# 宿主 Blink 的 decodingInfo/encodingInfo 与 WebKit 差异极大（AV1 假阳性、
# opus/flac 标志位、错误文案、configuration 归一化），检测站一行即可判定。
# 这里按 WebKit 源码（MediaCapabilities.cpp 的字典校验 + ContentType.cpp 的
# 参数解析 + PlatformMediaEngineConfigurationFactoryCocoa.cpp 的引擎选择 &&
# 标志位）逐项复刻，真值表来自基线 WebKit 实测（见 /tmp/f301/out_matrix.txt）。
# 只换原型方法，构造器/实例/品牌校验/SameObject 面保持原生。
# 标志位真值仍待真机采集单回填：opus/flac/aac 三项按源码口径给（Cocoa 的
# 音频分支只置 supported，不置 smooth/powerEfficient）。
_MC_SRC = r"""
var __mc = (function () {
    function __mcIsSpace(c) { return c === ' ' || (c >= '\t' && c <= '\r'); }

    function __mcTrim(s) {
        var a = 0, b = s.length;
        while (a < b && __mcIsSpace(s.charAt(a))) { a++; }
        while (b > a && __mcIsSpace(s.charAt(b - 1))) { b--; }
        return s.slice(a, b);
    }

    function __mcLower(s) {
        var out = '';
        for (var i = 0; i < s.length; i++) {
            var c = s.charCodeAt(i);
            out += (c >= 65 && c <= 90) ? String.fromCharCode(c + 32) : s.charAt(i);
        }
        return out;
    }

    function __mcFindCI(hay, needle, from) {
        if (!needle.length) { return -1; }
        return __mcLower(hay).indexOf(__mcLower(needle), from);
    }

    function __mcStartsCI(s, pfx) { return __mcLower(s.slice(0, pfx.length)) === pfx; }
    function __mcStarts(s, pfx) { return s.slice(0, pfx.length) === pfx; }

    // ---- ContentType.cpp 的 containerType / parameter / codecs 逐行对口 ----
    function __mcContainer(raw) {
        var i = raw.indexOf(';');
        return __mcTrim(i < 0 ? raw : raw.slice(0, i));
    }

    function __mcParam(raw, name) {
        var semi = raw.indexOf(';');
        if (semi < 0) { return ''; }
        var at = __mcFindCI(raw, name, semi + 1);
        if (at < 0) { return ''; }
        var eq = raw.indexOf('=', at + name.length);
        if (eq < 0) { return ''; }
        var q1 = raw.indexOf('"', eq + 1);
        var q2 = q1 < 0 ? -1 : raw.indexOf('"', q1 + 1);
        var start, end;
        if (q1 >= 0 && q2 >= 0) { start = q1 + 1; end = q2; }
        else {
            start = eq + 1;
            end = raw.indexOf(';', start);
            if (end < 0) { end = raw.length; }
        }
        return __mcTrim(raw.slice(start, end));
    }

    // AVFoundation 的 isPlayableExtendedMIMEType 对前置参数只认「第一个带等号的
    // 参数」的值，按该值是否带引号选两套校验表：带引号时认裸四字符码，不带引号
    // 时只认写全的 ISO 串。参数名只在 ContentType::parameter 那侧参与匹配
    // （子串匹配，"xcodecs" / "codecsx" 也算命中），闸门本身不看名字。
    // 引号里的空值（`foo=""`）闸门直接放行。真机对拍表明这套口径对畸形 MIME
    // 串并非逐字节一致（引号、空段、连续分号各有例外），这里只保证真实站点的
    // content type 完全对齐。
    function __mcDigits(s) {
        if (!s.length) { return false; }
        for (var i = 0; i < s.length; i++) {
            var c = s.charCodeAt(i);
            if (c < 48 || c > 57) { return false; }
        }
        return true;
    }

    // 去掉空段后的分段，mp4v 家族的分段计数按这个算
    function __mcNonEmpty(parts) {
        var out = [];
        for (var i = 0; i < parts.length; i++) {
            if (parts[i] !== '') { out.push(parts[i]); }
        }
        return out;
    }

    // avc1：三段式时 profile_idc 必须在 AVC 的档位集合里（按 uint8 截断比较），
    // level 同样按 uint8 截断后必须 <= 99；profile 不是纯数字就整条跳过校验。
    function __mcAvc1Gate(p) {
        if (p.length !== 3) { return true; }
        if (!__mcDigits(p[1])) { return true; }
        var idc = parseInt(p[1], 10) & 0xFF;
        if (idc !== 66 && idc !== 77 && idc !== 88 && idc !== 100
                && idc !== 110 && idc !== 122 && idc !== 244) { return false; }
        var lvl = parseInt(p[2], 10);
        if (!isFinite(lvl)) { lvl = 0; }
        return (lvl & 0xFF) <= 99;
    }

    // hvc1：profile_space 1..4，level 段必须大写 L 开头且 <= 186，其余分段忽略
    function __mcHvc1Gate(p) {
        if (p.length === 1) { return true; }
        if (p.length < 4) { return false; }
        var space = parseInt(p[1], 10);
        if (!(space >= 1 && space <= 4)) { return false; }
        if (p[3].charAt(0) !== 'L') { return false; }
        var lvl = parseInt(p[3].slice(1), 10);
        return isFinite(lvl) && lvl <= 186;
    }

    // vp09：前四段都必须能解析成数字（不限范围），多余分段忽略
    function __mcVp09Gate(p) {
        if (p.length === 1) { return true; }
        if (p.length < 4) { return false; }
        for (var i = 1; i <= 3; i++) {
            if (!isFinite(parseInt(p[i], 10))) { return false; }
        }
        return true;
    }

    // dvh1：必须正好三段，profile 只能是 05 / 08，level 是 01..09 的两位
    function __mcDvh1Gate(p) {
        if (p.length === 1) { return true; }
        if (p.length !== 3) { return false; }
        if (p[1] !== '05' && p[1] !== '08') { return false; }
        return p[2].length === 2 && p[2].charAt(0) === '0'
            && p[2].charAt(1) >= '1' && p[2].charAt(1) <= '9';
    }

    function __mcNumStr(s) {
        if (s === '') { return false; }
        var i = (s.charAt(0) === '+') ? 1 : 0;
        if (i >= s.length) { return false; }
        for (; i < s.length; i++) {
            var c = s.charAt(i);
            if (c < '0' || c > '9') { return false; }
        }
        return true;
    }

    // mp4v：第二段必须正好是 20；第三段以数字或 + 开头时，整段必须是数字且
    // 只能停在第三段，否则后面多出来的分段忽略
    function __mcMp4vGate(p) {
        if (p.length === 1) { return true; }
        if (!__mcNumStr(p[1]) || parseInt(p[1], 10) !== 20) { return false; }
        if (p.length === 2 || p[2] === '') { return true; }
        var c = p[2].charAt(0);
        if (!(c >= '0' && c <= '9') && c !== '+') { return true; }
        return p.length === 3 && __mcNumStr(p[2]);
    }

    function __mcMp4aGate(p) {
        if (p.length !== 3 || parseInt(p[1], 10) !== 40) { return false; }
        var aot = parseInt(p[2], 10);
        return aot === 2 || aot === 5;
    }

    function __mcAvfQuoted(container, codec, opt) {
        if (container === 'audio/ogg' || container === 'video/ogg') {
            return codec === 'opus' || codec === 'flac' || codec === 'vorbis';
        }
        var p = codec.split('.');
        if (__mcStarts(codec, 'avc1')) { return __mcAvc1Gate(p); }
        if (__mcStarts(codec, 'hvc1')) { return __mcHvc1Gate(p); }
        if (__mcStarts(codec, 'vp09')) { return __mcVp09Gate(p); }
        // AVAssetMIMETypeCache::canDecodeExtendedType 问的是系统
        // AVURLAsset isPlayableExtendedMIMEType，AV1 那档答案随机型硬解走
        if (__mcStarts(codec, 'av01')) { return !!(opt && opt.av1); }
        if (__mcStarts(codec, 'dvh1')) { return __mcDvh1Gate(p); }
        if (__mcStarts(codec, 'mp4v')) { return __mcMp4vGate(p); }
        if (__mcStarts(codec, 'mp4a')) { return __mcMp4aGate(p); }
        // 四字符码的大小写口径随容器走：quicktime 认小写 opus/flac 而不认
        // 注册名 Opus/fLaC，其余音频四字符码两边都认
        if (codec === 'alac' || codec === 'ac-3' || codec === 'ec-3'
                || codec === 'samr' || codec === 'ulaw') { return true; }
        if (container === 'video/quicktime') {
            return codec === 'opus' || codec === 'flac';
        }
        return codec === 'Opus' || codec === 'fLaC';
    }

    // 不带引号时同一族的口径更严：avc1 只看有没有第二段，裸四字符码一律不认
    // （hvc1 / vp09 必须写全，mp4v 必须正好写到 profile 段）。
    function __mcAvfUnquoted(container, codec) {
        var p = codec.split('.');
        if (__mcStarts(codec, 'avc1')) { return p.length >= 2; }
        if (__mcStarts(codec, 'hvc1')) { return p.length > 1 && __mcHvc1Gate(p); }
        if (__mcStarts(codec, 'vp09')) { return p.length > 1 && __mcVp09Gate(p); }
        if (__mcStarts(codec, 'mp4v')) {
            var q = __mcNonEmpty(p);
            return q.length === 2 && parseInt(q[1], 10) === 20;
        }
        return false;
    }

    // 按分号切参数段，引号里的分号不切
    function __mcSegs(body) {
        var out = [], start = 0, inQ = false;
        for (var i = 0; i < body.length; i++) {
            var ch = body.charAt(i);
            if (ch === '"') { inQ = !inQ; }
            else if (ch === ';' && !inQ) { out.push(body.slice(start, i)); start = i + 1; }
        }
        out.push(body.slice(start));
        return out;
    }

    // 带引号的 codecs 值里再出现分号：音频侧和 webm 的解析器直接判死，
    // mp4 家族的 AVFoundation 路径不介意
    function __mcSemiInQuotedCodec(raw) {
        var semi = raw.indexOf(';');
        if (semi < 0) { return false; }
        var at = __mcFindCI(raw, 'codecs', semi + 1);
        if (at < 0) { return false; }
        var eq = raw.indexOf('=', at + 'codecs'.length);
        if (eq < 0) { return false; }
        var q1 = raw.indexOf('"', eq + 1);
        if (q1 < 0) { return false; }
        var q2 = raw.indexOf('"', q1 + 1);
        return q2 >= 0 && raw.slice(q1 + 1, q2).indexOf(';') >= 0;
    }

    // 返回 false 表示前置参数闸门判死，'pass' 表示过了闸门（后续不再套容器表），
    // null 表示没有前置参数、走常规路径。
    function __mcPreGate(container, raw, audio, opt) {
        var semi = raw.indexOf(';');
        if (semi < 0) { return null; }
        // AVAssetMIMETypeCache::canDecodeExtendedType 在查表前把 mp4 家族里的
        // 小写 flac 改写成 MP4RA 注册的 fLaC（整条 MIME 一起改写）
        var body = raw.slice(semi + 1);
        if ((container === 'video/mp4' || container === 'audio/mp4')
            && __mcCodecs(raw).indexOf('flac') >= 0) {
            body = body.split('flac').join('fLaC');
        }
        var segs = __mcSegs(body);
        for (var i = 0; i < segs.length; i++) {
            var eq = segs[i].indexOf('=');
            if (eq < 0) { continue; }
            var rest = segs[i].slice(eq + 1);
            var q1 = rest.indexOf('"');
            var q2 = q1 < 0 ? -1 : rest.indexOf('"', q1 + 1);
            var v, check;
            if (q1 >= 0 && q2 >= 0) { v = __mcTrim(rest.slice(q1 + 1, q2)); check = __mcAvfQuoted; }
            else if (q1 >= 0) { return false; }   // 引号没闭合，值被当成半截串
            else {
                // 未加引号时按「值后面还有没有别的东西」分两套表，跟参数名无关：
                // 值正好收在整条 MIME 末尾（后面连一个分号都没有）走宽表，
                // 后面还跟着分号段就走更严的表。真机对拍：
                // `codecs=alac` 放行、`codecs=alac;` 判死。
                v = __mcTrim(rest);
                check = (i === segs.length - 1) ? __mcAvfQuoted : __mcAvfUnquoted;
            }
            if (v === '') { return q1 >= 0 ? 'pass' : false; }
            var items = v.split(',');
            for (var j = 0; j < items.length; j++) {
                var t = __mcTrim(items[j]);
                if (t !== '' && !check(container, t, opt)) { return false; }
            }
            return 'pass';
        }
        return null;
    }

    function __mcCodecs(raw) {
        var v = __mcParam(raw, 'codecs');
        if (v === '') { return []; }
        var parts = v.split(','), out = [];
        for (var i = 0; i < parts.length; i++) {
            // WTF 的 split 不产出尾部空项：`"a,"` 是一个 codec，`""` 是零个
            if (i === parts.length - 1 && parts[i] === '') { break; }
            out.push(__mcTrim(parts[i]));
        }
        return out;
    }

    var __mcBuckets = {
        'application/mp21': 1, 'application/mp4': 1,
        'audio/3gpp': 1, 'audio/3gpp2': 1, 'audio/mp4': 1, 'audio/ogg': 1,
        'audio/vnd.apple.mpegurl': 1, 'audio/webm': 1,
        'video/3gpp': 1, 'video/3gpp2': 1, 'video/mp4': 1, 'video/ogg': 1,
        'video/quicktime': 1, 'video/vnd.apple.mpegurl': 1, 'video/webm': 1
    };

    function __mcBucket(c) {
        return Object.prototype.hasOwnProperty.call(__mcBuckets, c);
    }

    function __mcValidMIME(raw) {
        if (raw === '') { return false; }
        var codecs = __mcCodecs(raw);
        // 桶容器必须恰好一个 codec，非桶容器必须没有 codecs 参数
        if (__mcBucket(__mcContainer(raw))) { return codecs.length === 1; }
        return codecs.length === 0;
    }

    function __mcValidVideoMIME(raw) {
        if (!__mcValidMIME(raw)) { return false; }
        var c = __mcContainer(raw);
        return __mcStartsCI(c, 'video/') || __mcStartsCI(c, 'application/');
    }

    function __mcValidAudioMIME(raw) {
        if (!__mcValidMIME(raw)) { return false; }
        var c = __mcContainer(raw);
        return __mcStartsCI(c, 'audio/') || __mcStartsCI(c, 'application/');
    }

    // ---- WebIDL 转换（文案逐字对齐 WebKit 生成代码）----
    function __mcErr(msg) { return new TypeError(msg); }

    function __mcRequired(iface, member, type) {
        throw __mcErr('Member ' + iface + '.' + member +
                      ' is required and must be an instance of ' + type);
    }

    function __mcToNumber(v) {
        if (typeof v === 'symbol') { throw __mcErr('Cannot convert a symbol to a number'); }
        if (typeof v === 'bigint') {
            throw __mcErr("Conversion from 'BigInt' to 'number' is not allowed.");
        }
        return Number(v);
    }

    function __mcToStr(v) {
        if (typeof v === 'symbol') { throw __mcErr('Cannot convert a symbol to a string'); }
        return String(v);
    }

    function __mcULong(v) {
        var n = __mcToNumber(v);
        if (!isFinite(n) || n === 0) { return 0; }
        n = n < 0 ? -Math.floor(-n) : Math.floor(n);   // trunc
        if (n < 0) { n += 4294967296; }
        return n % 4294967296;
    }

    function __mcULLong(v) {
        var n = __mcToNumber(v);
        if (!isFinite(n) || n === 0) { return 0; }
        n = n < 0 ? -Math.floor(-n) : Math.floor(n);
        var mod = n % 18446744073709551616;
        if (mod < 0) { mod += 18446744073709551616; }
        return mod;
    }

    function __mcDouble(v) {
        var n = __mcToNumber(v);
        if (!isFinite(n)) { throw __mcErr('The provided value is non-finite'); }
        return n;
    }

    function __mcEnum(v, values) {
        var s = __mcToStr(v);
        for (var i = 0; i < values.length; i++) {
            if (values[i] === s) { return s; }
        }
        throw __mcErr('Type error');
    }

    function __mcDictArg(v) {
        if (v === null) { return {}; }
        if (typeof v !== 'object' && typeof v !== 'function') {
            throw __mcErr('Type error');
        }
        return v;
    }

    // 成员按字典序转换（WebKit 生成的字典转换代码按名字排序），required 检查
    // 与转换交错在各自成员的位置上。
    function __mcVideoDict(src) {
        var o = __mcDictArg(src), out = {}, x;
        x = o.alphaChannel;
        if (x !== undefined) { out.alphaChannel = Boolean(x); }
        x = o.bitrate;
        if (x === undefined) { __mcRequired('VideoConfiguration', 'bitrate', 'unsigned long long'); }
        out.bitrate = __mcULLong(x);
        x = o.colorGamut;
        if (x !== undefined) { out.colorGamut = __mcEnum(x, ['srgb', 'p3', 'rec2020']); }
        x = o.contentType;
        if (x === undefined) { __mcRequired('VideoConfiguration', 'contentType', 'DOMString'); }
        out.contentType = __mcToStr(x);
        x = o.framerate;
        if (x === undefined) { __mcRequired('VideoConfiguration', 'framerate', 'double'); }
        out.framerate = __mcDouble(x);
        x = o.hdrMetadataType;
        if (x !== undefined) {
            out.hdrMetadataType = __mcEnum(x,
                ['smpteSt2086', 'smpteSt2094-10', 'smpteSt2094-40']);
        }
        x = o.height;
        if (x === undefined) { __mcRequired('VideoConfiguration', 'height', 'unsigned long'); }
        out.height = __mcULong(x);
        x = o.transferFunction;
        if (x !== undefined) {
            out.transferFunction = __mcEnum(x, ['srgb', 'pq', 'hlg']);
        }
        x = o.width;
        if (x === undefined) { __mcRequired('VideoConfiguration', 'width', 'unsigned long'); }
        out.width = __mcULong(x);
        return out;
    }

    function __mcAudioDict(src) {
        var o = __mcDictArg(src), out = {}, x;
        x = o.bitrate;
        if (x !== undefined) { out.bitrate = __mcULLong(x); }
        x = o.channels;
        if (x !== undefined) { out.channels = __mcToStr(x); }
        x = o.contentType;
        if (x === undefined) { __mcRequired('AudioConfiguration', 'contentType', 'DOMString'); }
        out.contentType = __mcToStr(x);
        x = o.samplerate;
        if (x !== undefined) { out.samplerate = __mcULong(x); }
        x = o.spatialRendering;
        if (x !== undefined) { out.spatialRendering = Boolean(x); }
        return out;
    }

    function __mcNormalize(kind, arg) {
        var cfg = arg;
        if (cfg === null || cfg === undefined) { cfg = {}; }
        else if (typeof cfg !== 'object' && typeof cfg !== 'function') {
            throw __mcErr('Type error');
        }
        var audio, video;
        var a = cfg.audio;
        if (a !== undefined) { audio = __mcAudioDict(a); }
        var v = cfg.video;
        if (v !== undefined) { video = __mcVideoDict(v); }
        var t = cfg.type;
        var iface = (kind === 'dec') ? 'MediaDecodingConfiguration'
                                     : 'MediaEncodingConfiguration';
        var typeName = (kind === 'dec') ? 'MediaDecodingType' : 'MediaEncodingType';
        if (t === undefined) { __mcRequired(iface, 'type', typeName); }
        var type = __mcEnum(t, (kind === 'dec')
            ? ['file', 'media-source', 'webrtc'] : ['record', 'webrtc']);
        if (!video && !audio) { throw __mcErr('Type error'); }
        if (video && !__mcValidVideoMIME(video.contentType)) { throw __mcErr('Type error'); }
        if (video && !(isFinite(video.framerate) && video.framerate > 0)) {
            throw __mcErr('Type error');
        }
        if (audio && !__mcValidAudioMIME(audio.contentType)) { throw __mcErr('Type error'); }
        return { type: type, audio: audio, video: video };
    }

    // ---- 结果对象（键序 = WebKit 的 IDL 字典序）----
    function __mcInfo(supported, smooth, powerEfficient) {
        return {
            supported: !!supported, smooth: !!smooth,
            powerEfficient: !!powerEfficient
        };
    }

    function __mcZero() { return __mcInfo(false, false, false); }

    function __mcConfig(type, audio, video) {
        var c = {};
        if (audio) { c.audio = audio; }
        if (video) { c.video = video; }
        c.type = type;
        return c;
    }

    function __mcResult(info, configuration) {
        return {
            powerEfficient: !!info.powerEfficient,
            smooth: !!info.smooth,
            supported: !!info.supported,
            configuration: configuration
        };
    }

    function __mcHdr(cfg) {
        return !!(cfg.colorGamut || cfg.hdrMetadataType || cfg.transferFunction);
    }

    // VP9UtilitiesCocoa::resolutionCategory 的像素阈值
    function __mcResolution(w, h) {
        var px = w * h;
        if (px > 7680 * 4320) { return 6; }
        if (px > 4096 * 2160) { return 5; }
        if (px > 2048 * 1080) { return 4; }
        if (px > 1920 * 1080) { return 3; }
        if (px > 1280 * 720) { return 2; }
        if (px > 640 * 480) { return 1; }
        return 0;
    }

    // isVPSoftwareDecoderSmooth
    function __mcVpSmooth(cfg) {
        if (cfg.height <= 1080 && cfg.framerate > 60) { return false; }
        if (cfg.height <= 2160 && cfg.framerate > 30) { return false; }
        return true;
    }

    function __mcH264Info(cfg) {
        if (cfg.alphaChannel || __mcHdr(cfg)) { return null; }
        return __mcInfo(true, true, true);
    }

    function __mcMpeg4Info(cfg) {
        if (cfg.alphaChannel || __mcHdr(cfg)) { return null; }
        return __mcInfo(true, true, false);
    }

    function __mcVp8Info(cfg) {
        if (cfg.alphaChannel || __mcHdr(cfg)) { return null; }
        return __mcInfo(true, __mcVpSmooth(cfg), false);
    }

    // 机型能力位已由调用方门控：能走到这里说明该机型有 AV1 硬解
    function __mcAv1Info() { return __mcInfo(true, true, true); }

    function __mcHevcParts(codec) {
        var p = codec.split('.');
        if (p.length < 4) { return null; }
        var a = p[1], b = p[2], c = p[3];
        var space = 0, idc;
        var ch = a.charAt(0);
        if (ch === 'A' || ch === 'B' || ch === 'C') {
            space = ch.charCodeAt(0) - 64;
            idc = parseInt(a.slice(1), 10);
        } else {
            idc = parseInt(a, 10);
        }
        var compat = parseInt(b, 16);
        var lv = c.charAt(0) === 'L' ? c.slice(1) : c;
        var level = parseInt(lv, 10);
        if (!isFinite(idc) || !isFinite(level)) { return null; }
        if (!isFinite(compat)) { compat = 0; }
        return { space: space, idc: idc, compat: compat, level: level };
    }

    function __mcHevcInfo(cfg, codec) {
        var p = __mcHevcParts(codec);
        if (!p) { return null; }
        if (__mcHdr(cfg)) {
            // 平台只支持 HEVC Main10 的 HDR 播放
            if (!(p.space === 0 && (p.idc === 2 || p.compat === 1))) { return null; }
        }
        // 设备 maxDecodeLevel（基线实测 L186 通过、L200 拒绝）
        if (p.level > 186) { return null; }
        return __mcInfo(true, p.level <= 186, true);
    }

    function __mcDoviInfo(cfg, codec) {
        var p = codec.split('.');
        if (p.length < 3) { return null; }
        var profile = parseInt(p[1], 10);
        var level = parseInt(p[2], 10);
        if (!isFinite(profile) || !isFinite(level)) { return null; }
        if (cfg.alphaChannel) { return null; }
        if (__mcHdr(cfg) && !(profile === 4 || profile === 5
                             || profile === 7 || profile === 8)) { return null; }
        // 设备 supportedProfiles：基线实测 5 / 8
        if (profile !== 5 && profile !== 8) { return null; }
        if (level > 6) { return null; }
        return __mcInfo(true, true, true);
    }

    function __mcVp9Parts(codec) {
        var p = codec.split('.');
        if (p.length < 4) { return null; }
        var out = {
            profile: parseInt(p[1], 10),
            level: parseInt(p[2], 10),
            bitDepth: parseInt(p[3], 10),
            chroma: p.length > 4 ? parseInt(p[4], 10) : 0,
            cp: p.length > 5 ? parseInt(p[5], 10) : 0,
            tc: p.length > 6 ? parseInt(p[6], 10) : 0
        };
        if (!isFinite(out.profile) || !isFinite(out.level)
                || !isFinite(out.bitDepth) || !isFinite(out.chroma)) { return null; }
        if (!isFinite(out.cp)) { out.cp = 0; }
        if (!isFinite(out.tc)) { out.tc = 0; }
        return out;
    }

    function __mcVp9Info(cfg, codec, hw) {
        var p = __mcVp9Parts(codec);
        if (!p) { return null; }
        if (p.profile && p.profile !== 2) { return null; }
        if (p.chroma > 0) { return null; }
        if (p.bitDepth > 10) { return null; }
        if (p.level > 60) { return null; }              // Level_6
        if (!hw) { return null; }
        if (cfg.transferFunction) {
            if (cfg.transferFunction === 'srgb' && p.tc > 15) { return null; }
            if (cfg.transferFunction === 'pq' && p.tc !== 16) { return null; }
            if (cfg.transferFunction === 'hlg' && p.tc !== 18) { return null; }
        }
        if (cfg.colorGamut === 'rec2020' && p.cp !== 9) { return null; }
        if (cfg.alphaChannel) { return null; }
        var res = __mcResolution(cfg.width, cfg.height);
        if (res > 5) { return null; }
        var smooth;
        if (res === 5 && cfg.framerate > 30) { smooth = false; }
        else if (res <= 4 && cfg.framerate > 120) { smooth = false; }
        else { smooth = true; }
        return __mcInfo(true, smooth, true);
    }

    function __mcCocoaVideoInfo(video, type, opt) {
        var raw = video.contentType;
        var container = __mcContainer(raw);
        var codecs = __mcCodecs(raw);
        if (codecs.length !== 1) { return null; }
        var codec = codecs[0];
        var isFile = (type === 'file');
        var mp4 = (container === 'video/mp4' || container === 'application/mp4'
                || container === 'video/quicktime' || container === 'video/3gpp'
                || container === 'video/3gpp2');
        // webm 走 MSE 自己的解析器，不受 AVFoundation 的参数顺序限制
        var gate = null;
        if (isFile && container !== 'video/webm') {
            gate = __mcPreGate(container, raw, false, opt);
            if (gate === false) { return null; }
        } else if (isFile && __mcSemiInQuotedCodec(raw)) {
            // webm 的 codec 串里再出现分号会被 MSE 的解析器判死，mp4 家族不会
            return null;
        }
        if (mp4) {
            // AVFoundation 的可播放面只认 ISO 品牌写法（avc1/hvc1/dvh1），
            // 带内参数集写法（avc3/hev1/dvhe）只有 MSE 的解析器收；前置参数
            // 已经过闸门的，闸门本身就是这条口径，不再重复判
            if (isFile && gate !== 'pass' && (__mcStarts(codec, 'avc3')
                || __mcStarts(codec, 'hev1') || __mcStarts(codec, 'dvhe'))) {
                return null;
            }
            if (__mcStarts(codec, 'hvc1') || __mcStarts(codec, 'hev1')) {
                return __mcHevcInfo(video, codec);
            }
            if (__mcStarts(codec, 'dvh1') || __mcStarts(codec, 'dvhe')) {
                // 闸门通过时 codec 串已经由 AVFoundation 认过，不再套 profile 表
                if (gate === 'pass') { return __mcInfo(true, true, true); }
                return __mcDoviInfo(video, codec);
            }
            if (__mcStarts(codec, 'vp09')) { return __mcVp9Info(video, codec, true); }
            if (__mcStarts(codec, 'vp8') || __mcStarts(codec, 'vp08')) {
                // VP8 在 mp4 家族里按软件解码放行，但只限闸门已经认过这条 MIME
                return gate === 'pass' ? __mcVp8Info(video) : null;
            }
            if (__mcStarts(codec, 'av01')) { return opt.av1 ? __mcAv1Info() : null; }
            if (__mcStarts(codec, 'avc1') || __mcStarts(codec, 'avc3')) {
                return __mcH264Info(video);
            }
            if (__mcStarts(codec, 'mp4v')) { return __mcMpeg4Info(video); }
            return null;
        }
        if (container === 'video/webm') {
            if (__mcStarts(codec, 'vp09')) { return __mcVp9Info(video, codec, true); }
            if (__mcStarts(codec, 'vp8') || __mcStarts(codec, 'vp08')) {
                return __mcVp8Info(video);
            }
            if (__mcStarts(codec, 'av01')) { return opt.av1 ? __mcAv1Info() : null; }
            return null;
        }
        return null;
    }

    function __mcMp4AudioCodec(codec) {
        if (codec === 'mp4a.40.2' || codec === 'mp4a.40.5') { return true; }
        return codec === 'Opus' || codec === 'fLaC' || codec === 'alac'
            || codec === 'ac-3' || codec === 'ec-3'
            || codec === 'samr' || codec === 'ulaw';
    }

    function __mcChannelCount(channels) {
        if (typeof channels !== 'string') { return 0; }
        var m = /^[0-9]+/.exec(__mcTrim(channels));
        return m ? parseInt(m[0], 10) : 0;
    }

    function __mcCocoaAudioInfo(audio, type) {
        var raw = audio.contentType;
        var container = __mcContainer(raw);
        var codecs = __mcCodecs(raw);
        if (codecs.length !== 1) { return null; }
        var codec = codecs[0];
        // 容器白名单在 canDecodeType 里先于扩展类型判定：MP21 与 HLS 清单在
        // 音频侧连闸门都走不到
        if (container === 'application/mp21'
                || container === 'audio/vnd.apple.mpegurl') { return null; }
        if (type === 'file' && __mcSemiInQuotedCodec(raw)) { return null; }
        if (type === 'file' && container !== 'audio/webm') {
            var gate = __mcPreGate(container, raw, true);
            if (gate === false) { return null; }
            // 过闸门等于 AVFoundation 认了这条 MIME，音频分支只回 supported
            if (gate === 'pass') { return __mcAudioFlags(audio, true); }
        }
        // 不带引号的 codecs 走的是同一套扩展类型判定，只是 AVFoundation 侧
        // 按未加引号的更严口径校验过一遍
        var ok = false;
        if (container === 'audio/mp4') {
            // 小写 flac 只在文件路径上被 AVFoundation 改写成 fLaC
            ok = __mcMp4AudioCodec(codec) || (codec === 'flac' && type === 'file');
        } else if (container === 'application/mp4' || container === 'audio/3gpp'
                || container === 'audio/3gpp2') {
            ok = __mcMp4AudioCodec(codec);
        } else if (container === 'audio/ogg') {
            ok = (codec === 'opus' || codec === 'vorbis' || codec === 'flac');
        } else if (container === 'audio/webm') {
            ok = (codec === 'opus' || codec === 'vorbis');
        }
        if (!ok) { return null; }
        return __mcAudioFlags(audio, true);
    }

    function __mcAudioFlags(audio, supported) {
        if (audio.spatialRendering) {
            supported = __mcChannelCount(audio.channels) >= 3;
        }
        // Cocoa 的音频分支只置 supported，smooth/powerEfficient 恒 false
        return __mcInfo(supported, false, false);
    }

    function __mcCocoaDecode(norm, opt) {
        var res = null;
        if (norm.video) {
            res = __mcCocoaVideoInfo(norm.video, norm.type, opt);
            if (!res) { return null; }
        }
        if (!norm.audio) { return res; }
        var a = __mcCocoaAudioInfo(norm.audio, norm.type);
        if (!a) { return null; }
        if (!res) { return a; }
        res.supported = a.supported;    // 音频分支只覆盖 supported
        return res;
    }

    // ---- WebRTC（LibWebRTCProvider 的 RTP 能力面）----
    // 收/发能力表不同源：GetRtpSenderCapabilities 里没有 flexfec-03（FEC 只收
    // 不发），拿接收表当发送表会让 encodingInfo 把它判成 supported。
    var __mcRtpVideoKinds = {
        'video/vp8': 1, 'video/vp9': 1, 'video/h264': 1, 'video/h265': 1,
        'video/av1': 1, 'video/rtx': 1, 'video/red': 1,
        'video/ulpfec': 1, 'video/flexfec-03': 1
    };

    var __mcRtpSendVideoKinds = {
        'video/vp8': 1, 'video/vp9': 1, 'video/h264': 1, 'video/h265': 1,
        'video/av1': 1, 'video/rtx': 1, 'video/red': 1, 'video/ulpfec': 1
    };

    var __mcRtpAudioKinds = {
        'audio/opus': 1, 'audio/pcmu': 1, 'audio/pcma': 1,
        'audio/telephone-event': 1, 'audio/cn': 1, 'audio/red': 1,
        'audio/g722': 1
    };

    function __mcRtpKey(raw) { return __mcLower(__mcContainer(raw)); }

    function __mcRtpVideoInfo(video, opt) {
        var k = __mcRtpKey(video.contentType);
        if (!Object.prototype.hasOwnProperty.call(__mcRtpVideoKinds, k)) { return null; }
        if (k === 'video/vp8') { return __mcInfo(true, __mcVpSmooth(video), false); }
        if (k === 'video/vp9') {
            // iOS 的 WebRTC VP9 硬件解码位恒 false，smooth 走软件表
            return __mcInfo(true, __mcVpSmooth(video), false);
        }
        if (k === 'video/h264' || k === 'video/h265') {
            return __mcInfo(true, true, true);
        }
        if (k === 'video/av1') {
            return opt.av1 ? __mcInfo(true, true, true) : __mcInfo(true, false, false);
        }
        return __mcInfo(true, false, false);
    }

    function __mcRtpAudioOk(audio) {
        return Object.prototype.hasOwnProperty.call(
            __mcRtpAudioKinds, __mcRtpKey(audio.contentType));
    }

    // WebRTCProvider::contentTypeFromRTPVideoMimeType 的等价映射
    function __mcRtpToContentType(raw) {
        var k = __mcRtpKey(raw);
        if (k === 'video/h264') {
            var plid = __mcParam(raw, 'profile-level-id');
            if (plid === '') { plid = '42001f'; }
            return 'video/mp4;codecs=avc1.' + plid;
        }
        if (k === 'video/h265') { return 'video/mp4;codecs=hvc1.1.6.L93.B0'; }
        if (k === 'video/vp9') { return 'video/mp4;codecs=vp09.00.05.08'; }
        if (k === 'video/av1') { return 'video/mp4;codecs=av01.0.03M.08'; }
        if (k === 'video/vp8') { return 'video/webm;codecs=vp08.00.50.08'; }
        return '';
    }

    function __mcDecoding(arg, opt) {
        var norm = __mcNormalize('dec', arg);
        var conf = __mcConfig(norm.type, norm.audio, norm.video);
        if (norm.type === 'webrtc') {
            var vinfo = null;
            if (norm.video) {
                vinfo = __mcRtpVideoInfo(norm.video, opt);
                if (!vinfo) { return __mcResult(__mcZero(), conf); }
            }
            if (norm.audio && !__mcRtpAudioOk(norm.audio)) {
                return __mcResult(__mcZero(), conf);
            }
            var info = vinfo || __mcInfo(true, false, false);
            info.supported = true;
            if (info.powerEfficient && norm.video) {
                // 硬解路径回落到媒体引擎（media-source）再算一次
                var v = norm.video;
                var re = {
                    contentType: __mcRtpToContentType(v.contentType),
                    width: v.width, height: v.height,
                    bitrate: v.bitrate, framerate: v.framerate
                };
                if (v.alphaChannel !== undefined) { re.alphaChannel = v.alphaChannel; }
                if (v.colorGamut !== undefined) { re.colorGamut = v.colorGamut; }
                if (v.hdrMetadataType !== undefined) { re.hdrMetadataType = v.hdrMetadataType; }
                if (v.transferFunction !== undefined) { re.transferFunction = v.transferFunction; }
                var r = re.contentType ? __mcCocoaVideoInfo(re, 'media-source', opt) : null;
                if (!r) { return __mcResult(__mcZero(), { type: 'file' }); }
                info = r;
            }
            return __mcResult(info, conf);
        }
        return __mcResult(__mcCocoaDecode(norm, opt) || __mcZero(), conf);
    }

    function __mcRtpEncodeVideoInfo(video) {
        var k = __mcRtpKey(video.contentType);
        if (!Object.prototype.hasOwnProperty.call(__mcRtpSendVideoKinds, k)) { return null; }
        if (k === 'video/h264' || k === 'video/h265') {
            return __mcInfo(true, true, true);
        }
        return __mcInfo(true, false, false);
    }

    function __mcEncoding(arg) {
        var norm = __mcNormalize('enc', arg);
        if (norm.type === 'record') {
            // Cocoa 没有编码 factory：全 false，configuration 只剩 {type:'record'}
            return __mcResult(__mcZero(), { type: 'record' });
        }
        var conf = __mcConfig(norm.type, norm.audio, norm.video);
        var vinfo = null;
        if (norm.video) {
            vinfo = __mcRtpEncodeVideoInfo(norm.video);
            if (!vinfo) { return __mcResult(__mcZero(), conf); }
        }
        if (norm.audio && !__mcRtpAudioOk(norm.audio)) {
            return __mcResult(__mcZero(), conf);
        }
        var info = vinfo || __mcInfo(true, false, false);
        info.supported = true;
        return __mcResult(info, conf);
    }

    function __mcRun(name, self, args, kind, inst, opt) {
        try {
            // 品牌校验先于参数计数，且一律是异步 rejection（真机实测）
            if (self !== inst) {
                return Promise.reject(__mcErr(
                    'Can only call MediaCapabilities.' + name +
                    ' on instances of MediaCapabilities'));
            }
            if (!args.length) {
                return Promise.reject(__mcErr('Not enough arguments'));
            }
            return Promise.resolve((kind === 'dec')
                ? __mcDecoding(args[0], opt) : __mcEncoding(args[0]));
        } catch (e) {
            return Promise.reject(e);
        }
    }

    return {
        dec: function (self, args, inst, opt) {
            return __mcRun('decodingInfo', self, args, 'dec', inst, opt);
        },
        enc: function (self, args, inst, opt) {
            return __mcRun('encodingInfo', self, args, 'enc', inst, opt);
        }
    };
})()
"""


# F-163：字体面收口。宿主 macOS 的本地字体经 FontFace 的 local() 源泄漏：
# `new FontFace(n, 'local("<n>")').load()` 对 Helvetica Neue / Luminari /
# Galvji / InaiMathi Bold / PingFang HK Light 全部解析成功，creepjs 的
# isFontOSBad 见 Android UA 却拿到 Apple 字体即记 bold（LowerEntropy.FONTS）。
# `document.fonts.check()` 不参与判定——宿主对任意家族都返回 true，creepjs
# 的 gate（`!check(随机名)`）为假，整条路径被跳过。
#
# 做法是在构造期改写 src：按顶层逗号切分，丢掉家族不在设备声明字体表里的
# local() 片段，其余（url()/format() 等）原样保留；没有 local() 的源整条
# 原样返回，保住空源的 SyntaxError。被丢掉的片段交给原生 load() 拒绝，
# 错误形状（NetworkError / code 19）天然正确，实例仍是纯原生对象——
# 0 个自有属性、load 与 family 都是原生实现，creepjs 的
# searchLies(FontFace, ['family','load','status']) 因此读不到任何改写痕迹。
#
# 构造器面同样按原生复刻：new.target 透传给 Reflect.construct（子类实例
# 原型链正确）、无 new 时借用原生报错文案、prototype 复用原生对象并置
# 不可写、length 2、原型上的 constructor 指回包装器。
_FONT_FACE_SRC = r"""var __ffWrap = (function () {
    try {
        var RealFF = (typeof self !== 'undefined' && self.FontFace)
            || (typeof FontFace !== 'undefined' ? FontFace : null);
        if (typeof RealFF !== 'function') { return null; }
        var allow = {};
        var list = (D && D.fonts) || [];
        for (var i = 0; i < list.length; i++) {
            allow[String(list[i]).toLowerCase()] = 1;
        }

        // 顶层逗号切分：括号内与引号内的逗号不算分隔（url(data:...;base64,..)）
        var tok = function (s) {
            var parts = [], cur = '', depth = 0, q = null;
            for (var j = 0; j < s.length; j++) {
                var ch = s.charAt(j);
                if (q) {
                    cur += ch;
                    if (ch === '\\') { j++; if (j < s.length) { cur += s.charAt(j); } }
                    else if (ch === q) { q = null; }
                    continue;
                }
                if (ch === '"' || ch === "'") { q = ch; cur += ch; continue; }
                if (ch === '(') { depth++; cur += ch; continue; }
                if (ch === ')') { if (depth > 0) { depth--; } cur += ch; continue; }
                if (ch === ',' && depth === 0) { parts.push(cur); cur = ''; continue; }
                cur += ch;
            }
            parts.push(cur);
            return parts;
        };

        // 整段必须恰好是一个 local()：`local("X") format("woff2")` 原生就是
        // 语法错误，不能按前缀匹配当成 local 处理
        var famOf = function (part) {
            var m = /^\s*local\s*\(\s*(?:"([^"]*)"|'([^']*)'|([^)]*?))\s*\)\s*$/i.exec(part);
            if (!m) { return null; }
            var f = (m[1] !== undefined ? m[1]
                : (m[2] !== undefined ? m[2] : m[3]));
            return f.replace(/^\s+|\s+$/g, '');
        };

        var fix = function (src) {
            // ArrayBuffer / TypedArray 源合法，String() 会毁掉它们
            if (typeof src !== 'string') { return src; }
            if (src.indexOf('local(') < 0) { return src; }
            var parts = tok(src), keep = [], dropped = 0;
            for (var k = 0; k < parts.length; k++) {
                var fam = famOf(parts[k]);
                if (fam === null) { keep.push(parts[k]); continue; }
                // CSS 家族名忽略大小写，子族名（"Helvetica Neue Bold"）按
                // 完整串比较——声明表里没有就丢
                if (allow[fam.toLowerCase()]) { keep.push(parts[k]); }
                else { dropped++; }
            }
            if (!dropped) { return src; }
            var out = keep.join(',');
            out = out.replace(/^\s+|\s+$/g, '');
            return out || 'local("__fp_absent__")';
        };

        var W = function FontFace(family, source, descriptors) {
            var nt = new.target;
            if (nt === undefined) {
                // 原生在参数个数检查之前就拒绝无 new 调用，直接借它的文案
                return RealFF.apply(this, arguments);
            }
            if (arguments.length < 2) {
                return Reflect.construct(RealFF,
                    arguments.length === 1 ? [family] : [], nt);
            }
            var args = arguments.length < 3
                ? [family, fix(source)]
                : [family, fix(source), descriptors];
            return Reflect.construct(RealFF, args, nt);
        };
        W.prototype = RealFF.prototype;
        try {
            Object.defineProperty(W, 'prototype', {
                value: RealFF.prototype, writable: false,
                enumerable: false, configurable: false,
            });
        } catch (e) {}
        try {
            Object.defineProperty(W, 'length', {
                value: 2, writable: false, enumerable: false, configurable: true,
            });
        } catch (e) {}
        try {
            Object.defineProperty(RealFF.prototype, 'constructor', {
                value: W, writable: true, enumerable: false, configurable: true,
            });
        } catch (e) {}
        return W;
    } catch (e) {
        return null;
    }
})();
"""

_PATCH_TEMPLATE = r"""
(function () {
    var D;
    try { D = __SPEC__; } catch (e) { return; }

    // F-142：MSE 独立真值表（源码见模块级 _MSE_SUPPORT_SRC）
    var __mseSupports = (__MSE_SRC__);
    // F-143：canPlayType 兜底按 WebKit 容器白名单自行判定（见 _CPT_IOS_SRC）
    var __cptIos = (__CPT_SRC__);
    // F-132：WebCodecs isConfigSupported 口径（见模块级 _WEBCODECS_ICS_SRC）
__WC_SRC__
    // F-135：mediaCapabilities 收口（见模块级 _MC_SRC）
__MC_SRC__
    // F-163：FontFace 的 local() 源裁剪（见模块级 _FONT_FACE_SRC）
__FF_SRC__

    // ---- 原生函数伪装：toString() 一律返回 [native code] 外观 ----
    var __maskNative = (function () {
        try {
            var origToString = Function.prototype.toString;
            if (typeof WeakMap === 'undefined') { return function (fn) { return fn; }; }
            var registry = new WeakMap();
            // 顶层 realm 的 toString，用于跨 realm 查询（见下方 apply）。
            var __topFts = null;
            var __topFtsTried = false;
            var faked = new Proxy(origToString, {
                apply: function (target, self, args) {
                    var v = registry.get(self);
                    if (v !== undefined) { return v; }
                    // creepjs 的 `queryLies` 用 phantom iframe（二级）的
                    // Function#toString 去读主 realm 的函数。两个 realm 各有
                    // 一份 toString 和一份 WeakMap，本 realm 查不到就吐载体
                    // 自身的真源码（方法简写文本），记 lie。故这里回退到顶层
                    // realm 的注册表；顶层对陌生对象也只会落到原生实现。
                    if (!__topFtsTried) {
                        __topFtsTried = true;
                        try {
                            var tw = window.top;
                            if (tw && tw !== window && tw.Function
                                    && tw.Function.prototype) {
                                __topFts = tw.Function.prototype.toString;
                            }
                        } catch (e5) {}
                    }
                    if (__topFts && __topFts !== faked) {
                        try { return __topFts.call(self); } catch (e6) {}
                    }
                    return __nativeToString(target, self);
                },
            });
            var __nativeToStringTail = function () {
                var s = origToString.call(this);
                // JSC 的原生函数是 `function f() {\n    [native code]\n}`（多行缩进），
                // V8 是单行 `function f() { [native code] }`。未登记（未被本补丁
                // 包装）的原生函数直接透传会暴露 V8 格式——检测站对
                // `Function.prototype.toString.call(eval)` 一行判定引擎，故 iOS
                // 目标上统一改写为 JSC 形态。
                if (D && D.ios && typeof s === 'string'
                        && s.indexOf('[native code]') !== -1
                        && s.indexOf('\n') === -1) {
                    var m = /^function\s*([^(]*)\(/.exec(s);
                    return 'function ' + __jscName(m ? m[1].trim() : '')
                        + '() {\n    [native code]\n}';
                }
                return s;
            };
            // JSC 的访问器源码不带 get/set 前缀（`function volume()`），
            // 与 V8 的 `function get volume()` 不同；Function#name 两侧都是
            // 'get volume'，只有 toString 分叉（/tmp/f401）。
            var __jscName = function (n) {
                return String(n).replace(/^(?:get|set)\s+/, '');
            };
            // 返回的载体必须**自身**在任何 realm 下都读作原生函数。仅靠上面的
            // 注册表做不到：creepjs 的 `queryLies` 用 phantom iframe（二级）
            // 的 `Function.prototype.toString` 去读主 realm 的函数，两个
            // realm 各有一份 toString 与各一份 WeakMap，查不到就吐真源码，
            // 连带暴露 JS 函数自带的 prototype/arguments/caller——实测
            // Navigator 10 个成员、Screen、WebGL 全部因此记 lie。
            // 故这里不再返回原函数，而是返回一个**方法简写**包装：简写方法
            // 天然无 prototype、不可构造，own 属性恰是 length,name，任何
            // realm 读源码都命中 [native code]（实测 lied:0）。构造器类
            // 属性走 asCtor 分支保持原函数，因为 `new`/`prototype` 必须能用
            // 而 creepjs 并不搜索构造器对象本身。
            var mask = function (fn, name, src, asCtor) {
                try {
                    if (typeof fn === 'function') {
                        var s = src || (D && D.ios
                            ? 'function ' + __jscName(name) + '() {\n    [native code]\n}'
                            : 'function ' + name + '() { [native code] }');
                        registry.set(fn, s);
                        try { Object.defineProperty(fn, 'name', { value: name, configurable: true }); } catch (e2) {}
                        if (asCtor) { return fn; }
                        var w = ({ m() { return fn.apply(this, arguments); } }).m;
                        registry.set(w, s);
                        try { Object.defineProperty(w, 'name', { value: name, configurable: true }); } catch (e3) {}
                        try {
                            Object.defineProperty(w, 'length', {
                                value: fn.length, configurable: true,
                            });
                        } catch (e4) {}
                        return w;
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
    // 原生方法同样校验接收者（Navigator.prototype.getBattery.call({}) 亦抛
    // Illegal invocation），故对「接口原型」上的方法做同样的检查；proto 本身
    // 不是原型对象时（挂在实例上的方法，如空 plugins 的 item）不做检查，
    // 避免误伤。
    var __isIfaceProto = function (proto) {
        try {
            var c = proto.constructor;
            return typeof c === 'function' && c.prototype === proto;
        } catch (e) { return false; }
    };
    var defineFn = function (proto, name, fn, noBrand) {
        try {
            var impl = fn;
            if (!noBrand && __isIfaceProto(proto)) {
                impl = function () {
                    if (!__slotHas(proto, this)) {
                        throw new TypeError('Illegal invocation');
                    }
                    return fn.apply(this, arguments);
                };
                // 包装后 length 会变成 0，原生方法的 length 是方法签名的一部分
                // （检测站点会读 postMessage.length 等），原样保留
                try {
                    Object.defineProperty(impl, 'length', {
                        value: fn.length, configurable: true,
                    });
                } catch (e) {}
            }
            Object.defineProperty(proto, name, {
                value: __maskNative(impl, name),
                writable: true, enumerable: true, configurable: true,
            });
            return true;
        } catch (e) { return false; }
    };
    // 静态方法包装（构造函数上的属性，没有接收者品牌校验面）：
    // length 原样保留，toString 走 __maskNative。
    var __staticW = function (Ctor, name, impl, len) {
        try {
            var fn = function () { return impl(this, arguments); };
            Object.defineProperty(fn, 'length', {
                value: len || 0, configurable: true,
            });
            Object.defineProperty(Ctor, name, {
                value: __maskNative(fn, name),
                writable: true, enumerable: true, configurable: true,
            });
        } catch (e) {}
    };
    // 与 defineFn 相同，但自定义 toString() 返回的源码文本：真机 Chrome iOS
    // 改写的 history/print/scrollTo 是非原生外观（源码含 invokeOnHost /
    // setWebViewScrollViewIsDragging 字样），不能伪装成 [native code]。
    var defineFnSrc = function (proto, name, fn, src) {
        try {
            var impl = fn;
            if (__isIfaceProto(proto)) {
                impl = function () {
                    if (!__slotHas(proto, this)) {
                        throw new TypeError('Illegal invocation');
                    }
                    return fn.apply(this, arguments);
                };
            }
            Object.defineProperty(proto, name, {
                value: __maskNative(impl, name, src),
                writable: true, enumerable: true, configurable: true,
            });
            return true;
        } catch (e) { return false; }
    };
    // window 上的 [Replaceable] 属性（devicePixelRatio/outerWidth 等）：
    // 原生是「get/set 均存在、赋值时自我替换为数据属性」，保持同样形态。
    // 原生访问器同样校验接收者，故这里也走 __slotHas。
    var replaceable = function (obj, name, value) {
        if (!obj) { return false; }
        var brand;
        try { brand = Object.getPrototypeOf(obj); } catch (e) { brand = null; }
        try {
            var getter = __maskNative(function () {
                if (!__slotHas(brand, this)) { throw new TypeError('Illegal invocation'); }
                return value;
            }, 'get ' + name);
            var setter = __maskNative(function (v) {
                if (!__slotHas(brand, this)) { throw new TypeError('Illegal invocation'); }
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

        // F-98：computed 声明 → 所属元素。CSSOM 补丁里按 inline 存储键查值，
        // computed 声明是另一个对象，靠这张表回落到元素 inline 样式。
        var __csOwner = new WeakMap();
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
            // F-87：真机 WebKit 的 getGamepads() 无手柄时返回空数组
            // （Playwright WebKit 27.2 实测：[]、Array.isArray true、
            // length 0、原型即 Array.prototype、方法本体 w/e/c 全 true、
            // length 0）；Blink 返回固定 4 槽位 [null,null,null,null]。
            // 只改 iOS：Android 真机 Chrome 是同一套 Blink 固定槽位，
            // 不随之改成空数组（那是把真机行为改错）。
            defineFn(N, 'getGamepads', function () { return []; });
            // 真机 iOS（Safari / Chrome）普通页面没有 window.chrome；桌面内核
            // 必然携带该对象且属性 configurable:false（删除/重定义均不可行）。
            // 赋值 undefined 后 typeof/truthiness 检查通过；'chrome' in window
            // 与 getOwnPropertyDescriptor 的残余暴露属内核层限制（已知边界）。
            try { window.chrome = undefined; } catch (e) {}

            // ---- JavaScriptCore 引擎面 ----
            // 检测站对「声明 iOS 但引擎是 V8」有四条独立判据，逐条对齐 WebKit：
            //   a) 调用栈格式：JSC 为 `fn@url:line:col`，V8 为 `    at fn (...)`。
            //   b) Error 实例的 line/column own 属性：JSC 恒有（ErrorInstance
            //      的物理属性），V8 没有。
            //   c) 原生函数 toString：JSC 多行
            //      `function f() {\n    [native code]\n}`，V8 单行。
            //   d) Error.prepareStackTrace：JSC 无该钩子（V8 专有）。
            // 取证：Playwright WebKit 27.2 实测 + WebKit main
            // Source/JavaScriptCore/runtime/{ErrorConstructor,ErrorPrototype}.cpp。
            //
            // 注意 Error.captureStackTrace 不是 V8 专有：WebKit main 的
            // ErrorConstructor.cpp:52 注册了同名静态方法（检测站该项文案已过时，
            // 与 SharedWorker 同类），故不抹除。
            //
            // 只有「显式构造」的 Error 能拿到 line/column：V8 引擎内部抛出的
            // TypeError/SyntaxError 在 C++ 侧直接创建，不经过 Error 全局的
            // construct/apply 陷阱。这类异常的 message 文本仍是 V8 口径
            // （"Cannot read properties of null"），属引擎层残余，见文档。
            try {
                if (typeof Error === 'function' && typeof WeakMap === 'function') {
                    var __jscLoc = new WeakMap();
                    var __parseV8Frame = function (text) {
                        // "    at fn (url:line:col)" / "    at url:line:col"
                        var m = /(?:\(|at )([^\s()]+):(\d+):(\d+)\)?\s*$/.exec(text);
                        if (!m) { return null; }
                        return { file: m[1], line: parseInt(m[2], 10), col: parseInt(m[3], 10) };
                    };
                    var __frameName = function (text) {
                        var t = text.replace(/^\s*at\s+/, '');
                        var p = t.indexOf(' (');
                        if (p > 0) { return t.slice(0, p); }
                        var m = /^(.*?):\d+:\d+$/.exec(t);
                        return m ? '' : t;
                    };
                    var __jscStack = function (raw) {
                        var lines = String(raw).split('\n');
                        var out = [];
                        // 首行是 "Error: msg"（无调用点）——JSC 从调用帧开始
                        for (var i = 1; i < lines.length; i++) {
                            if (!/^\s*at\s+/.test(lines[i])) { continue; }
                            var nm = __frameName(lines[i]);
                            // Proxy 陷阱帧：`Object.construct@<anonymous>:294:56`
                            // 之类的合成帧恒指向本补丁自身，真机不存在，且会让
                            // 每个 Error 的 line/column 变成同一个常量——直接丢弃。
                            if (/^Object\.(construct|apply)$/.test(nm)
                                    && /<anonymous>/.test(lines[i])) { continue; }
                            var loc = __parseV8Frame(lines[i]);
                            out.push(nm + '@' + (loc ? loc.file + ':' + loc.line + ':' + loc.col : ''));
                        }
                        return out.join('\n');
                    };
                    var __jscAttach = function (err) {
                        try {
                            var raw;
                            var getter = Object.getOwnPropertyDescriptor(Error.prototype, 'stack');
                            raw = getter && getter.get ? getter.get.call(err) : err.stack;
                            if (typeof raw !== 'string' || !/^\s*at\s+/m.test(raw)) { return err; }
                            var loc = null;
                            var __re = /^\s*at\s+.*$/gm, __mm;
                            while ((__mm = __re.exec(raw)) !== null) {
                                var __nm = __frameName(__mm[0]);
                                if (/^Object\.(construct|apply)$/.test(__nm)
                                        && /<anonymous>/.test(__mm[0])) { continue; }
                                loc = __parseV8Frame(__mm[0]);
                                break;
                            }
                            var text = __jscStack(raw);
                            Object.defineProperty(err, 'stack', {
                                value: text, writable: true,
                                enumerable: false, configurable: true,
                            });
                            // line/column 在 JSC 是 own、不可枚举、可写、可配置的
                            // 数据属性（Playwright WebKit 实测 {w:true,e:false,c:true}）。
                            Object.defineProperty(err, 'line', {
                                value: loc ? loc.line : 0, writable: true,
                                enumerable: false, configurable: true,
                            });
                            Object.defineProperty(err, 'column', {
                                value: loc ? loc.col : 0, writable: true,
                                enumerable: false, configurable: true,
                            });
                        } catch (e) {}
                        return err;
                    };
                    var __NErr = Error;
                    var __JscError = new Proxy(__NErr, {
                        construct: function (t, args, nt) {
                            return __jscAttach(Reflect.construct(t, args, nt));
                        },
                        apply: function (t, thisArg, args) {
                            return __jscAttach(Reflect.apply(t, thisArg, args));
                        },
                    });
                    try {
                        Object.defineProperty(__JscError, 'name', {
                            value: __NErr.name, configurable: true,
                        });
                    } catch (e) {}
                    try {
                        Object.defineProperty(__JscError, 'length', {
                            value: __NErr.length, configurable: true,
                        });
                    } catch (e) {}
                    Object.defineProperty(window, 'Error', {
                        value: __JscError, writable: true,
                        enumerable: false, configurable: true,
                    });
                    // JSC 无 prepareStackTrace：内核上它本就缺席，此处不做定义。
                }
            } catch (e) {}

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
                    }, 'GestureEvent', null, true);
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
            var __isIPad = (D.deviceType === 'tablet')
                || /iPad/.test(String(D.platform || ''))
                || /iPad/.test(String(D.ua || ''));
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

            // 12) WebKit 原生触控工厂 document.createTouch / createTouchList。
            //     MDN BCD：safari_ios=≤3（至今保留）、chrome 25–68（已移除）、
            //     safari=false；检测站按「声明 iOS 但无 createTouch」直接判
            //     「现代 Blink 内核」（本仓库对照检测器该单项 25 分）。
            //     真机形态取证（Playwright WebKit 27.2，/tmp/probe_wk_createtouch.py）：
            //       * createTouch / createTouchList 挂在 Document.prototype，
            //         writable+enumerable+configurable，length 0、原生外观；
            //       * 参数顺序 (window, target, identifier, pageX, pageY,
            //         screenX, screenY, radiusX, radiusY, rotationAngle, force)，
            //         **后四项全部生效**，写入 Touch 的 webkitRadiusX /
            //         webkitRadiusY / webkitRotationAngle / webkitForce
            //         （force 为 float32，-1 原样保留，不夹到 [0,1]）；
            //       * clientX/clientY 镜像 pageX/pageY；未传 target 时 target 为
            //         null、identifier/pageX/pageY 为 0；
            //       * Touch 表面是 WebKit 口径：原型成员仅
            //         clientX/clientY/screenX/screenY/pageX/pageY/target/
            //         identifier/webkitRadiusX/webkitRadiusY/
            //         webkitRotationAngle/webkitForce——**没有**无前缀的
            //         radiusX/radiusY/rotationAngle/force；`new Touch({})` 与
            //         `new TouchList()` 均抛 TypeError「Illegal constructor」；
            //       * 返回真 Touch / TouchList（instanceof、[object Touch]、
            //         索引与 item() 同源、越界 item() 为 null、实例无 own 属性）。
            //     实现：TouchList 取内核 TouchEvent 的 touches 列表（内核不开放
            //     TouchList 构造器，这是唯一能拿到真 TouchList 的路径）。Touch
            //     不能再用 `new Touch({...})`：内核构造器不接受 target=null，且
            //     造出来的实例带 Chromium 的 radiusX/force。改为「真触摸事件取
            //     一个实例作模板」——同文档派发一次 touchstart，拿到内核生成的
            //     真 Touch（内部槽齐备），把六个可设字段记进 WeakMap 后释放；
            //     实例的 webkit* 访问器从 WeakMap 取值，无前缀四项从原型上删除。
            try {
                var __touchOK = typeof Touch === 'function'
                    && typeof TouchList === 'function'
                    && typeof TouchEvent === 'function'
                    && typeof document.createTouch !== 'function';
                if (__touchOK) {
                    var __touchMeta = typeof WeakMap === 'function' ? new WeakMap() : null;
                    var __touchTpl = null;
                    var __touchFields = ['identifier', 'pageX', 'pageY', 'screenX',
                                         'screenY', 'webkitRadiusX', 'webkitRadiusY',
                                         'webkitRotationAngle', 'webkitForce'];
                    try {
                        var __tplTarget = document.documentElement || document;
                        var __tplSeen = null;
                        var __tplCb = function (e) {
                            try {
                                if (e.touches && e.touches.length) {
                                    __tplSeen = e.touches[0];
                                }
                            } catch (e2) {}
                        };
                        document.addEventListener('touchstart', __tplCb, true);
                        try {
                            var __tplEv = new TouchEvent('touchstart', {
                                touches: [new Touch({
                                    identifier: 0, target: __tplTarget,
                                    pageX: 0, pageY: 0,
                                    screenX: 0, screenY: 0,
                                })],
                            });
                            document.dispatchEvent(__tplEv);
                        } catch (e2) {}
                        document.removeEventListener('touchstart', __tplCb, true);
                        if (__tplSeen && __tplSeen instanceof Touch) {
                            __touchTpl = __tplSeen;
                        }
                    } catch (e2) {}

                    // 无前缀四项：真机原型上没有，删掉内核的（configurable:true，实测可删）。
                    // 删之前先把原生 getter 留一份：页面上真实派发的触摸（CDP 注入的
                    // touchmove）在内核里带 force/radiusX/radiusY/rotationAngle，
                    // WebKit 口径下这四个值要搬到 webkit* 前缀成员上，否则拖动时的
                    // 压力/接触面积会整块丢失（实测拖拽探针 force 变 undefined）。
                    var __chromiumTouchGets = {};
                    try {
                        var __drop = ['radiusX', 'radiusY', 'rotationAngle', 'force'];
                        for (var __di = 0; __di < __drop.length; __di++) {
                            var __dd = Object.getOwnPropertyDescriptor(
                                Touch.prototype, __drop[__di]);
                            if (__dd && __dd.get) {
                                __chromiumTouchGets[__drop[__di]] = __dd.get;
                            }
                            delete Touch.prototype[__drop[__di]];
                        }
                    } catch (e2) {}
                    // webkit* 成员与内核无前缀成员的对应关系（Touch.idl 的
                    // webkitRadiusX ↔ 内核 radiusX，webkitForce ↔ force）
                    var __wkFallback = {
                        webkitRadiusX: 'radiusX', webkitRadiusY: 'radiusY',
                        webkitRotationAngle: 'rotationAngle', webkitForce: 'force',
                    };

                    // 实例成员：真机从内部槽取值，这里用 WeakMap 存 createTouch
                    // 写入的九个字段；非 createTouch 造出来的 Touch（页面上真实
                    // 派发的触摸）没有 meta，回落原生 getter，语义不变。
                    var __touchField = function (name, nativeGet) {
                        try {
                            var getter = __maskNative(function () {
                                var m = __touchMeta ? __touchMeta.get(this) : null;
                                if (m && name in m) { return m[name]; }
                                return nativeGet.call(this);
                            }, 'get ' + name);
                            Object.defineProperty(Touch.prototype, name, {
                                get: getter, enumerable: true, configurable: true,
                            });
                        } catch (e2) {}
                    };
                    var __touchFieldsMap = {
                        identifier: 'identifier', clientX: 'clientX',
                        clientY: 'clientY', screenX: 'screenX', screenY: 'screenY',
                        pageX: 'pageX', pageY: 'pageY', target: 'target',
                        webkitRadiusX: 'webkitRadiusX', webkitRadiusY: 'webkitRadiusY',
                        webkitRotationAngle: 'webkitRotationAngle',
                        webkitForce: 'webkitForce',
                    };
                    try {
                        for (var __fn in __touchFieldsMap) {
                            var __fd = Object.getOwnPropertyDescriptor(
                                Touch.prototype, __fn);
                            var __fallback = __chromiumTouchGets[__wkFallback[__fn]];
                            if (__fd && __fd.get) {
                                __touchField(__fn, __fd.get);
                            } else if (__fallback) {
                                __touchField(__fn, __fallback);
                            } else {
                                __touchField(__fn, function () { return 0; });
                            }
                        }
                    } catch (e2) {}

                    // 原型成员顺序：真机按 IDL 声明序，内核是 Chromium 的
                    // 声明序（TouchList 的 constructor 在末尾、TouchEvent 的
                    // 修饰键在前）。三个原型的成员描述符都是 configurable，
                    // 按 IDL 序整体重挂即可对齐。
                    var __reorderProto = function (proto, order) {
                        try {
                            var cur = Object.getOwnPropertyNames(proto);
                            var same = cur.length === order.length
                                && order.every(function (n, i) { return cur[i] === n; });
                            if (same) { return; }
                            var saved = {};
                            for (var i = 0; i < cur.length; i++) {
                                saved[cur[i]] = Object.getOwnPropertyDescriptor(proto, cur[i]);
                                delete proto[cur[i]];
                            }
                            for (var j = 0; j < order.length; j++) {
                                if (saved[order[j]]) {
                                    Object.defineProperty(proto, order[j], saved[order[j]]);
                                }
                            }
                        } catch (e2) {}
                    };
                    __reorderProto(Touch.prototype, [
                        'constructor', 'clientX', 'clientY', 'screenX', 'screenY',
                        'pageX', 'pageY', 'target', 'identifier',
                        'webkitRadiusX', 'webkitRadiusY', 'webkitRotationAngle',
                        'webkitForce']);
                    __reorderProto(TouchList.prototype,
                                   ['constructor', 'length', 'item']);
                    __reorderProto(TouchEvent.prototype, [
                        'constructor', 'touches', 'targetTouches', 'changedTouches',
                        'ctrlKey', 'shiftKey', 'altKey', 'metaKey']);

                    // 模板缺失时退路：用空对象占位，字段全 0（形状仍正确）
                    var __mkTouch = function (win, target, id, px, py, sx, sy,
                                              rx, ry, rot, force) {
                        var t = __touchTpl;
                        if (!t) {
                            try {
                                var ev0 = new TouchEvent('touchstart', {
                                    touches: [new Touch({
                                        identifier: 0, target: document,
                                        pageX: 0, pageY: 0,
                                        screenX: 0, screenY: 0,
                                    })],
                                });
                                t = ev0.touches && ev0.touches[0];
                            } catch (e2) {}
                        }
                        if (!t) { return null; }
                        var num = function (v) { var n = Number(v); return n === n ? n : 0; };
                        var meta = {
                            identifier: (id === undefined || id === null) ? 0 : num(id),
                            pageX: num(px), pageY: num(py),
                            screenX: num(sx), screenY: num(sy),
                            // WebKit 的 webkitForce 是 float32，负值原样保留
                            webkitRadiusX: (rx === undefined || rx === null) ? 0 : num(rx),
                            webkitRadiusY: (ry === undefined || ry === null) ? 0 : num(ry),
                            webkitRotationAngle: (rot === undefined || rot === null) ? 0 : num(rot),
                            webkitForce: (force === undefined || force === null) ? 0 : num(force),
                        };
                        // pageX/pageY 同时镜像到 clientX/clientY（真机语义）
                        meta.clientX = meta.pageX;
                        meta.clientY = meta.pageY;
                        if (__touchMeta) { __touchMeta.set(t, meta); }
                        // target 为 null 时按真机语义返回 null；否则用模板的
                        // target（createTouch 的第一个参数是 window，第二个才是
                        // target，且内核 Touch 的 target 槽不可在事后改）
                        try {
                            if (target === undefined || target === null) {
                                Object.defineProperty(t, 'target', {
                                    get: __maskNative(function () { return null; }, 'get target'),
                                    enumerable: true, configurable: true,
                                });
                            } else {
                                meta.target = target;
                            }
                        } catch (e2) {}
                        return t;
                    };
                    var __mkTouchList = function (touches) {
                        // 唯一路径：内核 TouchEvent 的 touches 列表
                        var ev = new TouchEvent('touchend', {
                            touches: touches, changedTouches: touches,
                        });
                        return ev.touches;
                    };
                    defineFn(Document.prototype, 'createTouch', function () {
                        return __mkTouch.apply(null, arguments);
                    });
                    try {
                        Object.defineProperty(Document.prototype.createTouch, 'length', {
                            value: 0, configurable: true,
                        });
                    } catch (e2) {}
                    defineFn(Document.prototype, 'createTouchList', function () {
                        var args = [];
                        for (var __ti = 0; __ti < arguments.length; __ti++) {
                            args.push(arguments[__ti]);
                        }
                        return __mkTouchList(args);
                    });
                    try {
                        Object.defineProperty(Document.prototype.createTouchList, 'length', {
                            value: 0, configurable: true,
                        });
                    } catch (e2) {}
                }
            } catch (e) {}

            // ---- WebKit 媒体面（P2-13）：AirPlay / EME 前缀面 ----
            // 检测站按「声明 iOS 但无这些成员」判「现代 Blink」。真机形态取证
            // （Playwright WebKit 27.2 实测）：
            //   * WebKitPlaybackTargetAvailabilityEvent：**可构造**，length=1，
            //     `new E('x')` 得 availability=''、`new E('x',{availability:'available'})`
            //     得 'available'（未知值原样保留，不校验）；prototype.availability 为
            //     getter（enumerable+configurable）；toString 为 `[object WebKit...]`；
            //     无 new 抛 TypeError。
            //   * WebKitMediaKeys：isTypeSupported 静态方法（length 1，恒 false——
            //     内核无加密系统）；prototype.keySystem getter、createSession(length 2)；
            //     `new` 抛 NotSupportedError（不是 Illegal constructor）。
            //   * WebKitMediaKeySession / MessageEvent / NeededEvent：均为接口对象，
            //     own = [length,name,prototype]；prototype 上为只读成员。
            //   * WebKitMediaKeyError：静态常量 MEDIA_KEYERR_* = 1..6
            //     （writable:false, enumerable:true, configurable:false）。
            //   * HTMLMediaElement.prototype：webkitShowPlaybackTargetPicker(length 0)
            //     与 webkitSetMediaKeys(length 1) 为数据属性（w/e/c 均 true）；
            //     webkitCurrentPlaybackTargetIsWireless / webkitKeys 为 getter
            //     （enumerable+configurable，get.length 0，默认 false / null）。
            //   * HTMLVideoElement.prototype（不是 MediaElement）：webkitPresentationMode
            //     为 getter，默认 'inline'。
            // 全部走 __maskNative 保持原生外观；坏 receiver 走 __slotHas 抛 TypeError。
            try {
                if (typeof Event === 'function' && typeof document !== 'undefined') {
                    // ---- AirPlay 事件构造器（可构造）----
                    var __Avail = function (type, init) {
                        // 真机无 new 调用抛 TypeError（WebKit 构造器统一语义）
                        if (!(this instanceof __Avail)) {
                            throw new TypeError("Constructor WebKitPlaybackTargetAvailabilityEvent requires 'new'");
                        }
                        var e = new Event(String(type));
                        var avail = (init && init.availability !== undefined)
                            ? String(init.availability) : '';
                        Object.setPrototypeOf(e, __AvailP);
                        Object.defineProperty(e, '___av', {
                            value: avail, writable: true, enumerable: false, configurable: true,
                        });
                        try {
                            Object.defineProperty(e, Symbol.toStringTag, {
                                value: 'WebKitPlaybackTargetAvailabilityEvent',
                                configurable: true,
                            });
                        } catch (x) {}
                        return e;
                    };
                    try {
                        Object.defineProperty(__Avail, 'length', { value: 1, configurable: true });
                        Object.defineProperty(__Avail, 'name', {
                            value: 'WebKitPlaybackTargetAvailabilityEvent', configurable: true,
                        });
                    } catch (x) {}
                    var __AvailP = Object.create(Event.prototype);
                    Object.defineProperty(__AvailP, 'constructor', {
                        value: __Avail, writable: true, enumerable: false, configurable: true,
                    });
                    Object.defineProperty(__AvailP, 'availability', {
                        get: __maskNative(function () {
                            if (!(this instanceof Event)) { throw new TypeError('Illegal invocation'); }
                            return this.___av === undefined ? '' : this.___av;
                        }, 'get availability'),
                        enumerable: true, configurable: true,
                    });
                    Object.defineProperty(__Avail, 'prototype', {
                        value: __AvailP, writable: false, enumerable: false, configurable: false,
                    });
                    Object.defineProperty(window, 'WebKitPlaybackTargetAvailabilityEvent', {
                        value: __maskNative(__Avail, 'WebKitPlaybackTargetAvailabilityEvent', null, true),
                        writable: true, enumerable: false, configurable: true,
                    });

                    // ---- EME 接口对象（不可构造）----
                    var __ifaceCtor = function (name, len) {
                        var C = function () { throw new TypeError('Illegal constructor'); };
                        try {
                            Object.defineProperty(C, 'name', { value: name, configurable: true });
                            Object.defineProperty(C, 'length', { value: len, configurable: true });
                        } catch (x) {}
                        return C;
                    };
                    var __installIface = function (name, len) {
                        if (name in window) { return null; }
                        var C = __ifaceCtor(name, len);
                        Object.defineProperty(window, name, {
                            value: __maskNative(C, name, null, true),
                            writable: true, enumerable: false, configurable: true,
                        });
                        return C;
                    };
                    __installIface('WebKitMediaKeySession', 0);
                    __installIface('WebKitMediaKeyMessageEvent', 1);
                    __installIface('WebKitMediaKeyNeededEvent', 1);

                    // 原型成员：真机形态（Playwright WebKit 27.2 实测）
                    //   Session.prototype = constructor,error,keySystem,sessionId,
                    //     onwebkitkeyadded,onwebkitkeyerror,onwebkitkeymessage,update,close
                    //   MessageEvent.prototype = constructor,message,destinationURL
                    //   NeededEvent.prototype = constructor,initData
                    var __ifaceProtoMembers = function (name, props) {
                        var C = window[name];
                        if (typeof C !== 'function' || !C.prototype) { return; }
                        for (var i = 0; i < props.length; i++) {
                            var spec = props[i];
                            var pname = spec[0], kind = spec[1];
                            if (pname in C.prototype) { continue; }
                            try {
                                if (kind === 'fn') {
                                    Object.defineProperty(C.prototype, pname, {
                                        value: __maskNative(function () {
                                            if (!__slotHas(C.prototype, this)) { throw new TypeError('Illegal invocation'); }
                                            return undefined;
                                        }, pname),
                                        writable: true, enumerable: true, configurable: true,
                                    });
                                } else {
                                    // 事件钩子 / 只读属性：get/set 访问器（默认 null）
                                    var slot = '__' + pname;
                                    Object.defineProperty(C.prototype, pname, {
                                        get: __maskNative(function () {
                                            if (!__slotHas(C.prototype, this)) { throw new TypeError('Illegal invocation'); }
                                            return this[slot] === undefined ? null : this[slot];
                                        }, 'get ' + pname),
                                        set: __maskNative(function (v) {
                                            if (!__slotHas(C.prototype, this)) { throw new TypeError('Illegal invocation'); }
                                            try { Object.defineProperty(this, slot, { value: v, writable: true, enumerable: false, configurable: true }); } catch (x) {}
                                        }, 'set ' + pname),
                                        enumerable: true, configurable: true,
                                    });
                                }
                            } catch (x) {}
                        }
                    };
                    __ifaceProtoMembers('WebKitMediaKeySession', [
                        ['error', 'get'], ['keySystem', 'get'], ['sessionId', 'get'],
                        ['onwebkitkeyadded', 'get'], ['onwebkitkeyerror', 'get'],
                        ['onwebkitkeymessage', 'get'], ['update', 'fn'], ['close', 'fn'],
                    ]);
                    __ifaceProtoMembers('WebKitMediaKeyMessageEvent', [
                        ['message', 'get'], ['destinationURL', 'get'],
                    ]);
                    __ifaceProtoMembers('WebKitMediaKeyNeededEvent', [
                        ['initData', 'get'],
                    ]);

                    // WebKitMediaKeys：静态 isTypeSupported + 原型成员；new 抛 NotSupportedError
                    if (!('WebKitMediaKeys' in window)) {
                        var __MK = function () {
                            throw new DOMException('', 'NotSupportedError');
                        };
                        try {
                            Object.defineProperty(__MK, 'name', { value: 'WebKitMediaKeys', configurable: true });
                            Object.defineProperty(__MK, 'length', { value: 1, configurable: true });
                        } catch (x) {}
                        __MK.isTypeSupported = __maskNative(function () { return false; }, 'isTypeSupported');
                        try { Object.defineProperty(__MK.isTypeSupported, 'length', { value: 1, configurable: true }); } catch (x) {}
                        Object.defineProperty(__MK.prototype, 'keySystem', {
                            get: __maskNative(function () {
                                if (!__slotHas(__MK.prototype, this)) { throw new TypeError('Illegal invocation'); }
                                return this.___ks || '';
                            }, 'get keySystem'),
                            enumerable: true, configurable: true,
                        });
                        Object.defineProperty(__MK.prototype, 'createSession', {
                            value: __maskNative(function () {
                                if (!__slotHas(__MK.prototype, this)) { throw new TypeError('Illegal invocation'); }
                                return null;
                            }, 'createSession'),
                            writable: true, enumerable: true, configurable: true,
                        });
                        try { Object.defineProperty(__MK.prototype.createSession, 'length', { value: 2, configurable: true }); } catch (x) {}
                        Object.defineProperty(__MK.prototype, 'constructor', {
                            value: __MK, writable: true, enumerable: false, configurable: true,
                        });
                        Object.defineProperty(window, 'WebKitMediaKeys', {
                            value: __maskNative(__MK, 'WebKitMediaKeys', null, true),
                            writable: true, enumerable: false, configurable: true,
                        });
                    }

                    // WebKitMediaKeyError：6 个常量 1..6（w:false, e:true, c:false）
                    if (!('WebKitMediaKeyError' in window)) {
                        var __EK = __ifaceCtor('WebKitMediaKeyError', 0);
                        var __codes = ['MEDIA_KEYERR_UNKNOWN', 'MEDIA_KEYERR_CLIENT',
                            'MEDIA_KEYERR_SERVICE', 'MEDIA_KEYERR_OUTPUT',
                            'MEDIA_KEYERR_HARDWARECHANGE', 'MEDIA_KEYERR_DOMAIN'];
                        for (var __ci = 0; __ci < __codes.length; __ci++) {
                            (function (k, val) {
                                var d = { value: val, writable: false, enumerable: true, configurable: false };
                                try { Object.defineProperty(__EK, k, d); } catch (x) {}
                                try { Object.defineProperty(__EK.prototype, k, d); } catch (x) {}
                            })(__codes[__ci], __ci + 1);
                        }
                        Object.defineProperty(window, 'WebKitMediaKeyError', {
                            value: __maskNative(__EK, 'WebKitMediaKeyError', null, true),
                            writable: true, enumerable: false, configurable: true,
                        });
                    }
                }

                // ---- HTMLMediaElement / HTMLVideoElement 原型面 ----
                if (typeof HTMLMediaElement === 'function' && typeof HTMLVideoElement === 'function') {
                    var mp = HTMLMediaElement.prototype;
                    var vp = HTMLVideoElement.prototype;

                    if (!('webkitShowPlaybackTargetPicker' in mp)) {
                        Object.defineProperty(mp, 'webkitShowPlaybackTargetPicker', {
                            value: __maskNative(function () {
                                if (!__slotHas(mp, this)) { throw new TypeError('Illegal invocation'); }
                                return undefined;
                            }, 'webkitShowPlaybackTargetPicker'),
                            writable: true, enumerable: true, configurable: true,
                        });
                        try {
                            Object.defineProperty(mp.webkitShowPlaybackTargetPicker, 'length', {
                                value: 0, configurable: true,
                            });
                        } catch (x) {}
                    }
                    if (!('webkitSetMediaKeys' in mp)) {
                        Object.defineProperty(mp, 'webkitSetMediaKeys', {
                            value: __maskNative(function () {
                                if (!__slotHas(mp, this)) { throw new TypeError('Illegal invocation'); }
                                return null;
                            }, 'webkitSetMediaKeys'),
                            writable: true, enumerable: true, configurable: true,
                        });
                        try {
                            Object.defineProperty(mp.webkitSetMediaKeys, 'length', {
                                value: 1, configurable: true,
                            });
                        } catch (x) {}
                    }
                    if (!('webkitCurrentPlaybackTargetIsWireless' in mp)) {
                        Object.defineProperty(mp, 'webkitCurrentPlaybackTargetIsWireless', {
                            get: __maskNative(function () {
                                if (!__slotHas(mp, this)) { throw new TypeError('Illegal invocation'); }
                                return false;
                            }, 'get webkitCurrentPlaybackTargetIsWireless'),
                            enumerable: true, configurable: true,
                        });
                    }
                    if (!('webkitKeys' in mp)) {
                        Object.defineProperty(mp, 'webkitKeys', {
                            get: __maskNative(function () {
                                if (!__slotHas(mp, this)) { throw new TypeError('Illegal invocation'); }
                                return null;
                            }, 'get webkitKeys'),
                            enumerable: true, configurable: true,
                        });
                    }
                    if (!('webkitPresentationMode' in vp)) {
                        Object.defineProperty(vp, 'webkitPresentationMode', {
                            get: __maskNative(function () {
                                if (!__slotHas(vp, this)) { throw new TypeError('Illegal invocation'); }
                                return 'inline';
                            }, 'get webkitPresentationMode'),
                            enumerable: true, configurable: true,
                        });
                    }
                }
            } catch (e) {}


            // 13) WebKit 专属 CSS 面：-webkit-touch-callout（WebKit CSSProperties.json
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
                // F-98：-apple-pay-button-style 在真机有驼峰访问器
                // （applePayButtonStyle），其余专有属性的驼峰名在 WebKit 27.2
                // 上已不存在（marginTrim / webkitBackdropFilter 均 null），不补。
                try {
                    if (!Object.getOwnPropertyDescriptor(
                            CSSStyleDeclaration.prototype, 'applePayButtonStyle')) {
                        Object.defineProperty(
                            CSSStyleDeclaration.prototype, 'applePayButtonStyle', {
                                configurable: true, enumerable: false,
                                get: __maskNative(function () {
                                    return this.getPropertyValue(
                                        '-apple-pay-button-style');
                                }, 'get applePayButtonStyle'),
                                set: __maskNative(function (v) {
                                    this.setProperty('-apple-pay-button-style', v);
                                }, 'set applePayButtonStyle'),
                            });
                    }
                } catch (e) {}
                // F-98：声明 → 插入序（规范名），供 length/item 合并枚举。
                // __csOwner 在模板顶层声明（CSSOM 补丁与 getComputedStyle
                // 包装不在同一作用域）。
                var __cssOrder = new WeakMap();
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
                // F-98：length / item 把存储键并进原生枚举。真机的规范名口径：
                // -webkit-hyphens 序列化为 hyphens（CSSProperties.json 的别名
                // 目标），其余保持原名。
                var __cssCanon = function (key) {
                    return key === '-webkit-hyphens' ? 'hyphens' : key;
                };
                var __cssStore_put = function (decl, key, val) {
                    __wfStore(decl)[key] = val;
                };
                var __f98Stored = function (decl) {
                    var o = __cssOrder.get(decl);
                    return o ? o.slice() : [];
                };
                var __origCSLength = Object.getOwnPropertyDescriptor(
                    __csProto, 'length');
                var __origCSItem = __csProto.item;
                if (__origCSLength && __origCSLength.get) {
                    Object.defineProperty(__csProto, 'length', {
                        configurable: true,
                        enumerable: __origCSLength.enumerable,
                        get: __maskNative(function () {
                            var base = __origCSLength.get.call(this);
                            return base + __f98Stored(this).length;
                        }, 'get length'),
                    });
                }
                __csProto.item = __maskNative(__matchLen(function (i) {
                    var n = Number(i) | 0;
                    var base = __origCSLength ? __origCSLength.get.call(this) : 0;
                    if (n < 0) { return ''; }
                    if (n < base) { return __origCSItem.call(this, n); }
                    var mine = __f98Stored(this);
                    var k = n - base;
                    return k < mine.length ? mine[k] : '';
                }, __origCSItem), 'item');
                // F-98：数字索引。Blink 的 CSSStyleDeclaration 带索引拦截器，
                // `Object.defineProperty(st, '1', …)` 直接抛「Index property
                // setter is not supported」（访问器与数据属性同样拒绝），索引
                // 只能挂在原型上；而挂到 CSSStyleDeclaration.prototype 会让
                // **每个** 样式对象（含空声明）多出 `0 in style` 命中——真机为
                // false，属于一行可查的暴露面。故按声明实例换原型：换上一个
                // 继承 CSSStyleDeclaration.prototype 的中间对象，带 0..255 的
                // 数字访问器。原生已解析的自有索引仍优先命中，只有 base 之后
                // 的索引才落到访问器。真机 WebKit 的索引是实例自有可枚举数据
                // 属性；这里拿不到 getOwnPropertyNames 等价物，且 Object
                // .getPrototypeOf(decl) 对该声明不再是 CSSStyleDeclaration
                // .prototype——但方法身份、instanceof、品牌检查、toString 全部
                // 保持，且未涉及 WebKit 专有属性的声明完全不受影响。
                var __f98IdxProto = null;
                var __f98IdxAttach = function (decl) {
                    try {
                        if (!__f98IdxProto) {
                            var proto = Object.create(__csProto, {
                                constructor: {
                                    value: CSSStyleDeclaration,
                                    writable: true, configurable: true,
                                    enumerable: false,
                                },
                            });
                            for (var __f98i = 0; __f98i < 256; __f98i++) {
                                (function (idx) {
                                    Object.defineProperty(proto, String(idx), {
                                        configurable: true, enumerable: false,
                                        get: __maskNative(function () {
                                            var base = __origCSLength
                                                ? __origCSLength.get.call(this) : 0;
                                            var k = idx - base;
                                            if (k < 0) { return undefined; }
                                            var mine = __f98Stored(this);
                                            return k < mine.length
                                                ? mine[k] : undefined;
                                        }, 'get ' + idx),
                                    });
                                })(__f98i);
                            }
                            __f98IdxProto = proto;
                        }
                        if (Object.getPrototypeOf(decl) !== __f98IdxProto) {
                            Object.setPrototypeOf(decl, __f98IdxProto);
                        }
                    } catch (e) {}
                };
                var __f98Track = function (decl, name) {
                    var o = __cssOrder.get(decl);
                    if (!o) { o = []; __cssOrder.set(decl, o); }
                    if (o.indexOf(name) === -1) { o.push(name); }
                    __f98IdxAttach(decl);
                };
                var __f98Untrack = function (decl, name) {
                    var o = __cssOrder.get(decl);
                    if (!o) { return; }
                    var i = o.indexOf(name);
                    if (i !== -1) { o.splice(i, 1); }
                };

                var __origSetProperty = __csProto.setProperty;
                var __origGetPropertyValue = __csProto.getPropertyValue;
                var __origRemoveProperty = __csProto.removeProperty;
                __csProto.setProperty = __maskNative(__matchLen(function (property, value, priority) {
                    var hit = __wfInfo(property);
                    if (hit) {
                        if (__wfValueOk(hit.info, value)) {
                            __wfStore(this)[hit.key] = String(value).trim().toLowerCase();
                            __f98Track(this, hit.key);
                        }
                        return;
                    }
                    // F-98：值域白名单内的 WebKit 专有属性，内核不认（真机解析
                    // 后 getPropertyValue 原样回读）。合法值自行入库，非法值丢弃
                    // ——与真机「解析失败即不设置」同语义。
                    var key = String(property).trim().toLowerCase();
                    if (!__cssGate[key] && __cssAllow[key] !== undefined
                            && __cssLookup(key, value) === true) {
                        __cssStore_put(this, key, String(value).trim());
                        __f98Track(this, __cssCanon(key));
                        return;
                    }
                    // F-98：position 的 -webkit-sticky 别名。Blink 不认该值，
                    // 直接 setProperty 会静默丢弃；真机 inline 保留别名原文、
                    // computed 归一到 sticky（WebKit 27.2 实测）。所以本表存
                    // 原文，读取时按「inline 原文 / computed 归一值」分流。
                    if (key === 'position'
                            && String(value).trim().toLowerCase() === '-webkit-sticky') {
                        __cssStore_put(this, 'position', '-webkit-sticky');
                        __f98Track(this, 'position');
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
                    // F-98：computed 声明回落到所属元素的 inline 存储
                    var key = String(property).trim().toLowerCase();
                    var src = this;
                    var owner = __csOwner.get(this);
                    var computed = false;
                    if (owner && owner.style && owner.style !== this) {
                        src = owner.style;
                        computed = true;
                    }
                    var st = __cssStore.get(src);
                    if (st && st[key] !== undefined) {
                        var got = st[key];
                        if (computed && key === 'position'
                                && got === '-webkit-sticky') {
                            return 'sticky';
                        }
                        return got;
                    }
                    return __origGetPropertyValue.apply(this, arguments);
                }, __origGetPropertyValue), 'getPropertyValue');
                __csProto.removeProperty = __maskNative(__matchLen(function (property) {
                    var hit = __wfInfo(property);
                    if (hit) {
                        var s = __cssStore.get(this);
                        var prev = (s && s[hit.key] !== undefined) ? s[hit.key] : '';
                        if (s) { delete s[hit.key]; }
                        __f98Untrack(this, hit.key);
                        return prev;
                    }
                    var key = String(property).trim().toLowerCase();
                    if (!__cssGate[key] && __cssAllow[key] !== undefined) {
                        var s2 = __cssStore.get(this);
                        var prev2 = (s2 && s2[key] !== undefined) ? s2[key] : '';
                        if (s2) { delete s2[key]; }
                        __f98Untrack(this, __cssCanon(key));
                        return prev2;
                    }
                    if (key === 'position') {
                        var s3 = __cssStore.get(this);
                        if (s3 && s3.position !== undefined) {
                            delete s3.position;
                            __f98Untrack(this, 'position');
                            return '-webkit-sticky';
                        }
                    }
                    return __origRemoveProperty.apply(this, arguments);
                }, __origRemoveProperty), 'removeProperty');
                // 双向差集（F-07 / F-24）：桌面 Chromium 与 iOS WebKit 各有
                // 对方没有的 CSS 属性。deny 表命中直接 false；allow 表命中直接
                // true（不落到内核的解析器——内核解析器对 WebKit 专有属性
                // 一律 false）。值为 null 表示「任意值都支持」。
                // 反向陷阱：-webkit-touch-callout / -webkit-overflow-scrolling
                // 在 macOS WebKit 为 false 而 iOS 自 3.2 起支持，仿真侧 true
                // 是对的，故走 allow（沿用 __wf 的合法值表）。
                var __cssDeny = Object.create(null);
                __cssDeny['interpolate-size'] = null;
                __cssDeny['overflow-clip-margin'] = null;
                __cssDeny['corner-shape'] = null;
                __cssDeny['reading-flow'] = null;
                // F-88：WebKit 专有属性必须按值域校验——`vals === null` 的
                // 「任意值都支持」语义与真机不符（真机两参形态要求属性名与
                // 值同时合法）。值域取证：WebKit Source/WebCore/css/
                // CSSProperties.json + Playwright WebKit 27.2 移动上下文实测。
                var __cssAllow = Object.create(null);
                __cssAllow['-webkit-touch-callout'] = ['default', 'none'];
                __cssAllow['-webkit-overflow-scrolling'] = ['auto', 'touch'];
                // margin-trim：none | block | [ block-start || block-end ]
                // （inline / inline-start 在真机为 false，不在值域内）
                __cssAllow['margin-trim'] = ['none', 'block', 'block-start',
                                             'block-end'];
                // text-autospace：normal | auto | no-autospace |
                // [ ideograph-alpha || ideograph-numeric || insert ]
                __cssAllow['text-autospace'] = ['normal', 'auto', 'no-autospace',
                                                'ideograph-alpha',
                                                'ideograph-numeric', 'insert'];
                // hyphens（-webkit- 为 CSSProperties.json 里的别名）：none|manual|auto
                __cssAllow['-webkit-hyphens'] = ['none', 'manual', 'auto'];
                // -apple-pay-button-style：white | white-outline | black
                __cssAllow['-apple-pay-button-style'] = ['white', 'white-outline',
                                                         'black'];
                // -webkit-backdrop-filter：none | <filter-value-list>，函数式
                // 值域（consumeFilter），逐函数校验 + 至少一项。
                __cssAllow['-webkit-backdrop-filter'] = ['none'];
                var __cssFilterFns = ['blur', 'brightness', 'contrast', 'grayscale',
                                      'hue-rotate', 'invert', 'opacity', 'saturate',
                                      'sepia', 'drop-shadow', 'url'];
                var __cssFilterOk = function (v) {
                    if (v === 'none') { return true; }
                    var rest = v, count = 0;
                    while (rest.length) {
                        var m = /^\s*([-a-z]+)\(([^()]*)\)\s*/.exec(rest);
                        if (!m) { return false; }
                        if (__cssFilterFns.indexOf(m[1]) === -1) { return false; }
                        if (m[1] === 'blur' && m[2].trim() !== ''
                                && !/^-?(\d+\.?\d*|\.\d+)(px|rem|em)$/.test(
                                    m[2].trim())) {
                            return false;
                        }
                        rest = rest.slice(m[0].length);
                        count++;
                    }
                    return count > 0;
                };
                // 关键字组合项（|| 语法）：顺序无关、可多选，但不能与单值项混用。
                var __cssComboOk = function (vals, v, singles, combos) {
                    if (vals.indexOf(v) !== -1) { return true; }
                    var parts = v.split(/\s+/), seen = Object.create(null);
                    if (!parts.length) { return false; }
                    for (var __ci = 0; __ci < parts.length; __ci++) {
                        if (combos.indexOf(parts[__ci]) === -1) { return false; }
                        if (seen[parts[__ci]]) { return false; }
                        seen[parts[__ci]] = 1;
                    }
                    return true;
                };
                // F-89：低于 profile 版本的属性，真机 CSS.supports 一律 false。
                var __cssGate = Object.create(null);
                (D.cssGate || []).forEach(function (k) { __cssGate[k] = 1; });
                // position 的 -webkit-sticky 别名（WebCore 解析器保留，Blink 已移除）
                var __cssAllowValue = function (prop, value) {
                    var key = String(prop).trim().toLowerCase();
                    var v = String(value == null ? '' : value).trim().toLowerCase();
                    if (key === 'position' && v === '-webkit-sticky') { return true; }
                    return null;
                };
                var __cssLookup = function (prop, value) {
                    var key = String(prop).trim().toLowerCase();
                    if (__cssGate[key]) { return false; }
                    var vals = __cssAllow[key];
                    if (vals !== undefined) {
                        var v = String(value == null ? '' : value).trim().toLowerCase();
                        if (key === '-webkit-backdrop-filter') {
                            return __cssFilterOk(v);
                        }
                        if (key === 'margin-trim') {
                            return __cssComboOk(vals, v,
                                ['none', 'block'], ['block-start', 'block-end']);
                        }
                        if (key === 'text-autospace') {
                            return __cssComboOk(vals, v,
                                ['normal', 'auto', 'no-autospace'],
                                ['ideograph-alpha', 'ideograph-numeric', 'insert']);
                        }
                        return vals.indexOf(v) !== -1;
                    }
                    var alias = __cssAllowValue(prop, value);
                    if (alias !== null) { return alias; }
                    if (key in __cssDeny) { return false; }
                    return null;
                };
                // 单参条件文本：只处理「单一声明」形态（如 "(margin-trim: block)"）。
                // 复合条件与选择器函数保持原样交内核——误判面太大，且这些
                // 属性在真实检测里几乎只以单声明出现。
                var __cssDeclRe = /^\s*\(\s*(-?[a-zA-Z][\w-]*)\s*:\s*([^()]*?)\s*\)\s*$/;
                var __cssCondition = function (text) {
                    var m = __cssDeclRe.exec(text);
                    if (!m) { return null; }
                    return __cssLookup(m[1], m[2]);
                };
                var __origSupports = (typeof CSS !== 'undefined' && CSS
                    && typeof CSS.supports === 'function') ? CSS.supports : null;
                if (__origSupports) {
                    // 单参条件文本里的 WebKit 专有声明改写为内核可判定的等价
                    // 条件（合法值 → (color: red)，非法值 → 不可解析的颜色），
                    // 这样复合条件（and/or/not）仍由内核解析器整体求值，语义
                    // 与真机一致；嵌套括号（如 blur(4px)）按属性名整体匹配。
                    // F-101：改写名单必须收 __cssAllow 的**全部**键。此前只收
                    // `-webkit-` 前缀项，margin-trim / text-autospace 在复合
                    // 条件里就落回原生 Blink（单参形态却正确），同一属性两种
                    // 答案。position 是 -webkit-sticky 的改写入口，照旧保留。
                    var __wkPropNames = Object.keys(__cssAllow)
                        .concat(['position']);
                    var __wkPropAlt = __wkPropNames.map(function (k) {
                        return k.replace(/[-]/g, '\\-');
                    }).join('|');
                    var __wkDeclRe = new RegExp(
                        '(' + __wkPropAlt + ')\\s*:\\s*([^()]*(?:\\([^()]*\\)[^()]*)*)',
                        'gi');
                    var __rewriteConditions = function (text) {
                        return text.replace(__wkDeclRe, function (m, prop, val) {
                            var v = String(val).replace(/\)\s*$/, '');
                            // 与两参形态同源：走 __cssLookup（含 F-88 值域校验
                            // 与 F-89 版本门控），避免两条路径各判一套。
                            var ok = __cssLookup(prop, v) === true;
                            return ok
                                ? '(color: red)'
                                : '(color: __fp_invalid_color_0x9)';
                        });
                    };
                    var __fakeSupports = function (a, b) {
                        try {
                            if (arguments.length >= 2) {
                                var hit2 = __cssLookup(a, b);
                                if (hit2 !== null) { return hit2; }
                            } else if (arguments.length === 1) {
                                var text = String(a);
                                var cond = __cssCondition(text);
                                if (cond !== null) { return cond; }
                                if (__wkDeclRe.test(text)) {
                                    __wkDeclRe.lastIndex = 0;
                                    return __origSupports.call(
                                        CSS, __rewriteConditions(text));
                                }
                                __wkDeclRe.lastIndex = 0;
                            }
                        } catch (e) {}
                        return __origSupports.apply(CSS, arguments);
                    };
                    __matchLen(__fakeSupports, __origSupports);
                    Object.defineProperty(CSS, 'supports', {
                        value: __maskNative(__fakeSupports, 'supports'),
                        writable: true, enumerable: true, configurable: true,
                    });
                    // 单参形态：deny 表里出现的选择器函数为 false，其余交内核。
                    // selector(:host-context(a)) 在 Blink 为 true、WebKit 为
                    // false（BCD css.selectors.host-context safari_ios=false）。
                    var __selDeny = /selector\(\s*:host-context\(/i;
                    var __origSupports2 = CSS.supports;
                    Object.defineProperty(CSS, 'supports', {
                        value: __maskNative(__matchLen(function (a, b) {
                            try {
                                if (arguments.length === 1 && __selDeny.test(String(a))) {
                                    return false;
                                }
                            } catch (e) {}
                            return __origSupports2.apply(CSS, arguments);
                        }, __origSupports2), 'supports'),
                        writable: true, enumerable: true, configurable: true,
                    });
                }
            } catch (e) {}

            // 14) (dynamic-range: high)：iPhone 的 HDR 屏口径（F-12）。
            //     CDP 的 setEmulatedMedia 不支持该特性（实测仍 false，而同
            //     路径的 prefers-color-scheme 生效），只能改 matchMedia。
            //     实现放在 MediaQueryList.prototype 的 matches 访问器上（原生
            //     位置）：实例上不留 own property，media 仍是原查询串，
            //     非法接收者仍由原生 media getter 抛 Illegal invocation。
            if (D.dynamicRangeHigh && typeof window.matchMedia === 'function'
                    && typeof MediaQueryList !== 'undefined') {
                try {
                    var __mmProto = MediaQueryList.prototype;
                    var __mmDesc = Object.getOwnPropertyDescriptor(__mmProto, 'matches');
                    var __mmMediaDesc = Object.getOwnPropertyDescriptor(__mmProto, 'media');
                    if (__mmDesc && typeof __mmDesc.get === 'function'
                            && __mmMediaDesc && typeof __mmMediaDesc.get === 'function') {
                        var __mmOrigMatches = __mmDesc.get;
                        var __mmOrigMedia = __mmMediaDesc.get;
                        var __mmHdrRe = /\(\s*dynamic-range\s*:\s*high\s*\)/i;
                        Object.defineProperty(__mmProto, 'matches', {
                            configurable: true,
                            enumerable: __mmDesc.enumerable,
                            get: __maskNative(function () {
                                var q = __mmOrigMedia.call(this);
                                if (typeof q === 'string' && __mmHdrRe.test(q)) {
                                    return true;
                                }
                                return __mmOrigMatches.call(this);
                            }, 'get matches'),
                        });
                    }
                } catch (e) {}
            }

            // 15) Safari 全局面（WebKit main / iOS 27 口径）：
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
            //        CriOS 页面世界的常驻通道只有 crwebinvoke（注入桥 core，
            //        §9.4 源码级确认）。POSTSuccessHandler / POSTErrorHandler
            //        只在 POST 重放文档里存在（路由按需注册、重放完成即注销），
            //        由前置脚本 _CRIOS_POST_REQUEST_JS 在文档起点置交接标记、
            //        本补丁按标记把两通道并入同一份具名 getter 表；真机
            //        window.webkit 为 DontDelete+ReadOnly
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
                    }, 'WebKitPoint', null, true);
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
                    }, name, null, true);
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
                    }, 'WebKitBufferNamespace', null, true);
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
                    // B1-7：标记由前置脚本（_CRIOS_POST_REQUEST_JS）在同一
                    // 文档起点的上一段同步代码里写入，这里读完立刻删除——
                    // 页面脚本无论如何都看不到它。
                    var __wkPostDoc = false;
                    try {
                        if (window.__fpwbPostNav === true) { __wkPostDoc = true; }
                        delete window.__fpwbPostNav;
                    } catch (e) {}
                    __wkNsSlots.set(__wkNsObj, {
                        // 页面世界的常驻通道只有注入桥 core 一个（§9.4）。
                        // POSTSuccessHandler / POSTErrorHandler 由
                        // CRWWKScriptMessageRouter 只在 POST 重放期间临时注册，
                        // 见 _CRIOS_POST_REQUEST_JS 注释。
                        handlers: __wkHandlersOf(__wkPostDoc
                            ? ['crwebinvoke', 'POSTSuccessHandler',
                               'POSTErrorHandler']
                            : ['crwebinvoke']),
                        buffers: Object.create(__wkBufProto),
                    });
                    Object.defineProperty(window, 'webkit', {
                        value: __wkNsObj, writable: false,
                        enumerable: true, configurable: false,
                    });
                    // POST 文档：宿主重放面。实现用普通函数写，toString 经
                    // __maskNative 返回 Chrome 61 原文（与真机逐字节一致）——
                    // 不能用 eval：站点 CSP 未开 unsafe-eval 时会直接抛错。
                    if (__wkPostDoc) {
                        try {
                            var __crFn = __maskNative(
                                function (url, headers, body, contentType) {
                                    var base64ToBlob = function (chars, type) {
                                        type = type || '';
                                        var sliceSize = 512;
                                        var arrays = [];
                                        for (var off = 0; off < chars.length;
                                             off += sliceSize) {
                                            var slice = chars.slice(off,
                                                off + sliceSize);
                                            var nums = new Array(slice.length);
                                            for (var i = 0; i < slice.length; i++) {
                                                nums[i] = slice.charCodeAt(i);
                                            }
                                            arrays.push(new Uint8Array(nums));
                                        }
                                        return new Blob(arrays, {type: type});
                                    };
                                    var send = function (url, headers, body,
                                                         contentType) {
                                        var request = new XMLHttpRequest();
                                        request.open('POST', url, false);
                                        for (var key in headers) {
                                            if (headers.hasOwnProperty(key)) {
                                                request.setRequestHeader(
                                                    key, headers[key]);
                                            }
                                        }
                                        var blob = base64ToBlob(
                                            atob(body), contentType);
                                        request.send(blob);
                                        if (request.status != 200) {
                                            throw request.status;
                                        }
                                        return request.responseText;
                                    };
                                    document.open();
                                    try {
                                        document.write(send(
                                            url, headers, body, contentType));
                                        window.webkit.messageHandlers[
                                            'POSTSuccessHandler'
                                        ].postMessage("");
                                    } catch (error) {
                                        window.webkit.messageHandlers[
                                            'POSTErrorHandler'
                                        ].postMessage(error);
                                    }
                                    document.close();
                                },
                                'runPostRequest', D.crPostSrc);
                            var __crNs = {};
                            __crNs.runPostRequest = __crFn;
                            Object.defineProperty(
                                window, '__crPostRequestWorkaround', {
                                    value: __crNs, writable: true,
                                    enumerable: true, configurable: true,
                                });
                        } catch (e) {}
                    }
                }
            } catch (e) {}

            // 14) Notification 面（WebKit Notification.idl 口径）：
            //     桌面 Chromium 的 Notification 与 WebKit 差四处，任一处一行可判：
            //       a) 静态 permission 是**数据属性**（readonly attribute 生成
            //          不可写数据属性，enumerable/configurable 均 true），内核
            //          是原型外的访问器；
            //       b) 静态 maxActions —— Notification.idl:46 已注释掉
            //          （`// static readonly attribute unsigned long maxActions;`），
            //          WebKit 从不暴露；内核有且值为 2；
            //       c) 原型成员集：WebKit 只有 onclick/onerror/navigate/title/
            //          dir/lang/body/tag/icon/silent/data/onclose/ondisplay/
            //          onshow/close/show；内核多出 badge/vibrate/timestamp/
            //          renotify/requireInteraction/actions，且没有 ondisplay/
            //          navigate/show；
            //       d) NotificationOptions.silent 是 `boolean?`，缺省为 null
            //          （内核缺省 false）。
            //     permission 取值按 WebKit Notification::permission() 的判定顺序
            //     （Notification.cpp）：无 client → Default；非安全上下文 →
            //     Denied。真机 Safari 恒有 client，故首访（安全上下文）为
            //     'default'，非安全上下文为 'denied'——headless Chromium 直接
            //     'denied'，一行读值即差（F-33）。
            //
            //     实现要点：不改写内核原型对象，而是**另建一个原型对象**
            //     （Object.create(EventTarget.prototype)），构造器经 new.target
            //     把实例接上去。原因有二：
            //       * 内核原型上 badge/actions 等成员 configurable:false，delete
            //         删不掉，只能整块换；
            //       * 真机 Notification.prototype 继承 EventTarget.prototype
            //         （Notification : EventTarget），实例要能用 addEventListener /
            //         dispatchEvent。新原型的父链仍指向原生 EventTarget.prototype，
            //         事件系统照常工作。
            //
            //     iOS 整段跳过（F-90）：NotificationsEnabled 的 defaultValue 在
            //     PLATFORM(IOS_FAMILY) 为 false，普通浏览上下文里 Notification
            //     连同 NotificationEvent / NotificationPermission 等**整体缺席**；
            //     这些名字由 removeGlobals 删除。若在此处先建假面、再由删除表
            //     覆盖，等于白做一层且极易漏删。
            try {
                if (!D.ios && typeof Notification === 'function') {
                    var __Notif = Notification;
                    // WebKit：无 client → Default；client 存在且非安全上下文 →
                    // Denied。真机 Safari 恒有 client，因此首访安全上下文读作
                    // 'default'，http 页面读作 'denied'。
                    var __nPerm = 'default';
                    try {
                        if (!window.isSecureContext) { __nPerm = 'denied'; }
                    } catch (e) { __nPerm = 'default'; }
                    // 新原型：父链 = 原生 EventTarget.prototype，成员按 WebKit
                    // 顺序逐个重建（getOwnPropertyNames 的顺序真机可读）。
                    var __NotifP = Object.create(EventTarget.prototype);
                    var __nSlots = new WeakMap();
                    var __nStr = function (v) {
                        return (v === undefined || v === null) ? '' : String(v);
                    };
                    var __nCtor = __maskNative(function Notification(title, opts) {
                        if (!(this instanceof __nCtor)) {
                            throw new TypeError("Failed to construct 'Notification': "
                                + 'Please use the new operator to construct the Notification.');
                        }
                        var o = (opts && typeof opts === 'object') ? opts : {};
                        var data = null;
                        if (o.data !== undefined && o.data !== null) {
                            // 真机按结构化克隆；JSON 往返足以覆盖检测站会读的形状
                            try { data = JSON.parse(JSON.stringify(o.data)); } catch (e) { data = null; }
                        }
                        var d = o.dir;
                        var slot = {
                            title: __nStr(title),
                            dir: (d === 'ltr' || d === 'rtl') ? d : 'auto',
                            lang: __nStr(o.lang),
                            body: __nStr(o.body),
                            tag: __nStr(o.tag),
                            icon: __nStr(o.icon),
                            // NotificationOptions.silent 是 boolean?，缺省 null
                            silent: (o.silent === undefined || o.silent === null)
                                ? null : !!o.silent,
                            data: data,
                            navigate: __nStr(o.navigate),
                            handlers: { onclick: null, onerror: null, onclose: null,
                                        ondisplay: null, onshow: null },
                        };
                        // 实例必须是**真 EventTarget**：addEventListener /
                        // dispatchEvent 在原生实现里校验内部槽，Object.create
                        // 出来的普通对象过不了。`new EventTarget()` 带槽，再把
                        // 原型接到 __NotifP 上即可（instanceof 两链皆真）。
                        var made;
                        try {
                            made = new EventTarget();
                            Object.setPrototypeOf(made, __NotifP);
                        } catch (e) {
                            made = Object.create(__NotifP);
                        }
                        __nSlots.set(made, slot);
                        var nt = new.target;
                        if (nt && nt !== __nCtor) {
                            try { Object.setPrototypeOf(made, nt.prototype); } catch (e) {}
                        }
                        return made;
                    }, 'Notification', null, true);
                    Object.defineProperty(__nCtor, 'length', {
                        value: 1, writable: false, enumerable: false, configurable: true,
                    });
                    Object.defineProperty(__nCtor, 'prototype', {
                        value: __NotifP, writable: false,
                        enumerable: false, configurable: false,
                    });
                    // 静态 permission：数据属性，赋值静默失败
                    try {
                        Object.defineProperty(__nCtor, 'permission', {
                            value: __nPerm, writable: false,
                            enumerable: true, configurable: true,
                        });
                    } catch (e) {}
                    // requestPermission 沿用原生实现（手势/安全上下文语义内核已对）
                    try {
                        Object.defineProperty(__nCtor, 'requestPermission', {
                            value: __Notif.requestPermission, writable: true,
                            enumerable: true, configurable: true,
                        });
                    } catch (e) {}
                    Object.defineProperty(__NotifP, 'constructor', {
                        value: __nCtor, writable: true,
                        enumerable: false, configurable: true,
                    });
                    var __nHandler = function (name) {
                        try {
                            Object.defineProperty(__NotifP, name, {
                                get: __maskNative(function () {
                                    if (!__nSlots.has(this)) {
                                        throw new TypeError('The ' + name + ' getter can '
                                            + 'only be used on instances of Notification');
                                    }
                                    return __nSlots.get(this).handlers[name];
                                }, 'get ' + name),
                                set: __maskNative(function (v) {
                                    if (!__nSlots.has(this)) {
                                        throw new TypeError('The ' + name + ' setter can '
                                            + 'only be used on instances of Notification');
                                    }
                                    __nSlots.get(this).handlers[name] =
                                        (typeof v === 'function'
                                            || (v !== null && typeof v === 'object'))
                                            ? v : null;
                                }, 'set ' + name),
                                enumerable: true, configurable: true,
                            });
                        } catch (e) {}
                    };
                    var __nRo = function (name, read) {
                        try {
                            Object.defineProperty(__NotifP, name, {
                                get: __maskNative(function () {
                                    if (!__nSlots.has(this)) {
                                        throw new TypeError('The ' + name + ' getter can '
                                            + 'only be used on instances of Notification');
                                    }
                                    return read(__nSlots.get(this));
                                }, 'get ' + name),
                                enumerable: true, configurable: true,
                            });
                        } catch (e) {}
                    };
                    // 顺序对齐真机 getOwnPropertyNames：constructor, onclick,
                    // onerror, navigate, title, dir, lang, body, tag, icon,
                    // silent, data, onclose, ondisplay, onshow, close, show
                    __nHandler('onclick');
                    __nHandler('onerror');
                    __nRo('navigate', function (s) { return s.navigate; });
                    __nRo('title', function (s) { return s.title; });
                    __nRo('dir', function (s) { return s.dir; });
                    __nRo('lang', function (s) { return s.lang; });
                    __nRo('body', function (s) { return s.body; });
                    __nRo('tag', function (s) { return s.tag; });
                    __nRo('icon', function (s) { return s.icon; });
                    __nRo('silent', function (s) { return s.silent; });
                    __nRo('data', function (s) { return s.data; });
                    __nHandler('onclose');
                    __nHandler('ondisplay');
                    __nHandler('onshow');
                    // close / show：show 是 WebKit 的 legacy 方法
                    // （Notification.idl 末尾「Legacy. Not in the specification.」），
                    // 内核没有
                    var __nMethod = function (name) {
                        try {
                            Object.defineProperty(__NotifP, name, {
                                value: __maskNative(function () {
                                    if (!__nSlots.has(this)) {
                                        throw new TypeError('Illegal invocation');
                                    }
                                }, name),
                                writable: true, enumerable: true, configurable: true,
                            });
                        } catch (e) {}
                    };
                    __nMethod('close');
                    __nMethod('show');
                    try {
                        Object.defineProperty(__NotifP, Symbol.toStringTag, {
                            value: 'Notification', writable: false,
                            enumerable: false, configurable: true,
                        });
                    } catch (e) {}
                    // 静态 maxActions：WebKit 未实现，整项不存在
                    try {
                        delete __Notif.maxActions;
                        Object.defineProperty(__Notif, 'maxActions', {
                            value: undefined, writable: false,
                            enumerable: false, configurable: true,
                        });
                    } catch (e) {}
                    try {
                        Object.defineProperty(window, 'Notification', {
                            value: __nCtor, writable: true,
                            enumerable: false, configurable: true,
                        });
                    } catch (e) {}
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
        // Navigator.prototype / navigator / Screen.prototype / screen，
        // 仍残留时按类兜底 getter=undefined。
        //
        // 存量档案里的 removeGlobals 可能短于当前清单（新补的项写不进去），
        // 故与平台基线清单合并而非覆盖——与 removeProps 的归一化同理。
        var dropsG = [];
        var __seenG = {};
        var __poolG = (D.removeGlobals || []).concat(D.baseGlobals || []);
        for (var __pi = 0; __pi < __poolG.length; __pi++) {
            if (!__seenG[__poolG[__pi]]) {
                __seenG[__poolG[__pi]] = 1;
                dropsG.push(__poolG[__pi]);
            }
        }
        var __hasOwnG = function (o, n) {
            try { return Object.prototype.hasOwnProperty.call(o, n); } catch (e) { return false; }
        };
        for (var gi = 0; gi < dropsG.length; gi++) {
            var gn = dropsG[gi];
            try { delete window[gn]; } catch (e) {}
            try { delete Window.prototype[gn]; } catch (e) {}
            try { delete Document.prototype[gn]; } catch (e) {}
            try { delete document[gn]; } catch (e) {}
            try { delete Navigator.prototype[gn]; } catch (e) {}
            try { delete navigator[gn]; } catch (e) {}
            try { delete Screen.prototype[gn]; } catch (e) {}
            try { delete screen[gn]; } catch (e) {}
            // 兜底只对「从原型链继承、删不掉」的名字加遮蔽 getter。若目标
            // 对象自己还留着同名 own 属性（内核层不可配置，如 window.chrome），
            // own 属性会遮蔽原型 getter——补了无效，反而给原型凭空添一个
            // 真机没有的名字（F-92 实测：Window.prototype 只剩 'constructor'
            // 时我们却多出 'chrome'，本身就是一条检测面）。
            try {
                if (gn in window && !__hasOwnG(window, gn)) {
                    defineGet(Window.prototype, gn, undefined);
                }
            } catch (e) {}
            try {
                if (gn in document && !__hasOwnG(document, gn)) {
                    defineGet(Document.prototype, gn, undefined);
                }
            } catch (e) {}
            try {
                if (gn in navigator && !__hasOwnG(navigator, gn)) {
                    defineGet(Navigator.prototype, gn, undefined);
                }
            } catch (e) {}
            try {
                if (gn in screen && !__hasOwnG(screen, gn)) {
                    defineGet(Screen.prototype, gn, undefined);
                }
            } catch (e) {}
        }

        // ---- Intl 上的平台差集成员（Intl 既非 window 成员也非原型成员）----
        // v8BreakIterator 挂在 Intl 对象自身（`Object.getOwnPropertyDescriptor(Intl,
        // 'v8BreakIterator')` = {writable:true, enumerable:false, configurable:true}），
        // 按名 delete 即可。带兜底定时器：V8 在个别构建里会于脚本执行期把该成员
        // 惰性装回，0ms 定时器补删一次成本可忽略，且真机 WebKit 本就没有该成员，
        // 删除语义与真机一致。
        var __intlDrops = [];
        for (var di = 0; di < dropsG.length; di++) {
            if (dropsG[di] === 'v8BreakIterator') { __intlDrops.push(dropsG[di]); }
        }
        if (__intlDrops.length && typeof Intl !== 'undefined') {
            var __dropIntl = function () {
                for (var k = 0; k < __intlDrops.length; k++) {
                    try { delete Intl[__intlDrops[k]]; } catch (e) {}
                }
            };
            __dropIntl();
            try { setTimeout(__dropIntl, 0); } catch (e) {}
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

        // ---- iOS：PDF 查看器能力位（BCD safari_ios=16.4，真机恒 true）----
        // 与「plugins 为空」不矛盾：后者是插件面，前者是独立布尔能力位。
        if (D.emptyPlugins && D.ios) {
            defineGet(N, 'pdfViewerEnabled', true);
        }

        // ---- Android Chrome：PDF 面（构建口径 enable_pdf=!is_android）----
        // 真机 Android Chrome：plugins/mimeTypes 为空（上方已统一置空），
        // pdfViewerEnabled 属性存在且恒为 false（插件面与能力位在此同为
        // 阴性，与 iOS 的 true 不同）。桌面内核是 5 个 PDF 插件 + true。
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

        // ---- Android Chrome：downlinkMax / ontypechange（同属 NetInfoDownlinkMax）----
        // 与 type 同一运行期开关（BCD：chrome_android=38，桌面 chrome 仅
        // ChromeOS）——桌面内核上这两个成员同样缺席，也是「桌面装 Android」
        // 的单行判据。取值见指纹库 _ANDROID_DOWNLINK_MAX_MBPS（内核连接
        // 子类型表的 Wi-Fi 值域，非编造常量）。
        // ontypechange 是 EventHandler 属性：真机有该属性、默认 null、可由赋值
        // + dispatchEvent 驱动，故装配为 get/set 访问器对（与 onchange 形态
        // 一致），而不是只读空值。
        if (!D.ios && D.downlinkMax !== null && D.downlinkMax !== undefined
                && typeof NetworkInformation !== 'undefined') {
            defineGet(NetworkInformation.prototype, 'downlinkMax', D.downlinkMax);
            try {
                var __niHandlers = new WeakMap();
                var __niGet = __maskNative(function () {
                    if (!__slotHas(NetworkInformation.prototype, this)) {
                        throw new TypeError('Illegal invocation');
                    }
                    var h = __niHandlers.get(this);
                    return h === undefined ? null : h;
                }, 'get ontypechange');
                var __niSet = __maskNative(function (v) {
                    if (!__slotHas(NetworkInformation.prototype, this)) {
                        throw new TypeError('Illegal invocation');
                    }
                    __niHandlers.set(this, (typeof v === 'function') ? v : null);
                }, 'set ontypechange');
                Object.defineProperty(NetworkInformation.prototype, 'ontypechange', {
                    get: __niGet, set: __niSet, enumerable: true, configurable: true,
                });
            } catch (e) {}
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
                // F-97：Chrome 124+ 高熵提示；存量指纹由 derive_spec 补齐。
                if (ch.formFactors) { he.formFactors = ch.formFactors; }
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
                    // 原生 enumerateDevices 是 Blink 里少见的「不校验接收者」实现：
                    // MediaDevices.prototype.enumerateDevices.call({}) 返回 Promise
                    // 而不抛 Illegal invocation（实测 Chromium 148 原始内核）。
                    // 这里不能加校验，否则反而造出一处与原生不同的行为。
                }, true);
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
        // F-48：真机 iOS 的 visualViewport 被浏览器 UI（地址栏/底部工具栏）
        // inset，`visualViewport.height < innerHeight` 且差值随工具栏折叠变化；
        // 仿真侧两者恒等（同一个 CDP 视口），是「恒等」这个二元特征本身判假。
        // 按真机 Safari 竖屏的工具栏占位（地址栏 52 + 底部 44 ≈ 96 CSS px，
        // iPhone 15 Pro 口径）减去固定差值；动态折叠无法静态模拟，但恒等面
        // 已消除。Android Chrome 的 visualViewport 与 innerHeight 本就相等
        // （地址栏在 layout viewport 之外），勿动。
        if (D.ios && D.viewportHeight && typeof VisualViewport !== 'undefined'
                && VisualViewport.prototype) {
            var __vvInset = 96;
            var __vvH = Math.max(1, D.viewportHeight - __vvInset);
            var __vvOwner = null;
            var __vvp = VisualViewport.prototype;
            for (var __vi = 0; __vvp && __vi < 6; __vi++) {
                var __vd = Object.getOwnPropertyDescriptor(__vvp, 'height');
                if (__vd && typeof __vd.get === 'function') { __vvOwner = __vvp; break; }
                __vvp = Object.getPrototypeOf(__vvp);
            }
            if (__vvOwner) {
                Object.defineProperty(__vvOwner, 'height', {
                    get: __maskNative(function () {
                        if (!__slotHas(__vvOwner, this)) {
                            throw new TypeError('Illegal invocation');
                        }
                        return __vvH;
                    }, 'get height'),
                    enumerable: true, configurable: true,
                });
            }
        }
        if (typeof ScreenOrientation !== 'undefined' && ScreenOrientation.prototype) {
            defineGet(ScreenOrientation.prototype, 'type', 'portrait-primary');
            // F-44（修正 F-18）：angle 由「当前方向 + 自然方向」共同决定，
            // 不是按平台写死的常量。WebKit 的 naturalScreenOrientationType()
            // 在 iOS 上是 iPad → landscape-primary、其余 → portrait-primary，
            // 桌面/电视一律 landscape-primary（ScreenOrientationType.h）；
            // ScreenOrientation::angle() 据此查表——自然竖屏的
            // portrait-primary 是 0、landscape-primary 是 90，自然横屏反过来。
            // 竖屏 + angle=90 这个组合在任何 iPhone 上都不存在，检测站用
            // type/angle 自洽性一行即可判假。
            // angle 只能由上面写死的 type 常量推出：读 screen.orientation.type
            // 会拿到 CDP 原生（宿主窗口）的方向，与强制的 type 不一致。
            // 自然横屏时 primary/secondary 不变、portrait/landscape 互换。
            var __soType = 'portrait-primary';
            var __soAngle;
            if (D.naturalLandscape) {
                __soAngle = (__soType === 'landscape-primary') ? 0
                    : (__soType === 'landscape-secondary') ? 180
                    : (__soType === 'portrait-secondary') ? 270 : 90;
            } else {
                __soAngle = (__soType === 'landscape-primary') ? 90
                    : (__soType === 'landscape-secondary') ? 270
                    : (__soType === 'portrait-secondary') ? 180 : 0;
            }
            defineGet(ScreenOrientation.prototype, 'angle', __soAngle);
        }

        // ---- SpeechSynthesis：移动端语音列表口径 ----
        // iOS Safari 在无用户手势的检测场景返回空列表；宿主 macOS 会暴露上百
        // 个语音（婷婷/Albert/Samantha…），检测站点把这个数量直接作为 OS 判定
        // 输入（BrowserScan 的 "Different operating systems" 即来源于 macOS
        // 语音数 180 vs 期望 0）。
        //
        // F-45：Android 是**反方向**——真机 Chrome Android 预装 Google TTS，
        // getVoices() 返回数十条（lang 形如 zh-CN / cmn-Hans-CN / en-US），
        // 空列表本身即判据。宿主 macOS 上 Blink 返回的是宿主语音（几百条
        // macOS 语音名），同样判假，故 Android 身份也一律改写，只是改成
        // Google TTS 的真实形态。
        try {
            var SS = (typeof SpeechSynthesis !== 'undefined') ? SpeechSynthesis.prototype : null;
            var __voices = (D.android && D.androidVoices) ? D.androidVoices : [];
            if (__voices.length && typeof SpeechSynthesisVoice !== 'undefined'
                    && SpeechSynthesisVoice.prototype) {
                var __makeVoice = function (row) {
                    var v = Object.create(SpeechSynthesisVoice.prototype);
                    Object.defineProperty(v, 'voiceURI', { value: row[0] });
                    Object.defineProperty(v, 'name', { value: row[0] });
                    Object.defineProperty(v, 'lang', { value: row[1] });
                    Object.defineProperty(v, 'localService', { value: row[2] });
                    Object.defineProperty(v, 'default', { value: row[3] });
                    return v;
                };
                var __voiceList = __voices.map(__makeVoice);
                // F-161：默认语音必须与运行期 Intl locale 同语族。表在
                // derive_spec 里已按 locale 选过，这里再兜一道——locale 的
                // 最终值由 CDP setLocaleOverride 决定，若它没落到预期值
                // （例如 setLocaleOverride 失败、或内核另有默认），表侧的
                // 选择就会与实际 Intl 不一致。creepjs 的 voiceLangMismatch
                // 只看这一条交叉一致性，故以运行期真值收敛为准。
                try {
                    var __loc = '';
                    try {
                        __loc = Intl.DateTimeFormat().resolvedOptions().locale || '';
                    } catch (e0) {}
                    var __base = String(__loc).toLowerCase().split('-')[0];
                    if (__base) {
                        var __defIdx = -1, __nDef = 0;
                        for (var __i = 0; __i < __voices.length; __i++) {
                            if (__voices[__i][3]) { __nDef++; __defIdx = __i; }
                        }
                        var __defBase = __defIdx >= 0
                            ? String(__voices[__defIdx][1] || '')
                                .toLowerCase().split('-')[0]
                            : '';
                        if (__nDef !== 1 || __defBase !== __base) {
                            // 先挑完全同 locale，再挑同语族；都没有就不设默认
                            var __pick = -1, __pickFam = -1;
                            for (var __j = 0; __j < __voices.length; __j++) {
                                var __vl = String(__voices[__j][1] || '')
                                    .toLowerCase();
                                if (__vl === String(__loc).toLowerCase()) {
                                    __pick = __j;
                                    break;
                                }
                                if (__pickFam < 0
                                        && __vl.split('-')[0] === __base) {
                                    __pickFam = __j;
                                }
                            }
                            if (__pick < 0) { __pick = __pickFam; }
                            for (var __k = 0; __k < __voiceList.length; __k++) {
                                try {
                                    Object.defineProperty(__voiceList[__k],
                                        'default',
                                        { value: (__k === __pick) });
                                } catch (e1) {}
                            }
                        }
                    }
                } catch (e2) {}
                if (SS && SS.getVoices) {
                    defineFn(SS, 'getVoices', function () {
                        return __voiceList.slice();
                    });
                }
            } else if (SS && SS.getVoices) {
                defineFn(SS, 'getVoices', function () { return []; });
            } else if (window.speechSynthesis) {
                defineFn(window.speechSynthesis, 'getVoices', function () { return []; });
            }
        } catch (e) {}

        // ---- 媒体可播放能力：canPlayType 按 WebKit 容器白名单算法判定 ----
        // 内核是桌面 Chromium（FFmpeg 管线），ogg/webm/opus 一律 "probably"；
        // 真机 iOS Safari 走 AVFoundation，容器白名单里没有 ogg/webm ——
        // 单行 canPlayType('audio/ogg; codecs="vorbis"') 即可判「不是 iOS」。
        //
        // F-143：兜底不再是「表外透传原生」。稀疏表 + 原生兜底会让一切未覆盖
        // 的串继承 Blink 口径（`audio/mp4; codecs="opus"` → probably 是假
        // 阳性；`video/x-matroska` → maybe 是 F-126；版本化 codecs 串漏判是
        // F-124）。现在改为**白名单语义**：__cptIos 按 WebKit
        // MIMETypeCache::canDecodeType 逐条复刻（30 项静态容器白名单 + iOS
        // 动态项 + codecs 谓词），未命中一律 ''。
        // 档案里的 D.codecCapability 仍作为覆盖层（存量指纹的机型特化值），
        // 先查覆盖层、再走算法。
        // 匹配顺序：剥离参数后整串 → 完整串 → 逐个 codecs 标签，均大小写不敏感。
        if (typeof HTMLMediaElement !== 'undefined') {
            try {
                var __cptTable = (function () {
                    var t = {};
                    var src = D.codecCapability || {};
                    for (var k in src) {
                        if (Object.prototype.hasOwnProperty.call(src, k)) {
                            t[String(k).toLowerCase()] = src[k];
                        }
                    }
                    return t;
                })();
                var __cptLookup = function (type) {
                    var s = String(type).toLowerCase();
                    var i = s.indexOf(';');
                    var bare = i >= 0 ? s.slice(0, i) : s;
                    var hit = __cptTable[s];
                    if (hit === undefined) { hit = __cptTable[bare]; }
                    if (hit === undefined) {
                        // codecs 标签逐个匹配（"vp8, vorbis" → 分别查）
                        var ci = s.indexOf('codecs=');
                        if (ci >= 0) {
                            var raw = s.slice(ci + 7).replace(/^["']|["']$/g, '');
                            var parts = raw.split(',');
                            for (var j = 0; j < parts.length; j++) {
                                var tag = parts[j].replace(/^\s+|\s+$/g, '');
                                if (!tag) { continue; }
                                var v = __cptTable[bare + '; codecs="' + tag + '"'];
                                if (v !== undefined) { return v; }
                            }
                        }
                    }
                    return hit;
                };
                var __cptAV1 = !!(D.mseTypes && D.mseTypes.av1);
                var __cptOrig = HTMLMediaElement.prototype.canPlayType;
                defineFn(HTMLMediaElement.prototype, 'canPlayType', function (type) {
                    // WebKit 的 IDL 生成代码对必填参数抛 "Not enough arguments"；
                    // Blink 的文案不同（Android 目标保持内核原样）。
                    if (!arguments.length && D.ios) {
                        throw new TypeError('Not enough arguments');
                    }
                    if (D.ios) {
                        // iOS：算法为主、覆盖层只认「整串精确命中」。
                        // 不能再用上面的三级查找——裸容器键是「容器名查询」的
                        // 答案（audio/mp3 → maybe），拿它回答带 codecs 的串会
                        // 造出假阳性（audio/mp3; codecs="mp3" 应为 probably）；
                        // 逐个 codecs 标签取首个命中也会漏掉同串里另一个不受
                        // 支持的 codec（"hvc1, opus" → 应为 ''）。
                        var exact = __cptTable[String(type).toLowerCase()];
                        if (exact !== undefined) { return exact; }
                        return __cptIos(String(type), { av1: __cptAV1 });
                    }
                    var v = __cptLookup(type);
                    if (v !== undefined) { return v; }
                    return __cptOrig.apply(this, arguments);
                });
            } catch (e) {}
        }

        // ---- PerformanceObserver.supportedEntryTypes：真机 WebKit 只有 8 项 ----
        // WebKit PerformanceObserver.cpp:153 supportedEntryTypes() 按固定顺序
        // 产出 event / first-input / largest-contentful-paint / mark / measure /
        // navigation / paint / resource；桌面 Chromium 多出 element /
        // layout-shift / long-animation-frame / longtask / visibility-state
        // （Blink 专有或 WebKit 未实现），一次静态读取即可判假。
        // IDL 是 `readonly attribute FrozenArray<DOMString>` → 真机描述符为
        // 数据属性 {value, writable:false, enumerable:true, configurable:true}
        // 且数组 Object.isFrozen === true；不能用 getter 模拟（描述符会露馅）。
        if (D.ios && typeof PerformanceObserver !== 'undefined') {
            try {
                var __poTypes = Object.freeze([
                    'event', 'first-input', 'largest-contentful-paint',
                    'mark', 'measure', 'navigation', 'paint', 'resource'
                ]);
                Object.defineProperty(PerformanceObserver, 'supportedEntryTypes', {
                    value: __poTypes, writable: false,
                    enumerable: true, configurable: true,
                });
            } catch (e) {}
        }

        // ---- WebGL：非遮蔽厂商/渲染器 + 参数上限/扩展集 ----
        // 内核原生 GPU 伪装不含移动平台：宿主是 Apple M 系列（Metal/ANGLE），
        // 渲染器字符串必须换成声明的 Adreno/Mali；同时「渲染器 × 参数上限 ×
        // 扩展集」三元组也要自洽——宿主扩展集带桌面专属的 S3TC/BPTC/RGTC/
        // polygon_mode（移动 GPU 驱动不导出），检测站按 gpuinfo.org 数据集
        // 比对可直接命中。上限/剔除清单见指纹库 _GL_LIMITS_MOBILE /
        // _GL_EXT_DESKTOP_ONLY（按 GPU 家族，非按机型）。
        if ((D.webgl && D.webgl.renderer) || D.webglProfile) {
            try {
                var __glLimits = (D.webglProfile && D.webglProfile.limits) || {};
                var __glDrop = (D.webglProfile && D.webglProfile.dropExtensions) || [];
                // 枚举值 → 名称：直接取构造器上的同名常量，避免手抄魔数出错
                var __glEnumOf = function (Ctor, name) {
                    try { var v = Ctor[name]; return (typeof v === 'number') ? v : null; }
                    catch (e) { return null; }
                };
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
                        // F-16 / F-91：VERSION / SHADING_LANGUAGE_VERSION 按枚举
                        // 显式映射真值串。不能用「剥尾部括号」的通用正则——
                        // WebGL1 的 SHADING_LANGUAGE_VERSION 真值自带版本后缀
                        // `WebGL GLSL ES 1.0 (1.0)`，一刀切会把它剥成
                        // `WebGL GLSL ES 1.0`；WebGL2 的 `WebGL GLSL ES 3.00`
                        // 只是碰巧剥对。取值先走原生路径（保持调用开销同量级）。
                        if (D.ios && (p === __glEnumOf(Ctor, 'VERSION')
                                      || p === __glEnumOf(Ctor, 'SHADING_LANGUAGE_VERSION'))) {
                            try { orig.apply(this, arguments); } catch (e) {}
                            var __gl2 = (typeof WebGL2RenderingContext !== 'undefined'
                                         && Ctor === WebGL2RenderingContext);
                            if (p === __glEnumOf(Ctor, 'VERSION')) {
                                return __gl2 ? 'WebGL 2.0' : 'WebGL 1.0';
                            }
                            return __gl2 ? 'WebGL GLSL ES 3.00'
                                         : 'WebGL GLSL ES 1.0 (1.0)';
                        }
                        // 上限覆盖：先走一遍原生路径（耗时/状态机对齐），
                        // 原生返回 null（非法枚举）时保持 null——移动驱动对
                        // 未知枚举同样返回 null，不能凭空造值。
                        if (typeof p === 'number') {
                            var nat = orig.apply(this, arguments);
                            for (var ln in __glLimits) {
                                if (!Object.prototype.hasOwnProperty.call(__glLimits, ln)) { continue; }
                                var en = __glEnumOf(Ctor, ln);
                                if (en !== null && en === p && nat !== null && nat !== undefined) {
                                    var lv = __glLimits[ln];
                                    return (lv && lv.length !== undefined) ? new Int32Array(lv) : lv;
                                }
                            }
                            return nat;
                        }
                        return orig.apply(this, arguments);
                    }, 'getParameter');
                    Object.defineProperty(Ctor.prototype, 'getParameter', {
                        value: wrapped, writable: true, enumerable: true, configurable: true,
                    });

                    // 扩展集剔除：getSupportedExtensions 过滤 + getExtension 返回 null。
                    // 注意用 toLowerCase 比较：getExtension 对大小写不敏感。
                    if (__glDrop.length && Ctor.prototype.getSupportedExtensions) {
                        var __dropSet = {};
                        for (var di = 0; di < __glDrop.length; di++) {
                            __dropSet[String(__glDrop[di]).toLowerCase()] = true;
                        }
                        var origList = Ctor.prototype.getSupportedExtensions;
                        var origGetExt = Ctor.prototype.getExtension;
                        defineFn(Ctor.prototype, 'getSupportedExtensions', function () {
                            var all = origList.apply(this, arguments);
                            if (!all) { return all; }
                            var out = [];
                            for (var ai = 0; ai < all.length; ai++) {
                                if (!__dropSet[String(all[ai]).toLowerCase()]) { out.push(all[ai]); }
                            }
                            return out;
                        });
                        defineFn(Ctor.prototype, 'getExtension', function (name) {
                            if (name !== undefined && name !== null
                                    && __dropSet[String(name).toLowerCase()]) {
                                return null;
                            }
                            return origGetExt.apply(this, arguments);
                        });
                    }

                    // F-23：真机 iOS 有、内核不导出的扩展（WEBKIT_ 前缀 PVRTC）。
                    // 反向补齐：列表追加 + getExtension 返回带枚举常量的对象。
                    var __glAdd = (D.webglProfile && D.webglProfile.addExtensions) || [];
                    if (__glAdd.length && Ctor.prototype.getSupportedExtensions) {
                        var __addList = Ctor.prototype.getSupportedExtensions;
                        var __addGet = Ctor.prototype.getExtension;
                        var __addSet = {};
                        for (var __aj = 0; __aj < __glAdd.length; __aj++) {
                            __addSet[String(__glAdd[__aj]).toLowerCase()] = __glAdd[__aj];
                        }
                        defineFn(Ctor.prototype, 'getSupportedExtensions', function () {
                            var all = __addList.apply(this, arguments) || [];
                            var out = [];
                            var seen = {};
                            for (var __ak = 0; __ak < all.length; __ak++) {
                                var nm = String(all[__ak]);
                                if (!seen[nm.toLowerCase()]) {
                                    seen[nm.toLowerCase()] = 1;
                                    out.push(all[__ak]);
                                }
                            }
                            for (var __am = 0; __am < __glAdd.length; __am++) {
                                var an = String(__glAdd[__am]);
                                if (!seen[an.toLowerCase()]) {
                                    seen[an.toLowerCase()] = 1;
                                    out.push(an);
                                }
                            }
                            return out;
                        });
                        defineFn(Ctor.prototype, 'getExtension', function (name) {
                            var key = (name === undefined || name === null)
                                ? '' : String(name).toLowerCase();
                            if (Object.prototype.hasOwnProperty.call(__addSet, key)) {
                                var ext = {};
                                if (key === 'webkit_webgl_compressed_texture_pvrtc') {
                                    // WebKit WebGLCompressedTexturePVRTC 枚举值
                                    ext.COMPRESSED_RGB_PVRTC_4BPPV1_IMG = 0x8C00;
                                    ext.COMPRESSED_RGB_PVRTC_2BPPV1_IMG = 0x8C01;
                                    ext.COMPRESSED_RGBA_PVRTC_4BPPV1_IMG = 0x8C02;
                                    ext.COMPRESSED_RGBA_PVRTC_2BPPV1_IMG = 0x8C03;
                                }
                                return ext;
                            }
                            return __addGet.apply(this, arguments);
                        });
                    }

                    // F-22：真机 WebKit 的 WebGLContextAttributes 只有 8 个字段
                    // （alpha/antialias/depth/failIfMajorPerformanceCaveat/
                    // powerPreference/premultipliedAlpha/preserveDrawingBuffer/
                    // stencil）；desynchronized / xrCompatible 是 Blink 扩展。
                    // Object.keys(getContextAttributes()) 一行可判，返回值是
                    // 每次新建的字典（原生实现），直接删键即可。
                    if (D.ios && Ctor.prototype.getContextAttributes) {
                        var __origAttrs = Ctor.prototype.getContextAttributes;
                        defineFn(Ctor.prototype, 'getContextAttributes', function () {
                            var attrs = __origAttrs.apply(this, arguments);
                            if (!attrs) { return attrs; }
                            try { delete attrs.desynchronized; } catch (e) {}
                            try { delete attrs.xrCompatible; } catch (e) {}
                            return attrs;
                        });
                    }
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

        // ---- AudioContext.state：移动端首访恒 suspended（F-17 / F-54）----
        // iOS 自动播放策略：AudioContext 构造后停在 suspended，必须在用户手势
        // 内 resume() 才转 running；桌面 Chromium（尤其 headless）直接 running。
        // 手势前一律 suspended，手势后透传原生值（页面自己 resume 才变 running
        // 的语义由内核保持）。
        // F-54：Android 同策略——真机 Chrome Android 无用户手势时新建
        // AudioContext 也是 suspended（与桌面 Chrome 的 autoplay policy 一致），
        // 而内核 headless 直接 running。两端共用同一段改写。
        if (D.ios || D.android) {
            try {
                var __AC = window.AudioContext || window.webkitAudioContext;
                if (__AC && __AC.prototype) {
                    var __acOwner = null;
                    var __acDesc = null;
                    var __ap = __AC.prototype;
                    for (var __ai = 0; __ap && __ai < 6; __ai++) {
                        var __ad = Object.getOwnPropertyDescriptor(__ap, 'state');
                        if (__ad && typeof __ad.get === 'function') {
                            __acOwner = __ap; __acDesc = __ad; break;
                        }
                        __ap = Object.getPrototypeOf(__ap);
                    }
                    if (__acOwner) {
                        var __acGesture = false;
                        var __acMark = function () { __acGesture = true; };
                        try {
                            document.addEventListener('pointerdown', __acMark, true);
                            document.addEventListener('mousedown', __acMark, true);
                            document.addEventListener('touchend', __acMark, true);
                            document.addEventListener('keydown', __acMark, true);
                        } catch (e) {}
                        Object.defineProperty(__acOwner, 'state', {
                            get: __maskNative(function () {
                                if (!__slotHas(__acOwner, this)) {
                                    throw new TypeError('Illegal invocation');
                                }
                                if (!__acGesture) { return 'suspended'; }
                                return __acDesc.get.call(this);
                            }, 'get state'),
                            enumerable: true, configurable: true,
                        });
                    }
                }
            } catch (e) {}
        }

        // ---- 返回值改写：canvas 编码 / MediaRecorder / 2D 上下文属性 ----
        // 真机 iOS 的这几处是「同引擎、不同编解码与属性面」，内核值原样暴露
        // 即可一行判假（实测对照：fp-chromium 原生 vs Playwright WebKit 18.6）：
        //   F-52 webp 编码：真机回退 PNG（`toDataURL('image/webp')` 返回
        //        data:image/png）；Blink 原生编 webp。avif 无需处理——内核
        //        本就不编 avif，回退 PNG 与真机 26.x 同形（F-53 的 27+ 编码
        //        器差异无补丁可造，登记为已知面）。
        //   F-55 / F-131 MediaRecorder：真机按 MediaRecorderPrivateAVFImpl
        //        的「容器分支 + codecs 白名单」判定（mp4 与 webm 两族，
        //        裸容器为 true）；Blink 全 webm 系 true，且
        //        video/webm;codecs=h264 真机 false。
        //   F-56 2D getContextAttributes：真机 3 键（colorSpace/
        //        desynchronized/willReadFrequently），Blink 多 alpha/
        //        colorType/toneMapping。
        if (D.ios) {
            try {
                // ---- canvas 编码 MIME 回退 ----
                // toDataURL(type, quality) 的类型在实参 0；toBlob(cb, type,
                // quality) 在实参 1；convertToBlob 在实参 0 的 options.type。
                // 三处都把 webp 改写成 png，让内核按 PNG 出图。
                var __asPng = function (t) {
                    return /^\s*image\/webp/i.test(String(t || '')) ? 'image/png' : t;
                };
                var __wrapTypeArg = function (orig, idx) {
                    if (typeof orig !== 'function') { return orig; }
                    return function () {
                        var args = Array.prototype.slice.call(arguments);
                        // idx 是实参下标：单个实参的调用 args.length == idx + 1，
                        // 所以判据必须是 `> idx` 的取反（即 `>= idx` 且已存在）。
                        if (args.length >= idx + 1 && typeof args[idx] === 'string') {
                            args[idx] = __asPng(args[idx]);
                        }
                        return orig.apply(this, args);
                    };
                };
                if (typeof HTMLCanvasElement !== 'undefined'
                        && HTMLCanvasElement.prototype.toDataURL) {
                    defineFn(HTMLCanvasElement.prototype, 'toDataURL',
                             __wrapTypeArg(HTMLCanvasElement.prototype.toDataURL, 0),
                             true);
                }
                if (typeof HTMLCanvasElement !== 'undefined'
                        && HTMLCanvasElement.prototype.toBlob) {
                    defineFn(HTMLCanvasElement.prototype, 'toBlob',
                             __wrapTypeArg(HTMLCanvasElement.prototype.toBlob, 1),
                             true);
                }
                if (typeof OffscreenCanvas !== 'undefined'
                        && OffscreenCanvas.prototype.convertToBlob) {
                    var __ctbOrig = OffscreenCanvas.prototype.convertToBlob;
                    defineFn(OffscreenCanvas.prototype, 'convertToBlob', function (opts) {
                        var args = Array.prototype.slice.call(arguments);
                        if (opts && typeof opts === 'object' && typeof opts.type === 'string') {
                            var copy = {};
                            for (var k in opts) {
                                if (Object.prototype.hasOwnProperty.call(opts, k)) {
                                    copy[k] = opts[k];
                                }
                            }
                            copy.type = __asPng(opts.type);
                            args[0] = copy;
                        }
                        return __ctbOrig.apply(this, args);
                    }, true);
                }
                // ---- MediaRecorder.isTypeSupported：WebKit 口径 ----
                // 按 MediaRecorderPrivateAVFImpl::isTypeSupported 逐条复刻：
                //   1. 原始串为空 → true；容器为空 → true。
                //   2. ContentType 解析：容器取首个 ';' 之前并 trim；codecs
                //      参数按 WebKit 的 parameter() 取（优先取引号对内，否则
                //      到下一个 ';'），再按 ',' 切分并逐项 trim，**空项丢弃**
                //      （StringView::split 默认不产生空项）。
                //   3. mp4 族：每个 codec 须命中 avc1* / hev1.* / hvc1.* /
                //      opus / pcm / alac / mp4a* 之一，否则 false；裸容器 true。
                //      av01.* 受 webCodecsAV1Enabled 门控（默认关）→ 不认。
                //   4. webm 族：vp09*（非 vp09.02*）/ vp9 / vp9.0、vp09.02*、
                //      vp08* / vp8 / vp8.0、opus 命中即过；avc1*/pcm 仅在
                //      LimitedMatroskaSupport 下成立（默认关）→ 不认。
                //   5. 其余容器一律 false（含 x-matroska、ogg、application/*）。
                // 大小写：容器与 avc1/mp4a 前缀按 ASCII 忽略大小写比较，
                // 其余 codec 比较（opus/pcm/alac/hev1./hvc1./vp8/vp9）区分大小写。
                // 归一化只 trim ASCII 空白，**不能**整串去空格或去引号。
                if (typeof MediaRecorder !== 'undefined'
                        && typeof MediaRecorder.isTypeSupported === 'function') {
                    var __ws = ' \t\n\r\f';
                    var __trim = function (s) {
                        var a = 0, b = s.length;
                        while (a < b && __ws.indexOf(s.charAt(a)) !== -1) { a++; }
                        while (b > a && __ws.indexOf(s.charAt(b - 1)) !== -1) { b--; }
                        return s.slice(a, b);
                    };
                    var __lower = function (s) {
                        return s.replace(/[A-Z]+/g, function (m) {
                            return m.toLowerCase();
                        });
                    };
                    var __mrContainer = function (raw) {
                        var i = raw.indexOf(';');
                        return __trim(i === -1 ? raw : raw.slice(0, i));
                    };
                    // 复刻 ContentType::parameter('codecs')
                    var __mrParam = function (raw) {
                        var semi = raw.indexOf(';');
                        if (semi === -1) { return ''; }
                        var rest = raw.slice(semi + 1);
                        var at = __lower(rest).indexOf('codecs');
                        if (at === -1) { return ''; }
                        var eq = raw.indexOf('=', semi + 1 + at + 6);
                        if (eq === -1) { return ''; }
                        var q1 = raw.indexOf('"', eq + 1);
                        var q2 = q1 === -1 ? -1 : raw.indexOf('"', q1 + 1);
                        if (q1 !== -1 && q2 !== -1) {
                            return __trim(raw.slice(q1 + 1, q2));
                        }
                        var end = raw.indexOf(';', eq + 1);
                        return __trim(raw.slice(eq + 1,
                                                end === -1 ? raw.length : end));
                    };
                    var __mrCodecs = function (raw) {
                        var v = __mrParam(raw);
                        if (!v) { return []; }
                        var parts = v.split(',');
                        var out = [];
                        for (var i = 0; i < parts.length; i++) {
                            var p = __trim(parts[i]);
                            // WebKit 的 split 不产出空项
                            if (p) { out.push(p); }
                        }
                        return out;
                    };
                    var __mrPrefixCI = function (codec, pfx) {
                        return __lower(codec.slice(0, pfx.length)) === pfx;
                    };
                    var __mrMp4CodecOk = function (codec) {
                        return __mrPrefixCI(codec, 'avc1')
                            // av01.* 需 webCodecsAV1Enabled（默认关）→ 不认
                            || codec.indexOf('hev1.') === 0
                            || codec.indexOf('hvc1.') === 0
                            || codec === 'opus'   // AVASSETWRITER_WITH_OPUS_SUPPORTED
                            || codec === 'pcm'
                            || codec === 'alac'
                            || __mrPrefixCI(codec, 'mp4a');
                    };
                    var __mrWebmCodecOk = function (codec) {
                        var isVP90 = (codec.indexOf('vp09') === 0
                                      || codec === 'vp9'
                                      || codec === 'vp9.0')
                            && codec.indexOf('vp09.02') !== 0;
                        var isVP92 = codec.indexOf('vp09.02') === 0;
                        var isVP8 = codec.indexOf('vp08') === 0
                            || codec === 'vp8' || codec === 'vp8.0';
                        return isVP90 || isVP92 || isVP8 || codec === 'opus';
                    };
                    defineFn(MediaRecorder, 'isTypeSupported', function (type) {
                        if (arguments.length < 1) {
                            // WebKit 无参抛 Not enough arguments（Blink 文案不同）
                            throw new TypeError('Not enough arguments');
                        }
                        // WebKit 的 DOMString 转换对 Symbol 抛错；V8 的
                        // String(Symbol()) 不抛，需手工对齐。
                        if (typeof type === 'symbol') {
                            throw new TypeError(
                                'Cannot convert a symbol to a string');
                        }
                        var raw = String(type);
                        if (raw === '') { return true; }
                        var container = __mrContainer(raw);
                        if (container === '') { return true; }
                        var lc = __lower(container);
                        var codecs = __mrCodecs(raw);
                        var i;
                        if (lc === 'audio/mp4' || lc === 'video/mp4') {
                            for (i = 0; i < codecs.length; i++) {
                                if (!__mrMp4CodecOk(codecs[i])) { return false; }
                            }
                            return true;
                        }
                        if (lc === 'audio/webm' || lc === 'video/webm') {
                            for (i = 0; i < codecs.length; i++) {
                                if (!__mrWebmCodecOk(codecs[i])) { return false; }
                            }
                            return true;
                        }
                        return false;
                    }, true);
                }
                // ---- MediaSource.isTypeSupported：MSE 独立真值表（F-142）----
                // MediaSource 与 ManagedMediaSource 各有**自有**的静态
                // `isTypeSupported`（实测两者 length 均为 1，函数不校验 this），
                // 必须分别包装。真值表与 canPlayType 不同源（见模块级注释），
                // 不能复用 D.codecCapability。
                if (typeof MediaSource !== 'undefined'
                        && typeof MediaSource.isTypeSupported === 'function') {
                    defineFn(MediaSource, 'isTypeSupported', function (t) {
                        if (!arguments.length) {
                            throw new TypeError('Not enough arguments');
                        }
                        return __mseSupports(t, D.mseTypes);
                    }, true);
                }
                if (typeof ManagedMediaSource !== 'undefined'
                        && typeof ManagedMediaSource.isTypeSupported === 'function') {
                    defineFn(ManagedMediaSource, 'isTypeSupported', function (t) {
                        if (!arguments.length) {
                            throw new TypeError('Not enough arguments');
                        }
                        return __mseSupports(t, D.mseTypes);
                    }, true);
                }
                // ---- WebCodecs 静态面（F-132）：主世界这四个接口是原生
                // Blink 类，isConfigSupported 的 codec 矩阵与异常文案双泄漏。
                // 只包静态方法，保留原生构造器/原型——VideoFrame、
                // EncodedVideoChunk 的实例品牌校验依赖原生内部槽。
                ['AudioDecoder', 'AudioEncoder', 'VideoDecoder', 'VideoEncoder']
                    .forEach(function (name) {
                        try {
                            var Ctor = window[name];
                            if (!Ctor || typeof Ctor.isConfigSupported
                                    !== 'function') { return; }
                            __staticW(Ctor, 'isConfigSupported',
                                      __wcIcsImpl(name, __wcIcsKind(name)), 1);
                        } catch (e) {}
                    });
                // ---- mediaCapabilities 原型方法（F-135）----
                // 构造器/实例/属性面保持原生（SameObject 与 Illegal constructor
                // 都对），只换两个原型方法；品牌校验由 __mc 按 WebKit 口径做
                // （异步 rejection，文案 "Can only call MediaCapabilities.…"）。
                try {
                    var __mcProto = window.MediaCapabilities
                        && window.MediaCapabilities.prototype;
                    var __mcInst = navigator.mediaCapabilities;
                    if (__mcProto && __mcInst) {
                        var __mcOpt = { av1: !!D.av1 };
                        defineFn(__mcProto, 'decodingInfo', function (config) {
                            return __mc.dec(this, arguments, __mcInst, __mcOpt);
                        }, true);
                        defineFn(__mcProto, 'encodingInfo', function (config) {
                            return __mc.enc(this, arguments, __mcInst, __mcOpt);
                        }, true);
                    }
                } catch (e) {}
                // ---- 2D getContextAttributes：真机 3 键 ----
                if (typeof CanvasRenderingContext2D !== 'undefined'
                        && CanvasRenderingContext2D.prototype.getContextAttributes) {
                    var __ctx2dOrig = CanvasRenderingContext2D.prototype
                        .getContextAttributes;
                    defineFn(CanvasRenderingContext2D.prototype,
                             'getContextAttributes', function () {
                        var attrs = __ctx2dOrig.apply(this, arguments);
                        if (!attrs) { return attrs; }
                        try { delete attrs.alpha; } catch (e) {}
                        try { delete attrs.colorType; } catch (e) {}
                        try { delete attrs.toneMapping; } catch (e) {}
                        return attrs;
                    }, true);
                }
            } catch (e) {}
        }

        // ---- 事件原型 legacy 面（F-58 / F-59）----
        // WebKit 把 pageX/pageY/layerX/layerY 留在 UIEvent.prototype（实测
        // UIEvent 实例可读，初值 0），Blink 已挪到 MouseEvent；AnimationEvent/
        // TransitionEvent 在 WebKit 上多一个 legacy `animation` 属性（恒 null）。
        // Android 侧与真机 Chrome Android 同引擎口径，勿动。
        if (D.ios) {
            try {
                if (typeof UIEvent !== 'undefined' && UIEvent.prototype) {
                    ['pageX', 'pageY', 'layerX', 'layerY'].forEach(function (nm) {
                        if (nm in UIEvent.prototype) { return; }
                        Object.defineProperty(UIEvent.prototype, nm, {
                            get: __maskNative(function () { return 0; },
                                              'get ' + nm),
                            enumerable: true, configurable: true,
                        });
                    });
                }
                if (typeof AnimationEvent !== 'undefined' && AnimationEvent.prototype
                        && !('animation' in AnimationEvent.prototype)) {
                    Object.defineProperty(AnimationEvent.prototype, 'animation', {
                        get: __maskNative(function () { return null; },
                                          'get animation'),
                        enumerable: true, configurable: true,
                    });
                }
                if (typeof TransitionEvent !== 'undefined' && TransitionEvent.prototype
                        && !('animation' in TransitionEvent.prototype)) {
                    Object.defineProperty(TransitionEvent.prototype, 'animation', {
                        get: __maskNative(function () { return null; },
                                          'get animation'),
                        enumerable: true, configurable: true,
                    });
                }
            } catch (e) {}
        }

        // ---- execCommand / queryCommandSupported 的手势与平台口径（F-60/F-61）----
        // 真机 iOS 无用户手势、无选区时 copy/cut 返回 false（WebKit 实测）；
        // Blink 在 CDP 挂载环境里直接返回 true。paste 命令的 supported 面
        // WebKit 报 true、Blink 报 false。其余命令三方一致，一律透传。
        if (D.ios) {
            try {
                if (document.execCommand) {
                    var __execOrig = document.execCommand;
                    var __clipCmds = { copy: 1, cut: 1, paste: 1 };
                    defineFn(document, 'execCommand', function (cmd) {
                        var c = String(cmd || '').toLowerCase();
                        if (__clipCmds[c]) {
                            var sel = '';
                            try { sel = String(window.getSelection() || ''); } catch (e) {}
                            if (!sel) { return false; }
                        }
                        return __execOrig.apply(this, arguments);
                    }, true);
                }
                if (document.queryCommandSupported) {
                    var __qcsOrig = document.queryCommandSupported;
                    defineFn(document, 'queryCommandSupported', function (cmd) {
                        if (String(cmd || '').toLowerCase() === 'paste') { return true; }
                        return __qcsOrig.apply(this, arguments);
                    }, true);
                }
            } catch (e) {}
        }

        // ---- Intl.DateTimeFormat 的 zh/ja 排版口径（F-43）----
        // WebKit 的 libicucore 与 Blink 的 ICU 在 CLDR 版本上差一档，实测
        // （Playwright WebKit 27.2 vs fp-chromium 148，America/Los_Angeles，
        // zh/ja 全部区域变体 + 24 小时逐时扫描）。三条独立规则：
        //
        // ① 星期前空格（full dateStyle 与「月 + 日 + 星期」显式组合）：
        //    zh-Hans 组（zh/zh-CN/zh-Hans*/zh-SG/zh-Hans-MY）与 zh-HK/MO/Hant-HK
        //    真机「10月19日 星期一」，Chromium「10月19日星期一」；
        //    ja 真机「2026年10月19日 月曜日」，Chromium「…日曜日」。
        //    只在星期型别为 long/short 时插；narrow 两边一致（「19 一」）。
        //    日字段必须带「日」后缀（zh-Hans 的 numeric day 有、zh-HK 没有），
        //    zh-TW/Hant-TW 两边一致（不加空格），故按 locale 分组。
        // ② 时区名与时刻之间（24 小时制，zh-Hans 组）：真机无空格。
        // ③ 12 小时制 dayPeriod：真机按本地小时给出凌晨/早上/上午/中午/下午/
        //    晚上（zh-TW 05-07 时用清晨），Chromium 只有上午/下午两个值。
        // ②③ 的 locale 面比 ① 宽：zh-HK 的 dayPeriod 两边一致，zh-TW 的
        // dayPeriod 不一致但 ② 的时区名位置是 CLDR 结构差异（真机把时区名
        // 提到 dayPeriod 之前），属于另一类问题，这里不碰。
        // 其余 locale（ko-KR 时区名、ar-EG 分隔符、hi-IN 月份名、vi-VN 的
        // 「ngày」…）是 CLDR 数据本身，不在此处改写。
        if (D.ios && typeof Intl !== 'undefined'
                && Intl.DateTimeFormat && Intl.DateTimeFormat.prototype
                && Intl.DateTimeFormat.prototype.formatToParts) {
            try {
                var __dtfProto = Intl.DateTimeFormat.prototype;
                // 拆 BCP-47 子标签：语言 / 文字（Hans、Hant）/ 区域（CN、HK…）
                var __dtfTag = function (loc) {
                    var a = String(loc || '').split('-');
                    var t = { lang: (a[0] || '').toLowerCase(),
                              script: '', region: '' };
                    for (var i = 1; i < a.length; i++) {
                        var s = a[i];
                        if (/^[A-Za-z]{4}$/.test(s)) {
                            t.script = s.charAt(0).toUpperCase()
                                + s.slice(1).toLowerCase();
                        } else if (/^([A-Za-z]{2}|\d{3})$/.test(s)) {
                            t.region = s.toUpperCase();
                        }
                    }
                    return t;
                };
                // 真机把 zh 拆成几套排版模板，空格与时区名位置各不相同：
                // 简-CN（大陆/无区域）、简-SG（新马）、港、澳、港繁、台繁
                var __dtfGroup = function (ro) {
                    var t = __dtfTag(ro && ro.locale);
                    if (t.lang === 'ja') { return 'ja'; }
                    if (t.lang !== 'zh') { return null; }
                    if (t.script === 'Hant') {
                        if (t.region === 'HK') { return 'hantHK'; }
                        if (t.region === 'MO') { return 'mo'; }
                        return 'tw';
                    }
                    if (t.region === 'HK') { return 'hk'; }
                    if (t.region === 'MO') { return 'mo'; }
                    if (t.region === 'TW') { return 'tw'; }
                    if (t.region === 'SG' || t.region === 'MY') {
                        return 'hansSG';
                    }
                    return 'hansCN';
                };
                // 真机 12 小时制 dayPeriod（下标 = 本地小时）。台繁的
                // 05-07 时是「清晨」；港澳两边一致，不进表。
                var __dtfDpTable = {
                    hans: ['凌晨', '凌晨', '凌晨', '凌晨', '凌晨',
                           '早上', '早上', '早上', '上午', '上午', '上午', '上午',
                           '中午',
                           '下午', '下午', '下午', '下午', '下午', '下午',
                           '晚上', '晚上', '晚上', '晚上', '晚上'],
                    tw: ['凌晨', '凌晨', '凌晨', '凌晨', '凌晨',
                         '清晨', '清晨', '清晨', '上午', '上午', '上午', '上午',
                         '中午',
                         '下午', '下午', '下午', '下午', '下午', '下午',
                         '晚上', '晚上', '晚上', '晚上', '晚上'],
                };
                var __dtfStyle = function (dtf) {
                    var ro;
                    try { ro = dtf.resolvedOptions(); } catch (e) { return null; }
                    if (!ro) { return null; }
                    var fam = __dtfGroup(ro);
                    if (!fam) { return null; }
                    var hasHour = ro.hour !== undefined || !!ro.timeStyle;
                    var h23 = ro.hourCycle === 'h23' || ro.hourCycle === 'h24';
                    // dateStyle 路径下 resolvedOptions 不回报 weekday，
                    // full 档一定带星期，故按 dateStyle 补判。
                    var wk = ro.weekday !== undefined
                        ? ro.weekday : (ro.dateStyle === 'full' ? 'long' : null);
                    var dsPath = !!(ro.dateStyle || ro.timeStyle);
                    // 时区名是 timeStyle 带出来的，还是显式 timeZoneName
                    var tzFromStyle = ro.timeStyle !== undefined
                        && ro.timeZoneName === undefined;
                    var hans = fam === 'hansCN' || fam === 'hansSG';
                    // 「日」后补空格：简-CN/简-SG/港/澳/港繁；台繁不补；
                    // 日文只在有月份时补（无月份时「日」整体换成空格）
                    var daySp = hans || fam === 'hk' || fam === 'mo'
                        || fam === 'hantHK'
                        || (fam === 'ja' && ro.month !== undefined);
                    // 简-CN 的 12 小时制 dateStyle 不补
                    if (fam === 'hansCN' && dsPath && hasHour && !h23) {
                        daySp = false;
                    }
                    // 台繁：时刻带分钟才提前（只有时/秒时仍留在后面），
                    // 台繁与港繁 timeStyle 路径一律提前；港/澳 12 小时制
                    // 且只有「时:分」时仍留在括号里，其余组合都提前
                    var tzMove;
                    if (fam === 'tw') {
                        tzMove = dsPath || ro.minute !== undefined;
                    } else if (fam === 'hantHK') {
                        tzMove = dsPath || !(ro.minute !== undefined
                            && ro.second === undefined && !h23);
                    } else if (fam === 'hk' || fam === 'mo') {
                        tzMove = dsPath ? h23 : !(ro.minute !== undefined
                            && ro.second === undefined && !h23);
                    }
                    return {
                        tz: ro.timeZone,
                        daySp: daySp,
                        // 日文 dateStyle 路径下「日」后有空格
                        jaDsSp: fam === 'ja' && dsPath,
                        // 只有「日」而无月份时，简-CN/简-SG 把「日」换成空格
                        dayToSp: hans && ro.day !== undefined
                            && ro.month === undefined,
                        // 简-CN/简-SG 在「星期 → 时刻」之间补空格（带分钟）；
                        // 澳门相反，模板里本有空格要去掉
                        wkSpAdd: hans && wk !== null && hasHour && !dsPath
                            && ro.minute !== undefined,
                        wkSpDrop: fam === 'mo' && wk !== null
                            && !dsPath && ro.day === undefined,
                        // 日文带分钟时星期前置
                        jaMove: fam === 'ja' && hasHour
                            && ro.minute !== undefined,
                        // timeStyle 带出的时区名后真机无空格（简-CN/简-SG）
                        tzGapDrop: hans && tzFromStyle,
                        tzMove: tzMove,
                        // ③ 12 小时制且当前语言有分区 dayPeriod 表
                        dp: (hans || fam === 'mo') && hasHour && !h23
                            ? __dtfDpTable.hans
                            : (fam === 'tw' && hasHour && !h23
                                ? __dtfDpTable.tw : null),
                        // 日文 12 小时制里 12 时写作「0 时」
                        jaHour0: fam === 'ja' && hasHour && !h23,
                        // 台繁 24 小时制只有「时」时引擎写作「h」，真机是「時」
                        twHourSuffix: fam === 'tw' && hasHour && h23
                            && ro.minute === undefined,
                        // 显式 hour 时 dayPeriod 由 hour 决定，不能用日期取
                        explicitHour: ro.hour !== undefined,
                    };
                };
                var __dtfHour = function (parts) {
                    for (var i = 0; i < parts.length; i++) {
                        if (parts[i].type === 'hour') {
                            var h = parseInt(parts[i].value, 10);
                            return isNaN(h) ? null : h;
                        }
                    }
                    return null;
                };
                // 同一时区的 24 小时制读数：dayPeriod 按本地小时取，不能受
                // 运行环境时区影响，故用 formatter 自己的 timeZone 另建一个。
                var __dtfH23 = {};
                var __dtfLocalHour = function (tz, d) {
                    var key = String(tz || '');
                    var f = __dtfH23[key];
                    if (!f) {
                        try {
                            f = new Intl.DateTimeFormat('en-US', {
                                timeZone: tz, hour: '2-digit', hourCycle: 'h23',
                            });
                        } catch (e) { return null; }
                        __dtfH23[key] = f;
                    }
                    var h = parseInt(f.format(d), 10);
                    return isNaN(h) ? null : h;
                };
                // 显式 hour：内核给出的 1-12（h11 为 0-11）+ dayPeriod 定上下午
                var __dtfDpFor = function (table, hour, parts) {
                    var pm = false;
                    for (var i = 0; i < parts.length; i++) {
                        if (parts[i].type === 'dayPeriod') {
                            pm = parts[i].value === '下午';
                            break;
                        }
                    }
                    var h = hour % 12;
                    return table[pm ? (h === 0 ? 12 : h + 12) : h];
                };
                // 把「[时区名]」整段（含两侧括号 literal）搬到时刻之前。
                // 简繁 24 小时制与台繁的模板都是「… 时刻 [时区名]」。
                var __dtfTzFirst = function (parts) {
                    var a = -1, at = -1, i;
                    // 时刻段起点：dayPeriod / hour 中靠前的那个
                    for (i = 0; i < parts.length; i++) {
                        if (parts[i].type === 'timeZoneName' && a < 0) { a = i; }
                        if ((parts[i].type === 'hour'
                                || parts[i].type === 'dayPeriod') && at < 0) {
                            at = i;
                        }
                    }
                    // 已在时刻之前则不动
                    if (a < 0 || at < 0 || a < at) { return parts; }
                    var s = a, b = a;
                    if (s > 0 && parts[s - 1].type === 'literal'
                            && /^[\s]*[\[(]$/.test(parts[s - 1].value)) {
                        s--;
                    } else if (s > 0 && parts[s - 1].type === 'literal'
                            && parts[s - 1].value === ' ') {
                        s--;
                    }
                    if (b + 1 < parts.length && parts[b + 1].type === 'literal'
                            && /^[\s\])]+$/.test(parts[b + 1].value)) {
                        b++;
                    }
                    var out = [];
                    for (i = 0; i < parts.length; i++) {
                        if (i >= s && i <= b) { continue; }
                        if (i === at) {
                            out.push({ type: 'timeZoneName',
                                       value: parts[a].value });
                            out.push({ type: 'literal', value: ' ' });
                        }
                        out.push(parts[i]);
                    }
                    return out;
                };
                // 日文「月曜日 23:05」：把 weekday 连同其后的括号搬到最前
                var __dtfJaMove = function (parts) {
                    var a = -1, i;
                    for (i = 0; i < parts.length; i++) {
                        if (parts[i].type === 'weekday') { a = i; break; }
                    }
                    if (a < 0) { return parts; }
                    var b = a;
                    while (b + 1 < parts.length
                            && parts[b + 1].type === 'literal'
                            && /^[\s()]*$/.test(parts[b + 1].value)) {
                        b++;
                    }
                    var out = [{ type: 'weekday', value: parts[a].value },
                               { type: 'literal', value: ' ' }];
                    for (i = 0; i < parts.length; i++) {
                        if (i >= a && i <= b) { continue; }
                        if (parts[i].type === 'literal'
                                && /^[\s()]*$/.test(parts[i].value)) {
                            continue;
                        }
                        out.push(parts[i]);
                    }
                    return out;
                };
                // inRange：区间两端各自的段不含收尾括号，搬移时直接跳过
                var __dtfFix = function (parts, st, d, inRange) {
                    var out = parts, i;
                    // ③ dayPeriod
                    if (st.dp) {
                        var want = null;
                        if (st.explicitHour) {
                            var clock = __dtfHour(out);
                            if (clock !== null) {
                                want = __dtfDpFor(st.dp, clock, out);
                            }
                        } else {
                            var h = __dtfLocalHour(st.tz, d);
                            if (h !== null) { want = st.dp[h]; }
                        }
                        if (want) {
                            for (i = 0; i < out.length; i++) {
                                if (out[i].type === 'dayPeriod') {
                                    out[i] = { type: 'dayPeriod', value: want };
                                }
                            }
                        }
                    }
                    // 区间模板的空格与时区名位置是另一套 CLDR 数据，
                    // 不属于本项，区间内只修 dayPeriod
                    if (inRange) { return out; }
                    for (i = 0; i < out.length; i++) {
                        var p = out[i];
                        var nxt = out[i + 1] ? out[i + 1].type : '';
                        // ① 「日」+ 星期：在「日」后补空格（「日 」与「日」
                        //    紧邻 weekday 两种排布都要覆盖）
                        if (st.daySp && p.type === 'literal'
                                && /日$/.test(p.value) && nxt === 'weekday') {
                            out[i] = { type: 'literal', value: p.value + ' ' };
                        }
                        if (st.jaDsSp && p.type === 'literal'
                                && /日$/.test(p.value) && nxt === 'weekday') {
                            out[i] = { type: 'literal', value: p.value + ' ' };
                        }
                        // ① 只有「日」而没有月份：整段换成空格
                        if (st.dayToSp && p.type === 'literal'
                                && p.value === '日' && nxt === 'weekday') {
                            out[i] = { type: 'literal', value: ' ' };
                        }
                        // ① 台繁 24 小时制只有「时」时，引擎给「h」，
                        //    真机是「時」
                        if (st.twHourSuffix && p.type === 'literal'
                                && /^h/.test(p.value)
                                && out[i - 1] && out[i - 1].type === 'hour') {
                            out[i] = { type: 'literal',
                                value: '時' + p.value.slice(1) };
                        }
                        // ① 日文 12 小时制里 12 时写作「0 时」
                        if (st.jaHour0 && p.type === 'hour'
                                && p.value === '12') {
                            out[i] = { type: 'hour', value: '0' };
                        }
                        // ① 星期后紧邻时刻（无 literal）时补空格
                        if (st.wkSpAdd && p.type === 'weekday'
                                && (nxt === 'hour' || nxt === 'dayPeriod')) {
                            out.splice(i + 1, 0,
                                { type: 'literal', value: ' ' });
                            i++;
                        }
                        // ① 澳门模板里「星期 时刻」之间的空格要去掉
                        if (st.wkSpDrop && p.type === 'literal'
                                && /^\s+$/.test(p.value)
                                && out[i - 1] && out[i - 1].type === 'weekday'
                                && (nxt === 'hour' || nxt === 'dayPeriod')) {
                            out[i] = { type: 'literal', value: '' };
                        }
                        // ② timeStyle 带出的时区名后：真机无空格
                        if (st.tzGapDrop && p.type === 'literal' && p.value === ' '
                                && out[i - 1] && out[i - 1].type === 'timeZoneName'
                                && (nxt === 'hour' || nxt === 'dayPeriod')) {
                            out[i] = { type: 'literal', value: '' };
                        }
                    }
                    // ② 时区名在时刻之后（「… 23:05:09 [PDT]」）的排布
                    //    整段搬到时刻之前
                    if (st.tzMove && !inRange) {
                        out = __dtfTzFirst(out);
                    }
                    // ① 日文带分钟时星期整体前置到最前
                    if (st.jaMove) {
                        out = __dtfJaMove(out);
                    }
                    return out;
                };
                var __dtfPartsOrig = __dtfProto.formatToParts;
                var __dtfRangeOrig = __dtfProto.formatRange;
                var __dtfRangePartsOrig = __dtfProto.formatRangeToParts;
                // 单次调用的公共路径：st 为空（非 zh/ja 或不涉及）时直接回原值
                var __dtfCtx = function (dtf, d) {
                    var st = __dtfStyle(dtf);
                    if (!st) { return null; }
                    var parts = __dtfPartsOrig.call(dtf, d);
                    return { st: st, parts: __dtfFix(parts, st, d) };
                };
                var __dtfText = function (c) {
                    return c.parts.map(function (p) { return p.value; }).join('');
                };
                Object.defineProperty(__dtfProto, 'formatToParts', {
                    value: __maskNative(function formatToParts(d) {
                        var parts = __dtfPartsOrig.apply(this, arguments);
                        var st = __dtfStyle(this);
                        return st ? __dtfFix(parts, st, d) : parts;
                    }, 'formatToParts'),
                    writable: true, enumerable: false, configurable: true,
                });
                // format 在 V8/WebKit 上都是原型上的取值器，返回绑定函数。
                // 直接换掉访问器会暴露「format 变成普通方法」，故保留 getter，
                // 只包它返回的那个绑定函数（每次访问都拿到同一个，实测）。
                var __dtfFmtDesc = Object.getOwnPropertyDescriptor(
                    __dtfProto, 'format');
                if (__dtfFmtDesc && __dtfFmtDesc.get) {
                    var __dtfGetOrig = __dtfFmtDesc.get;
                    Object.defineProperty(__dtfProto, 'format', {
                        get: __maskNative(function () {
                            var dtf = this;
                            var bound = __dtfGetOrig.call(dtf);
                            if (!__dtfStyle(dtf)) { return bound; }
                            var wrapped = __maskNative(function (d) {
                                var c = __dtfCtx(dtf, d);
                                return c ? __dtfText(c) : bound.apply(dtf, arguments);
                            }, '');
                            try {
                                Object.defineProperty(wrapped, 'length', {
                                    value: bound.length, configurable: true,
                                });
                            } catch (e) {}
                            return wrapped;
                        }, 'get format'),
                        enumerable: false, configurable: true,
                    });
                }
                // formatRange / formatRangeToParts 走同一条骨架：两端各自
                // 按上面的规则改写，区间分隔符本身两边一致（实测 zh-CN/
                // zh-HK/zh-TW 的「–」「至」）。
                if (typeof __dtfRangePartsOrig === 'function') {
                    var __dtfFixRange = function (dtf, a, b) {
                        var st = __dtfStyle(dtf);
                        if (!st) { return null; }
                        var parts = __dtfRangePartsOrig.call(dtf, a, b);
                        // 按 source 分段：两端各自用各自的日期取 dayPeriod，
                        // 区间分隔符（source 为 undefined）原样保留。
                        var segs = { startRange: [], endRange: [] };
                        var out = [];
                        for (var i = 0; i < parts.length; i++) {
                            var p = parts[i];
                            out.push({ type: p.type, value: p.value,
                                       source: p.source });
                            if (segs[p.source]) {
                                segs[p.source].push({ type: p.type,
                                                      value: p.value });
                            }
                        }
                        var fixed = {
                            startRange: __dtfFix(segs.startRange, st, a, true),
                            endRange: __dtfFix(segs.endRange, st, b, true),
                        };
                        var idx = { startRange: 0, endRange: 0 };
                        for (i = 0; i < out.length; i++) {
                            var src = out[i].source;
                            if (fixed[src]) {
                                out[i] = fixed[src][idx[src]++];
                            }
                        }
                        return out;
                    };
                Object.defineProperty(__dtfProto, 'formatRange', {
                    value: __maskNative(function formatRange(a, b) {
                        var out = __dtfFixRange(this, a, b);
                        return out === null
                            ? __dtfRangeOrig.apply(this, arguments)
                            : out.map(function (p) {
                                return p.value;
                            }).join('');
                    }, 'formatRange'),
                    writable: true, enumerable: false, configurable: true,
                });
                Object.defineProperty(__dtfProto, 'formatRangeToParts', {
                    value: __maskNative(function formatRangeToParts(a, b) {
                        var out = __dtfFixRange(this, a, b);
                        return out === null
                            ? __dtfRangePartsOrig.apply(this, arguments) : out;
                    }, 'formatRangeToParts'),
                    writable: true, enumerable: false, configurable: true,
                });
                }
                // Date.prototype.toLocaleString 走同一条 ICU 骨架，真机与
                // format 输出逐字节一致（实测）。toLocaleDateString /
                // toLocaleTimeString 带对侧 style 时内核本就抛 TypeError，
                // 这里必须原样透传，不能被上面的 DateTimeFormat 构造吞掉。
                var __dtfLocOpts = function (args) {
                    var o = args[1];
                    if (o === null || typeof o !== 'object') { return {}; }
                    return o;
                };
                var __dtfLocTable = {
                    toLocaleString: null,
                    toLocaleDateString: 'timeStyle',
                    toLocaleTimeString: 'dateStyle',
                };
                Object.keys(__dtfLocTable).forEach(function (m) {
                    var orig = Date.prototype[m];
                    if (typeof orig !== 'function') { return; }
                    var forbidden = __dtfLocTable[m];
                    Object.defineProperty(Date.prototype, m, {
                        value: __maskNative(function () {
                            var o = __dtfLocOpts(arguments);
                            if (forbidden && o[forbidden] !== undefined) {
                                return orig.apply(this, arguments);
                            }
                            var dtf = new Intl.DateTimeFormat(
                                arguments[0], arguments[1]);
                            var c = __dtfCtx(dtf, this);
                            if (!c) { return orig.apply(this, arguments); }
                            // 没有日期/时间组件时内核给的是日期 + 时间，而
                            // 上面的 DateTimeFormat 默认只有日期，需按真机
                            // 口径补齐（hourCycle 不指定，跟随 locale）
                            var hasPart = false;
                            ['year', 'month', 'day', 'hour', 'minute',
                             'second', 'weekday', 'dateStyle', 'timeStyle'
                            ].forEach(function (k) {
                                if (o[k] !== undefined) { hasPart = true; }
                            });
                            if (!forbidden && !hasPart) {
                                var full = new Intl.DateTimeFormat(
                                    arguments[0], {
                                        year: 'numeric', month: 'numeric',
                                        day: 'numeric', hour: 'numeric',
                                        minute: 'numeric', second: 'numeric',
                                    });
                                c = __dtfCtx(full, this) || c;
                            }
                            return __dtfText(c);
                        }, m),
                        writable: true, enumerable: false, configurable: true,
                    });
                    try {
                        Object.defineProperty(Date.prototype[m], 'length', {
                            value: orig.length, configurable: true,
                        });
                    } catch (e) {}
                });
            } catch (e) {}
        }

        // ---- Permissions.query：按真机 iOS 口径返回 state / 抛错（F-20）----
        // 真机只有 geolocation / notifications / push 等少数权限名；其余要么
        // 抛 NotSupportedError（WebKit 未实现），要么因 WebIDL 枚举没有该值抛
        // TypeError。直接返回普通对象会丢 EventTarget 槽，故借用原生
        // geolocation 的状态对象再遮写 name/state。
        if (D.permissions && typeof Permissions !== 'undefined'
                && Permissions.prototype && Permissions.prototype.query) {
            try {
                var __permTable = D.permissions;
                var __permOrig = Permissions.prototype.query;
                var __permObj = function (name, state) {
                    var st = { name: name, state: state, onchange: null };
                    try {
                        if (typeof PermissionStatus === 'function') {
                            Object.setPrototypeOf(st, PermissionStatus.prototype);
                        }
                    } catch (e) {}
                    return st;
                };
                defineFn(Permissions.prototype, 'query', function (desc) {
                    var name = (desc && typeof desc === 'object') ? desc.name : undefined;
                    var mode = (typeof name === 'string'
                                && Object.prototype.hasOwnProperty.call(__permTable, name))
                        ? __permTable[name] : undefined;
                    if (mode === undefined) { return __permOrig.apply(this, arguments); }
                    if (mode === 'TypeError') {
                        return Promise.reject(new TypeError(
                            "Failed to execute 'query' on 'Permissions': "
                            + "The provided value '" + name
                            + "' is not a valid enum value of type PermissionName."));
                    }
                    if (mode === 'NotSupportedError') {
                        return Promise.reject(new DOMException(
                            "'" + name + "' is not supported.", 'NotSupportedError'));
                    }
                    var fallback = __permObj(name, mode);
                    var nativeP;
                    try { nativeP = __permOrig.call(this, { name: 'geolocation' }); }
                    catch (e) { return Promise.resolve(fallback); }
                    return nativeP.then(function (status) {
                        try {
                            Object.defineProperty(status, 'name', {
                                value: name, writable: false,
                                enumerable: true, configurable: true,
                            });
                            Object.defineProperty(status, 'state', {
                                value: mode, writable: false,
                                enumerable: true, configurable: true,
                            });
                            return status;
                        } catch (e) { return fallback; }
                    }, function () { return fallback; });
                });
            } catch (e) {}
        }

        // ---- StorageManager.estimate：真机 iOS 配额约 1 GiB（F-31）----
        // 桌面 Chromium 按磁盘 60% 计算 → 10 GiB 级；WebKit 的 per-origin
        // 配额固定为 1 GiB（macOS WebKit 实测 quota=1048576000），且返回体
        // 没有 usageDetails（Chromium 扩展字段）。
        if (D.ios && typeof StorageManager !== 'undefined' && StorageManager.prototype
                && StorageManager.prototype.estimate) {
            try {
                var __estOrig = StorageManager.prototype.estimate;
                defineFn(StorageManager.prototype, 'estimate', function () {
                    return __estOrig.apply(this, arguments).then(function (r) {
                        var usage = (r && typeof r.usage === 'number') ? r.usage : 0;
                        return { quota: 1048576000, usage: usage };
                    });
                });
            } catch (e) {}
        }

        // ---- enumerateDevices：真机 iOS 无 audiooutput 设备面（F-32）----
        // iPhone Safari 的 MediaDevices 只枚举 audioinput / videoinput；
        // audiooutput 是桌面 Chromium（以及 iPadOS/macOS）才有的面。
        if (D.ios && typeof MediaDevices !== 'undefined' && MediaDevices.prototype
                && MediaDevices.prototype.enumerateDevices) {
            try {
                var __enumOrig = MediaDevices.prototype.enumerateDevices;
                defineFn(MediaDevices.prototype, 'enumerateDevices', function () {
                    return __enumOrig.apply(this, arguments).then(function (list) {
                        var out = [];
                        for (var i = 0; i < (list ? list.length : 0); i++) {
                            if (list[i] && list[i].kind === 'audiooutput') { continue; }
                            out.push(list[i]);
                        }
                        return out;
                    });
                }, true);
            } catch (e) {}
        }

        // ---- console：Blink 专有成员（F-34）----
        // context（V8 inspector）/ createTask（async stack tagging）/ memory
        // （performance.memory 镜像）在 WebKit 的 console 上都不存在。
        if (D.ios && typeof console !== 'undefined') {
            try {
                delete console.context;
                delete console.createTask;
                delete console.memory;
            } catch (e) {}
        }

        // ---- iOS：input 的 computed overflow（F-46 的 input 分支）----
        // Blink 的 UA 样式表对文本输入框写了 `overflow: clip !important`，
        // 而 UA !important 在层叠里高于作者 !important——CSS 无法改。真机
        // WebKit 的 input 是 visible（/tmp/f40wk/probe5.py 实测），检测站一行
        // `getComputedStyle(input).overflow === 'clip'` 即判假。这里只对
        // INPUT 元素包一层 Proxy：属性读取与 getPropertyValue 改写成
        // visible，其余全部透传；ownKeys/描述符走 target 默认行为，不会在
        // CSSStyleDeclaration 上留下 own property 痕迹。
        if (D.ios && typeof window.getComputedStyle === 'function') {
            try {
                var __gcsOrig = window.getComputedStyle;
                var __ovKeys = { overflow: 1, overflowX: 1, overflowY: 1 };
                var __ovLong = { overflow: 1, 'overflow-x': 1, 'overflow-y': 1 };

                // F-99：computed 值的序列化口径（WebKit 27.2 实测，取证脚本
                // /tmp/f99cls/*、/tmp/f99cmp*）。WebKit 打印计算长度分三档，
                // Blink 全部不同：
                //   · 布局长度（width/height/block-size/inline-size + 除
                //     physical right 外的外边距）是 LayoutUnit（1/64px），
                //     真机打满小数（33.296875px），Blink 截到 4 位
                //     （33.2969px）。→ 反推 1/64 再打。
                //   · 其余长度是 float32 按 %.6f 打印后去尾零（21.44px →
                //     21.440001px，27.2px → 27.200001px），Blink 只打 2~4 位。
                //     → Math.fround 后同法打印。
                //   · outline/border 宽度是整数截断（1.5px → 1px，-1.9px →
                //     -1px），Blink 已一致，不动。
                // 只改 computed 回读：inline 声明在 WebKit 同样保留作者原文
                // （`el.style.width` 仍是 33.3px），故不能碰 __cssStore。
                var __f99FmtPx = function (v) {
                    var s = Math.fround(v).toFixed(6);
                    if (s.indexOf('.') !== -1) {
                        s = s.replace(/0+$/, '').replace(/\.$/, '');
                    }
                    return s + 'px';
                };
                var __f99PxRe = /^-?(?:\d+\.?\d*|\.\d+)px$/;
                var __f99Camel = function (k) {
                    return k.replace(/-([a-z])/g, function (m, c) {
                        return c.toUpperCase();
                    });
                };
                // 属性名 → 归档。同时登记 kebab 与 camel 两种写法（检测站两种
                // 都会用：`cs.marginTop` / `cs.getPropertyValue('margin-top')`）。
                var __f99Kind = Object.create(null);
                var __f99KebabOf = Object.create(null);
                var __f99Reg = function (kind, names) {
                    for (var i = 0; i < names.length; i++) {
                        var k = names[i];
                        __f99Kind[k] = kind;
                        __f99KebabOf[k] = k;
                        var c = __f99Camel(k);
                        __f99Kind[c] = kind;
                        __f99KebabOf[c] = k;
                    }
                };
                // 布局长度（1/64）
                __f99Reg('snap', ['width', 'height', 'block-size', 'inline-size']);
                // 外边距：物理/逻辑共 11 个长写，外加 3 个简写（简写要按物理
                // 侧分别取值后重新折叠——真机 `margin:21.44px` 折叠不成单值）。
                __f99Reg('margin', ['margin', 'margin-inline', 'margin-block',
                                    'margin-top', 'margin-right', 'margin-bottom',
                                    'margin-left', 'margin-inline-start',
                                    'margin-inline-end', 'margin-block-start',
                                    'margin-block-end']);
                // float32 长写
                __f99Reg('float', [
                    'min-width', 'min-height', 'max-width', 'max-height',
                    'min-inline-size', 'max-inline-size',
                    'min-block-size', 'max-block-size',
                    'padding-top', 'padding-right', 'padding-bottom',
                    'padding-left', 'padding-inline-start',
                    'padding-inline-end', 'padding-block-start',
                    'padding-block-end',
                    'top', 'right', 'bottom', 'left',
                    'inset-block-start', 'inset-block-end',
                    'inset-inline-start', 'inset-inline-end',
                    'border-top-left-radius', 'border-top-right-radius',
                    'border-bottom-right-radius', 'border-bottom-left-radius',
                    'border-start-start-radius', 'border-start-end-radius',
                    'border-end-start-radius', 'border-end-end-radius',
                    'letter-spacing', 'word-spacing', 'text-indent',
                    'font-size', 'line-height',
                    'row-gap', 'column-gap', 'flex-basis',
                    'scroll-margin-top', 'scroll-margin-right',
                    'scroll-margin-bottom', 'scroll-margin-left',
                    'scroll-padding-top', 'scroll-padding-right',
                    'scroll-padding-bottom', 'scroll-padding-left',
                    'column-width', 'perspective', 'text-decoration-thickness',
                    'text-underline-offset', 'shape-margin', 'border-spacing',
                    'vertical-align', 'transform-origin', 'translate',
                    'stroke-width', 'stroke-dashoffset',
                ]);
                // 含多个长度 token 的简写：逐个 token 归一（真机的折叠规则与
                // Blink 相同，只是数值不同，所以保留原折叠形态）。
                __f99Reg('tok', ['padding', 'padding-inline', 'padding-block',
                                 'inset', 'inset-inline', 'inset-block',
                                 'gap', 'border-radius', 'scroll-margin',
                                 'scroll-padding', 'border-spacing',
                                 'transform-origin', 'translate']);
                // -webkit-locale：真机是未加引号的 custom-ident（`zh-CN`），
                // Blink 回读带引号（`"zh-CN"`）。
                __f99Reg('locale', ['-webkit-locale']);
                __f99Kind.webkitLocale = 'locale';
                __f99KebabOf.webkitLocale = '-webkit-locale';

                // 物理侧判定：WebKit 只有「物理右侧」的外边距不参与 1/64 量化
                // （CSSPropertyMarginRight 走 zoomAdjustedPixelValue 分支，
                // 其余三边走 zoomAdjustedPaddingOrMarginPixelValue）。
                var __f99PhysSide = function (key, rtl, vert, wm) {
                    switch (key) {
                    case 'margin-top': return 'top';
                    case 'margin-bottom': return 'bottom';
                    case 'margin-left': return 'left';
                    case 'margin-right': return 'right';
                    case 'margin-inline-start':
                        return vert ? (rtl ? 'bottom' : 'top')
                                    : (rtl ? 'right' : 'left');
                    case 'margin-inline-end':
                        return vert ? (rtl ? 'top' : 'bottom')
                                    : (rtl ? 'left' : 'right');
                    // 竖排时 block 轴是水平方向，block-start 与 direction 无关
                    // （vertical-rl 从右往左推进，起点即物理右侧）。
                    case 'margin-block-start':
                        if (!vert) { return 'top'; }
                        return wm === 'vertical-rl' ? 'right' : 'left';
                    case 'margin-block-end':
                        if (!vert) { return 'bottom'; }
                        return wm === 'vertical-rl' ? 'left' : 'right';
                    }
                    return null;
                };
                var __f99Collapse = function (v) {
                    if (v.length === 1) { return v[0]; }
                    if (v.length === 2) {
                        return v[0] === v[1] ? v[0] : v[0] + ' ' + v[1];
                    }
                    if (v[0] === v[1] && v[1] === v[2] && v[2] === v[3]) {
                        return v[0];
                    }
                    if (v[0] === v[2] && v[1] === v[3]) {
                        return v[0] + ' ' + v[1];
                    }
                    if (v[1] === v[3]) {
                        return v[0] + ' ' + v[1] + ' ' + v[2];
                    }
                    return v[0] + ' ' + v[1] + ' ' + v[2] + ' ' + v[3];
                };
                // 简写里每个 px token 各自归一到 float32 口径。
                var __f99TokFmt = function (val) {
                    if (val.indexOf('(') !== -1) { return val; }
                    return val.replace(
                        /(-?(?:\d+\.?\d*|\.\d+))px/g,
                        function (m, num) {
                            return __f99FmtPx(parseFloat(num));
                        });
                };

                // F-100：真机 UA 默认值（WebKit 27.2 实测 /tmp/f99cls/forms.js）。
                // 仿真的 uaFormCss 钉了 `line-height:13px`——那是控件高度
                // （button 18px）的真正来源，不能删（删了 CJK button 在 Blink
                // 撑到 21px，真机 18px）。故只在回读层把「落回 UA 口径」的值
                // 报成 normal：line-height 恰为 13px、font-size 恰为 11px、
                // 且作者没在 inline 上设过 line-height。
                var __f100NoCursorAuto = {
                    button: 1, submit: 1, reset: 1, image: 1,
                };
                var __f100Norm = function (t, key, val, el) {
                    if (!el || !el.tagName) { return val; }
                    var tag = el.tagName;
                    if (key === 'line-height') {
                        if ((tag === 'INPUT' || tag === 'BUTTON'
                                || tag === 'TEXTAREA')
                                && val === '13px'
                                && Reflect.get(t, 'fontSize', t) === '11px'
                                && !(el.style && el.style.lineHeight)) {
                            return 'normal';
                        }
                        return val;
                    }
                    // 真机 UA：文本类控件 auto，按钮类 default。Blink UA 反过来
                    // 给文本类 `text`、给 checkbox/radio/range/color/file/date
                    // `default`，两处都要归位。作者显式设过的值（pointer 等）
                    // 不动。
                    if (key === 'cursor' && (val === 'text' || val === 'default')) {
                        if (tag === 'TEXTAREA') { return 'auto'; }
                        if (tag === 'INPUT') {
                            var ty = String(el.type || 'text').toLowerCase();
                            return __f100NoCursorAuto[ty] ? 'default' : 'auto';
                        }
                    }
                    return val;
                };

                var __f99Norm = function (t, name, val, el) {
                    if (typeof val !== 'string' || val === '') { return val; }
                    var kind = __f99Kind[name];
                    if (kind === undefined) {
                        // F-100 的 cursor 不是长度，没有归档；仍要走 UA 归一。
                        return name === 'cursor'
                            ? __f100Norm(t, 'cursor', val, el) : val;
                    }
                    var key = __f99KebabOf[name];
                    if (kind === 'locale') {
                        var n = val.length;
                        if (n > 1 && val.charAt(0) === '"'
                                && val.charAt(n - 1) === '"') {
                            return val.slice(1, -1);
                        }
                        return val;
                    }
                    if (kind === 'float' || kind === 'tok') {
                        val = __f100Norm(t, key, val, el);
                        if (kind === 'float') {
                            return __f99PxRe.test(val)
                                ? __f99FmtPx(parseFloat(val)) : val;
                        }
                        return __f99TokFmt(val);
                    }
                    // kind === 'snap' | 'margin'
                    var rtl = Reflect.get(t, 'direction', t) === 'rtl';
                    var wm = String(Reflect.get(t, 'writingMode', t) || '');
                    var vert = wm.indexOf('vertical') === 0;
                    // 未参与布局的元素（display:none / inline / contents）没有
                    // LayoutUnit 参与，WebKit 直接走 float32 打印。
                    var disp = Reflect.get(t, 'display', t);
                    var snapOk = disp !== 'none' && disp !== 'inline'
                                 && disp !== 'contents';
                    var read = function (k, snapByDefault) {
                        var v = Reflect.get(t, __f99Camel(k), t);
                        if (typeof v !== 'string' || v === '') { return v; }
                        if (!__f99PxRe.test(v)) { return v; }
                        var f = parseFloat(v);
                        var side = __f99PhysSide(k, rtl, vert, wm);
                        var snap = snapOk && (snapByDefault
                            ? side !== 'right' : side && side !== 'right');
                        return snap
                            ? __f99FmtPx(Math.round(f * 64) / 64)
                            : __f99FmtPx(f);
                    };
                    if (kind === 'snap') { return read(key, true); }
                    if (key === 'margin') {
                        return __f99Collapse([
                            read('margin-top'), read('margin-right'),
                            read('margin-bottom'), read('margin-left'),
                        ]);
                    }
                    if (key === 'margin-inline') {
                        return __f99Collapse([
                            read('margin-inline-start'),
                            read('margin-inline-end'),
                        ]);
                    }
                    if (key === 'margin-block') {
                        return __f99Collapse([
                            read('margin-block-start'),
                            read('margin-block-end'),
                        ]);
                    }
                    return read(key);
                };

                // F-98：登记「computed 声明 → 元素」，getPropertyValue 才能
                // 回落到 inline 存储。WeakMap 不挂在声明对象上，不留痕迹。
                // F-46：INPUT 的 overflow 在真机是 visible，Blink UA 样式表写
                // 死了 `overflow:clip !important`，CSS 改不动——回读层改写。
                defineFn(window, 'getComputedStyle', function (el, pseudo) {
                    var cs = __gcsOrig.call(window, el, pseudo);
                    try { __csOwner.set(cs, el); } catch (e) {}
                    try {
                        var ovFix = !!(el && el.tagName === 'INPUT'
                                       && cs.overflow === 'clip');
                        var bound = Object.create(null);
                        var bindTo = function (fn, name) {
                            var b = bound[name];
                            if (!b) {
                                b = __maskNative(function () {
                                    return fn.apply(cs, arguments);
                                }, name);
                                try {
                                    Object.defineProperty(b, 'length', {
                                        value: fn.length, configurable: true,
                                    });
                                } catch (e2) {}
                                bound[name] = b;
                            }
                            return b;
                        };
                        var gpv = __maskNative(function (p) {
                            var k = String(p).toLowerCase();
                            if (ovFix && __ovLong[k]) { return 'visible'; }
                            var raw = cs.getPropertyValue(p);
                            return __f99Norm(cs, k, raw, el);
                        }, 'getPropertyValue');
                        try {
                            Object.defineProperty(gpv, 'length', {
                                value: 1, configurable: true,
                            });
                        } catch (e2) {}
                        bound.getPropertyValue = gpv;
                        return new Proxy(cs, {
                            get: function (t, k) {
                                if (typeof k !== 'string') {
                                    return Reflect.get(t, k, t);
                                }
                                if (k === 'getPropertyValue') { return gpv; }
                                if (ovFix && __ovKeys[k]) { return 'visible'; }
                                var v = Reflect.get(t, k, t);
                                if (typeof v === 'function') {
                                    return bindTo(v, k);
                                }
                                if (typeof v === 'string') {
                                    return __f99Norm(t, k, v, el);
                                }
                                return v;
                            },
                            // 自有属性描述符也要给归一后的值：`Object
                            // .getOwnPropertyDescriptor(cs, 'width').value`
                            // 是一条常用旁路。
                            getOwnPropertyDescriptor: function (t, k) {
                                var d = Reflect.getOwnPropertyDescriptor(t, k);
                                if (!d || typeof k !== 'string'
                                        || typeof d.value !== 'string'
                                        || (!d.configurable && !d.writable)) {
                                    return d;
                                }
                                var nv = __f99Norm(t, k, d.value, el);
                                if (nv === d.value) { return d; }
                                return {
                                    value: nv, writable: d.writable,
                                    enumerable: d.enumerable,
                                    configurable: d.configurable,
                                };
                            },
                        });
                    } catch (e) { return cs; }
                }, true);
            } catch (e) {}
        }

        // ---- Android：UA 样式表跨平台差（F-46 的 Android 侧）----
        // 真机 Chrome Android 的控件默认族是 Roboto，宿主 macOS 的 Blink UA
        // sheet 落到 Arial（`-webkit-small-control` 被宿主字体解析成 Arial）——
        // `getComputedStyle(select).fontFamily.includes('Arial')` 一行判假。
        // textarea 的 monospace 与 input 的 overflow:clip 是 Blink 全平台口径，
        // 真机 Android 同样如此，**不可**对齐 iOS 改写。
        if (D.android) {
            try {
                var __putUaFormDroid = function () {
                    if (document.getElementById('__wk_uaform_droid')) { return; }
                    var root = document.head || document.documentElement;
                    if (!root) {
                        document.addEventListener('readystatechange', __putUaFormDroid);
                        return;
                    }
                    var st = document.createElement('style');
                    st.id = '__wk_uaform_droid';
                    st.textContent = '@layer wkua{button,input,select,textarea{'
                        + 'font-family:Roboto,system-ui,sans-serif}}';
                    root.insertBefore(st, root.firstChild);
                };
                __putUaFormDroid();
            } catch (e) {}
        }

        // ---- 补成员：真机有、Chromium 没有的 WebKit 面（F-25 / F-36）----
        // 与下面的删除清单是反向机制。清单与取值口径见指纹库 _IOS_ADD_SPEC
        // （全部取自 macOS WebKit 27 实测，与 iPhone Safari 同一 WebKit 源码）：
        //   interfaces : 全局接口对象 + 原型成员（getter / 方法）
        //   protos     : 既有原型的补充成员（value = 固定返回值或初始值）
        //   globals    : 非接口的全局项（webkitIndexedDB / openDatabase）
        //   constants  : 接口对象与原型上同时存在的 WebIDL 常量
        if (D.addMembers && D.ios) {
            try {
                var __add = D.addMembers;
                var __addConst = function (host, name, value) {
                    try {
                        Object.defineProperty(host, name, {
                            value: value, writable: false,
                            enumerable: true, configurable: false,
                        });
                    } catch (e) {}
                };
                // CanvasRenderingContext2D 的 legacy 方法族改的是画布状态，
                // 用同一个 defineFn 包装即可（返回值恒 undefined）。
                var __addLegacy2D = function (proto, name, applyFn) {
                    if (!proto || name in proto) { return; }
                    defineFn(proto, name, applyFn);
                };
                if (typeof CanvasRenderingContext2D !== 'undefined') {
                    var __c2d = CanvasRenderingContext2D.prototype;
                    __addLegacy2D(__c2d, 'setLineWidth', function (w) {
                        this.lineWidth = w;
                    });
                    __addLegacy2D(__c2d, 'setLineCap', function (v) {
                        this.lineCap = v;
                    });
                    __addLegacy2D(__c2d, 'setLineJoin', function (v) {
                        this.lineJoin = v;
                    });
                    __addLegacy2D(__c2d, 'setMiterLimit', function (v) {
                        this.miterLimit = v;
                    });
                    __addLegacy2D(__c2d, 'setStrokeColor', function (c) {
                        this.strokeStyle = (arguments.length > 1)
                            ? 'rgba(' + arguments[0] + ',' + arguments[1] + ','
                              + arguments[2] + ','
                              + (arguments.length > 3 ? arguments[3] : 1) + ')'
                            : c;
                    });
                    __addLegacy2D(__c2d, 'setFillColor', function (c) {
                        this.fillStyle = (arguments.length > 1)
                            ? 'rgba(' + arguments[0] + ',' + arguments[1] + ','
                              + arguments[2] + ','
                              + (arguments.length > 3 ? arguments[3] : 1) + ')'
                            : c;
                    });
                    __addLegacy2D(__c2d, 'setShadow', function (x, y, blur, color) {
                        this.shadowOffsetX = x;
                        this.shadowOffsetY = y;
                        this.shadowBlur = blur;
                        if (arguments.length > 3) { this.shadowColor = color; }
                    });
                    __addLegacy2D(__c2d, 'clearShadow', function () {
                        this.shadowOffsetX = 0;
                        this.shadowOffsetY = 0;
                        this.shadowBlur = 0;
                        this.shadowColor = 'rgba(0, 0, 0, 0)';
                    });
                    // webkitLineDash / Offset 是 WebKit 独有的画布状态别名
                    ['webkitLineDash', 'webkitLineDashOffset'].forEach(function (n) {
                        if (n in __c2d) { return; }
                        var target = (n === 'webkitLineDash')
                            ? 'lineDash' : 'lineDashOffset';
                        Object.defineProperty(__c2d, n, {
                            get: __maskNative(function () {
                                try {
                                    return (target === 'lineDash')
                                        ? this.getLineDash() : this.lineDashOffset;
                                } catch (e) {
                                    return (target === 'lineDash') ? [] : 0;
                                }
                            }, 'get ' + n),
                            set: __maskNative(function (v) {
                                try {
                                    if (target === 'lineDash') { this.setLineDash(v || []); }
                                    else { this.lineDashOffset = v; }
                                } catch (e) {}
                            }, 'set ' + n),
                            enumerable: true, configurable: true,
                        });
                    });
                }
                // Force Touch：MouseEvent 访问器 + 常量（数值取自 WebKit
                // WebKitForce 枚举）。真机实测该访问器只定义在 MouseEvent
                // 原型上，WheelEvent 经原型链继承；Wheel 方向位恒 false。
                if (typeof MouseEvent !== 'undefined') {
                    if (!('webkitForce' in MouseEvent.prototype)) {
                        Object.defineProperty(MouseEvent.prototype, 'webkitForce', {
                            get: __maskNative(function () { return 0; },
                                              'get webkitForce'),
                            enumerable: true, configurable: true,
                        });
                    }
                }
                if (typeof WheelEvent !== 'undefined'
                        && !('webkitDirectionInvertedFromDevice'
                             in WheelEvent.prototype)) {
                    Object.defineProperty(WheelEvent.prototype,
                        'webkitDirectionInvertedFromDevice', {
                            get: __maskNative(function () { return false; },
                                'get webkitDirectionInvertedFromDevice'),
                            enumerable: true, configurable: true,
                        });
                }
                if (typeof KeyboardEvent !== 'undefined'
                        && !('keyIdentifier' in KeyboardEvent.prototype)) {
                    Object.defineProperty(KeyboardEvent.prototype, 'keyIdentifier', {
                        get: __maskNative(function () { return ''; },
                                          'get keyIdentifier'),
                        enumerable: true, configurable: true,
                    });
                }
                // 媒体面：WebKit 的 AudioTrackList / VideoTrackList 面。实例
                // 形态与成员口径见下方 F-154 块（__wkList）。
                if (typeof HTMLMediaElement !== 'undefined') {
                    var __hm = HTMLMediaElement.prototype;
                    [['audioTracks', 'AudioTrackList'],
                     ['videoTracks', 'VideoTrackList']].forEach(function (pair) {
                        if (pair[0] in __hm) { return; }
                        Object.defineProperty(__hm, pair[0], {
                            get: __maskNative(function () {
                                return __wkList(pair[1], this);
                            }, 'get ' + pair[0]),
                            enumerable: true, configurable: true,
                        });
                    });
                    if (!('fastSeek' in __hm)) {
                        defineFn(__hm, 'fastSeek', function (t) {
                            this.currentTime = t;
                        });
                    }
                    if (!('getStartDate' in __hm)) {
                        defineFn(__hm, 'getStartDate', function () {
                            return new Date(NaN);
                        });
                    }
                    // 真机把 getVideoPlaybackQuality 放在 HTMLMediaElement 上，
                    // Chromium 放在 HTMLVideoElement 上；这里只补 audio 侧。
                    // video 侧沿用原生实现（真实播放统计），缺的
                    // totalFrameDelay 由 protos 表补到 VideoPlaybackQuality
                    // 原型上（见 _IOS_ADD_SPEC）。
                    if (!('getVideoPlaybackQuality' in __hm)) {
                        defineFn(__hm, 'getVideoPlaybackQuality', function () {
                            var VQ = window.VideoPlaybackQuality;
                            return (VQ && VQ.prototype) ? Object.create(VQ.prototype) : {};
                        });
                    }
                    [['controller', null], ['mediaGroup', '']].forEach(function (p) {
                        if (p[0] in __hm) { return; }
                        Object.defineProperty(__hm, p[0], {
                            get: __maskNative(function () {
                                var v = this['__wk_' + p[0]];
                                return (v === undefined) ? p[1] : v;
                            }, 'get ' + p[0]),
                            set: __maskNative(function (v) {
                                this['__wk_' + p[0]] = v;
                            }, 'set ' + p[0]),
                            enumerable: true, configurable: true,
                        });
                    });
                    if (!('webkitShowPlaybackTargetPicker' in __hm)) {
                        defineFn(__hm, 'webkitShowPlaybackTargetPicker',
                                 function () {});
                    }
                    if (!('webkitCurrentPlaybackTargetIsWireless' in __hm)) {
                        Object.defineProperty(__hm,
                            'webkitCurrentPlaybackTargetIsWireless', {
                                get: __maskNative(function () { return false; },
                                    'get webkitCurrentPlaybackTargetIsWireless'),
                                enumerable: true, configurable: true,
                            });
                    }
                    // F-28：真机 iOS 的自动播放策略。未静音媒体在没有用户激活
                    // 时 play() 必被拒——WebKit 在 HTMLMediaElement::play 里
                    // 直接 promise.reject(ExceptionCode::NotAllowedError)，
                    // 不传自定义文案，DOMException 用通用描述（DOMException.cpp
                    // 的 NotAllowedError 条目）。静音（或 volume 0）+ playsInline
                    // 的自动播放真机是允许的，不能一刀切。
                    // 宿主 Chromium 可能自己也会拒，但文案/时机不同，故统一接管。
                    (function () {
                        var origPlay = __hm.play;
                        if (typeof origPlay !== 'function') { return; }
                        var deny = function () {
                            var msg = 'The request is not allowed by the user agent '
                                + 'or the platform in the current context, possibly '
                                + 'because the user denied permission.';
                            try {
                                return new DOMException(msg, 'NotAllowedError');
                            } catch (e) {
                                var err = new Error(msg);
                                err.name = 'NotAllowedError';
                                return err;
                            }
                        };
                        defineFn(__hm, 'play', function () {
                            if (!__slotHas(__hm, this)) {
                                throw new TypeError('Illegal invocation');
                            }
                            var act = null;
                            try { act = navigator.userActivation; } catch (e) {}
                            var active = !!(act && act.isActive);
                            // 静音 / 音量为 0 的播放不需要手势：
                            // WebKit 的 MediaElementSession 把
                            // `element.muted() || !element.volume()` 直接判为
                            // 无需手势。宿主 Chromium 对「静音 <audio>」仍会拒
                            // （muted <video> 才放行），这里按真机口径吞掉这条
                            // 拒绝，只吞 NotAllowedError，其它失败照常透出。
                            var silent = !!this.muted || this.volume === 0;
                            if (!active && !silent) {
                                return Promise.reject(deny());
                            }
                            var p = origPlay.apply(this, arguments);
                            if (!silent || !p || typeof p.then !== 'function') {
                                return p;
                            }
                            return p.then(function (v) { return v; },
                                          function (e) {
                                if (e && e.name === 'NotAllowedError') {
                                    return undefined;
                                }
                                throw e;
                            });
                        });
                    })();
                }
                if (typeof HTMLVideoElement !== 'undefined') {
                    var __hv = HTMLVideoElement.prototype;
                    if (!('webkitSetPresentationMode' in __hv)) {
                        defineFn(__hv, 'webkitSetPresentationMode', function () {});
                    }
                    if (!('webkitSupportsPresentationMode' in __hv)) {
                        defineFn(__hv, 'webkitSupportsPresentationMode',
                                 function () { return true; });
                    }
                    if (!('webkitWirelessVideoPlaybackDisabled' in __hv)) {
                        Object.defineProperty(__hv,
                            'webkitWirelessVideoPlaybackDisabled', {
                                get: __maskNative(function () {
                                    return this.__wkWirelessDisabled !== false;
                                }, 'get webkitWirelessVideoPlaybackDisabled'),
                                set: __maskNative(function (v) {
                                    this.__wkWirelessDisabled = !!v;
                                }, 'set webkitWirelessVideoPlaybackDisabled'),
                                enumerable: true, configurable: true,
                            });
                    }
                    if (!('autoPictureInPicture' in __hv)) {
                        Object.defineProperty(__hv, 'autoPictureInPicture', {
                            get: __maskNative(function () {
                                return !!this.__wkAutoPip;
                            }, 'get autoPictureInPicture'),
                            set: __maskNative(function (v) {
                                this.__wkAutoPip = !!v;
                            }, 'set autoPictureInPicture'),
                            enumerable: true, configurable: true,
                        });
                    }
                }
                // SourceBuffer 的三条轨道列表真机都是真对象（零自有属性、
                // 可读 length），实例口径见下方 F-154 块（__wkList）。
                if (typeof SourceBuffer !== 'undefined') {
                    [['audioTracks', 'AudioTrackList'],
                     ['videoTracks', 'VideoTrackList'],
                     ['textTracks', 'TextTrackList']].forEach(function (pair) {
                        if (pair[0] in SourceBuffer.prototype) { return; }
                        try {
                            Object.defineProperty(SourceBuffer.prototype, pair[0], {
                                get: __maskNative(function () {
                                    return __wkList(pair[1], this);
                                }, 'get ' + pair[0]),
                                enumerable: true, configurable: true,
                            });
                        } catch (e) {}
                    });
                }
                // 音频会话：真机 iOS 专有全局，type 初值 'auto'
                // 接口对象：WebKit 生成器形态（不可枚举、可写、可配置）。
                // 真机文案（实测 /tmp/wkprobe/f25nonew.py、pres.py）：
                //   'illegal' → 不带 new、带 new 都是裸 `Illegal constructor`
                //   'args'    → 不带 new 是 `Constructor requires 'new' operator`，
                //               new 时缺参是裸 `Not enough arguments`
                //   'ok'      → 不带 new 同上；new 不限参数
                // FontFaceSet 是混合体：noNew 走 args 文案、new 走 illegal
                // （实测），故 noNew 可单独指定。
                // Blink 的文案是 `Failed to construct 'X': <reason>`，repair 时
                // 统一归一化成 WebKit 的裸文案。
                // 实例内部状态放 WeakMap：真机实例没有任何自有属性
                // （Object.keys / getOwnPropertyNames 均为 []）。
                var __wkState = new WeakMap();
                // 接口修复轮装的占位成员（不是宿主原生实现），列表接口
                // 判定「有没有原生方法可转交」时要排除掉。
                var __wkStubFn = new WeakSet();
                var __wkSt = function (o) {
                    var s = __wkState.get(o);
                    if (!s) { s = Object.create(null); __wkState.set(o, s); }
                    return s;
                };
                // MediaController 的 buffered/seekable/played 每次读取都是新对象
                // （实测 mc.buffered !== mc.buffered），init 里存占位符、读取时
                // 现造。真机空 TimeRanges 的 start(0)/end(0) 抛 IndexSizeError。
                var __wkTimeRanges = function () {
                    var TR = window.TimeRanges;
                    var o = (TR && TR.prototype) ? Object.create(TR.prototype) : {};
                    var idxErr = function () {
                        try {
                            return new DOMException(
                                'The index is not in the allowed range.',
                                'IndexSizeError');
                        } catch (e) {
                            return new Error('The index is not in the allowed range.');
                        }
                    };
                    try {
                        Object.defineProperty(o, 'length', {
                            get: __maskNative(function () { return 0; },
                                              'get length'),
                            enumerable: false, configurable: true,
                        });
                    } catch (e) {}
                    ['start', 'end'].forEach(function (n) {
                        try {
                            Object.defineProperty(o, n, {
                                value: __maskNative(function () { throw idxErr(); },
                                                    n),
                                writable: true, enumerable: false,
                                configurable: true,
                            });
                        } catch (e) {}
                    });
                    return o;
                };
                var __wkResolve = function (v) {
                    return (v === '@TimeRanges') ? __wkTimeRanges() : v;
                };
                // 真机接收者校验文案（实测 /tmp/wkprobe/f25recv.py）：
                //   getter → The X.m getter can only be used on instances of X
                //   setter → The X.m setter can only be used on instances of X
                //   method → Can only call X.m on instances of X
                var __wkBrandErr = function (iface, member, kind) {
                    if (kind === 'method') {
                        return new TypeError('Can only call ' + iface + '.' + member
                                             + ' on instances of ' + iface);
                    }
                    return new TypeError('The ' + iface + '.' + member + ' ' + kind
                                         + ' can only be used on instances of '
                                         + iface);
                };
                // brandOk：可选的接收者探针。合成接口若建在真实宿主对象上
                // （spec.realBase，如 ManagedMediaSource → MediaSource），
                // 真机连 Object.create(原型) 都不认，而 __slotHas 走原型链会
                // 误收；此时用父接口的原生 getter 当探针（F-152）。
                var __wkProp = function (proto, iface, member, rw, init, brandOk) {
                    var ok = brandOk || function (recv) { return __slotHas(proto, recv); };
                    var d = {
                        get: __maskNative(function () {
                            if (!ok(this)) {
                                throw __wkBrandErr(iface, member, 'getter');
                            }
                            var s = __wkSt(this);
                            if (!(member in s) && (member in init)) {
                                s[member] = init[member];
                            }
                            var v = s[member];
                            // DOM 事件处理器属性未赋值时读作 null（不是 undefined）。
                            if (v === undefined && member.slice(0, 2) === 'on') {
                                return null;
                            }
                            return __wkResolve(v);
                        }, 'get ' + member),
                        enumerable: true, configurable: true,
                    };
                    if (rw) {
                        d.set = __maskNative(function (v) {
                            if (!ok(this)) {
                                throw __wkBrandErr(iface, member, 'setter');
                            }
                            __wkSt(this)[member] = v;
                        }, 'set ' + member);
                    }
                    Object.defineProperty(proto, member, d);
                };
                var __wkMethod = function (proto, iface, member, brandOk) {
                    var ok = brandOk || function (recv) { return __slotHas(proto, recv); };
                    var fn = __maskNative(function () {
                        if (!ok(this)) {
                            throw __wkBrandErr(iface, member, 'method');
                        }
                        return undefined;
                    }, member);
                    __wkStubFn.add(fn);
                    Object.defineProperty(proto, member, {
                        value: fn,
                        writable: true, enumerable: true, configurable: true,
                    });
                };
                // 把 Blink 抛出的 TypeError 文案归一化成 WebKit 形态。
                // F-151 / F-157：Blink 的每条异常都带
                //   Failed to execute '<m>' on '<Iface>': <rest>
                //   Failed to set the '<p>' property on '<Iface>': <rest>
                // 前缀，WebKit 一律没有；剥掉前缀后逐条映射到真机文案
                // （实测 /tmp/f347 的 txt 组 20 项 + closed/gone 组）。
                var __wkNormMsg = function (msg) {
                    var s = String(msg);
                    var m = /^Failed to construct '[^']*': (.*)$/.exec(s);
                    if (m) {
                        var rest = m[1];
                        if (/^Please use the 'new' operator/.test(rest)) {
                            return "Constructor requires 'new' operator";
                        }
                        if (/arguments? required, but only .* present/.test(rest)) {
                            return 'Not enough arguments';
                        }
                        return rest;
                    }
                    var p = /^Failed to (?:execute '[^']*' on|set the '[^']*' property on) '[^']*': (.*)$/.exec(s);
                    if (!p) { return s; }
                    var body = p[1];
                    if (/arguments? required, but only \d+ present/.test(body)) {
                        return 'Not enough arguments';
                    }
                    if (/^Cannot convert a Symbol value to a string$/.test(body)) {
                        return 'Cannot convert a symbol to a string';
                    }
                    if (/^Cannot convert a Symbol value to a number$/.test(body)) {
                        return 'Cannot convert a symbol to a number';
                    }
                    if (/^The provided double value is non-finite\.?$/.test(body)) {
                        return 'The provided value is non-finite';
                    }
                    // 枚举/字典/越界/类型族：WebKit 一律塌成裸 'Type error'，
                    // 只有 removeSourceBuffer 的实例校验和 endOfStream 的枚举
                    // 有专属文案（见下）。
                    if (/^The provided value .* is not a valid enum value of type /.test(body)) {
                        return "Argument 1 ('error') to MediaSource.endOfStream must be one of: "
                            + '"network", "decode"';
                    }
                    if (/^parameter 1 is not of type 'SourceBuffer'\.$/.test(body)) {
                        return "Argument 1 ('buffer') to MediaSource.removeSourceBuffer"
                            + ' must be an instance of SourceBuffer';
                    }
                    // Blink 的区间/枚举/重载族措辞不一（'The start provided … is
                    // outside the range'、'The value provided is not a number'、
                    // 'The mode value provided … is invalid …'），WebKit 一律
                    // 塌成裸 'Type error'。
                    if (/^The (?:provided value|value provided|start provided|end provided|duration|mode value)\b/
                            .test(body)
                            || /^The (?:start|end) value provided\b/.test(body)
                            || /^The type provided/.test(body)
                            || /^Overload resolution failed\.$/.test(body)
                            || /^Changing to the type provided/.test(body)) {
                        return 'Type error';
                    }
                    return body;
                };
                var __addIfaceObj = function (name, spec) {
                    var existing = (typeof window[name] !== 'undefined')
                        ? window[name] : null;
                    if (existing && !(spec && spec.repair)) { return; }
                    var mode = (spec && spec.construct) || 'illegal';
                    var noNew = (spec && spec.noNew) || mode;
                    var len = (spec && spec.length) || 0;
                    var init = (spec && spec.init) || {};
                    var argInit = (spec && spec.argInit) || [];
                    // WebKit 的字典参数转换：非 undefined/null 且不是对象
                    // （含可调用对象）→ `Type error`（实测 new MMS(123)）。
                    var dictArgOk = function (v) {
                        if (v === undefined || v === null) { return true; }
                        var t = typeof v;
                        return t === 'object' || t === 'function';
                    };
                    var applyMsg = (noNew === 'illegal')
                        ? 'Illegal constructor'
                        : "Constructor requires 'new' operator";
                    var seed = function (obj, args) {
                        var s = __wkSt(obj);
                        for (var k in init) {
                            if (Object.prototype.hasOwnProperty.call(init, k)) {
                                s[k] = init[k];
                            }
                        }
                        // 构造参数 → 成员：真机 DataCue 的 type 取第 4 个参数
                        // （实测 new DataCue(5, 9, 'V', 'text/plain')）。
                        // 空串占位表示该位不映射（真机 DataCue 前两位是
                        // startTime/endTime，由 TextTrackCue 继承）。
                        for (var i = 0; i < argInit.length; i++) {
                            if (argInit[i] && i < args.length
                                    && args[i] !== undefined) {
                                s[argInit[i]] = args[i];
                            }
                        }
                    };
                    var proto;
                    var out;
                    if (existing) {
                        // repair：Chromium 已有同名接口，保留其原型（实例形态、
                        // 既有成员都不动），只改构造语义与缺口成员。
                        proto = existing.prototype;
                        var base = existing;
                        out = new Proxy(base, {
                            apply: function () {
                                throw new TypeError(applyMsg);
                            },
                            construct: function (t, args) {
                                if (mode === 'illegal') {
                                    throw new TypeError('Illegal constructor');
                                }
                                if (mode === 'args' && args.length < len) {
                                    throw new TypeError('Not enough arguments');
                                }
                                var o;
                                try {
                                    o = Reflect.construct(base, args, t);
                                } catch (e) {
                                    var msg = __wkNormMsg(e && e.message);
                                    if (mode === 'args'
                                            && msg === "Constructor requires 'new' operator") {
                                        msg = 'Not enough arguments';
                                    }
                                    throw new TypeError(msg);
                                }
                                seed(o, args);
                                return o;
                            },
                        });
                        try {
                            Object.defineProperty(out, 'length', {
                                value: len, configurable: true,
                            });
                        } catch (e) {}
                        // 代理没有自己的函数体，Function#toString 会落到目标对象上，
                        // 报成 V8 的 `function () { [native code] }`；JSC 的同名
                        // 构造器是 `function MediaError() {...}`（/tmp/f404）。
                        try { __maskNative(out, name, null, true); } catch (e) {}
                        try {
                            Object.defineProperty(proto, 'constructor', {
                                value: out, writable: true,
                                enumerable: false, configurable: true,
                            });
                        } catch (e) {}
                    } else {
                        var target = __maskNative(function () {
                            if (mode === 'illegal') {
                                throw new TypeError('Illegal constructor');
                            }
                        }, name, null, true);
                        try {
                            Object.defineProperty(target, 'length', {
                                value: len, configurable: true,
                            });
                        } catch (e) {}
                        try {
                            Object.defineProperty(target, 'prototype', {
                                writable: false,
                            });
                        } catch (e) {}
                        proto = target.prototype;
                        // 原型链：真机 DataCue.prototype → TextTrackCue.prototype
                        // → EventTarget.prototype 等（实测 f25chain.py）。
                        var parent = null;
                        if (spec && spec.extends) {
                            try { parent = window[spec.extends]; } catch (e) {}
                        }
                        if (parent && parent.prototype) {
                            try {
                                Object.setPrototypeOf(proto, parent.prototype);
                            } catch (e) {}
                            // 构造器本身也要接到父构造器上：真机
                            // `Object.getPrototypeOf(ManagedMediaSource) === MediaSource`
                            // （静态成员随原型链继承，F-130 的 isTypeSupported
                            // 就是继承来的）。合成构造器默认挂在 Object.prototype。
                            try {
                                Object.setPrototypeOf(target, parent);
                            } catch (e) {}
                        }
                        out = new Proxy(target, {
                            apply: function () {
                                throw new TypeError(applyMsg);
                            },
                            construct: function (t, args) {
                                if (mode === 'illegal') {
                                    throw new TypeError('Illegal constructor');
                                }
                                if (mode === 'args' && args.length < len) {
                                    throw new TypeError('Not enough arguments');
                                }
                                if (spec && spec.dictArg && !dictArgOk(args[0])) {
                                    throw new TypeError('Type error');
                                }
                                var o = null;
                                if (spec && spec.realBase && parent
                                        && typeof parent === 'function') {
                                    // F-152：合成实例必须带 Blink 内部槽——真机
                                    // ManagedMediaSource 就是 MediaSource 的子类，
                                    // 继承来的 readyState/duration/sourceBuffers
                                    // 与 URL.createObjectURL 的类型检查都靠它。
                                    // 原型仍是本接口的 proto（t.prototype）。
                                    try {
                                        o = Reflect.construct(parent, [], t);
                                    } catch (e) { o = null; }
                                }
                                if (!o) { o = Reflect.construct(target, args, t); }
                                seed(o, args);
                                return o;
                            },
                        });
                        Object.defineProperty(proto, 'constructor', {
                            value: out, writable: true,
                            enumerable: false, configurable: true,
                        });
                    }
                    try {
                        Object.defineProperty(proto, Symbol.toStringTag, {
                            value: name, writable: false, enumerable: false,
                            configurable: true,
                        });
                    } catch (e) {}
                    // members：'!m' = 方法；'=m' = 可写属性；其余为只读 getter
                    var memberOk = null;
                    if (spec && spec.realBase && parent && parent.prototype) {
                        // 合成实例带父接口的真实内部槽，接收者校验也走父接口
                        // 的原生 getter（真机 Object.create(原型) 不算实例）。
                        var probeGet = null;
                        try {
                            var pd = Object.getOwnPropertyDescriptor(
                                parent.prototype, 'readyState');
                            probeGet = (pd && pd.get) || null;
                        } catch (e) {}
                        if (probeGet) {
                            memberOk = function (recv) {
                                if (recv === null || recv === undefined) { return false; }
                                try { probeGet.call(recv); return true; }
                                catch (e) { return false; }
                            };
                        }
                    }
                    ((spec && spec.members) || []).forEach(function (m) {
                        var kind = 'ro';
                        var member = m;
                        if (m.charAt(0) === '!') {
                            kind = 'method'; member = m.slice(1);
                        } else if (m.charAt(0) === '=') {
                            kind = 'rw'; member = m.slice(1);
                        }
                        try {
                            if (existing && (member in proto)) { return; }
                            if (kind === 'method') {
                                __wkMethod(proto, name, member, memberOk);
                            } else {
                                __wkProp(proto, name, member, kind === 'rw', init,
                                         memberOk);
                            }
                        } catch (e) {}
                    });
                    // 静态方法（F-130）：WebKit 的 ManagedMediaSource 继承
                    // MediaSource，`isTypeSupported` 是继承来的静态方法；
                    // Blink 的 ManagedMediaSource 是另一个类、没有该静态。
                    // 判定走 F-142 的 MSE 独立真值表（与 MediaSource 同表，
                    // 与 Worker 侧同表）——不能沿用 canPlayType 白名单。
                    ((spec && spec.statics) || []).forEach(function (s) {
                        var sname = (s.charAt(0) === '!') ? s.slice(1) : s;
                        // 只看自有属性：构造器已接到父构造器上（静态成员可
                        // 经原型链继承到），`in` 会把继承来的同名静态误判成
                        // "已存在"而跳过定义。
                        if (Object.prototype.hasOwnProperty.call(out, sname)) { return; }
                        if (sname === 'isTypeSupported') {
                            try {
                                var fn = __maskNative(function (t) {
                                    if (!arguments.length) {
                                        throw new TypeError('Not enough arguments');
                                    }
                                    return __mseSupports(t, D.mseTypes);
                                }, sname);
                                try {
                                    Object.defineProperty(fn, 'length', {
                                        value: 1, configurable: true,
                                    });
                                } catch (e3) {}
                                Object.defineProperty(out, sname, {
                                    value: fn, writable: true,
                                    enumerable: true, configurable: true,
                                });
                            } catch (e) {}
                        }
                    });
                    if (!existing) {
                        Object.defineProperty(window, name, {
                            value: out, writable: true,
                            enumerable: false, configurable: true,
                        });
                    } else {
                        // repair：原型保留，但构造器必须换成代理，否则真机文案
                        // 永远走不到（Chromium 原构造器仍在 window 上）。
                        try {
                            Object.defineProperty(window, name, {
                                value: out, writable: true,
                                enumerable: false, configurable: true,
                            });
                        } catch (e) {}
                    }
                };
                var __addIface = __add.interfaces || {};
                for (var __ifName in __addIface) {
                    if (Object.prototype.hasOwnProperty.call(__addIface, __ifName)) {
                        __addIfaceObj(__ifName, __addIface[__ifName]);
                    }
                }
                // 既有原型：'!m' = 方法（返回 value）；'=m' = 可写属性（初值 value）；
                // singleton = 与接口同实例的只读单例（F-84：navigator.audioSession
                // 与 window.AudioSession 同源，真机每次读同一个实例、零自有属性）。
                (__add.protos || []).forEach(function (r) {
                    var C = window[r.ctor];
                    if (!C || !C.prototype) { return; }
                    if (r.name in C.prototype) { return; }
                    try {
                        if (r.singleton) {
                            var S = window[r.singleton];
                            if (!S || !S.prototype) { return; }
                            var inst = Object.create(S.prototype);
                            try {
                                var st = __wkSt(inst);
                                var init = r.init || {};
                                for (var ik in init) {
                                    if (Object.prototype.hasOwnProperty.call(init, ik)) {
                                        st[ik] = init[ik];
                                    }
                                }
                            } catch (e) {}
                            Object.defineProperty(C.prototype, r.name, {
                                get: __maskNative(function () { return inst; },
                                                  'get ' + r.name),
                                enumerable: true, configurable: true,
                            });
                            return;
                        }
                        if (r.kind === 'fn') {
                            var __rv = r.value;
                            defineFn(C.prototype, r.name, function () { return __rv; });
                        } else if (r.kind === 'rw') {
                            var __wv = r.value;
                            Object.defineProperty(C.prototype, r.name, {
                                get: __maskNative(function () {
                                    return this['__wk_' + r.name] !== undefined
                                        ? this['__wk_' + r.name] : __wv;
                                }, 'get ' + r.name),
                                set: __maskNative(function (v) {
                                    this['__wk_' + r.name] = v;
                                }, 'set ' + r.name),
                                enumerable: true, configurable: true,
                            });
                        } else {
                            var __gv = r.value;
                            Object.defineProperty(C.prototype, r.name, {
                                get: __maskNative(function () { return __gv; },
                                                  'get ' + r.name),
                                enumerable: true, configurable: true,
                            });
                        }
                    } catch (e) {}
                });
                (__add.constants || []).forEach(function (c) {
                    var C = window[c.ctor];
                    if (!C) { return; }
                    __addConst(C, c.name, c.value);
                    if (C.prototype) { __addConst(C.prototype, c.name, c.value); }
                });
                // 静态量：DataView.BYTES_PER_ELEMENT（WebKit 里是 1，JS 里被
                // 实现成构造函数上的普通属性）、RegExp.multiline（legacy）。
                (__add.statics || []).forEach(function (s) {
                    var C = window[s.ctor];
                    if (!C || (s.name in C)) { return; }
                    try {
                        Object.defineProperty(C, s.name, {
                            value: s.value, writable: true,
                            enumerable: false, configurable: true,
                        });
                    } catch (e) {}
                });
                // 事件处理属性：Window / Document / HTMLElement / SVGElement 上的
                // copy/cut/paste + Force Touch + touch 家族。真机是 [Replaceable]
                // 访问器：未赋值 null，赋值后保存函数对象。可枚举性按行给（真机
                // copy/cut/paste 为 true，webkitmouseforce* / touchforcechange /
                // ontouch* 为 false——/tmp/f77p/hosts.py 实测）。
                //
                // 内核在 DOMContentLoaded 时会把 ontouch* 一族**重新**装成可枚举
                // 访问器（实测 fp-chromium 148：init 脚本里改完立即生效，但 DCL
                // 一到又回到 enumerable:true），因此安装函数要能重入，并在 DCL
                // 与 load 时各补一次。
                var __installHandlers = function () {
                    (__add.handlers || []).forEach(function (h) {
                        var host = (h.ctor === 'window') ? window
                            : (window[h.ctor] && window[h.ctor].prototype);
                        if (!host) { return; }
                        var want = (h.enum !== false);
                        try {
                            if (h.name in host) {
                                // 内核已装（ontouch* 由触控仿真原生安装）。只纠正
                                // 自有访问器的可枚举性，不动原生实现。描述符必须
                                // 按种类重建：访问器不能带 value/writable，数据
                                // 属性不能带 get/set，混写会直接抛 TypeError。
                                var ex = Object.getOwnPropertyDescriptor(host, h.name);
                                if (ex && ex.configurable && ex.enumerable !== want) {
                                    var nd;
                                    if (ex.get || ex.set) {
                                        nd = {get: ex.get, set: ex.set,
                                              enumerable: want, configurable: true};
                                    } else {
                                        nd = {value: ex.value, writable: ex.writable,
                                              enumerable: want, configurable: true};
                                    }
                                    Object.defineProperty(host, h.name, nd);
                                }
                                return;
                            }
                            var key = '__wkH_' + h.name;
                            var evName = h.name.replace(/^on/, '');
                            Object.defineProperty(host, h.name, {
                                get: __maskNative(function () { return this[key] || null; },
                                                  'get ' + h.name),
                                set: __maskNative(function (v) {
                                    if (typeof v === 'function' && !this[key]) {
                                        var self = this;
                                        try {
                                            this.addEventListener(evName,
                                                function (ev) { return v.call(self, ev); });
                                        } catch (e) {}
                                    }
                                    this[key] = (typeof v === 'function') ? v : null;
                                }, 'set ' + h.name),
                                enumerable: want, configurable: true,
                            });
                        } catch (e) {}
                    });
                };
                __installHandlers();
                try {
                    if (document.readyState === 'loading') {
                        document.addEventListener('DOMContentLoaded', __installHandlers);
                    }
                    window.addEventListener('load', __installHandlers);
                } catch (e) {}
                // 全局项三分支（真机形态实测 /tmp/f77p/gdesc.py、odb5.py）：
                //   alias    —— 只读 getter，值即 window[alias]（SameObject）；
                //   accessor —— get/set 访问器、enumerable false；未赋值读作
                //               undefined（真机 getter 返回的是 JSC 内部函数
                //               对象，typeof 同样是 "undefined"，V8 造不出
                //               这种对象——`typeof openDatabase` 这一行判据
                //               已对齐，代价是 `== null`/调用文案不同）；
                //   object   —— 可写数据属性（w/e/c 全 true），子成员带自定义
                //               toStringTag。
                var __addGlobals = __add.globals || {};
                Object.keys(__addGlobals).forEach(function (gname) {
                    var g = __addGlobals[gname];
                    if (!g || typeof g !== 'object') { return; }
                    try {
                        if (g.alias) {
                            Object.defineProperty(window, gname, {
                                get: __maskNative(function () { return window[g.alias]; },
                                                  'get ' + gname),
                                enumerable: true, configurable: true,
                            });
                            return;
                        }
                        if (g.accessor === 'undefined') {
                            if (gname in window) { return; }
                            var store = undefined;
                            Object.defineProperty(window, gname, {
                                get: __maskNative(function () { return store; },
                                                  'get ' + gname),
                                set: __maskNative(function (v) { store = v; },
                                                  'set ' + gname),
                                enumerable: false, configurable: true,
                            });
                            return;
                        }
                        if (g.object) {
                            var obj = {};
                            Object.keys(g.object).forEach(function (k) {
                                var sub = g.object[k] || {};
                                var v = {};
                                if (sub.toStringTag) {
                                    Object.defineProperty(v, Symbol.toStringTag, {
                                        value: sub.toStringTag, configurable: true,
                                    });
                                    Object.defineProperty(v, 'toString', {
                                        value: __maskNative(function () {
                                            return '[object ' + sub.toStringTag + ']';
                                        }, 'toString'),
                                        writable: true, enumerable: false,
                                        configurable: true,
                                    });
                                }
                                Object.defineProperty(obj, k, {
                                    value: v, writable: true,
                                    enumerable: true, configurable: true,
                                });
                            });
                            Object.defineProperty(window, gname, {
                                value: obj, writable: true,
                                enumerable: true, configurable: true,
                            });
                        }
                    } catch (e) {}
                });
                // F-29 / F-30：表单控件默认样式。宿主 Chromium（macOS）的 UA
                // 样式表把控件字体定为 sans-serif→Arial、<button> 高度 25px；
                // 真机 iOS WebKit 是 system-ui、button 18 / input 21 /
                // select 20 / textarea 32（实测 /tmp/wkprobe/forms2.py）。
                // ---- F-150 / F-151 / F-157：MediaSource / SourceBuffer 面 ----
                // 真机口径（实测 /tmp/f347 /tmp/f349 /tmp/f350 /tmp/f353 /tmp/f354）：
                //   1) addSourceBuffer / changeType 的类型表要与 isTypeSupported 同
                //      一张（__mseSupports）。Blink 的表更窄（拒 mp4 裸容器、
                //      quicktime、mp2t、ac-3/ec-3/alac/ulaw/samr、dvh1/dvhe、
                //      mp4v.20.x、ogg opus/vorbis），又更宽（收 audio/mp4 的
                //      opus/flac 与 av01）。
                //   2) 异常文案：TypeError 走 __wkNormMsg，DOMException 一律换成
                //      WebKit 的 legacy message（Blink 每条都带 'Failed to ...'
                //      前缀且措辞自有）。
                //   3) 接收者校验：WebKit 是 'Can only call X.m on instances of X'，
                //      Blink 是 'Illegal invocation'；真机连 Object.create(proto)
                //      也不认，故用宿主原生 getter 探针判定，不走原型链。
                var __WK_DOM_MSG = {
                    NotSupportedError: 'The operation is not supported.',
                    InvalidStateError: 'The object is in an invalid state.',
                    NotFoundError: 'The object can not be found here.',
                    IndexSizeError: 'The index is not in the allowed range.',
                    InvalidAccessError:
                        'The object does not support the operation or argument.',
                    QuotaExceededError: 'The quota has been exceeded.',
                    AbortError: 'The operation was aborted.',
                    InvalidModificationError:
                        'The object can not be modified in this way.',
                    SecurityError: 'The operation is insecure.',
                    SyntaxError: 'The string did not match the expected pattern.',
                    UnknownError:
                        'The operation failed for an unknown transient reason (e.g. out of memory).',
                    DataCloneError: 'The object can not be cloned.',
                    NetworkError: 'A network error occurred.',
                    EncodingError:
                        'The encoding operation (either encoded or decoding) failed.',
                    TimeoutError: 'The operation timed out.',
                };
                var __wkNormErr = function (e) {
                    try {
                        if (!e || typeof e !== 'object') { return e; }
                        if (typeof DOMException !== 'undefined'
                                && e instanceof DOMException) {
                            var dm = __WK_DOM_MSG[e.name];
                            if (dm) {
                                try { return new DOMException(dm, e.name); }
                                catch (e2) { return e; }
                            }
                            return e;
                        }
                        if (e.name === 'TypeError') {
                            return new TypeError(__wkNormMsg(e.message));
                        }
                    } catch (e3) {}
                    return e;
                };
                var __wkMseContainer = function (t) {
                    return String(t).split(';')[0].trim().toLowerCase();
                };
                // 真机认、宿主不认的类型：换一个宿主认得的等价类型去建 buffer。
                // SourceBuffer 没有 type 属性，替换不可观测；mode 只由容器决定，
                // 故音频裸流仍用 audio/mpeg 占位以保持 sequence。
                var __wkStandIn = function (t) {
                    var c = __wkMseContainer(t);
                    if (c === 'audio/mpeg' || c === 'audio/aac') {
                        return 'audio/mpeg';
                    }
                    return 'video/mp4; codecs="avc1.42E01E"';
                };
                // 原生 getter 当品牌探针：宿主自己判定内部槽，比原型链精确
                // （Object.create(MediaSource.prototype) 在真机上也不是实例）。
                var __wkBrandWrap = function (proto, iface, probeName, skip) {
                    if (!proto) { return null; }
                    var probe = null;
                    try {
                        var pd = Object.getOwnPropertyDescriptor(proto, probeName);
                        probe = (pd && pd.get) || null;
                    } catch (e) {}
                    var ok = function (recv) {
                        if (!probe) { return __slotHas(proto, recv); }
                        try { probe.call(recv); return true; }
                        catch (e) { return false; }
                    };
                    var names = [];
                    try { names = Object.getOwnPropertyNames(proto); }
                    catch (e) { return ok; }
                    names.forEach(function (n) {
                        if (n === 'constructor') { return; }
                        if (skip && skip.indexOf(n) >= 0) { return; }
                        var d;
                        try { d = Object.getOwnPropertyDescriptor(proto, n); }
                        catch (e) { return; }
                        if (!d) { return; }
                        try {
                            if (typeof d.value === 'function') {
                                var orig = d.value;
                                var impl = function () {
                                    if (!ok(this)) {
                                        throw __wkBrandErr(iface, n, 'method');
                                    }
                                    try { return orig.apply(this, arguments); }
                                    catch (e) { throw __wkNormErr(e); }
                                };
                                try {
                                    Object.defineProperty(impl, 'length', {
                                        value: orig.length, configurable: true,
                                    });
                                } catch (e) {}
                                Object.defineProperty(proto, n, {
                                    value: __maskNative(impl, n),
                                    writable: true,
                                    enumerable: d.enumerable,
                                    configurable: true,
                                });
                            } else if (d.get || d.set) {
                                var ng = d.get ? function () {
                                    if (!ok(this)) {
                                        throw __wkBrandErr(iface, n, 'getter');
                                    }
                                    try { return d.get.call(this); }
                                    catch (e) { throw __wkNormErr(e); }
                                } : undefined;
                                var ns = d.set ? function (v) {
                                    if (!ok(this)) {
                                        throw __wkBrandErr(iface, n, 'setter');
                                    }
                                    try { return d.set.call(this, v); }
                                    catch (e) { throw __wkNormErr(e); }
                                } : undefined;
                                Object.defineProperty(proto, n, {
                                    get: ng && __maskNative(ng, 'get ' + n),
                                    set: ns && __maskNative(ns, 'set ' + n),
                                    enumerable: d.enumerable,
                                    configurable: true,
                                });
                            }
                        } catch (e) {}
                    });
                    return ok;
                };
                // ---- F-155 残项：MediaError 面 ----
                // 真机文案是固定串（WebKit 源码 hme.cpp 3012-3081 / 3561 /
                // 7118，/tmp/f403 实测）：
                //   code 1 → 'Load was aborted'      code 2 → 'Media failed to load'
                //   code 3 → 'Media failed to decode' code 4 → 播放器 lastErrorMessage
                //   code 5 → 'Media is encrypted'
                // code 4 取播放器的 lastErrorMessage，实测是空串（WebKit 的
                // MediaPlayerPrivateAVFoundationObjC 在该路径不填）。Blink 却把
                // 内部串原样交给页面（'PipelineStatus::CHUNK_DEMUXER_ERROR_…'、
                // 'MEDIA_ELEMENT_ERROR: …'），既泄漏引擎也偏离真机，一律改写。
                // 构造文案与缺的 MEDIA_ERR_ENCRYPTED 走 _IOS_ADD_SPEC 的
                // repair + constants；原型成员顺序因 Blink 的常量不可配置而
                // 无法重排（属既有 own-name 顺序残差）。
                if (typeof MediaError !== 'undefined' && MediaError.prototype) {
                    var __meOrig = {};
                    ['code', 'message'].forEach(function (n) {
                        try {
                            __meOrig[n] = Object.getOwnPropertyDescriptor(
                                MediaError.prototype, n) || null;
                        } catch (e) { __meOrig[n] = null; }
                    });
                    var __ME_MSG = { 1: 'Load was aborted',
                                     2: 'Media failed to load',
                                     3: 'Media failed to decode',
                                     4: '',
                                     5: 'Media is encrypted' };
                    // 品牌判定直接用宿主 getter：非实例会抛 Illegal invocation。
                    // F-156 的同步窗口要在原生级联之前交出 video.error，故留
                    // 一条旁路：合成实例只把 code 记在 __meVirt 里，品牌、
                    // 文案、toString 仍走真实原型（/tmp/f404 identity 组）。
                    var __meVirt = new WeakMap();
                    var __wkMediaError = function (code) {
                        var o = Object.create(MediaError.prototype);
                        __meVirt.set(o, code);
                        return o;
                    };
                    var __meCode = function (recv) {
                        if (__meVirt.has(recv)) { return __meVirt.get(recv); }
                        var d = __meOrig.code;
                        if (!d || !d.get) { return undefined; }
                        return d.get.call(recv);
                    };
                    var __meOk = function (recv) {
                        if (recv === null || recv === undefined) { return false; }
                        if (__meVirt.has(recv)) { return true; }
                        try { __meCode(recv); return true; }
                        catch (e) { return false; }
                    };
                    if (__meOrig.code && __meOrig.code.get) {
                        try {
                            Object.defineProperty(MediaError.prototype, 'code', {
                                get: __maskNative(function () {
                                    if (!__meOk(this)) {
                                        throw __wkBrandErr('MediaError', 'code',
                                                           'getter');
                                    }
                                    return __meCode(this);
                                }, 'get code'),
                                enumerable: true, configurable: true,
                            });
                        } catch (e) {}
                    }
                    if (__meOrig.message && __meOrig.message.get) {
                        try {
                            Object.defineProperty(MediaError.prototype, 'message', {
                                get: __maskNative(function () {
                                    if (!__meOk(this)) {
                                        throw __wkBrandErr('MediaError',
                                                           'message', 'getter');
                                    }
                                    var c = __meCode(this);
                                    if (Object.prototype.hasOwnProperty
                                            .call(__ME_MSG, c)) {
                                        return __ME_MSG[c];
                                    }
                                    return String(__meOrig.message.get
                                                  .call(this) || '');
                                }, 'get message'),
                                enumerable: true, configurable: true,
                            });
                        } catch (e) {}
                    }
                }
                // 判定顺序照 WebKit 源码（MediaSource::addSourceBuffer 695-759）：
                // 空串 → 类型表 → readyState。类型先于状态，故 closed 态下不支持
                // 的类型仍抛 NotSupportedError（实测 /tmp/f347 closed 组）。
                if (typeof MediaSource !== 'undefined' && MediaSource.prototype) {
                    var __msOk = __wkBrandWrap(MediaSource.prototype, 'MediaSource',
                                               'readyState', ['addSourceBuffer']);
                    try {
                        var __msAddD = Object.getOwnPropertyDescriptor(
                            MediaSource.prototype, 'addSourceBuffer');
                        var __msAddOrig = __msAddD && __msAddD.value;
                        if (typeof __msAddOrig === 'function') {
                            var __msAddImpl = function (type) {
                                if (__msOk && !__msOk(this)) {
                                    throw __wkBrandErr('MediaSource',
                                                       'addSourceBuffer', 'method');
                                }
                                if (!arguments.length) {
                                    throw new TypeError('Not enough arguments');
                                }
                                if (typeof type === 'symbol') {
                                    throw new TypeError(
                                        'Cannot convert a symbol to a string');
                                }
                                var t = String(type);
                                if (t === '') { throw new TypeError('Type error'); }
                                if (!__mseSupports(t, D.mseTypes)) {
                                    throw new DOMException(
                                        __WK_DOM_MSG.NotSupportedError,
                                        'NotSupportedError');
                                }
                                try {
                                    return __msAddOrig.call(this, t);
                                } catch (e) {
                                    var ne = __wkNormErr(e);
                                    if (ne && ne.name === 'NotSupportedError') {
                                        try {
                                            return __msAddOrig.call(
                                                this, __wkStandIn(t));
                                        } catch (e2) {
                                            throw __wkNormErr(e2);
                                        }
                                    }
                                    throw ne;
                                }
                            };
                            try {
                                Object.defineProperty(__msAddImpl, 'length', {
                                    value: 1, configurable: true,
                                });
                            } catch (e) {}
                            Object.defineProperty(MediaSource.prototype,
                                                  'addSourceBuffer', {
                                value: __maskNative(__msAddImpl, 'addSourceBuffer'),
                                writable: true, enumerable: true,
                                configurable: true,
                            });
                        }
                    } catch (e) {}
                }
                // SourceBuffer::changeType（470-532）的顺序：空串 → isRemoved()/
                // updating → 类型表。移除/更新中的状态压过类型判定（实测
                // /tmp/f353：移除后连合法类型也抛 InvalidStateError）。
                if (typeof SourceBuffer !== 'undefined' && SourceBuffer.prototype) {
                    var __sbOk = __wkBrandWrap(SourceBuffer.prototype, 'SourceBuffer',
                                               'updating', ['changeType']);
                    try {
                        var __sbCtD = Object.getOwnPropertyDescriptor(
                            SourceBuffer.prototype, 'changeType');
                        var __sbCtOrig = __sbCtD && __sbCtD.value;
                        if (typeof __sbCtOrig === 'function') {
                            var __sbCtImpl = function (type) {
                                if (__sbOk && !__sbOk(this)) {
                                    throw __wkBrandErr('SourceBuffer',
                                                       'changeType', 'method');
                                }
                                if (!arguments.length) {
                                    throw new TypeError('Not enough arguments');
                                }
                                if (typeof type === 'symbol') {
                                    throw new TypeError(
                                        'Cannot convert a symbol to a string');
                                }
                                var t = String(type);
                                if (t === '') { throw new TypeError('Type error'); }
                                var gone = false;
                                try { void this.mode; } catch (e) { gone = true; }
                                if (gone || this.updating) {
                                    throw new DOMException(
                                        __WK_DOM_MSG.InvalidStateError,
                                        'InvalidStateError');
                                }
                                if (!__mseSupports(t, D.mseTypes)) {
                                    throw new DOMException(
                                        __WK_DOM_MSG.NotSupportedError,
                                        'NotSupportedError');
                                }
                                try {
                                    return __sbCtOrig.call(this, t);
                                } catch (e) {
                                    var ne = __wkNormErr(e);
                                    if (ne && ne.name === 'NotSupportedError') {
                                        try {
                                            return __sbCtOrig.call(
                                                this, __wkStandIn(t));
                                        } catch (e2) {
                                            throw __wkNormErr(e2);
                                        }
                                    }
                                    throw ne;
                                }
                            };
                            try {
                                Object.defineProperty(__sbCtImpl, 'length', {
                                    value: 1, configurable: true,
                                });
                            } catch (e) {}
                            Object.defineProperty(SourceBuffer.prototype,
                                                  'changeType', {
                                value: __maskNative(__sbCtImpl, 'changeType'),
                                writable: true, enumerable: true,
                                configurable: true,
                            });
                        }
                    } catch (e) {}
                }
                // ---- F-154：轨道列表接口 ----
                // AudioTrackList / VideoTrackList / TextTrackList /
                // SourceBufferList 的宿主面。真机口径（/tmp/f356、/tmp/f357，
                // WebKit 27.2）：
                //   · 实例零自有属性（keys / getOwnPropertyNames 均为空），
                //     成员全在原型上；同一宿主重复读取恒等（可缓存实例）；
                //   · length 恒 0；VideoTrackList.selectedIndex 恒 -1；
                //   · item() / getTrackById() 恒返回 null，缺参抛
                //     `Not enough arguments`；接收者校验先于参数个数校验；
                //   · 接收者校验走内部槽，Object.create(原型) 不算实例；
                //   · 事件处理器属性未赋值读 null，赋值不产生自有属性。
                var __wkListBrand = new WeakSet();
                var __wkListState = new WeakMap();
                var __wkListCache = new WeakMap();
                var __wkListCfg = {
                    AudioTrackList: {
                        on: ['onchange', 'onaddtrack', 'onremovetrack'],
                        sel: false, byId: true,
                        order: ['constructor', 'length', 'onchange', 'onaddtrack',
                                'onremovetrack', 'item', 'getTrackById'],
                    },
                    VideoTrackList: {
                        on: ['onchange', 'onaddtrack', 'onremovetrack'],
                        sel: true, byId: true,
                        order: ['constructor', 'length', 'selectedIndex',
                                'onchange', 'onaddtrack', 'onremovetrack', 'item',
                                'getTrackById'],
                    },
                    // TextTrackList 的事件顺序与 ATL/VTL 不同：onaddtrack 在
                    // onchange 之前（实测）。
                    TextTrackList: {
                        on: ['onaddtrack', 'onchange', 'onremovetrack'],
                        sel: false, byId: true,
                        order: ['constructor', 'length', 'onaddtrack', 'onchange',
                                'onremovetrack', 'item', 'getTrackById'],
                    },
                    SourceBufferList: {
                        on: ['onaddsourcebuffer', 'onremovesourcebuffer'],
                        sel: false, byId: false,
                        order: ['constructor', 'length', 'onaddsourcebuffer',
                                'onremovesourcebuffer', 'item'],
                    },
                };
                // 宿主 → 列表实例。真机列表对象挂在宿主内部，不占元素的自有
                // 属性（Object.getOwnPropertyNames(video) 为空），故用 WeakMap。
                var __wkList = function (name, host) {
                    var C = null;
                    try { C = window[name]; } catch (e) {}
                    if (!C || !C.prototype) { return null; }
                    if (host === null || host === undefined) { return null; }
                    var ht = typeof host;
                    if (ht !== 'object' && ht !== 'function') { return null; }
                    var cache = __wkListCache.get(host);
                    if (!cache) {
                        cache = Object.create(null);
                        __wkListCache.set(host, cache);
                    }
                    if (!cache[name]) {
                        var inst = Object.create(C.prototype);
                        __wkListBrand.add(inst);
                        cache[name] = inst;
                    }
                    return cache[name];
                };
                // 原型成员顺序可被 getOwnPropertyNames 读到，真机是生成器的
                // 声明顺序（构造器 → 数据成员 → 事件处理器 → 方法）。成员
                // 全部 configurable，删除后按表重建即可；表外的成员排末尾，
                // 不会被静默丢掉。
                var __wkOrder = function (proto, names) {
                    if (!proto) { return; }
                    var all = [];
                    try { all = Object.getOwnPropertyNames(proto); }
                    catch (e) { return; }
                    var ds = {};
                    all.forEach(function (n) {
                        try { ds[n] = Object.getOwnPropertyDescriptor(proto, n); }
                        catch (e) {}
                    });
                    var seq = names.filter(function (n) { return !!ds[n]; });
                    all.forEach(function (n) {
                        if (names.indexOf(n) < 0) { seq.push(n); }
                    });
                    seq.forEach(function (n) {
                        try { delete proto[n]; } catch (e) {}
                    });
                    seq.forEach(function (n) {
                        try { Object.defineProperty(proto, n, ds[n]); }
                        catch (e) {}
                    });
                };
                var __wkListSetup = function (name) {
                    var C = null;
                    try { C = window[name]; } catch (e) {}
                    var cfg = __wkListCfg[name];
                    if (!C || !C.prototype || !cfg) { return; }
                    var proto = C.prototype;
                    // 这些接口在 Blink 里可能本来就有真实例（video.textTracks、
                    // ms.sourceBuffers），它们带内部槽、行为与真机一致，不能
                    // 一律当假接收者拒掉。先存下原成员，合成实例走 canned 值，
                    // 真实例转交原实现。
                    var orig = {};
                    ['length', 'selectedIndex', 'item', 'getTrackById']
                        .concat(cfg.on).forEach(function (m) {
                            try { orig[m] = Object.getOwnPropertyDescriptor(proto, m) || null; }
                            catch (e) { orig[m] = null; }
                        });
                    // 用原生 length 访问器探内部槽：真实例能过，Object.create(原型)
                    // 与普通对象过不了。Blink 的 AudioTrackList / VideoTrackList
                    // 根本没有接收者校验（任何接收者都读 undefined），这两个
                    // 接口也就没有「真实例」可让，只留合成实例一条路。
                    var canProbe = false;
                    try {
                        if (orig.length && orig.length.get) {
                            orig.length.get.call(Object.create(C.prototype));
                        }
                    } catch (e) { canProbe = true; }
                    var realOk = function (recv) {
                        if (!canProbe) { return false; }
                        try { orig.length.get.call(recv); return true; }
                        catch (e) { return false; }
                    };
                    var st = function (o) {
                        var s = __wkListState.get(o);
                        if (!s) { s = Object.create(null); __wkListState.set(o, s); }
                        return s;
                    };
                    var defAcc = function (member, get, set) {
                        var d0 = orig[member];
                        var d = {
                            get: __maskNative(function () {
                                if (__wkListBrand.has(this)) { return get(this); }
                                if (realOk(this)) {
                                    return (d0 && d0.get) ? d0.get.call(this) : get(this);
                                }
                                throw __wkBrandErr(name, member, 'getter');
                            }, 'get ' + member),
                            enumerable: true, configurable: true,
                        };
                        if (set) {
                            d.set = __maskNative(function (v) {
                                if (__wkListBrand.has(this)) { set(this, v); return; }
                                if (realOk(this)) {
                                    if (d0 && d0.set) { d0.set.call(this, v); return; }
                                    set(this, v);
                                    return;
                                }
                                throw __wkBrandErr(name, member, 'setter');
                            }, 'set ' + member);
                        }
                        Object.defineProperty(proto, member, d);
                    };
                    // Blink 的 item() 越界返回 undefined，真机一律 null
                    // （WebKit 的列表接口都返回 RefPtr，空位即 null）。
                    var nullItem = function (v) {
                        return (v === undefined) ? null : v;
                    };
                    var defFn = function (member, fn, realFallback) {
                        var d0 = orig[member];
                        // 接口修复轮（__wkMethod）可能已在原型上装了占位实现，
                        // 那不是宿主原生方法，不能当真实例的实现转交。
                        var native = (d0 && typeof d0.value === 'function'
                                      && !__wkStubFn.has(d0.value)) ? d0.value : null;
                        var impl = function () {
                            if (__wkListBrand.has(this)) {
                                return fn.apply(this, arguments);
                            }
                            if (realOk(this)) {
                                if (!arguments.length) {
                                    throw new TypeError('Not enough arguments');
                                }
                                try {
                                    if (native) {
                                        return nullItem(native.apply(this,
                                                                    arguments));
                                    }
                                    // Blink 的 SourceBufferList / 轨道列表
                                    // 根本没有 item()，真实例退回按下标取。
                                    if (realFallback) {
                                        return realFallback.apply(this, arguments);
                                    }
                                    return nullItem(fn.apply(this, arguments));
                                } catch (e) {
                                    throw __wkNormErr(e);
                                }
                            }
                            throw __wkBrandErr(name, member, 'method');
                        };
                        try {
                            Object.defineProperty(impl, 'length', {
                                value: fn.length, configurable: true,
                            });
                        } catch (e) {}
                        Object.defineProperty(proto, member, {
                            value: __maskNative(impl, member), writable: true,
                            enumerable: true, configurable: true,
                        });
                    };
                    defAcc('length', function () { return 0; });
                    if (cfg.sel) {
                        defAcc('selectedIndex', function () { return -1; });
                    }
                    cfg.on.forEach(function (m) {
                        defAcc(m, function (o) {
                            var v = st(o)[m];
                            return (v === undefined) ? null : v;
                        }, function (o, v) { st(o)[m] = v; });
                    });
                    defFn('item', function (i) {
                        if (!arguments.length) {
                            throw new TypeError('Not enough arguments');
                        }
                        return null;
                    }, function (i) {
                        // 真机 item() 越界返回 null；合成实例恒空，走上面的
                        // 分支，这里只服务 Blink 的真实例。
                        var v = this[i >>> 0];
                        return (v === undefined) ? null : v;
                    });
                    if (cfg.byId) {
                        defFn('getTrackById', function (id) {
                            if (!arguments.length) {
                                throw new TypeError('Not enough arguments');
                            }
                            return null;
                        });
                    }
                    __wkOrder(proto, cfg.order);
                };
                ['AudioTrackList', 'VideoTrackList', 'TextTrackList',
                 'SourceBufferList'].forEach(__wkListSetup);
                // MediaSource / SourceBuffer 的成员顺序同样是可读的指纹，
                // 真机顺序见 /tmp/f357。
                try {
                    if (typeof MediaSource !== 'undefined' && MediaSource.prototype) {
                        __wkOrder(MediaSource.prototype,
                            ['constructor', 'sourceBuffers', 'activeSourceBuffers',
                             'readyState', 'duration', 'onsourceopen',
                             'onsourceended', 'onsourceclose', 'addSourceBuffer',
                             'removeSourceBuffer', 'endOfStream',
                             'setLiveSeekableRange', 'clearLiveSeekableRange']);
                    }
                } catch (e) {}
                try {
                    if (typeof SourceBuffer !== 'undefined' && SourceBuffer.prototype) {
                        __wkOrder(SourceBuffer.prototype,
                            ['constructor', 'mode', 'updating', 'buffered',
                             'timestampOffset', 'audioTracks', 'videoTracks',
                             'textTracks', 'appendWindowStart', 'appendWindowEnd',
                             'onupdatestart', 'onupdate', 'onupdateend', 'onerror',
                             'onabort', 'appendBuffer', 'abort', 'remove',
                             'changeType']);
                    }
                } catch (e) {}
                // ---- F-155：无效段 append 的错误级联 ----
                // 真机 WebKit 对同一坨垃圾段走 `sb:err → sb:ue → ms:ended →
                // video err → ms:close` 并逐出 SB；Blink 视作「还不完整，继续
                // 等」静默收下。实测 /tmp/f372~f383（WebKit 27.2）：
                //   · Blink 一旦自己判定解析失败，级联与真机逐项一致（22 项
                //     后置状态全等，/tmp/f383 B/C 组）——所以不必合成级联，
                //     只要让 Blink 的解析器拒掉同样的输入，它自己会走对。
                //   · 故这里只做「预检 + 换载荷」：预检判定真机会报错时，把
                //     载荷换成 Blink 也必拒的 ftyp+空 moov，由 Blink 原生跑
                //     完整条级联。
                // 判定表（全部为基线实测，勿按直觉推）：
                //   size==0 / size<8            → 报错（任意类型，/tmp/f377）
                //   截断（size 超出剩余字节）    → 不报错，等更多数据（/tmp/f376）
                //   ftyp 品牌不可打印或体不足 8  → 报错（/tmp/f377 ftyp8）
                //   moov 非完整可解析           → 报错（/tmp/f376 逐级截断：
                //                                 需 mvhd + 至少一条走通
                //                                 mdia/minf/stbl/stsd 的 trak）
                //   mdat：已见 ftyp 且前一盒非 moof → 报错（/tmp/f374/f379/f382；
                //         mdat 在 ftyp 之前、或紧跟 moof、或未见 ftyp 时不报错）
                //   emsg：已见 ftyp → 报错（/tmp/f372、/tmp/f375）
                //   styp：已见 ftyp 但尚未解析出 moov → 报错（/tmp/f372 vs f382）
                //   free/skip/wide/pdin/meta/sidx/moof/未知类型 → 不报错
                var __wkBoxKids = function (u, s, e) {
                    var out = [];
                    var i = s;
                    while (i + 8 <= e) {
                        var sz = ((u[i] << 24) | (u[i + 1] << 16)
                                  | (u[i + 2] << 8) | u[i + 3]) >>> 0;
                        var body = i + 8;
                        if (sz === 1) {
                            sz = ((u[i + 12] << 24) | (u[i + 13] << 16)
                                  | (u[i + 14] << 8) | u[i + 15]) >>> 0;
                            body = i + 16;
                        }
                        if (sz < 8 || i + sz > e) { break; }
                        out.push({ t: String.fromCharCode(u[i + 4], u[i + 5],
                                                          u[i + 6], u[i + 7]),
                                   b: body, e: i + sz });
                        i += sz;
                    }
                    return out;
                };
                var __wkKid = function (u, s, e, t) {
                    var ks = __wkBoxKids(u, s, e);
                    for (var i = 0; i < ks.length; i++) {
                        if (ks[i].t === t) { return ks[i]; }
                    }
                    return null;
                };
                var __wkTrakOk = function (u, s, e) {
                    var mdia = __wkKid(u, s, e, 'mdia');
                    if (!mdia) { return false; }
                    var minf = __wkKid(u, mdia.b, mdia.e, 'minf');
                    if (!minf) { return false; }
                    var stbl = __wkKid(u, minf.b, minf.e, 'stbl');
                    if (!stbl) { return false; }
                    var stsd = __wkKid(u, stbl.b, stbl.e, 'stsd');
                    if (!stsd || stsd.e - stsd.b < 8) { return false; }
                    var cnt = ((u[stsd.b + 4] << 24) | (u[stsd.b + 5] << 16)
                               | (u[stsd.b + 6] << 8) | u[stsd.b + 7]) >>> 0;
                    return cnt > 0;
                };
                var __wkMoovOk = function (u, s, e) {
                    var ks = __wkBoxKids(u, s, e);
                    var mvhd = false, trak = false;
                    for (var i = 0; i < ks.length; i++) {
                        if (ks[i].t === 'mvhd') { mvhd = true; }
                        else if (ks[i].t === 'trak'
                                 && __wkTrakOk(u, ks[i].b, ks[i].e)) { trak = true; }
                    }
                    return mvhd && trak;
                };
                var __wkFtypOk = function (u, s, e) {
                    if (e - s < 8) { return false; }
                    for (var i = s; i < s + 4; i++) {
                        if (u[i] < 0x20 || u[i] > 0x7e) { return false; }
                    }
                    return true;
                };
                // 返回 true 表示「真机会报解析错误」。st 是每 SB 的流状态
                // （已见 ftyp / 已解析出 moov），跨 append 累计。状态边走边
                // 写：截断时提前返回，但已完整读过的盒仍要记账——MSE 的
                // 分片 append（ftyp 一片、moov 一片）靠这个延续判定。
                var __wkMseBad = function (buf, st) {
                    var u;
                    try {
                        u = (buf instanceof ArrayBuffer)
                            ? new Uint8Array(buf) : new Uint8Array(buf.buffer,
                                buf.byteOffset, buf.byteLength);
                    } catch (e) { return false; }
                    var i = 0, prevMoof = false;
                    while (i + 8 <= u.length) {
                        var sz = ((u[i] << 24) | (u[i + 1] << 16)
                                  | (u[i + 2] << 8) | u[i + 3]) >>> 0;
                        var t = String.fromCharCode(u[i + 4], u[i + 5],
                                                    u[i + 6], u[i + 7]);
                        var body = i + 8;
                        if (sz === 1) {
                            if (i + 16 > u.length) { return false; }
                            var hi = ((u[i + 8] << 24) | (u[i + 9] << 16)
                                      | (u[i + 10] << 8) | u[i + 11]) >>> 0;
                            sz = ((u[i + 12] << 24) | (u[i + 13] << 16)
                                  | (u[i + 14] << 8) | u[i + 15]) >>> 0;
                            if (hi !== 0) { return true; }
                            body = i + 16;
                        }
                        if (sz === 0 || sz < 8) { return true; }
                        if (i + sz > u.length) { return false; }
                        var e = i + sz;
                        if (t === 'ftyp') {
                            if (!__wkFtypOk(u, body, e)) { return true; }
                            st.ftyp = true;
                        } else if (t === 'moov') {
                            if (!__wkMoovOk(u, body, e)) { return true; }
                            st.moov = true;
                        } else if (t === 'mdat') {
                            if (st.ftyp && !prevMoof) { return true; }
                        } else if (t === 'emsg') {
                            if (st.ftyp) { return true; }
                        } else if (t === 'styp') {
                            if (st.ftyp && !st.moov) { return true; }
                        }
                        prevMoof = (t === 'moof');
                        i = e;
                    }
                    return false;
                };
                // Blink 必拒的载荷（实测 /tmp/f383 B 组：级联与真机逐项一致）。
                var __wkBadSeg = (function () {
                    var ftyp = [0, 0, 0, 32, 0x66, 0x74, 0x79, 0x70,
                                0x69, 0x73, 0x6f, 0x6d, 0, 0, 2, 0,
                                0x69, 0x73, 0x6f, 0x6d, 0x69, 0x73, 0x6f, 0x32,
                                0x61, 0x76, 0x63, 0x31, 0x6d, 0x70, 0x34, 0x31];
                    var moov = [0, 0, 0, 8, 0x6d, 0x6f, 0x6f, 0x76];
                    var out = new Uint8Array(40);
                    out.set(ftyp, 0);
                    out.set(moov, 32);
                    return out.buffer;
                })();
                try {
                    if (typeof SourceBuffer !== 'undefined' && SourceBuffer.prototype) {
                        var __sbApD = Object.getOwnPropertyDescriptor(
                            SourceBuffer.prototype, 'appendBuffer');
                        var __sbApOrig = __sbApD && __sbApD.value;
                        if (typeof __sbApOrig === 'function') {
                            var __sbApImpl = function (buf) {
                                if (!arguments.length) {
                                    return __sbApOrig.call(this);
                                }
                                var bad = false;
                                try {
                                    bad = __wkMseBad(buf, __wkSt(this));
                                } catch (e) { bad = false; }
                                if (bad) {
                                    return __sbApOrig.call(this, __wkBadSeg);
                                }
                                return __sbApOrig.call(this, buf);
                            };
                            try {
                                Object.defineProperty(__sbApImpl, 'length', {
                                    value: 1, configurable: true,
                                });
                            } catch (e) {}
                            Object.defineProperty(SourceBuffer.prototype,
                                                  'appendBuffer', {
                                value: __maskNative(__sbApImpl, 'appendBuffer'),
                                writable: true, enumerable: true,
                                configurable: true,
                            });
                        }
                    }
                } catch (e) {}
                // ---- F-156：endOfStream 的同步可读窗口 ----
                // 真机口径（/tmp/f393，WebKit 27.2，一个实例只做一个动作）：
                //   · endOfStream('decode'|'network')：**同步**置 closed、
                //     两个列表归零、duration 变 NaN；之后所有 SB 成员抛
                //     InvalidStateError（removeSourceBuffer 抛
                //     NotFoundError）；事件异步 ended → verr4 → close。
                //   · endOfStream()：**同步**置 ended、列表保留 1 个 SB、
                //     duration 归 0；add / eosAgain / abort / durSet /
                //     setLive / setLiveClear 抛 InvalidStateError，而
                //     appendBuffer / remove / changeType 正常并把 readyState
                //     打回 open（规范行为），buffered / mode / item / rmSB
                //     照常；事件只有一次 ended，没有 verr、没有 close。
                // Blink 的差异只在 closed 是否同步落地：原生事件级联（无数据
                // 与有数据都一样）与真机逐字一致，所以事件一律不合成
                // （/tmp/f393 仿真腿实测 ended → verr4 → close），
                // 这里只虚拟同步读数。
                var __wk156 = new WeakMap();
                var __wk156Reg = function (ms, sb) {
                    var s = __wk156.get(ms);
                    if (!s) {
                        s = { phase: 'open', sbs: [], eosDur: undefined,
                              lists: Object.create(null) };
                        __wk156.set(ms, s);
                    }
                    if (sb && s.sbs.indexOf(sb) < 0) {
                        s.sbs.push(sb);
                        try { __wkSt(sb).ms = ms; } catch (e) {}
                    }
                    return s;
                };
                var __wk156Fail = function (name) {
                    return new DOMException(__WK_DOM_MSG[name] || '', name);
                };
                // 逐出：SB 与宿主解绑后只剩 mode / updating /
                // timestampOffset / appendWindow* 可读。
                var __wk156Evict = function (sb) {
                    try { __wkSt(sb).gone = true; } catch (e) {}
                };
                // 合成的空 SourceBufferList：借用 F-154 的牌子与接收者校验，
                // 实例零自有属性、每宿主每槽位恒等。
                var __wk156List = function (s, slot) {
                    var L = s.lists[slot];
                    if (L) { return L; }
                    var C = null;
                    try { C = window.SourceBufferList; } catch (e) {}
                    if (!C || !C.prototype) { return null; }
                    L = Object.create(C.prototype);
                    __wkListBrand.add(L);
                    s.lists[slot] = L;
                    return L;
                };
                // ---- F-156 残项：MediaSource 与 HTMLMediaElement 的联动 ----
                // 真机（/tmp/f405、/tmp/f408）：
                //   无参 endOfStream()        → 元素 networkState 同步 1、error 仍 null
                //   显式 endOfStream('decode'|'network')
                //                             → 同步 3，且 video.error 同步可读为
                //                               code 4 + 空 message
                // Blink 两种情形都停在 2（LOADING），错误对象要等原生级联落地
                // 才出现，且 message 是引擎内部串。故这里虚拟这两个读数。
                // 元素与 MediaSource 的关联宿主不暴露，靠 createObjectURL /
                // src / srcObject 三处挂钩建立——探针与实际页面都走这条路。
                var __wk156Urls = null, __wk156Els = new WeakMap();
                try { __wk156Urls = new Map(); } catch (e) {}
                var __wk156Link = function (el, ms) {
                    if (!el || !ms) { return; }
                    try { __wk156Els.set(ms, el); } catch (e) {}
                };
                var __wk156El = function (ms) {
                    try { return __wk156Els.get(ms) || null; } catch (e) { return null; }
                };
                var __wk156ErrCode = new WeakMap();
                // err：undefined = 沿用原生；null = 强制 null；数字 = 合成
                // MediaError（同一元素同一 code 恒等，/tmp/f404 identity：
                // 真机实例零自有属性，故 code 记在旁路表里）。
                var __wk156Mark = function (el, ns, err) {
                    var st = __wkSt(el);
                    st.ns156 = ns;
                    if (err === undefined) { return; }
                    st.err156 = err;
                    if (err === null) { st.err156Obj = null; return; }
                    if (typeof __wkMediaError !== 'function') {
                        st.err156 = undefined; st.err156Obj = null; return;
                    }
                    if (!st.err156Obj || __wk156ErrCode.get(st.err156Obj) !== err) {
                        var o = __wkMediaError(err);
                        __wk156ErrCode.set(o, err);
                        st.err156Obj = o;
                    }
                };
                var __wk156Unmark = function (el) {
                    var st = __wkSt(el);
                    st.ns156 = undefined;
                    st.err156 = undefined;
                    st.err156Obj = null;
                };
                try {
                    if (typeof URL !== 'undefined'
                            && typeof URL.createObjectURL === 'function') {
                        var __wk156Cou = URL.createObjectURL;
                        var __wk156CouImpl = function (obj) {
                            var u = __wk156Cou.apply(this, arguments);
                            try {
                                if (__wk156Urls && obj && typeof MediaSource !== 'undefined'
                                        && (obj instanceof MediaSource)) {
                                    __wk156Urls.set(u, obj);
                                }
                            } catch (e) {}
                            return u;
                        };
                        try {
                            Object.defineProperty(__wk156CouImpl, 'length', {
                                value: __wk156Cou.length, configurable: true,
                            });
                        } catch (e) {}
                        Object.defineProperty(URL, 'createObjectURL', {
                            value: __maskNative(__wk156CouImpl,
                                                'createObjectURL'),
                            writable: true, enumerable: false, configurable: true,
                        });
                    }
                } catch (e) {}
                if (typeof HTMLMediaElement !== 'undefined'
                        && HTMLMediaElement.prototype) {
                    var __hm156 = HTMLMediaElement.prototype;
                    var __hm156Orig = {};
                    ['networkState', 'error', 'src', 'srcObject', 'load']
                        .forEach(function (n) {
                            try {
                                __hm156Orig[n] = Object.getOwnPropertyDescriptor(
                                    __hm156, n) || null;
                            } catch (e) { __hm156Orig[n] = null; }
                        });
                    ['src', 'srcObject'].forEach(function (n) {
                        var d = __hm156Orig[n];
                        if (!d || !d.set) { return; }
                        var set0 = d.set;
                        try {
                            Object.defineProperty(__hm156, n, {
                                get: d.get ? __maskNative(function () {
                                    return d.get.call(this);
                                }, 'get ' + n) : undefined,
                                set: __maskNative(function (v) {
                                    var ms = null;
                                    if (n === 'srcObject') {
                                        ms = v;
                                    } else if (__wk156Urls) {
                                        try { ms = __wk156Urls.get(String(v)) || null; }
                                        catch (e) { ms = null; }
                                    }
                                    __wk156Unmark(this);
                                    var r = set0.call(this, v);
                                    __wk156Link(this, ms);
                                    return r;
                                }, 'set ' + n),
                                enumerable: d.enumerable, configurable: true,
                            });
                        } catch (e) {}
                    });
                    if (__hm156Orig.load && typeof __hm156Orig.load.value === 'function') {
                        var load0 = __hm156Orig.load.value;
                        try {
                            Object.defineProperty(__hm156, 'load', {
                                value: __maskNative(function () {
                                    __wk156Unmark(this);
                                    return load0.apply(this, arguments);
                                }, 'load'),
                                writable: true,
                                enumerable: __hm156Orig.load.enumerable,
                                configurable: true,
                            });
                        } catch (e) {}
                    }
                    if (__hm156Orig.networkState && __hm156Orig.networkState.get) {
                        var ns0 = __hm156Orig.networkState.get;
                        try {
                            Object.defineProperty(__hm156, 'networkState', {
                                get: __maskNative(function () {
                                    var st = __wkSt(this);
                                    if (st.ns156 !== undefined) { return st.ns156; }
                                    return ns0.call(this);
                                }, 'get networkState'),
                                enumerable: true, configurable: true,
                            });
                        } catch (e) {}
                    }
                    if (__hm156Orig.error && __hm156Orig.error.get) {
                        var err0 = __hm156Orig.error.get;
                        try {
                            Object.defineProperty(__hm156, 'error', {
                                get: __maskNative(function () {
                                    var st = __wkSt(this);
                                    if (st.err156 === null) { return null; }
                                    if (st.err156 !== undefined) {
                                        return st.err156Obj || null;
                                    }
                                    return err0.call(this);
                                }, 'get error'),
                                enumerable: true, configurable: true,
                            });
                        } catch (e) {}
                    }
                }
                if (typeof MediaSource !== 'undefined' && MediaSource.prototype) {
                    var __wk156Orig = {};
                    ['readyState', 'duration', 'sourceBuffers',
                     'activeSourceBuffers', 'addSourceBuffer',
                     'removeSourceBuffer', 'endOfStream',
                     'setLiveSeekableRange', 'clearLiveSeekableRange']
                        .forEach(function (n) {
                            try {
                                __wk156Orig[n] = Object.getOwnPropertyDescriptor(
                                    MediaSource.prototype, n) || null;
                            } catch (e) { __wk156Orig[n] = null; }
                        });
                    var __wk156Call = function (n, self, args) {
                        var d = __wk156Orig[n];
                        if (!d) { throw new TypeError('Illegal invocation'); }
                        try {
                            if (typeof d.value === 'function') {
                                return d.value.apply(self, args || []);
                            }
                            return d.get.call(self);
                        } catch (e) { throw __wkNormErr(e); }
                    };
                    var __wk156Def = function (n, fn, len) {
                        try {
                            Object.defineProperty(fn, 'length', {
                                value: len, configurable: true,
                            });
                        } catch (e) {}
                        Object.defineProperty(MediaSource.prototype, n, {
                            value: __maskNative(fn, n), writable: true,
                            enumerable: true, configurable: true,
                        });
                    };
                    var __wk156DefGet = function (n, fn) {
                        var d0 = __wk156Orig[n];
                        Object.defineProperty(MediaSource.prototype, n, {
                            get: __maskNative(fn, 'get ' + n),
                            enumerable: (d0 && d0.enumerable !== undefined)
                                ? d0.enumerable : true,
                            configurable: true,
                        });
                    };
                    var __wk156EosImpl = function (arg) {
                        if (!__msOk || !__msOk(this)) {
                            throw __wkBrandErr('MediaSource', 'endOfStream',
                                               'method');
                        }
                        var explicit = false, mode = 'end';
                        if (arguments.length && arg !== undefined) {
                            if (typeof arg === 'symbol') {
                                throw new TypeError(
                                    'Cannot convert a symbol to a string');
                            }
                            var a = String(arg);
                            if (a !== 'network' && a !== 'decode') {
                                throw new TypeError("Argument 1 ('error') to "
                                    + 'MediaSource.endOfStream must be one of: '
                                    + '"network", "decode"');
                            }
                            explicit = true; mode = a;
                        }
                        var ms = this;
                        var s = __wk156Reg(ms);
                        // 真机在非 open 态一律 InvalidStateError（含二次
                        // endOfStream 与 closed 之后的调用）。
                        var rsNow = null;
                        try { rsNow = __wk156Call('readyState', ms); }
                        catch (e) { rsNow = null; }
                        if (rsNow !== 'open' || s.phase !== 'open') {
                            throw __wk156Fail('InvalidStateError');
                        }
                        if (explicit) {
                            // Blink 的原生级联（有无数据都一样）就是真机那一串
                            // ended → verr4 → close，直接放它跑，只补同步读数。
                            __wk156Call('endOfStream', ms, [mode]);
                            s.phase = 'closed';
                            s.eosDur = NaN;
                            s.sbs.forEach(__wk156Evict);
                            var elX = __wk156El(ms);
                            if (elX) { __wk156Mark(elX, 3, 4); }
                        } else {
                            // 无参**不调原生**：Blink 无数据会直接 close 并把
                            // video.error 置 4，真机只停在 ended、SB 仍活着。
                            // 但 Blink 的 remove() 拿**原生** duration 校验起点，
                            // 真机此刻虚拟 duration 是 0（ended 态的 remove 能过，
                            // /tmp/f396）。故同步把原生 duration 也置 0。
                            s.phase = 'ended';
                            s.eosDur = 0;
                            var elY = __wk156El(ms);
                            if (elY) { __wk156Mark(elY, 1, null); }
                            try {
                                var dd = __wk156Orig.duration;
                                if (dd && dd.set) { dd.set.call(ms, 0); }
                            } catch (e) {}
                            setTimeout(function () {
                                try {
                                    ms.dispatchEvent(
                                        new Event('sourceended'));
                                } catch (e) {}
                            }, 0);
                        }
                        return undefined;
                    };
                    __wk156Def('endOfStream', __wk156EosImpl, 0);
                    __wk156DefGet('readyState', function () {
                        if (!__msOk || !__msOk(this)) {
                            throw __wkBrandErr('MediaSource', 'readyState',
                                               'getter');
                        }
                        var s = __wk156.get(this);
                        if (s && s.phase !== 'open') { return s.phase; }
                        return __wk156Call('readyState', this);
                    });
                    // duration：closed 变 NaN、ended 归 0，之后即使被 append
                    // 打回 open 也保持 0（真机不回填）。
                    var __wk156DurGet = function () {
                        if (!__msOk || !__msOk(this)) {
                            throw __wkBrandErr('MediaSource', 'duration',
                                               'getter');
                        }
                        var s = __wk156.get(this);
                        if (!s) { return __wk156Call('duration', this); }
                        if (s.phase === 'closed') { return NaN; }
                        if (s.eosDur !== undefined) { return s.eosDur; }
                        return __wk156Call('duration', this);
                    };
                    var __wk156DurSet = function (v) {
                        if (!__msOk || !__msOk(this)) {
                            throw __wkBrandErr('MediaSource', 'duration',
                                               'setter');
                        }
                        var s = __wk156.get(this);
                        if (s && s.phase !== 'open') {
                            var n = NaN;
                            try { n = Number(v); }
                            catch (e) { throw new TypeError('Type error'); }
                            // 真机先做范围判定（负数 / NaN 一律 Type error），
                            // 再看状态。
                            if (!isFinite(n) || n < 0) {
                                throw new TypeError('Type error');
                            }
                            throw __wk156Fail('InvalidStateError');
                        }
                        var d0 = __wk156Orig.duration;
                        if (!d0 || !d0.set) {
                            throw new TypeError('Illegal invocation');
                        }
                        try { d0.set.call(this, v); }
                        catch (e) { throw __wkNormErr(e); }
                        if (s) { s.eosDur = Number(v); }
                    };
                    (function () {
                        var d0 = __wk156Orig.duration;
                        Object.defineProperty(MediaSource.prototype, 'duration', {
                            get: __maskNative(__wk156DurGet, 'get duration'),
                            set: __maskNative(__wk156DurSet, 'set duration'),
                            enumerable: (d0 && d0.enumerable !== undefined)
                                ? d0.enumerable : true,
                            configurable: true,
                        });
                    })();
                    // ended 态 Blink 的列表天然正确（SB 还活着、n=1、条目
                    // 恒等），只有 closed 要换成空列表。
                    __wk156DefGet('sourceBuffers', function () {
                        if (!__msOk || !__msOk(this)) {
                            throw __wkBrandErr('MediaSource', 'sourceBuffers',
                                               'getter');
                        }
                        var s = __wk156.get(this);
                        if (s && s.phase === 'closed') {
                            var L = __wk156List(s, 'sb');
                            if (L) { return L; }
                        }
                        return __wk156Call('sourceBuffers', this);
                    });
                    // activeSourceBuffers 在 ended 态真机为 0（Blink 有数据
                    // 时可能是 1），两个非 open 态都返回空列表。
                    __wk156DefGet('activeSourceBuffers', function () {
                        if (!__msOk || !__msOk(this)) {
                            throw __wkBrandErr('MediaSource',
                                               'activeSourceBuffers', 'getter');
                        }
                        var s = __wk156.get(this);
                        if (s && s.phase !== 'open') {
                            var L = __wk156List(s, 'asb');
                            if (L) { return L; }
                        }
                        return __wk156Call('activeSourceBuffers', this);
                    });
                    var __wk156RmImpl = function (sb) {
                        if (!__msOk || !__msOk(this)) {
                            throw __wkBrandErr('MediaSource',
                                               'removeSourceBuffer', 'method');
                        }
                        if (!arguments.length) {
                            throw new TypeError('Not enough arguments');
                        }
                        var s = __wk156.get(this);
                        if (s && s.phase === 'closed') {
                            throw __wk156Fail('NotFoundError');
                        }
                        var r = __wk156Call('removeSourceBuffer', this, [sb]);
                        if (s) {
                            var k = s.sbs.indexOf(sb);
                            if (k >= 0) { s.sbs.splice(k, 1); }
                        }
                        return r;
                    };
                    __wk156Def('removeSourceBuffer', __wk156RmImpl, 1);
                    var __wk156SlImpl = function (a, b) {
                        if (!__msOk || !__msOk(this)) {
                            throw __wkBrandErr('MediaSource',
                                               'setLiveSeekableRange', 'method');
                        }
                        if (arguments.length < 2) {
                            throw new TypeError('Not enough arguments');
                        }
                        if (!isFinite(Number(a)) || !isFinite(Number(b))) {
                            throw new TypeError(
                                'The provided value is non-finite');
                        }
                        var s = __wk156.get(this);
                        if (s && s.phase !== 'open') {
                            throw __wk156Fail('InvalidStateError');
                        }
                        return __wk156Call('setLiveSeekableRange', this,
                                           [a, b]);
                    };
                    __wk156Def('setLiveSeekableRange', __wk156SlImpl, 2);
                    var __wk156ClImpl = function () {
                        if (!__msOk || !__msOk(this)) {
                            throw __wkBrandErr('MediaSource',
                                               'clearLiveSeekableRange',
                                               'method');
                        }
                        var s = __wk156.get(this);
                        if (s && s.phase !== 'open') {
                            throw __wk156Fail('InvalidStateError');
                        }
                        return __wk156Call('clearLiveSeekableRange', this, []);
                    };
                    __wk156Def('clearLiveSeekableRange', __wk156ClImpl, 0);
                    // addSourceBuffer：空串 → 类型表 → 状态（真机就是这个
                    // 顺序，closed 下不支持的类型仍抛 NotSupportedError）。
                    // Blink 在非 open 态会先撞配额抛 QuotaExceededError
                    // （/tmp/f390 复现），所以状态判定必须自己来。
                    var __wk156AddD = Object.getOwnPropertyDescriptor(
                        MediaSource.prototype, 'addSourceBuffer');
                    var __wk156AddPrev = __wk156AddD && __wk156AddD.value;
                    if (typeof __wk156AddPrev === 'function') {
                        var __wk156AddImpl = function (type) {
                            if (__msOk && !__msOk(this)) {
                                throw __wkBrandErr('MediaSource',
                                                   'addSourceBuffer', 'method');
                            }
                            if (!arguments.length) {
                                throw new TypeError('Not enough arguments');
                            }
                            if (typeof type === 'symbol') {
                                throw new TypeError(
                                    'Cannot convert a symbol to a string');
                            }
                            var t = String(type);
                            if (t === '') { throw new TypeError('Type error'); }
                            if (!__mseSupports(t, D.mseTypes)) {
                                throw new DOMException(
                                    __WK_DOM_MSG.NotSupportedError,
                                    'NotSupportedError');
                            }
                            var s = __wk156Reg(this);
                            if (s.phase !== 'open') {
                                throw __wk156Fail('InvalidStateError');
                            }
                            var sb = __wk156AddPrev.apply(this, [t]);
                            __wk156Reg(this, sb);
                            return sb;
                        };
                        __wk156Def('addSourceBuffer', __wk156AddImpl, 1);
                    }
                }
                // SourceBuffer 侧：closed 后五个成员全抛 InvalidStateError；
                // ended 后只有 abort 抛（append / remove / changeType 会把
                // readyState 打回 open），buffered 照常可读。
                if (typeof SourceBuffer !== 'undefined' && SourceBuffer.prototype) {
                    var __sb156Orig = {};
                    ['buffered', 'appendBuffer', 'abort', 'remove', 'changeType']
                        .forEach(function (n) {
                            try {
                                __sb156Orig[n] = Object.getOwnPropertyDescriptor(
                                    SourceBuffer.prototype, n) || null;
                            } catch (e) { __sb156Orig[n] = null; }
                        });
                    var __sb156Ms = function (sb) {
                        var ms = null;
                        try { ms = __wkSt(sb).ms || null; } catch (e) { ms = null; }
                        if (!ms) { return null; }
                        return __wk156.get(ms) || null;
                    };
                    var __sb156Block = function (sb, member) {
                        var s = __sb156Ms(sb);
                        if (!s) { return false; }
                        if (s.phase === 'closed') { return true; }
                        return (s.phase === 'ended' && member === 'abort');
                    };
                    if (__sb156Orig.buffered && __sb156Orig.buffered.get) {
                        var __sb156BufGet = __sb156Orig.buffered.get;
                        Object.defineProperty(SourceBuffer.prototype, 'buffered', {
                            get: __maskNative(function () {
                                if (__sb156Block(this, 'buffered')) {
                                    throw __wk156Fail('InvalidStateError');
                                }
                                return __sb156BufGet.call(this);
                            }, 'get buffered'),
                            enumerable: true, configurable: true,
                        });
                    }
                    ['appendBuffer', 'abort', 'remove', 'changeType']
                        .forEach(function (n) {
                            var d = __sb156Orig[n];
                            var prev = d && d.value;
                            if (typeof prev !== 'function') { return; }
                            var impl = function () {
                                if (__sb156Block(this, n)) {
                                    throw __wk156Fail('InvalidStateError');
                                }
                                var r = prev.apply(this, arguments);
                                var s = __sb156Ms(this);
                                // 规范：ended 态下成功的 append / remove /
                                // changeType 把 readyState 同步打回 open，并
                                // 异步补发 sourceopen——它排在该操作自己的
                                // updatestart 之前（/tmp/f397）。
                                if (s && s.phase === 'ended') {
                                    s.phase = 'open';
                                    var ms = null;
                                    try { ms = __wkSt(this).ms || null; }
                                    catch (e) { ms = null; }
                                    if (ms) {
                                        Promise.resolve().then(function () {
                                            try {
                                                ms.dispatchEvent(
                                                    new Event('sourceopen'));
                                            } catch (e) {}
                                        });
                                    }
                                }
                                return r;
                            };
                            try {
                                Object.defineProperty(impl, 'length', {
                                    value: prev.length, configurable: true,
                                });
                            } catch (e) {}
                            Object.defineProperty(SourceBuffer.prototype, n, {
                                value: __maskNative(impl, n), writable: true,
                                enumerable: true, configurable: true,
                            });
                        });
                }

                // 整段 CSS 在 @layer 里，作者样式表照旧优先；插入时机要延后：
                // 本脚本在 document-start 执行，此时 documentElement 还是 null。
                var __uaForm = __add.uaFormCss;
                if (__uaForm && !document.getElementById('__wk_uaform')) {
                    var __putUaForm = function () {
                        if (document.getElementById('__wk_uaform')) { return; }
                        var root = document.head || document.documentElement;
                        if (!root) {
                            document.addEventListener('readystatechange',
                                                      __putUaForm);
                            return;
                        }
                        var st = document.createElement('style');
                        st.id = '__wk_uaform';
                        st.textContent = __uaForm;
                        root.insertBefore(st, root.firstChild);
                    };
                    __putUaForm();
                }
            } catch (e) {}
        }

        // ---- 接口原型成员差集（F-26 / F-27）----
        // 放在所有补丁之后：先让其他补丁建立自己的成员，再统一执行删除，
        // 避免「先删后补」的顺序依赖。configurable:false 的成员删不掉时，
        // 以 undefined getter 兜底（与 removeProp 同语义）。
        var __dropMembers = D.dropMembers || {};
        for (var __ctorName in __dropMembers) {
            if (!Object.prototype.hasOwnProperty.call(__dropMembers, __ctorName)) { continue; }
            var __ctor = null;
            try { __ctor = window[__ctorName]; } catch (e) {}
            if (!__ctor) { continue; }
            // 接口构造器删原型成员；命名空间对象（CSS 等无 prototype 的宿主
            // 对象）删自身成员——F-40/F-49/F-51 的对象子属性缺口走这条路。
            var __owner = __ctor.prototype || __ctor;
            var __mems = __dropMembers[__ctorName] || [];
            for (var __mi = 0; __mi < __mems.length; __mi++) {
                var __mn = __mems[__mi];
                try { delete __owner[__mn]; } catch (e) {}
                if (__mn in __owner) {
                    defineGet(__owner, __mn, undefined);
                }
            }
        }

        // ---- FontFace：裁掉宿主本地字体（F-163）----
        // 放在最后：包装器复用原生原型对象，前面的成员差集/顺序重排都作用在
        // 同一个原型上，先装后删/后装都不影响结果。
        if (__ffWrap) {
            try {
                __maskNative(__ffWrap, 'FontFace', null, true);
                Object.defineProperty(window, 'FontFace', {
                    value: __ffWrap, writable: true,
                    enumerable: false, configurable: true,
                });
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
        "baseGlobals": list(spec.get("baseGlobals") or []),
        "removeWorkerGlobals": list(spec.get("removeWorkerGlobals") or []),
        "emptyPlugins": bool(spec.get("emptyPlugins")),
        "connectionType": str(spec.get("connectionType") or ""),
        "downlinkMax": spec.get("downlinkMax"),
        "codecCapability": dict(spec.get("codecCapability") or {}),
        "webglProfile": spec.get("webglProfile"),
        "ios": bool(spec.get("ios")),
        "android": bool(spec.get("android")),
        "androidVoices": list(spec.get("androidVoices") or []),
        "dynamicRangeHigh": bool(spec.get("dynamicRangeHigh")),
        "naturalLandscape": bool(spec.get("naturalLandscape")),
        "iosChrome": spec.get("iosChrome"),
        # B1-7：POST 重放面函数的原文（真机 toString 口径）
        "crPostSrc": _CRIOS_POST_REQUEST_FN_SRC,
        "webgl": spec.get("webgl"),
        "webgpu": spec.get("webgpu"),
        "mediaVideoInputs": int(spec.get("mediaVideoInputs") or 0),
        # 跨域 iframe（OOPIF）无页面级 CDP 度量仿真：screen/DPR/outer 镜像主文档
        "cssWidth": int(spec.get("cssWidth") or 0),
        "cssHeight": int(spec.get("cssHeight") or 0),
        "viewportHeight": int(spec.get("viewportHeight") or 0),
        "dpr": float(spec.get("dpr") or 0),
        "dropMembers": dict(spec.get("dropMembers") or {}),
        # F-89：CSS.supports 白名单的版本门控（低于 profile 版本的属性 false）
        "cssGate": list(spec.get("cssGate") or []),
        "permissions": dict(spec.get("permissions") or {}),
        "addMembers": spec.get("addMembers") or {},
        "av1": bool(spec.get("av1")),
        # F-142：MSE 真值表的运行期开关（见 _MSE_SUPPORT_SRC）
        "mseTypes": spec.get("mseTypes") or {},
        # F-163：设备声明的字体表（FontFace 的 local() 白名单）
        "fonts": list(spec.get("fonts") or []),
    }
    return (_PATCH_TEMPLATE
            .replace("__MSE_SRC__", _MSE_SUPPORT_SRC)
            .replace("__CPT_SRC__", _CPT_IOS_SRC)
            .replace("__WC_SRC__", _WEBCODECS_ICS_SRC)
            .replace("__MC_SRC__", _MC_SRC)
            .replace("__FF_SRC__", _FONT_FACE_SRC)
            .replace("__SPEC__", json.dumps(payload, ensure_ascii=False)))


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

    // F-142：MSE 独立真值表（源码见模块级 _MSE_SUPPORT_SRC）
    var __mseSupports = (__MSE_SRC__);

    // F-163：FontFace 的 local() 源裁剪（见模块级 _FONT_FACE_SRC）
    // 必须在顶层：末尾的安装点在顶层 try 块里，声明进嵌套作用域会读不到。
__FF_SRC__

    // ---- 原生函数伪装（与页面补丁同一实现）----
    var __maskNative = (function () {
        try {
            var origToString = Function.prototype.toString;
            if (typeof WeakMap === 'undefined') { return function (fn) { return fn; }; }
            var registry = new WeakMap();
            // 顶层 realm 的 toString，用于跨 realm 查询（见下方 apply）。
            var __topFts = null;
            var __topFtsTried = false;
            var faked = new Proxy(origToString, {
                apply: function (target, self, args) {
                    var v = registry.get(self);
                    if (v !== undefined) { return v; }
                    // creepjs 的 `queryLies` 用 phantom iframe（二级）的
                    // Function#toString 去读主 realm 的函数。两个 realm 各有
                    // 一份 toString 和一份 WeakMap，本 realm 查不到就吐载体
                    // 自身的真源码（方法简写文本），记 lie。故这里回退到顶层
                    // realm 的注册表；顶层对陌生对象也只会落到原生实现。
                    if (!__topFtsTried) {
                        __topFtsTried = true;
                        try {
                            var tw = window.top;
                            if (tw && tw !== window && tw.Function
                                    && tw.Function.prototype) {
                                __topFts = tw.Function.prototype.toString;
                            }
                        } catch (e5) {}
                    }
                    if (__topFts && __topFts !== faked) {
                        try { return __topFts.call(self); } catch (e6) {}
                    }
                    return __nativeToString(target, self);
                },
            });
            var __nativeToStringTail = function () {
                var s = origToString.call(this);
                // JSC 的原生函数是 `function f() {\n    [native code]\n}`（多行缩进），
                // V8 是单行 `function f() { [native code] }`。未登记（未被本补丁
                // 包装）的原生函数直接透传会暴露 V8 格式——检测站对
                // `Function.prototype.toString.call(eval)` 一行判定引擎，故 iOS
                // 目标上统一改写为 JSC 形态。
                if (D && D.ios && typeof s === 'string'
                        && s.indexOf('[native code]') !== -1
                        && s.indexOf('\n') === -1) {
                    var m = /^function\s*([^(]*)\(/.exec(s);
                    return 'function ' + __jscName(m ? m[1].trim() : '')
                        + '() {\n    [native code]\n}';
                }
                return s;
            };
            // JSC 的访问器源码不带 get/set 前缀（`function volume()`），
            // 与 V8 的 `function get volume()` 不同；Function#name 两侧都是
            // 'get volume'，只有 toString 分叉（/tmp/f401）。
            var __jscName = function (n) {
                return String(n).replace(/^(?:get|set)\s+/, '');
            };
            // 返回的载体必须**自身**在任何 realm 下都读作原生函数。仅靠上面的
            // 注册表做不到：creepjs 的 `queryLies` 用 phantom iframe（二级）
            // 的 `Function.prototype.toString` 去读主 realm 的函数，两个
            // realm 各有一份 toString 与各一份 WeakMap，查不到就吐真源码，
            // 连带暴露 JS 函数自带的 prototype/arguments/caller——实测
            // Navigator 10 个成员、Screen、WebGL 全部因此记 lie。
            // 故这里不再返回原函数，而是返回一个**方法简写**包装：简写方法
            // 天然无 prototype、不可构造，own 属性恰是 length,name，任何
            // realm 读源码都命中 [native code]（实测 lied:0）。构造器类
            // 属性走 asCtor 分支保持原函数，因为 `new`/`prototype` 必须能用
            // 而 creepjs 并不搜索构造器对象本身。
            var mask = function (fn, name, src, asCtor) {
                try {
                    if (typeof fn === 'function') {
                        var s = src || (D && D.ios
                            ? 'function ' + __jscName(name) + '() {\n    [native code]\n}'
                            : 'function ' + name + '() { [native code] }');
                        registry.set(fn, s);
                        try { Object.defineProperty(fn, 'name', { value: name, configurable: true }); } catch (e2) {}
                        if (asCtor) { return fn; }
                        var w = ({ m() { return fn.apply(this, arguments); } }).m;
                        registry.set(w, s);
                        try { Object.defineProperty(w, 'name', { value: name, configurable: true }); } catch (e3) {}
                        try {
                            Object.defineProperty(w, 'length', {
                                value: fn.length, configurable: true,
                            });
                        } catch (e4) {}
                        return w;
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
    // 原生方法同样校验接收者（Navigator.prototype.getBattery.call({}) 亦抛
    // Illegal invocation），故对「接口原型」上的方法做同样的检查；proto 本身
    // 不是原型对象时（挂在实例上的方法，如空 plugins 的 item）不做检查，
    // 避免误伤。
    var __isIfaceProto = function (proto) {
        try {
            var c = proto.constructor;
            return typeof c === 'function' && c.prototype === proto;
        } catch (e) { return false; }
    };
    var defineFn = function (proto, name, fn, noBrand) {
        try {
            var impl = fn;
            if (!noBrand && __isIfaceProto(proto)) {
                impl = function () {
                    if (!__slotHas(proto, this)) {
                        throw new TypeError('Illegal invocation');
                    }
                    return fn.apply(this, arguments);
                };
                // 包装后 length 会变成 0，原生方法的 length 是方法签名的一部分
                // （检测站点会读 postMessage.length 等），原样保留
                try {
                    Object.defineProperty(impl, 'length', {
                        value: fn.length, configurable: true,
                    });
                } catch (e) {}
            }
            Object.defineProperty(proto, name, {
                value: __maskNative(impl, name),
                writable: true, enumerable: true, configurable: true,
            });
            return true;
        } catch (e) { return false; }
    };

    try {
        var __wkWA = D.workerAdd || {};
        // ---- Worker 作用域补成员（F-37 / F-38）----
        // 与页面 addMembers 同一套口径，但宿主是 Worker 全局（self）。真机 WebKit
        // 的 Worker 里有 40 个 Chromium Worker 没有的接口（WebCodecs 编解码器、
        // CSS Typed OM 整族、FontFaceSet、ManagedMediaSource…）；检测方在 Worker
        // 里一行 `'FontFaceSet' in self` 即可判假，故必须与页面同源补齐。
        //
        // 注入时机：Worker 被 setAutoAttach 门控暂停时 Runtime.evaluate。实测
        // （/tmp/wkworker4/probe_persist.py）此刻的全局面与运行态完全一致
        // （242 项逐名相同），且暂停态做的 delete 在放行后依然生效——所以补建
        // 与删除都可以在这里一次做完，不需要 beforeScriptExecution 插桩。
        var __wkWIface = __wkWA.interfaces || null;
        // 反方向：WorkerNavigator 只留真机那 16 个成员（实测顺序即 navProto），
        // connection / deviceMemory / userAgentData 等一律删。globalDrops 是
        // Worker 全局面的删除清单。globalPrivacyControl 不在 navProto 里：
        // WorkerNavigator 不 include NavigatorGlobalPrivacyControl（WebKit
        // Navigator.idl 才 include），真机 Worker 里该成员缺席，这里也不合成。
        // 删除后按 navProto 的顺序重排原型成员（真机的成员顺序就是这张表）。
        var __wkPruneNav = function () {
            if (!__wkWA.navProto || !__wkWA.navProto.length) { return; }
            var N = null;
            try { N = self.WorkerNavigator; } catch (e) {}
            if (!N || !N.prototype) { return; }
            var P = N.prototype;
            var keep = Object.create(null);
            __wkWA.navProto.forEach(function (k) { keep[k] = 1; });
            var drop = [];
            Object.getOwnPropertyNames(P).forEach(function (k) {
                if (k === 'constructor' || keep[k]) { return; }
                drop.push(k);
            });
            Object.getOwnPropertySymbols(P).forEach(function (k) { drop.push(k); });
            drop.forEach(function (k) {
                try { delete P[k]; } catch (e) {}
                try { if (k in P) { Object.defineProperty(P, k, { value: undefined }); } } catch (e) {}
            });
            // 属性顺序：先把留下的成员摘下，再按表重建。
            var saved = [];
            __wkWA.navProto.forEach(function (k) {
                var d = null;
                try { d = Object.getOwnPropertyDescriptor(P, k); } catch (e) {}
                if (!d) { return; }
                try { delete P[k]; } catch (e) {}
                saved.push([k, d]);
            });
            saved.forEach(function (kv) {
                try { Object.defineProperty(P, kv[0], kv[1]); } catch (e) {}
            });
        };
        var __wkDropGlobals = function () {
            if (!__wkWA.globalDrops || !__wkWA.globalDrops.length) { return; }
            __wkWA.globalDrops.forEach(function (k) {
                try { delete self[k]; } catch (e) {}
                try {
                    if (k in self) {
                        Object.defineProperty(self, k, {
                            value: undefined, writable: true, enumerable: false, configurable: true,
                        });
                    }
                } catch (e) {}
            });
        };
        __wkDropGlobals();
        if (__wkWIface) {
            // 实例内部状态放 WeakMap：真机实例没有任何自有属性（实测
            // Object.getOwnPropertyNames(inst) 除 Event 子类与索引集合外为 []）。
            var __wkStateW = new WeakMap();
            var __wkHasW = function (o) {
                if (o === null || o === undefined) { return false; }
                var t = typeof o;
                if (t !== 'object' && t !== 'function') { return false; }
                try { return __wkStateW.get(o) !== undefined; } catch (e) { return false; }
            };
            var __wkStW = function (o) {
                var s = __wkStateW.get(o);
                if (!s) { s = Object.create(null); __wkStateW.set(o, s); }
                return s;
            };
            // 真机接收者校验文案（实测）：getter / setter / 方法三种，与页面同。
            var __wkBrandErrW = function (iface, member, kind) {
                if (kind === 'method') {
                    return new TypeError('Can only call ' + iface + '.' + member
                                         + ' on instances of ' + iface);
                }
                return new TypeError('The ' + iface + '.' + member + ' ' + kind
                                     + ' can only be used on instances of ' + iface);
            };
            // 带 Symbol.iterator 的原型（三个索引集合 + FontFaceSet /
            // StylePropertyMapReadOnly）；真机上 iterator === values。
            var __wkIterIface = function (name) {
                return name === 'CSSNumericArray' || name === 'CSSTransformValue'
                    || name === 'CSSUnparsedValue' || name === 'FontFaceSet'
                    || name === 'StylePropertyMapReadOnly';
            };
            // 真机原型上 constructor 永远是第一个自有属性（Chromium 自带的
            // CSSSkewX / CSSTransformComponent 原型上它排在最后），重排一次。
            var __wkCtorFirst = function (proto) {
                var ctor = null;
                try { ctor = Object.getOwnPropertyDescriptor(proto, 'constructor'); } catch (e) {}
                if (!ctor) { return; }
                var rest = [];
                Object.getOwnPropertyNames(proto).forEach(function (k) {
                    if (k === 'constructor') { return; }
                    var d = Object.getOwnPropertyDescriptor(proto, k);
                    if (!d || !d.configurable) { return; }
                    rest.push([k, d]);
                });
                try {
                    rest.forEach(function (kv) { delete proto[kv[0]]; });
                    delete proto.constructor;
                    Object.defineProperty(proto, 'constructor', ctor);
                    rest.forEach(function (kv) { Object.defineProperty(proto, kv[0], kv[1]); });
                } catch (e) {}
            };
            var __wkPropW = function (proto, iface, member, rw, init, noBrand) {
                var d = {
                    get: __maskNative(function () {
                        if (!noBrand && !__wkHasW(this)) {
                            throw __wkBrandErrW(iface, member, 'getter');
                        }
                        var s = __wkStW(this);
                        if (!(member in s) && init && (member in init)) { s[member] = init[member]; }
                        var v = s[member];
                        // DOM 事件处理器属性未赋值时读作 null（不是 undefined）。
                        if (v === undefined && member.slice(0, 2) === 'on') { return null; }
                        return v;
                    }, 'get ' + member),
                    enumerable: true, configurable: true,
                };
                if (rw) {
                    d.set = __maskNative(function (v) {
                        if (!noBrand && !__wkHasW(this)) {
                            throw __wkBrandErrW(iface, member, 'setter');
                        }
                        __wkStW(this)[member] = v;
                    }, 'set ' + member);
                }
                Object.defineProperty(proto, member, d);
            };
            var __wkMethodW = function (proto, iface, member, len, ret, noBrand) {
                var impl = function () {
                    if (!noBrand && !__wkHasW(this)) {
                        throw __wkBrandErrW(iface, member, 'method');
                    }
                    return (typeof ret === 'function') ? ret(this, arguments) : ret;
                };
                try {
                    Object.defineProperty(impl, 'length', { value: len || 0, configurable: true });
                } catch (e) {}
                Object.defineProperty(proto, member, {
                    value: __maskNative(impl, member),
                    writable: true, enumerable: true, configurable: true,
                });
            };
            // 索引集合（CSSNumericArray / CSSTransformValue / CSSUnparsedValue）：
            // 真机的迭代方法与 length 都挂在原型上，索引是自有属性。
            var __wkIdxKeys = function (o) {
                var out = [];
                if (o === null || o === undefined) { return out; }
                var n = 0;
                while (Object.prototype.hasOwnProperty.call(o, String(n))) {
                    out.push(String(n)); n++;
                }
                return out;
            };
            // 索引集合的 length：真机是原型上的访问器（{e:true,c:true}，无 setter），
            // 值等于自有索引个数——不是成员表里那个读初值的通用 getter。
            var __wkFixCollections = function (name) {
                var C = null;
                try { C = self[name]; } catch (e) {}
                if (!C || !C.prototype) { return; }
                try {
                    Object.defineProperty(C.prototype, 'length', {
                        get: __maskNative(function () {
                            if (!__wkHasW(this)) {
                                throw __wkBrandErrW(name, 'length', 'getter');
                            }
                            return __wkIdxKeys(this).length;
                        }, 'get length'),
                        enumerable: true, configurable: true,
                    });
                } catch (e) {}
            };
            var __wkIdxMethods = function (proto, iface, noBrand) {
                ['entries', 'keys', 'values'].forEach(function (m) {
                    __wkMethodW(proto, iface, m, 0, function (recv) {
                        if (!__wkHasW(recv)) { return [][Symbol.iterator](); }
                        var ks = __wkIdxKeys(recv);
                        if (m === 'keys') { return ks.map(Number)[Symbol.iterator](); }
                        var vs = ks.map(function (k) { return recv[k]; });
                        return (m === 'values') ? vs[Symbol.iterator]() : vs.entries();
                    }, noBrand);
                });
                __wkMethodW(proto, iface, 'forEach', 1, function (recv, args) {
                    if (!__wkHasW(recv)) { return undefined; }
                    var cb = args[0];
                    var thisArg = args[1];
                    __wkIdxKeys(recv).forEach(function (k) {
                        cb.call(thisArg, recv[k], Number(k), recv);
                    });
                    return undefined;
                }, noBrand);
                __wkFixCollections(iface);
            };
            // ---- CSS Typed OM 数值语义（真机 WebKit 27.2 Worker 实测）----
            var __wkUnitDim = {
                px: 'length', cm: 'length', mm: 'length', q: 'length', 'in': 'length',
                pt: 'length', pc: 'length', em: 'length', rem: 'length', ex: 'length',
                ch: 'length', vw: 'length', vh: 'length', vmin: 'length',
                vmax: 'length', cap: 'length', ic: 'length', lh: 'length',
                rlh: 'length', vi: 'length', vb: 'length', svw: 'length',
                svh: 'length', lvw: 'length', lvh: 'length', dvw: 'length',
                dvh: 'length', cqw: 'length', cqh: 'length', cqi: 'length',
                cqb: 'length', cqmin: 'length', cqmax: 'length',
                deg: 'angle', grad: 'angle', rad: 'angle', turn: 'angle',
                s: 'time', ms: 'time',
                hz: 'frequency', khz: 'frequency',
                dpi: 'resolution', dpcm: 'resolution', dppx: 'resolution',
                fr: 'flex', percent: 'percent',
            };
            var __wkTypeOfUnit = function (unit) {
                var d = __wkUnitDim[unit];
                var t = {};
                if (d) { t[d] = 1; }
                return t;
            };
            var __wkTypeAdd = function (a, b, sign) {
                var out = {};
                var k;
                for (k in a) { if (Object.prototype.hasOwnProperty.call(a, k)) { out[k] = a[k]; } }
                for (k in b) {
                    if (Object.prototype.hasOwnProperty.call(b, k)) {
                        out[k] = (out[k] || 0) + (sign || 1) * b[k];
                        if (!out[k]) { delete out[k]; }
                    }
                }
                return out;
            };
            var __wkTypeScaled = function (a, n) {
                var out = {};
                for (var k in a) {
                    if (Object.prototype.hasOwnProperty.call(a, k)) { out[k] = a[k] * n; }
                }
                return out;
            };
            var __wkNumStr = function (v) { return String(v); };
            var __wkUnitStr = function (v, unit) {
                if (unit === 'number') { return __wkNumStr(v); }
                if (unit === 'percent') { return __wkNumStr(v) + '%'; }
                return __wkNumStr(v) + unit;
            };
            // 构造器入参 → CSSNumericValue：真机只对 CSSScale / CSSRotate 的
            // x/y/z 接受裸数字（实测 new CSSScale(2,3) → x "2"），其余位置
            // （CSSTranslate / CSSSkew / CSSPerspective 的数值参数）传数字抛
            // `Argument 1 ('x') ... must be an instance of CSSNumericValue`。
            var __wkToNum = function (v) {
                if (__wkHasW(v)) { return v; }
                if (typeof v === 'number') {
                    try { return new self.CSSUnitValue(v, 'number'); } catch (e) {}
                }
                return null;
            };
            // 序列里的片段：真机把字符串/数字/布尔一律折成字符串，CSSVariableReferenceValue
            // 保留对象，但拼进 CSS 文本时写作 var(--x[, fallback])。
            var __wkFragStr = function (f) {
                if (__wkHasW(f) && __wkStW(f).__kind === 'vr') {
                    var s = __wkStW(f);
                    return 'var(' + s.variable + (s.fallback ? ', ' + String(s.fallback) : '') + ')';
                }
                if (f && typeof f === 'object' && !__wkHasW(f)
                        && Object.prototype.toString.call(f) === '[object CSSVariableReferenceValue]') {
                    return 'var(' + f.variable + (f.fallback ? ', ' + String(f.fallback) : '') + ')';
                }
                return String(f);
            };
            var __wkCssString = function (o) {
                if (!__wkHasW(o)) { return Object.prototype.toString.call(o); }
                var s = __wkStW(o);
                var k = s.__kind;
                if (k === 'unit') { return __wkUnitStr(s.value, s.unit); }
                if (k === 'kw') { return String(s.value); }
                if (k === 'unparsed') {
                    return (s.__frags || []).map(__wkFragStr).join('');
                }
                if (k === 'sum') {
                    return 'calc(' + s.__vals.map(__wkCssString).join(' + ') + ')';
                }
                if (k === 'product') {
                    return 'calc(' + s.__vals.map(__wkCssString).join(' * ') + ')';
                }
                if (k === 'min' || k === 'max') {
                    return k + '(' + s.__vals.map(__wkCssString).join(', ') + ')';
                }
                if (k === 'clamp') {
                    return 'clamp(' + __wkCssString(s.lower) + ', ' + __wkCssString(s.value)
                           + ', ' + __wkCssString(s.upper) + ')';
                }
                if (k === 'negate') { return 'calc(-' + __wkCssString(s.value) + ')'; }
                if (k === 'invert') { return 'calc(1 / ' + __wkCssString(s.value) + ')'; }
                // CSSStyleValue.prototype.toString 在真机上是全族共享的那一个实现：
                // 变换值（CSSTransformValue / CSSTranslate / …）也走它。
                if (s.__tk) { return __wkTransformStr(o); }
                return Object.prototype.toString.call(o);
            };
            var __wkCssType = function (o) {
                if (!__wkHasW(o)) { return {}; }
                var s = __wkStW(o);
                var k = s.__kind;
                if (k === 'unit') { return __wkTypeOfUnit(s.unit); }
                if (k === 'sum' || k === 'min' || k === 'max' || k === 'clamp') {
                    var first = (k === 'clamp') ? s.lower : (s.__vals && s.__vals[0]);
                    return first ? __wkCssType(first) : {};
                }
                if (k === 'product') {
                    var t = {};
                    (s.__vals || []).forEach(function (v) { t = __wkTypeAdd(t, __wkCssType(v)); });
                    return t;
                }
                if (k === 'negate') { return s.value ? __wkCssType(s.value) : {}; }
                if (k === 'invert') {
                    return s.value ? __wkTypeScaled(__wkCssType(s.value), -1) : {};
                }
                return {};
            };
            // 单位换算：真机只做绝对长度/角度/时间/频率/分辨率之间的换算。
            var __wkAbs = {
                px: 1, cm: 96 / 2.54, mm: 96 / 25.4, q: 96 / 101.6, 'in': 96,
                pt: 4 / 3, pc: 16,
                deg: 1, grad: 0.9, rad: 180 / Math.PI, turn: 360,
                s: 1, ms: 0.001,
                hz: 1, khz: 1000,
                dpi: 1, dpcm: 2.54, dppx: 96,
            };
            var __wkConvert = function (v, from, to) {
                if (from === to) { return v; }
                var f = __wkAbs[from], t = __wkAbs[to];
                if (!f || !t) { return null; }
                return v * f / t;
            };
            var __wkNumPair = function (a, b) {
                // 同单位 → 直接算；可换算 → 换算到 a 的单位；否则按 calc 组合。
                var sa = __wkStW(a), sb = __wkStW(b);
                if (sa.__kind === 'unit' && sb.__kind === 'unit') {
                    var c = __wkConvert(sb.value, sb.unit, sa.unit);
                    if (c !== null) { return { unit: sa.unit, a: sa.value, b: c, exact: true }; }
                }
                return null;
            };
            var __wkCssMath = function (kind, vals, extra) {
                var o = Object.create(self[kind].prototype);
                __wkStateW.set(o, Object.create(null));
                var s = __wkStW(o);
                s.__kind = (kind === 'CSSMathSum') ? 'sum'
                         : (kind === 'CSSMathProduct') ? 'product'
                         : (kind === 'CSSMathMin') ? 'min'
                         : (kind === 'CSSMathMax') ? 'max'
                         : (kind === 'CSSMathNegate') ? 'negate'
                         : (kind === 'CSSMathInvert') ? 'invert'
                         : (kind === 'CSSMathClamp') ? 'clamp' : kind;
                if (kind === 'CSSMathClamp') {
                    s.lower = vals[0]; s.value = vals[1]; s.upper = vals[2];
                } else if (kind === 'CSSMathNegate' || kind === 'CSSMathInvert') {
                    s.value = vals[0];
                } else {
                    s.__vals = vals;
                    s.__arr = __wkNumArray(vals);
                }
                if (extra) {
                    for (var k in extra) {
                        if (Object.prototype.hasOwnProperty.call(extra, k)) { s[k] = extra[k]; }
                    }
                }
                return o;
            };
            var __wkNumArray = function (vals) {
                var a = Object.create(self.CSSNumericArray.prototype);
                __wkStateW.set(a, Object.create(null));
                __wkStW(a).__kind = 'numericarray';
                vals.forEach(function (v, i) {
                    Object.defineProperty(a, String(i), {
                        value: v, writable: false, enumerable: true, configurable: true,
                    });
                });
                return a;
            };
            var __wkTransformStr = function (o) {
                if (!__wkHasW(o)) { return Object.prototype.toString.call(o); }
                var s = __wkStW(o);
                var k = s.__tk;
                if (k === 'tv') {
                    return (s.__tv || []).map(__wkCssString).join(' ');
                }
                if (k === 'translate') {
                    return s.is2D ? 'translate(' + __wkCssString(s.x) + ', ' + __wkCssString(s.y) + ')'
                                  : 'translate3d(' + __wkCssString(s.x) + ', ' + __wkCssString(s.y)
                                    + ', ' + __wkCssString(s.z) + ')';
                }
                if (k === 'rotate') {
                    return s.is2D ? 'rotate(' + __wkCssString(s.angle) + ')'
                                  : 'rotate3d(' + __wkCssString(s.x) + ', ' + __wkCssString(s.y)
                                    + ', ' + __wkCssString(s.z) + ', ' + __wkCssString(s.angle) + ')';
                }
                if (k === 'scale') {
                    return s.is2D ? 'scale(' + __wkCssString(s.x) + ', ' + __wkCssString(s.y) + ')'
                                  : 'scale3d(' + __wkCssString(s.x) + ', ' + __wkCssString(s.y)
                                    + ', ' + __wkCssString(s.z) + ')';
                }
                if (k === 'skew') {
                    return 'skew(' + __wkCssString(s.ax) + ', ' + __wkCssString(s.ay) + ')';
                }
                if (k === 'skewx') { return 'skewX(' + __wkCssString(s.ax) + ')'; }
                if (k === 'skewy') { return 'skewY(' + __wkCssString(s.ay) + ')'; }
                if (k === 'perspective') { return 'perspective(' + __wkCssString(s.length) + ')'; }
                if (k === 'matrix') {
                    var m = s.matrix;
                    var f = [];
                    if (m && m.is2D) {
                        return 'matrix(' + m.a + ', ' + m.b + ', ' + m.c + ', ' + m.d + ', '
                               + m.e + ', ' + m.f + ')';
                    }
                    for (var i = 0; i < 16; i++) { f.push(m ? m['m' + String(11 + i)] : 0); }
                    return 'matrix3d(' + f.join(', ') + ')';
                }
                return Object.prototype.toString.call(o);
            };
            var __wkTransformMatrix = function (o) {
                if (!__wkHasW(o)) { return new self.DOMMatrix(); }
                var s = __wkStW(o);
                var k = s.__tk;
                var n = function (v) { return __wkHasW(v) ? (__wkStW(v).value || 0) : 0; };
                var M = self.DOMMatrix;
                if (k === 'translate') {
                    return new M([1, 0, 0, s.is2D ? 1 : 0, n(s.x), n(s.y),
                                  s.is2D ? 0 : n(s.z), s.is2D ? 0 : 1,
                                  s.is2D ? 0 : 0, s.is2D ? 0 : 0, s.is2D ? 0 : 1, 0,
                                  0, 0, 0, 1]);
                }
                if (k === 'scale') {
                    return new M([n(s.x), 0, 0, 0, 0, n(s.y), 0, 0, 0, 0, s.is2D ? 1 : n(s.z), 0,
                                  0, 0, 0, 1]);
                }
                if (k === 'rotate') {
                    var ang = (s.angle ? n(s.angle) : 0) * Math.PI / 180;
                    var x = n(s.x), y = n(s.y), z = n(s.z);
                    var len = Math.sqrt(x * x + y * y + z * z) || 1;
                    x /= len; y /= len; z /= len;
                    var c = Math.cos(ang), sn = Math.sin(ang), t = 1 - c;
                    return new M([
                        t * x * x + c, t * x * y + sn * z, t * x * z - sn * y, 0,
                        t * x * y - sn * z, t * y * y + c, t * y * z + sn * x, 0,
                        t * x * z + sn * y, t * y * z - sn * x, t * z * z + c, 0,
                        0, 0, 0, 1,
                    ]);
                }
                if (k === 'skew') {
                    return new M([1, Math.tan((n(s.ay)) * Math.PI / 180), 0, 0,
                                  Math.tan((n(s.ax)) * Math.PI / 180), 1, 0, 0,
                                  0, 0, 1, 0, 0, 0, 0, 1]);
                }
                if (k === 'skewx') {
                    return new M([1, 0, 0, 0, Math.tan(n(s.ax) * Math.PI / 180), 1, 0, 0,
                                  0, 0, 1, 0, 0, 0, 0, 1]);
                }
                if (k === 'skewy') {
                    return new M([1, Math.tan(n(s.ay) * Math.PI / 180), 0, 0, 0, 1, 0, 0,
                                  0, 0, 1, 0, 0, 0, 0, 1]);
                }
                if (k === 'perspective') {
                    return new M([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, -1 / (n(s.length) || 1), 0, 0, 0, 1]);
                }
                if (k === 'matrix') { return new M(s.matrix ? s.matrix.toFloat64Array() : []); }
                return new M();
            };
            // 编解码器能力判定（WebKit main 源码口径，F-133）：与主世界同一份
            // 实现，见模块级 _WEBCODECS_ICS_SRC（`__wc*` 前缀）。
__WC_SRC__
            // mediaCapabilities 口径（F-135），见模块级 _MC_SRC（`__mc*` 前缀）
__MC_SRC__
            var __wkStaticW = function (Ctor, name, impl, len) {
                try {
                    var fn = function () { return impl(this, arguments); };
                    Object.defineProperty(fn, 'length', { value: len || 0, configurable: true });
                    Object.defineProperty(Ctor, name, {
                        value: __maskNative(fn, name),
                        writable: true, enumerable: true, configurable: true,
                    });
                } catch (e) {}
            };
            // ManagedMediaSource.isTypeSupported（F-130/F-134/F-142）：真机
            // 走 MediaSource 同一张 MSE 真值表（ManagedMediaSource 在 WebKit
            // 里继承 MediaSource，静态方法是同一实现）。返回布尔而非 Promise。
            // 表见模块级 _MSE_SUPPORT_SRC；页面侧 `__addIfaceObj` 用同一份。
            var __wkStatics = function (out, name, statics) {
                (statics || []).forEach(function (s) {
                    var sname = (s.charAt(0) === '!') ? s.slice(1) : s;
                    if (sname === 'isConfigSupported') {
                        __wkStaticW(out, sname, __wcIcsImpl(name, __wcIcsKind(name)), 1);
                    } else if (sname === 'isTypeSupported') {
                        __wkStaticW(out, sname, function (t, args) {
                            if (!args.length) {
                                throw new TypeError('Not enough arguments');
                            }
                            return __mseSupports(args[0], D.mseTypes);
                        }, 1);
                    }
                });
            };
            // ---- 逐个接口建面 ----
            var __wkSeed = function (obj, spec, args) {
                var s = __wkStW(obj);
                var init = spec.init || {};
                for (var k in init) {
                    if (Object.prototype.hasOwnProperty.call(init, k)) { s[k] = init[k]; }
                }
                return s;
            };
            var __wkBase = function (spec, args) {
                // 父接口在 Chromium Worker 里是原生实现时，用真实例做基底，这样
                // 继承来的原生成员（EventTarget.addEventListener 等）仍能通过
                // 内部槽校验；再换原型接到补出来的接口上。
                var pname = spec.extends;
                if (!pname) { return {}; }
                var P = null;
                try { P = self[pname]; } catch (e) {}
                if (!P || typeof P !== 'function') { return {}; }
                var b = null;
                try {
                    if (pname === 'EventTarget') { b = new P(); }
                    else if (pname === 'Event') { b = new P(args[0], args[1]); }
                    else if (pname === 'MediaSource') { b = new P(); }
                } catch (e) { b = null; }
                if (b) { return b; }
                try { return Object.create(P.prototype); } catch (e) {}
                return {};
            };
            var __wkIsObj = function (v) {
                return v !== null && typeof v === 'object' && !Array.isArray(v);
            };
            // 字典参数：WebKit 接受任何对象（含数组）与可调用对象，只有原始值
            // 抛 `Type error`（实测 new ManagedMediaSource([]) / (fn) 均成功）。
            var __wkIsDictArg = function (v) {
                if (v === null) { return false; }
                var t = typeof v;
                return t === 'object' || t === 'function';
            };
            var __wkCtorArgs = function (name, args) {
                // 构造参数 → 成员（真机口径）。返回 [成员表, 额外状态]
                var out = {};
                var need = function (i, what) {
                    if (i < args.length && args[i] !== undefined && args[i] !== null) { return args[i]; }
                    throw new TypeError('Not enough arguments');
                };
                var numeric = function (v, label, idx) {
                    if (__wkHasW(v)) { return v; }
                    throw new TypeError("Argument " + idx + " ('" + label
                        + "') to the " + name + ' constructor must be an instance of CSSNumericValue');
                };
                // 只有 CSSMath* 的成员值、CSSScale 的 x/y/z、CSSRotate 的 x/y/z
                // 接受裸数字（实测）；其余位置（CSSTranslate / CSSSkew 的数值
                // 参数、CSSRotate 的 angle）传数字抛 CSSNumericValue 文案。
                var numericN = function (v, label, idx) {
                    var n = __wkToNum(v);
                    if (n) { return n; }
                    if (typeof v === 'number' && isFinite(v)) {
                        return new self.CSSUnitValue(v, 'number');
                    }
                    return numeric(v, label, idx);
                };
                var fnReq = function (dict, iface, member, type) {
                    if (typeof dict[member] === 'function') { return; }
                    throw new TypeError('Member ' + iface + '.' + member
                                        + ' is required and must be an instance of ' + type);
                };
                if (name === 'CSSUnitValue') {
                    var val = Number(need(0));
                    var unit = String(need(1)).toLowerCase();
                    if (!isFinite(val)) { throw new TypeError('The provided value is non-finite'); }
                    if (!(unit in __wkUnitDim) && unit !== 'number') { throw new TypeError('Type error'); }
                    out.__kind = 'unit'; out.value = val; out.unit = unit;
                } else if (name === 'CSSKeywordValue') {
                    var kw = String(need(0));
                    if (!kw) { throw new TypeError('Type error'); }
                    out.__kind = 'kw'; out.value = kw;
                } else if (name === 'CSSUnparsedValue') {
                    var frags = need(0);
                    if (!frags || typeof frags !== 'object' || typeof frags === 'string'
                            || typeof frags.length !== 'number') {
                        throw new TypeError('Value is not a sequence');
                    }
                    out.__kind = 'unparsed';
                    out.__frags = Array.prototype.slice.call(frags).map(function (f) {
                        if (f && typeof f === 'object') { return f; }
                        return String(f);
                    });
                } else if (name === 'CSSVariableReferenceValue') {
                    var vn = String(need(0));
                    // 实测：fallback 类型先校验，"--" 前缀后校验。
                    if (args.length > 1 && args[1] !== undefined && args[1] !== null) {
                        var fb = args[1];
                        if (!(fb && typeof fb === 'object' && __wkHasW(fb))) {
                            throw new TypeError("Argument 2 ('fallback') to the CSSVariableReferenceValue constructor must be an instance of CSSUnparsedValue");
                        }
                        out.fallback = fb;
                    } else { out.fallback = null; }
                    if (vn.slice(0, 2) !== '--') {
                        throw new TypeError('Custom Variable Reference needs to have "--" prefix.');
                    }
                    out.variable = vn;
                    out.__kind = 'vr';
                } else if (name === 'CSSMathSum' || name === 'CSSMathProduct'
                           || name === 'CSSMathMin' || name === 'CSSMathMax') {
                    var list = Array.prototype.slice.call(args).map(function (v, i) {
                        return numericN(v, 'values', i + 1);
                    });
                    if (!list.length) {
                        throw new SyntaxError('The string did not match the expected pattern.');
                    }
                    out.__vals = list;
                    out.__kind = (name === 'CSSMathSum') ? 'sum'
                               : (name === 'CSSMathProduct') ? 'product'
                               : (name === 'CSSMathMin') ? 'min' : 'max';
                } else if (name === 'CSSMathNegate' || name === 'CSSMathInvert') {
                    out.value = numericN(need(0), 'value', 1);
                    out.__kind = (name === 'CSSMathNegate') ? 'negate' : 'invert';
                } else if (name === 'CSSMathClamp') {
                    out.lower = numericN(need(0), 'lower', 1);
                    out.value = numericN(need(1), 'value', 2);
                    out.upper = numericN(need(2), 'upper', 3);
                    out.__kind = 'clamp';
                } else if (name === 'CSSTranslate') {
                    out.x = numeric(need(0), 'x', 1);
                    out.y = numeric(need(1), 'y', 2);
                    if (args.length > 2 && args[2] !== undefined && args[2] !== null) {
                        out.z = numeric(args[2], 'z', 3);
                        out.is2D = false;
                    } else {
                        out.z = new self.CSSUnitValue(0, 'px');
                        out.is2D = true;
                    }
                    out.__tk = 'translate';
                } else if (name === 'CSSRotate') {
                    if (args.length === 1) {
                        out.angle = numeric(need(0), 'angle', 1);
                        out.x = new self.CSSUnitValue(0, 'number');
                        out.y = new self.CSSUnitValue(0, 'number');
                        out.z = new self.CSSUnitValue(1, 'number');
                        out.is2D = true;
                    } else {
                        out.x = numericN(need(0), 'x', 1);
                        out.y = numericN(need(1), 'y', 2);
                        out.z = numericN(need(2), 'z', 3);
                        out.angle = numeric(need(3), 'angle', 4);
                        out.is2D = false;
                    }
                    out.__tk = 'rotate';
                } else if (name === 'CSSScale') {
                    var sx = need(0), sy = need(1);
                    out.x = numericN(sx, 'x', 1);
                    out.y = numericN(sy, 'y', 2);
                    if (args.length > 2 && args[2] !== undefined && args[2] !== null) {
                        out.z = numericN(args[2], 'z', 3);
                        out.is2D = false;
                    } else {
                        out.z = new self.CSSUnitValue(1, 'number');
                        out.is2D = true;
                    }
                    out.__tk = 'scale';
                } else if (name === 'CSSSkew') {
                    out.ax = numeric(need(0), 'ax', 1);
                    out.ay = numeric(need(1), 'ay', 2);
                    out.is2D = true;
                    out.__tk = 'skew';
                } else if (name === 'CSSSkewX') {
                    out.ax = numeric(need(0), 'ax', 1);
                    out.is2D = true;
                    out.__tk = 'skewx';
                } else if (name === 'CSSSkewY') {
                    out.ay = numeric(need(0), 'ay', 1);
                    out.is2D = true;
                    out.__tk = 'skewy';
                } else if (name === 'CSSPerspective') {
                    // 实测：裸数字在这里给的是通用 'Type error'，不是 CSSNumericValue 文案。
                    var pv = need(0);
                    if (!__wkHasW(pv)) { throw new TypeError('Type error'); }
                    out.length = pv;
                    out.is2D = false;
                    out.__tk = 'perspective';
                } else if (name === 'CSSMatrixComponent') {
                    var mx = need(0);
                    if (!mx || typeof mx !== 'object'
                            || Object.prototype.toString.call(mx) !== '[object DOMMatrix]'
                               && Object.prototype.toString.call(mx) !== '[object DOMMatrixReadOnly]') {
                        throw new TypeError("Argument 1 ('matrix') to the CSSMatrixComponent constructor must be an instance of DOMMatrixReadOnly");
                    }
                    out.matrix = mx;
                    out.is2D = (args[1] && args[1].is2D !== undefined) ? !!args[1].is2D : !!mx.is2D;
                    out.__tk = 'matrix';
                } else if (name === 'CSSTransformValue') {
                    var tv = need(0);
                    if (!tv || typeof tv !== 'object' || typeof tv.length !== 'number') {
                        throw new TypeError('Value is not a sequence');
                    }
                    if (!tv.length) { throw new TypeError('Type error'); }
                    out.__tv = Array.prototype.slice.call(tv);
                    out.__tk = 'tv';
                } else if (name === 'BufferedChangeEvent') {
                    out.type = String(need(0));
                    if (!__wkIsObj(args[1])) { throw new TypeError('Type error'); }
                    var bi = args[1];
                    out.addedRanges = (bi.addedRanges !== undefined) ? bi.addedRanges : null;
                    out.removedRanges = (bi.removedRanges !== undefined) ? bi.removedRanges : null;
                } else if (name === 'ManagedMediaSource') {
                    // 实测 WebKit 完全忽略 MediaSourceInit：new MMS({quality:'low'})
                    // 仍读 'high'，{streaming:true} 仍读 false。这里只做参数类型
                    // 校验，成员值一律走 spec.init 的默认值。
                    if (args.length && args[0] !== undefined && args[0] !== null && !__wkIsDictArg(args[0])) {
                        throw new TypeError('Type error');
                    }
                } else if (name === 'MediaStreamTrackProcessor') {
                    var ti = need(0);
                    if (!__wkIsObj(ti)) { throw new TypeError('Type error'); }
                    if (ti.track === undefined || ti.track === null) {
                        throw new TypeError('Member MediaStreamTrackProcessorInit.track is required and must be an instance of (MediaStreamTrack or MediaStreamTrackHandle)');
                    }
                    out.readable = new ReadableStream();
                } else if (name === 'AudioDecoder' || name === 'AudioEncoder'
                           || name === 'VideoDecoder' || name === 'VideoEncoder') {
                    if (!__wkIsObj(need(0))) { throw new TypeError('Type error'); }
                    var di = args[0];
                    var initName = 'WebCodecs' + name + 'Init';
                    fnReq(di, initName, 'error', 'WebCodecsErrorCallback');
                    fnReq(di, initName, 'output', (name === 'AudioDecoder')
                            ? 'WebCodecsAudioDataOutputCallback'
                            : (name === 'VideoDecoder') ? 'WebCodecsVideoFrameOutputCallback'
                            : (name === 'AudioEncoder') ? 'WebCodecsEncodedAudioChunkOutputCallback'
                            : 'WebCodecsEncodedVideoChunkOutputCallback');
                } else if (name === 'VideoTrackGenerator') {
                    out.muted = false;
                }
                return out;
            };
            var __wkAddIfaceW = function (name, spec) {
                var existing = null;
                try { existing = (typeof self[name] !== 'undefined') ? self[name] : null; } catch (e) {}
                if (existing && !spec.repair) { return false; }
                var mode = spec.construct || 'illegal';
                var noNew = spec.noNew || mode;
                var len = spec.length || 0;
                var noBrand = Object.create(null);
                (spec.noBrand || []).forEach(function (m) { noBrand[m] = 1; });
                var applyMsg = (noNew === 'illegal') ? 'Illegal constructor'
                                                     : "Constructor requires 'new' operator";
                var seed = function (obj, args) {
                    var s = __wkSeed(obj, spec, args);
                    var extra = __wkCtorArgs(name, args);
                    for (var k in extra) {
                        if (Object.prototype.hasOwnProperty.call(extra, k)) { s[k] = extra[k]; }
                    }
                    if (name === 'CSSTransformValue') {
                        var tv = s.__tv || [];
                        tv.forEach(function (v, i) {
                            Object.defineProperty(obj, String(i), {
                                value: v, writable: true, enumerable: true, configurable: true,
                            });
                        });
                        s.is2D = tv.every(function (v) { return __wkHasW(v) && __wkStW(v).is2D !== false; });
                    }
                    if (name === 'CSSUnparsedValue') {
                        (s.__frags || []).forEach(function (f, i) {
                            Object.defineProperty(obj, String(i), {
                                value: f, writable: true, enumerable: true, configurable: true,
                            });
                        });
                    }
                    if (name === 'MediaStreamTrack') {
                        if (!s.id) { s.id = __wkUuid(); }
                        if (!('label' in s)) { s.label = ''; }
                        if (!('kind' in s)) { s.kind = 'video'; }
                    }
                    if (name === 'BufferedChangeEvent') {
                        try {
                            Object.defineProperty(obj, 'isTrusted', {
                                value: false, writable: false, enumerable: false, configurable: false,
                            });
                        } catch (e) {}
                    }
                    return obj;
                };
                var proto;
                var out;
                if (existing) {
                    // repair：Chromium 已有同名接口（Worker 里的 CSSSkewX /
                    // CSSSkewY）。保留原生原型（实例形态、原型链不动），只把构造
                    // 语义与成员换成我们的一套——原生构造器只认内核自己的
                    // CSSNumericValue，补出来的实例过不了它的品牌校验。
                    proto = existing.prototype;
                    out = new Proxy(existing, {
                        apply: function () { throw new TypeError(applyMsg); },
                        construct: function (t, args) {
                            if (mode === 'illegal') { throw new TypeError('Illegal constructor'); }
                            if (mode === 'args' && args.length < len) {
                                throw new TypeError('Not enough arguments');
                            }
                            var base = Object.create(t.prototype);
                            __wkStateW.set(base, Object.create(null));
                            return seed(base, args);
                        },
                    });
                    try {
                        Object.defineProperty(out, 'length', { value: len, configurable: true });
                    } catch (e) {}
                } else {
                    var target = function () {};
                    try {
                        Object.defineProperty(target, 'length', { value: len, configurable: true });
                    } catch (e) {}
                    try {
                        Object.defineProperty(target, 'prototype', { writable: false });
                    } catch (e) {}
                    proto = target.prototype;
                    var parent = null;
                    if (spec.extends) {
                        try { parent = self[spec.extends]; } catch (e) {}
                    }
                    if (parent && parent.prototype) {
                        try { Object.setPrototypeOf(proto, parent.prototype); } catch (e) {}
                        // 构造器本身也要接到父构造器上：真机 Worker 里
                        // `Object.getPrototypeOf(ManagedMediaSource) === MediaSource`
                        // （静态成员经原型链继承，F-130 的 isTypeSupported 就是
                        // 继承来的）。与页面 __addIfaceObj 同口径。
                        try { Object.setPrototypeOf(target, parent); } catch (e) {}
                    }
                    out = new Proxy(target, {
                        apply: function () { throw new TypeError(applyMsg); },
                        construct: function (t, args) {
                            if (mode === 'illegal') { throw new TypeError('Illegal constructor'); }
                            if (mode === 'args' && args.length < len) {
                                throw new TypeError('Not enough arguments');
                            }
                            var base = __wkBase(spec, args);
                            try { Object.setPrototypeOf(base, proto); } catch (e) {}
                            __wkStateW.set(base, Object.create(null));
                            return seed(base, args);
                        },
                    });
                }
                // Function.prototype.toString 的伪装要登记在对外可见的 Proxy 上，
                // 登记内层函数会让 String(CSSUnitValue) 退回 V8 形态。
                __maskNative(out, name, null, true);
                try {
                    Object.defineProperty(proto, 'constructor', {
                        value: out, writable: true, enumerable: false, configurable: true,
                    });
                } catch (e) {}
                __wkCtorFirst(proto);
                // Symbol.iterator 在真机上排在 Symbol.toStringTag 之前；先占位，
                // 成员装完再把 values 赋进去（赋值不改变属性顺序）。
                var iter = __wkIterIface(name);
                if (iter) {
                    try {
                        Object.defineProperty(proto, Symbol.iterator, {
                            value: undefined, writable: true, enumerable: false, configurable: true,
                        });
                    } catch (e) {}
                }
                try {
                    Object.defineProperty(proto, Symbol.toStringTag, {
                        value: name, writable: false, enumerable: false, configurable: true,
                    });
                } catch (e) {}
                var init = spec.init || {};
                var members = spec.members || [];
                members.forEach(function (m) {
                    var kind = 'ro';
                    var member = m;
                    var mlen = 0;
                    if (m.charAt(0) === '!') {
                        kind = 'method'; member = m.slice(1);
                    } else if (m.charAt(0) === '=') {
                        kind = 'rw'; member = m.slice(1);
                    }
                    var ci = member.indexOf(':');
                    if (ci !== -1) {
                        mlen = parseInt(member.slice(ci + 1), 10) || 0;
                        member = member.slice(0, ci);
                    }
                    try {
                        // 只有「本原型自己已经定义过」才跳过；用 `in` 会把
                        // Object.prototype.toString 也算进来，导致整个 CSS Typed OM
                        // 的 toString 永远装不上（退化成 "[object CSSUnitValue]"）。
                        var own = Object.prototype.hasOwnProperty.call(proto, member);
                        if (own && !spec.override) { return; }
                        if (kind === 'method') {
                            var ret = __wkMethodRet(name, member);
                            __wkMethodW(proto, name, member, mlen, ret, noBrand[member]);
                        } else {
                            __wkPropW(proto, name, member, kind === 'rw', init, noBrand[member]);
                        }
                    } catch (e) {}
                });
                if (name === 'CSSNumericArray' || name === 'CSSTransformValue'
                        || name === 'CSSUnparsedValue') {
                    try { __wkIdxMethods(proto, name, noBrand); } catch (e) {}
                }
                if (iter) {
                    try {
                        if (name === 'FontFaceSet' || name === 'StylePropertyMapReadOnly') {
                            Object.defineProperty(proto, Symbol.iterator, {
                                value: proto.values, writable: true, enumerable: false, configurable: true,
                            });
                        } else { proto[Symbol.iterator] = proto.values; }
                    } catch (e) {}
                }
                __wkStatics(out, name, spec.statics);
                try {
                    Object.defineProperty(self, name, {
                        value: out, writable: true, enumerable: false, configurable: true,
                    });
                } catch (e) {}
                return true;
            };
            var __wkUuid = function () {
                try {
                    if (typeof crypto !== 'undefined' && crypto.randomUUID) { return crypto.randomUUID(); }
                } catch (e) {}
                var h = '';
                for (var i = 0; i < 32; i++) { h += Math.floor(Math.random() * 16).toString(16); }
                return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16)
                       + '-' + h.slice(16, 20) + '-' + h.slice(20);
            };
            var __wkMethodRet = function (iface, member) {
                var isNum = (iface === 'CSSNumericValue');
                if (isNum) {
                    if (member === 'type') {
                        return function (recv) { return __wkCssType(recv); };
                    }
                    if (member === 'equals') {
                        return function (recv, args) {
                            var other = args[0];
                            if (!__wkHasW(other)) { return false; }
                            return __wkCssString(recv) === __wkCssString(other);
                        };
                    }
                    if (member === 'to') {
                        return function (recv, args) {
                            var s = __wkStW(recv);
                            var unit = String(args[0]).toLowerCase();
                            if (s.__kind !== 'unit') { throw new TypeError('Type error'); }
                            if (unit === s.unit) { return new self.CSSUnitValue(s.value, unit); }
                            var c = __wkConvert(s.value, s.unit, unit);
                            if (c === null) { throw new TypeError('Type error'); }
                            return new self.CSSUnitValue(c, unit);
                        };
                    }
                    if (member === 'toSum') {
                        return function (recv, args) {
                            var s = __wkStW(recv);
                            var units = Array.prototype.slice.call(args).map(function (u) {
                                return String(u).toLowerCase();
                            });
                            if (s.__kind !== 'unit') { throw new TypeError('Type error'); }
                            if (!units.length) { units = [s.unit]; }
                            var hit = false;
                            var vals = units.map(function (u) {
                                if (u === s.unit) { hit = true; return new self.CSSUnitValue(s.value, u); }
                                var c = __wkConvert(s.value, s.unit, u);
                                if (c !== null) { hit = true; return new self.CSSUnitValue(c, u); }
                                // 相对单位无法换算：真机填 0（实测 px → 0em / 0vw）。
                                return new self.CSSUnitValue(0, u);
                            });
                            if (!hit) { throw new TypeError('Failed to convert all values'); }
                            return __wkCssMath('CSSMathSum', vals);
                        };
                    }
                    if (member === 'add' || member === 'sub' || member === 'min' || member === 'max'
                            || member === 'mul' || member === 'div') {
                        return function (recv, args) {
                            var other = args[0];
                            var multiplicative = (member === 'mul' || member === 'div');
                            if (typeof other === 'number') {
                                if (member === 'mul' || member === 'div') {
                                    if (member === 'div' && !other) { throw new RangeError('Bad value'); }
                                    var su = __wkStW(recv);
                                    if (su.__kind === 'unit') {
                                        return new self.CSSUnitValue(
                                            member === 'mul' ? su.value * other : su.value / other, su.unit);
                                    }
                                    return __wkCssMath('CSSMathProduct', [
                                        recv, new self.CSSUnitValue(other, 'number')]);
                                }
                                throw new TypeError('Type error');
                            }
                            if (!__wkHasW(other)) {
                                if (other && typeof other === 'object') {
                                    throw new TypeError('The provided value is non-finite');
                                }
                                throw new TypeError('Type error');
                            }
                            var pair = __wkNumPair(recv, other);
                            if (pair && !multiplicative) {
                                var v = (member === 'add') ? pair.a + pair.b
                                      : (member === 'sub') ? pair.a - pair.b
                                      : (member === 'min') ? Math.min(pair.a, pair.b)
                                      : Math.max(pair.a, pair.b);
                                return new self.CSSUnitValue(v, pair.unit);
                            }
                            if (member === 'add' || member === 'sub') {
                                return __wkCssMath('CSSMathSum', [recv, other]);
                            }
                            if (member === 'min') { return __wkCssMath('CSSMathMin', [recv, other]); }
                            if (member === 'max') { return __wkCssMath('CSSMathMax', [recv, other]); }
                            return __wkCssMath('CSSMathProduct', [recv, other]);
                        };
                    }
                }
                if (iface === 'CSSStyleValue' && member === 'toString') {
                    return function (recv) { return __wkCssString(recv); };
                }
                if (iface === 'CSSTransformComponent') {
                    if (member === 'toString') {
                        return function (recv) { return __wkTransformStr(recv); };
                    }
                    if (member === 'toMatrix') {
                        return function (recv) { return __wkTransformMatrix(recv); };
                    }
                }
                if (iface === 'CSSTransformValue' && member === 'toMatrix') {
                    return function () { return new self.DOMMatrix(); };
                }
                if (iface === 'FontFaceSet' || iface === 'StylePropertyMapReadOnly') {
                    if (member === 'entries' || member === 'keys' || member === 'values') {
                        return function () { return [][Symbol.iterator](); };
                    }
                    if (member === 'forEach') { return function () { return undefined; }; }
                }
                if (iface === 'FontFaceSet') {
                    if (member === 'has') { return function () { return false; }; }
                    if (member === 'check') { return function () { return Promise.resolve([]); }; }
                    if (member === 'load') { return function () { return Promise.resolve([]); }; }
                    if (member === 'add' || member === 'delete') { return function () { return undefined; }; }
                    if (member === 'clear') { return function () { return undefined; }; }
                }
                if (iface === 'NavigationPreloadManager') {
                    if (member === 'getState') {
                        return function () { return Promise.resolve({ enabled: false, headerValue: '' }); };
                    }
                    return function () { return Promise.resolve(); };
                }
                if (iface === 'MediaStreamTrack') {
                    if (member === 'clone') { return function (recv) { return recv; }; }
                    if (member === 'getCapabilities' || member === 'getConstraints'
                            || member === 'getSettings') {
                        return function () { return {}; };
                    }
                    if (member === 'applyConstraints') { return function () { return Promise.resolve(); }; }
                    if (member === 'stop') { return function () { return undefined; }; }
                }
                if (iface === 'StylePropertyMapReadOnly') {
                    if (member === 'get' || member === 'getAll') { return function () { return undefined; }; }
                    if (member === 'has') { return function () { return false; }; }
                }
                if (iface === 'Report' && member === 'toJSON') {
                    return function (recv) {
                        var s = __wkStW(recv);
                        return { type: s.type || '', url: s.url || '', body: s.body || null };
                    };
                }
                // ---- ServiceWorker 家族（F-77 D3）----
                // 真机（/tmp/f77p/swmeth2.py）：getRegistrations 兑现 []、
                // getRegistration 兑现 null（无注册时）、register 兑现注册对象、
                // startMessages / postMessage 返回 undefined。这里都返回已兑现
                // 的 Promise，避免合成实例上产生 unhandled rejection。
                if (iface === 'ServiceWorkerContainer') {
                    if (member === 'getRegistrations') {
                        return function () { return Promise.resolve([]); };
                    }
                    if (member === 'getRegistration') {
                        return function () { return Promise.resolve(undefined); };
                    }
                    if (member === 'register') {
                        return function (recv, args) {
                            if (!args.length || args[0] === undefined) {
                                return Promise.reject(new TypeError('Not enough arguments'));
                            }
                            var R = null;
                            try { R = self.ServiceWorkerRegistration; } catch (e) {}
                            if (!R || !R.prototype) { return Promise.resolve(undefined); }
                            return Promise.resolve(Object.create(R.prototype));
                        };
                    }
                    if (member === 'startMessages') {
                        return function () { return undefined; };
                    }
                }
                if (iface === 'ServiceWorker' && member === 'postMessage') {
                    return function () { return undefined; };
                }
                if (iface === 'WebCodecsDecoder' && member === 'flush') { return undefined; }
                return undefined;
            };
            // ---- CSSTransformComponent：Chromium Worker 里只有 CSSSkewX / CSSSkewY
            // 的原型链父级，没有全局名。取原生构造器包一层，保留其原型对象，使
            // CSSSkewX 实例的 instanceof 关系不变；共享原型上的 is2D / toMatrix /
            // toString 换成「我们的实例走假实现、原生实例走原生实现」的分派。
            (function () {
                var spec = __wkWIface.CSSTransformComponent;
                if (!spec || (typeof self.CSSTransformComponent !== 'undefined')) { return; }
                var skew = null;
                try { skew = self.CSSSkewX; } catch (e) {}
                if (!skew || !skew.prototype) { return; }
                var hidden = null;
                try { hidden = Object.getPrototypeOf(skew.prototype).constructor; } catch (e) {}
                if (typeof hidden !== 'function') { return; }
                var proto = hidden.prototype;
                var dispatch = function (member, fake) {
                    var d = Object.getOwnPropertyDescriptor(proto, member);
                    var native = null;
                    if (d && d.get) { native = function (recv) { return d.get.call(recv); }; }
                    if (d && d.value) { native = function (recv, args) { return d.value.apply(recv, args || []); }; }
                    if (member === 'is2D') {
                        Object.defineProperty(proto, 'is2D', {
                            get: __maskNative(function () {
                                if (__wkHasW(this)) { return __wkStW(this).is2D; }
                                if (native) { return native(this); }
                                throw __wkBrandErrW('CSSTransformComponent', 'is2D', 'getter');
                            }, 'get is2D'),
                            set: __maskNative(function (v) {
                                if (__wkHasW(this)) { __wkStW(this).is2D = !!v; return; }
                                if (d && d.set) { return d.set.call(this, v); }
                                throw __wkBrandErrW('CSSTransformComponent', 'is2D', 'setter');
                            }, 'set is2D'),
                            enumerable: true, configurable: true,
                        });
                        return;
                    }
                    var impl = __maskNative(function () {
                        if (__wkHasW(this)) { return fake(this, arguments); }
                        if (native) { return native(this, arguments); }
                        throw __wkBrandErrW('CSSTransformComponent', member, 'method');
                    }, member);
                    Object.defineProperty(proto, member, {
                        value: impl, writable: true, enumerable: true, configurable: true,
                    });
                };
                dispatch('is2D');
                dispatch('toMatrix', function (recv) { return __wkTransformMatrix(recv); });
                dispatch('toString', function (recv) { return __wkTransformStr(recv); });
                var out = new Proxy(hidden, {
                    apply: function () { throw new TypeError('Illegal constructor'); },
                    construct: function () { throw new TypeError('Illegal constructor'); },
                });
                try { Object.defineProperty(out, 'length', { value: 0, configurable: true }); } catch (e) {}
                __maskNative(out, 'CSSTransformComponent', null, true);
                try {
                    Object.defineProperty(proto, 'constructor', {
                        value: out, writable: true, enumerable: false, configurable: true,
                    });
                } catch (e) {}
                __wkCtorFirst(proto);
                try {
                    Object.defineProperty(self, 'CSSTransformComponent', {
                        value: out, writable: true, enumerable: false, configurable: true,
                    });
                } catch (e) {}
            })();
            // ---- 内核已有同名接口的修补（CSSSkewX / CSSSkewY）----
            // Chromium Worker 里这两个构造器只认内核自己的 CSSNumericValue，
            // 补出来的 CSSUnitValue 过不了它的品牌校验，必须换掉构造器。原型
            // 对象保留（实例形态、instanceof 链不动），只重装成员。
            (function () {
                [['CSSSkewX', 'ax', 'skewx'], ['CSSSkewY', 'ay', 'skewy']].forEach(function (row) {
                    var name = row[0], member = row[1], tk = row[2];
                    var C = null;
                    try { C = self[name]; } catch (e) {}
                    if (typeof C !== 'function' || !C.prototype) { return; }
                    var proto = C.prototype;
                    var out = new Proxy(C, {
                        apply: function () { throw new TypeError("Constructor requires 'new' operator"); },
                        construct: function (t, args) {
                            if (!args.length || args[0] === undefined || args[0] === null) {
                                throw new TypeError('Not enough arguments');
                            }
                            var base = Object.create(proto);
                            __wkStateW.set(base, Object.create(null));
                            var st = __wkStW(base);
                            if (!__wkHasW(args[0])) {
                                throw new TypeError("Argument 1 ('" + member + "') to the " + name
                                    + ' constructor must be an instance of CSSNumericValue');
                            }
                            st[member] = args[0];
                            st.is2D = true;
                            st.__tk = tk;
                            return base;
                        },
                    });
                    try { Object.defineProperty(out, 'length', { value: 1, configurable: true }); } catch (e) {}
                    __maskNative(out, name, null, true);
                    try {
                        Object.defineProperty(proto, 'constructor', {
                            value: out, writable: true, enumerable: false, configurable: true,
                        });
                    } catch (e) {}
                    __wkPropW(proto, name, member, true, null, 0);
                    __wkCtorFirst(proto);
                    try {
                        Object.defineProperty(self, name, {
                            value: out, writable: true, enumerable: false, configurable: true,
                        });
                    } catch (e) {}
                });
            })();
            var __wkPasses = 0;
            var __wkDone = {};
            while (__wkPasses < 6) {
                var __wkProgress = false;
                for (var __wkName in __wkWIface) {
                    if (!Object.prototype.hasOwnProperty.call(__wkWIface, __wkName)) { continue; }
                    if (__wkDone[__wkName]) { continue; }
                    var __wkSpec = __wkWIface[__wkName];
                    var __wkParentReady = true;
                    if (__wkSpec.extends && !__wkSpec.repair) {
                        try { __wkParentReady = (typeof self[__wkSpec.extends] !== 'undefined'); } catch (e) {}
                    }
                    if (!__wkParentReady) { continue; }
                    try {
                        if (__wkAddIfaceW(__wkName, __wkSpec)) {
                            __wkDone[__wkName] = 1;
                            __wkProgress = true;
                        } else { __wkDone[__wkName] = 1; }
                    } catch (e) { __wkDone[__wkName] = 1; }
                }
                if (!__wkProgress) { break; }
                __wkPasses++;
            }
            // 索引集合的 length 需要覆盖成「自有索引计数」（CSSNumericArray 的
            // length 是只读 getter，CSSTransformValue / CSSUnparsedValue 同）。
            ['CSSNumericArray', 'CSSTransformValue', 'CSSUnparsedValue'].forEach(function (n) {
                try { __wkFixCollections(n); } catch (e) {}
            });
            // MediaSource.isTypeSupported（F-142）：Worker 里 MediaSource 是
            // 原生 Blink 全局，不走 __wkAddIfaceW，得单独包一层——否则 Worker
            // 面仍是 Blink 口径（webm 家族 true、无参文案也是 Blink 的）。
            // 与页面同一张 MSE 真值表（见模块级 _MSE_SUPPORT_SRC）。
            try {
                var __wkMS = self.MediaSource;
                if (__wkMS && typeof __wkMS.isTypeSupported === 'function') {
                    __wkStaticW(__wkMS, 'isTypeSupported', function (t, args) {
                        if (!args.length) {
                            throw new TypeError('Not enough arguments');
                        }
                        return __mseSupports(args[0], D.mseTypes);
                    }, 1);
                }
            } catch (e) {}
            // ---- F-150 / F-151 / F-154：Worker 侧 MSE 面 ----
            // Worker 里 MediaSource / SourceBuffer 是 Blink 原生全局，不经过
            // __wkAddIfaceW，接收者校验、判定顺序、文案、原型顺序全得单独接管
            // （真机口径见 /tmp/f361 Worker 侧探针，页面侧见 /tmp/f357）：
            //   · 接收者校验：真机连 Object.create(原型) 也不认，用原生
            //     readyState / mode 的 getter 当探针；Blink 一律 Illegal
            //     invocation，真机是 'Can only call …' / 'The … getter can
            //     only be used on instances of …'；校验先于参数个数。
            //   · addSourceBuffer：缺参 → Symbol/空串 → 类型表 → readyState；
            //   · endOfStream：枚举转换（undefined 取默认 'end'，null 与
            //     非法值都是 TypeError）→ readyState；
            //   · removeSourceBuffer：缺参 → 实例校验 → 状态；
            //   · setLiveSeekableRange：缺参（两个都要）→ 非有限数 → 状态；
            //   · changeType：缺参 → 空串 → isRemoved()/updating → 类型表；
            //   · SourceBufferList 的 item() 在 Worker 里缺（页面侧走 repair
            //     规格补建），越界返回 null、缺参抛 Not enough。
            var __WK_DOM_MSG_W = {
                NotSupportedError: 'The operation is not supported.',
                InvalidStateError: 'The object is in an invalid state.',
                NotFoundError: 'The object can not be found here.',
                IndexSizeError: 'The index is not in the allowed range.',
                InvalidAccessError:
                    'The object does not support the operation or argument.',
                QuotaExceededError: 'The quota has been exceeded.',
                AbortError: 'The operation was aborted.',
                InvalidModificationError:
                    'The object can not be modified in this way.',
                SecurityError: 'The operation is insecure.',
                SyntaxError: 'The string did not match the expected pattern.',
                UnknownError:
                    'The operation failed for an unknown transient reason (e.g. out of memory).',
                DataCloneError: 'The object can not be cloned.',
                NetworkError: 'A network error occurred.',
                EncodingError:
                    'The encoding operation (either encoded or decoding) failed.',
                TimeoutError: 'The operation timed out.',
            };
            // Blink 的 Worker 异常同样带 'Failed to execute …' 前缀。
            var __wkNormMsgW = function (msg) {
                var s = String(msg);
                var p = /^Failed to (?:execute '[^']*' on|set the '[^']*' property on) '[^']*': (.*)$/.exec(s);
                if (!p) { return s; }
                var body = p[1];
                if (/arguments? required, but only \d+ present/.test(body)) {
                    return 'Not enough arguments';
                }
                if (/^Cannot convert a Symbol value to a string$/.test(body)) {
                    return 'Cannot convert a symbol to a string';
                }
                if (/^The provided double value is non-finite\.?$/.test(body)) {
                    return 'The provided value is non-finite';
                }
                if (/^The provided value .* is not a valid enum value of type /.test(body)) {
                    return "Argument 1 ('error') to MediaSource.endOfStream must be one of: "
                        + '"network", "decode"';
                }
                if (/^parameter 1 is not of type 'SourceBuffer'\.$/.test(body)) {
                    return "Argument 1 ('buffer') to MediaSource.removeSourceBuffer"
                        + ' must be an instance of SourceBuffer';
                }
                if (/^The (?:provided value|value provided|start provided|end provided|duration|mode value)\b/
                        .test(body)
                        || /^The (?:start|end) value provided\b/.test(body)
                        || /^The type provided/.test(body)
                        || /^Overload resolution failed\.$/.test(body)
                        || /^Changing to the type provided/.test(body)) {
                    return 'Type error';
                }
                return body;
            };
            var __wkNormErrW = function (e) {
                try {
                    if (!e || typeof e !== 'object') { return e; }
                    if (typeof DOMException !== 'undefined'
                            && e instanceof DOMException) {
                        var dm = __WK_DOM_MSG_W[e.name];
                        if (dm) {
                            try { return new DOMException(dm, e.name); }
                            catch (e2) { return e; }
                        }
                        return e;
                    }
                    if (e.name === 'TypeError') {
                        return new TypeError(__wkNormMsgW(e.message));
                    }
                } catch (e3) {}
                return e;
            };
            var __wkDomW = function (name) {
                try { return new DOMException(__WK_DOM_MSG_W[name], name); }
                catch (e) {
                    var err = new Error(__WK_DOM_MSG_W[name]);
                    err.name = name;
                    return err;
                }
            };
            var __wkMseContainerW = function (t) {
                return String(t).split(';')[0].trim().toLowerCase();
            };
            // 真机认、宿主 Worker 里不认的类型：换等价类型去建 buffer（与页面
            // 同一口径）。SourceBuffer 没有 type 属性，替换不可观测。
            var __wkStandInW = function (t) {
                var c = __wkMseContainerW(t);
                if (c === 'audio/mpeg' || c === 'audio/aac') {
                    return 'audio/mpeg';
                }
                return 'video/mp4; codecs="avc1.42E01E"';
            };
            var __wkOrderW = function (proto, names) {
                if (!proto) { return; }
                var all = [];
                try { all = Object.getOwnPropertyNames(proto); }
                catch (e) { return; }
                var ds = {};
                all.forEach(function (n) {
                    try { ds[n] = Object.getOwnPropertyDescriptor(proto, n); }
                    catch (e) {}
                });
                var seq = names.filter(function (n) { return !!ds[n]; });
                all.forEach(function (n) {
                    if (names.indexOf(n) < 0) { seq.push(n); }
                });
                seq.forEach(function (n) {
                    try { delete proto[n]; } catch (e) {}
                });
                seq.forEach(function (n) {
                    try { Object.defineProperty(proto, n, ds[n]); }
                    catch (e) {}
                });
            };
            // 原生 getter 当接收者探针：宿主自己判定内部槽，Object.create(原型)
            // 与 {} 都会抛，和真机一致（原型链判定会误收前者）。
            var __wkBrandProbeW = function (proto, member) {
                var get = null;
                try {
                    var d = Object.getOwnPropertyDescriptor(proto, member);
                    get = (d && d.get) || null;
                } catch (e) {}
                if (!get) {
                    return function (recv) { return __slotHas(proto, recv); };
                }
                return function (recv) {
                    if (recv === null || recv === undefined) { return false; }
                    try { get.call(recv); return true; }
                    catch (e) { return false; }
                };
            };
            // 给接口原型上每个成员套接收者校验 + 文案归一化；skip 里的方法由
            // 下面的专属实现接管（它们自己先查接收者，再按 IDL 顺序判参）。
            var __wkBrandAllW = function (proto, iface, probeName, skip) {
                var ok = __wkBrandProbeW(proto, probeName);
                var names = [];
                try { names = Object.getOwnPropertyNames(proto); } catch (e) { return ok; }
                names.forEach(function (n) {
                    if (n === 'constructor') { return; }
                    if (skip && skip.indexOf(n) >= 0) { return; }
                    var d;
                    try { d = Object.getOwnPropertyDescriptor(proto, n); } catch (e) { return; }
                    if (!d || !d.configurable) { return; }
                    try {
                        if (typeof d.value === 'function') {
                            var orig = d.value;
                            var impl = function () {
                                if (!ok(this)) {
                                    throw __wkBrandErrW(iface, n, 'method');
                                }
                                try { return orig.apply(this, arguments); }
                                catch (e) { throw __wkNormErrW(e); }
                            };
                            try {
                                Object.defineProperty(impl, 'length', {
                                    value: orig.length, configurable: true,
                                });
                            } catch (e) {}
                            Object.defineProperty(proto, n, {
                                value: __maskNative(impl, n), writable: true,
                                enumerable: d.enumerable, configurable: true,
                            });
                        } else if (d.get || d.set) {
                            var ng = d.get ? function () {
                                if (!ok(this)) {
                                    throw __wkBrandErrW(iface, n, 'getter');
                                }
                                try { return d.get.call(this); }
                                catch (e) { throw __wkNormErrW(e); }
                            } : undefined;
                            var ns = d.set ? function (v) {
                                if (!ok(this)) {
                                    throw __wkBrandErrW(iface, n, 'setter');
                                }
                                try { return d.set.call(this, v); }
                                catch (e) { throw __wkNormErrW(e); }
                            } : undefined;
                            Object.defineProperty(proto, n, {
                                get: ng && __maskNative(ng, 'get ' + n),
                                set: ns && __maskNative(ns, 'set ' + n),
                                enumerable: d.enumerable, configurable: true,
                            });
                        }
                    } catch (e) {}
                });
                return ok;
            };
            var __wkDefMseW = function (proto, member, impl, len) {
                try {
                    var d = Object.getOwnPropertyDescriptor(proto, member);
                    var orig = d && d.value;
                    if (typeof orig !== 'function') { return; }
                    var fn = function () {
                        return impl.call(this, orig, arguments);
                    };
                    try {
                        Object.defineProperty(fn, 'length', {
                            value: len, configurable: true,
                        });
                    } catch (e) {}
                    Object.defineProperty(proto, member, {
                        value: __maskNative(fn, member), writable: true,
                        enumerable: true, configurable: true,
                    });
                } catch (e) {}
            };
            var __wkMseTypeW = function (type) {
                if (typeof type === 'symbol') {
                    throw new TypeError('Cannot convert a symbol to a string');
                }
                var t = String(type);
                if (t === '') { throw new TypeError('Type error'); }
                return t;
            };
            var __sbOkW = null;
            try {
                if (self.MediaSource && self.MediaSource.prototype) {
                    var __msProtoW = self.MediaSource.prototype;
                    var __msOkW = __wkBrandAllW(__msProtoW, 'MediaSource', 'readyState',
                        ['addSourceBuffer', 'removeSourceBuffer', 'endOfStream',
                         'setLiveSeekableRange', 'clearLiveSeekableRange']);
                    __wkDefMseW(__msProtoW, 'addSourceBuffer', function (orig, args) {
                        if (!__msOkW(this)) {
                            throw __wkBrandErrW('MediaSource', 'addSourceBuffer', 'method');
                        }
                        if (!args.length) {
                            throw new TypeError('Not enough arguments');
                        }
                        var t = __wkMseTypeW(args[0]);
                        if (!__mseSupports(t, D.mseTypes)) {
                            throw __wkDomW('NotSupportedError');
                        }
                        if (this.readyState !== 'open') {
                            throw __wkDomW('InvalidStateError');
                        }
                        try {
                            return orig.call(this, t);
                        } catch (e) {
                            var ne = __wkNormErrW(e);
                            if (ne && ne.name === 'NotSupportedError') {
                                try { return orig.call(this, __wkStandInW(t)); }
                                catch (e2) { throw __wkNormErrW(e2); }
                            }
                            throw ne;
                        }
                    }, 1);
                    __wkDefMseW(__msProtoW, 'removeSourceBuffer', function (orig, args) {
                        if (!__msOkW(this)) {
                            throw __wkBrandErrW('MediaSource', 'removeSourceBuffer', 'method');
                        }
                        if (!args.length) {
                            throw new TypeError('Not enough arguments');
                        }
                        if (!__sbOkW || !__sbOkW(args[0])) {
                            throw new TypeError("Argument 1 ('buffer') to "
                                + 'MediaSource.removeSourceBuffer must be an '
                                + 'instance of SourceBuffer');
                        }
                        try { return orig.call(this, args[0]); }
                        catch (e) { throw __wkNormErrW(e); }
                    }, 1);
                    __wkDefMseW(__msProtoW, 'endOfStream', function (orig, args) {
                        if (!__msOkW(this)) {
                            throw __wkBrandErrW('MediaSource', 'endOfStream', 'method');
                        }
                        if (args.length && args[0] !== undefined) {
                            var v = args[0];
                            // WebIDL 先做 ToString（Symbol 在此抛），再查枚举。
                            if (typeof v === 'symbol') {
                                throw new TypeError('Cannot convert a symbol to a string');
                            }
                            if (v !== 'network' && v !== 'decode') {
                                throw new TypeError("Argument 1 ('error') to "
                                    + 'MediaSource.endOfStream must be one of: '
                                    + '"network", "decode"');
                            }
                        }
                        try { return orig.call(this, args[0]); }
                        catch (e) { throw __wkNormErrW(e); }
                    }, 0);
                    __wkDefMseW(__msProtoW, 'setLiveSeekableRange',
                                 function (orig, args) {
                        if (!__msOkW(this)) {
                            throw __wkBrandErrW('MediaSource', 'setLiveSeekableRange',
                                                'method');
                        }
                        if (args.length < 2) {
                            throw new TypeError('Not enough arguments');
                        }
                        var a = Number(args[0]), b = Number(args[1]);
                        if (!isFinite(a) || !isFinite(b)) {
                            throw new TypeError('The provided value is non-finite');
                        }
                        try { return orig.call(this, a, b); }
                        catch (e) { throw __wkNormErrW(e); }
                    }, 2);
                    __wkDefMseW(__msProtoW, 'clearLiveSeekableRange',
                                 function (orig, args) {
                        if (!__msOkW(this)) {
                            throw __wkBrandErrW('MediaSource', 'clearLiveSeekableRange',
                                                'method');
                        }
                        try { return orig.call(this); }
                        catch (e) { throw __wkNormErrW(e); }
                    }, 0);
                    __wkOrderW(__msProtoW, ['constructor', 'handle', 'sourceBuffers',
                        'activeSourceBuffers', 'readyState', 'duration', 'onsourceopen',
                        'onsourceended', 'onsourceclose', 'addSourceBuffer',
                        'removeSourceBuffer', 'endOfStream', 'setLiveSeekableRange',
                        'clearLiveSeekableRange']);
                }
            } catch (e) {}
            try {
                if (self.SourceBuffer && self.SourceBuffer.prototype) {
                    var __sbProtoW = self.SourceBuffer.prototype;
                    __sbOkW = __wkBrandAllW(__sbProtoW, 'SourceBuffer', 'updating',
                                            ['changeType']);
                    __wkDefMseW(__sbProtoW, 'changeType', function (orig, args) {
                        if (!__sbOkW(this)) {
                            throw __wkBrandErrW('SourceBuffer', 'changeType', 'method');
                        }
                        if (!args.length) {
                            throw new TypeError('Not enough arguments');
                        }
                        var t = __wkMseTypeW(args[0]);
                        var gone = false;
                        try { void this.mode; } catch (e) { gone = true; }
                        if (gone || this.updating) {
                            throw __wkDomW('InvalidStateError');
                        }
                        if (!__mseSupports(t, D.mseTypes)) {
                            throw __wkDomW('NotSupportedError');
                        }
                        try { return orig.call(this, t); }
                        catch (e) {
                            var ne = __wkNormErrW(e);
                            if (ne && ne.name === 'NotSupportedError') {
                                try { return orig.call(this, __wkStandInW(t)); }
                                catch (e2) { throw __wkNormErrW(e2); }
                            }
                            throw ne;
                        }
                    }, 1);
                    __wkOrderW(__sbProtoW, ['constructor', 'mode', 'updating', 'buffered',
                        'timestampOffset', 'appendWindowStart', 'appendWindowEnd',
                        'onupdatestart', 'onupdate', 'onupdateend', 'onerror', 'onabort',
                        'appendBuffer', 'abort', 'remove', 'changeType']);
                }
            } catch (e) {}
            // Worker 里没有 SourceBufferList 全局（WebKit 也不暴露），实例的
            // 原型是 Blink 的隐藏原型，只能从 MediaSource 实例上取。
            try {
                var __wkMS2 = self.MediaSource;
                var __wkSblProto = null;
                if (__wkMS2 && __wkMS2.prototype) {
                    var __wkSblD = Object.getOwnPropertyDescriptor(
                        __wkMS2.prototype, 'sourceBuffers');
                    if (__wkSblD && __wkSblD.get) {
                        __wkSblProto = Object.getPrototypeOf(
                            __wkSblD.get.call(new __wkMS2()));
                    }
                }
                if (__wkSblProto) {
                    var __sblOkW = __wkBrandProbeW(__wkSblProto, 'length');
                    if (!('item' in __wkSblProto)) {
                        var __wkSblItem = function (i) {
                            if (!__sblOkW(this)) {
                                throw __wkBrandErrW('SourceBufferList', 'item',
                                                    'method');
                            }
                            if (!arguments.length) {
                                throw new TypeError('Not enough arguments');
                            }
                            return null;
                        };
                        try {
                            Object.defineProperty(__wkSblItem, 'length', {
                                value: 1, configurable: true,
                            });
                        } catch (e) {}
                        Object.defineProperty(__wkSblProto, 'item', {
                            value: __maskNative(__wkSblItem, 'item'), writable: true,
                            enumerable: true, configurable: true,
                        });
                    }
                    __wkBrandAllW(__wkSblProto, 'SourceBufferList', 'length', ['item']);
                    __wkOrderW(__wkSblProto, ['constructor', 'length',
                        'onaddsourcebuffer', 'onremovesourcebuffer', 'item']);
                }
            } catch (e) {}
            // WebCodecs 静态面（F-133/F-132）：Worker 里这四个接口由 Blink 原生
            // 提供，补建链路 `existing && !spec.repair` 会整条跳过。走 repair
            // 会把原生原型与实例形态一起换掉（VideoFrame/EncodedVideoChunk 的
            // 原生实例就再也过不了品牌校验），所以只包静态：保留原生构造器与
            // 原型，把 isConfigSupported 换成 WebKit 口径。
            ['AudioDecoder', 'AudioEncoder', 'VideoDecoder', 'VideoEncoder']
                .forEach(function (name) {
                    try {
                        var Ctor = self[name];
                        if (!Ctor || typeof Ctor.isConfigSupported !== 'function') { return; }
                        __wkStaticW(Ctor, 'isConfigSupported',
                                    __wcIcsImpl(name, __wcIcsKind(name)), 1);
                    } catch (e) {}
                });
            // mediaCapabilities 原型方法（F-135）：MediaCapabilities 在
            // `Exposed=(Window,DedicatedWorker)` 里，WorkerNavigator 也有该属性。
            try {
                var __mcProto = self.MediaCapabilities
                    && self.MediaCapabilities.prototype;
                var __mcInst = self.navigator && self.navigator.mediaCapabilities;
                if (__mcProto && __mcInst) {
                    var __mcOpt = { av1: !!D.av1 };
                    defineFn(__mcProto, 'decodingInfo', function (config) {
                        return __mc.dec(this, arguments, __mcInst, __mcOpt);
                    }, true);
                    defineFn(__mcProto, 'encodingInfo', function (config) {
                        return __mc.enc(this, arguments, __mcInst, __mcOpt);
                    }, true);
                }
            } catch (e) {}
            // CSSMathSum.values 每次读取都是同一个对象（实测 sum.values === sum.values）
            ['CSSMathSum', 'CSSMathProduct', 'CSSMathMin', 'CSSMathMax'].forEach(function (n) {
                var C = self[n];
                if (!C || !C.prototype) { return; }
                try {
                    Object.defineProperty(C.prototype, 'values', {
                        get: __maskNative(function () {
                            if (!__wkHasW(this)) {
                                throw __wkBrandErrW(n, 'values', 'getter');
                            }
                            var s = __wkStW(this);
                            if (!s.__arr) { s.__arr = __wkNumArray(s.__vals || []); }
                            return s.__arr;
                        }, 'get values'),
                        enumerable: true, configurable: true,
                    });
                } catch (e) {}
            });
            // CSSMathValue.operator 由 init 直接给（sum / product / min / max /
            // negate / invert / clamp），无需覆盖。
        }
        // ---- Worker 独有的全局成员（F-77 D3）----
        // 真机 WebKit 的 WorkerNavigator.prototype.serviceWorker 是只读 getter
        // （enumerable true、无 setter、get.length 0），每次读取返回同一个
        // ServiceWorkerContainer 实例（SameObject）；Chromium 的 Worker 里该
        // 成员与接口都不存在。实例由补建的接口原型生成，零自有属性，controller
        // 为 null、ready 为永不 settle 的 Promise（真机无注册时同样不 settle）。
        (function () {
            var addG = __wkWA.globals || {};
            if (!addG.serviceWorker || typeof addG.serviceWorker.instance !== 'string') { return; }
            var C = null;
            try { C = self[addG.serviceWorker.instance]; } catch (e) {}
            if (!C || !C.prototype) { return; }
            var inst = null;
            try {
                inst = Object.create(C.prototype);
                if (typeof __wkStateW !== 'undefined' && __wkStateW) {
                    var s = Object.create(null);
                    s.controller = null;
                    s.ready = new Promise(function () {});
                    __wkStateW.set(inst, s);
                }
            } catch (e) { inst = null; }
            if (!inst) { return; }
            var P = null;
            try { P = self.WorkerNavigator && self.WorkerNavigator.prototype; } catch (e) {}
            if (!P || Object.prototype.hasOwnProperty.call(P, 'serviceWorker')) { return; }
            try {
                Object.defineProperty(P, 'serviceWorker', {
                    get: __maskNative(function () { return inst; }, 'get serviceWorker'),
                    enumerable: true, configurable: true,
                });
            } catch (e) {}
        })();
        // ---- 接口原型成员差集（F-102）----
        // 与页面 dropMembers 同一张表；放在接口补建之后，删除才不会被随后的
        // repair 重新装回。Worker 全局没有的构造器直接跳过。
        (function () {
            var dm = D.dropMembers || {};
            for (var cn in dm) {
                if (!Object.prototype.hasOwnProperty.call(dm, cn)) { continue; }
                var C = null;
                try { C = self[cn]; } catch (e) {}
                if (typeof C !== 'function' || !C.prototype) { continue; }
                var P = C.prototype;
                var mems = dm[cn] || [];
                for (var mi = 0; mi < mems.length; mi++) {
                    try { delete P[mems[mi]]; } catch (e) {}
                    try {
                        if (mems[mi] in P) {
                            Object.defineProperty(P, mems[mi], {
                                get: __maskNative(function () { return undefined; },
                                                  'get ' + mems[mi]),
                                enumerable: true, configurable: true,
                            });
                        }
                    } catch (e) {}
                }
            }
        })();
        __wkPruneNav();

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
        // Intl 上的平台差集成员：Intl 既非 self 成员也非 WorkerGlobalScope
        // 原型成员（挂在 Intl 对象自身），上面的按名删除覆盖不到。Worker 里
        // `Intl.v8BreakIterator` 与页面同源存在（实测），不补则「页面 false、
        // Worker true」构成跨上下文矛盾。D.removeGlobals 与
        // D.removeWorkerGlobals 两表都需查（iOS 的 v8BreakIterator 在页面表里）。
        (function () {
            if (typeof Intl === 'undefined') { return; }
            var pools = [dropsW, D.removeGlobals || [], D.baseGlobals || []];
            var seen = {};
            for (var pi = 0; pi < pools.length; pi++) {
                for (var qi = 0; qi < pools[pi].length; qi++) {
                    if (pools[pi][qi] === 'v8BreakIterator') { seen[pools[pi][qi]] = 1; }
                }
            }
            var __dropIntlW = function () {
                for (var k in seen) {
                    try { delete Intl[k]; } catch (e) {}
                }
            };
            __dropIntlW();
            try { setTimeout(__dropIntlW, 0); } catch (e) {}
        })();

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

        // ---- Android Chrome：Worker 内 downlinkMax / ontypechange ----
        // NetworkInformation 的 Exposed=(Window,Worker)：真机 Android Worker
        // 与主文档同面（BCD worker_support 同 chrome_android=38）。形态与
        // 页面模板一致：downlinkMax 只读、ontypechange 为 get/set 访问器对。
        if (!D.ios && D.downlinkMax !== null && D.downlinkMax !== undefined
                && typeof NetworkInformation !== 'undefined') {
            defineGet(NetworkInformation.prototype, 'downlinkMax', D.downlinkMax);
            try {
                var __niHandlers = new WeakMap();
                var __niGet = __maskNative(function () {
                    if (!__slotHas(NetworkInformation.prototype, this)) {
                        throw new TypeError('Illegal invocation');
                    }
                    var h = __niHandlers.get(this);
                    return h === undefined ? null : h;
                }, 'get ontypechange');
                var __niSet = __maskNative(function (v) {
                    if (!__slotHas(NetworkInformation.prototype, this)) {
                        throw new TypeError('Illegal invocation');
                    }
                    __niHandlers.set(this, (typeof v === 'function') ? v : null);
                }, 'set ontypechange');
                Object.defineProperty(NetworkInformation.prototype, 'ontypechange', {
                    get: __niGet, set: __niSet, enumerable: true, configurable: true,
                });
            } catch (e) {}
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

        // ---- PerformanceObserver.supportedEntryTypes：Worker 作用域（F-06）----
        // WebKit 的 supportedEntryTypes(ScriptExecutionContext&) 在 Worker 里
        // 没有 Document → 不追加 event / first-input /
        // largest-contentful-paint / paint，只剩 mark / measure / navigation /
        // resource 四项（PerformanceObserver.cpp:153-174）。
        if (D.ios && typeof PerformanceObserver !== 'undefined') {
            try {
                var __wpoTypes = Object.freeze([
                    'mark', 'measure', 'navigation', 'resource'
                ]);
                Object.defineProperty(PerformanceObserver, 'supportedEntryTypes', {
                    value: __wpoTypes, writable: false,
                    enumerable: true, configurable: true,
                });
            } catch (e) {}
        }

        // ---- WebGL：Worker 内（OffscreenCanvas）与主文档同口径 ----
        // 实测：Worker 里 WebGLRenderingContext 与 OffscreenCanvas 都可用，
        // 扩展集与主文档完全相同（宿主 39 项，含 S3TC/BPTC/polygon_mode），
        // 且 UNMASKED_RENDERER 直接报宿主机型（Apple M2）。只补主文档不补
        // Worker，等于把「页面是 Adreno、Worker 是 Apple M」的跨上下文矛盾
        // 留在原地——检测站起一个 Worker 读一次即可命中。
        if ((D.webgl && D.webgl.renderer) || D.webglProfile) {
            try {
                var __wglDrop = (D.webglProfile && D.webglProfile.dropExtensions) || [];
                var __wglLimits = (D.webglProfile && D.webglProfile.limits) || {};
                var __wglPatch = function (Ctor) {
                    if (!Ctor || !Ctor.prototype || !Ctor.prototype.getParameter) { return; }
                    var __wglEnum = function (name) {
                        try { var v = Ctor[name]; return (typeof v === 'number') ? v : null; }
                        catch (e) { return null; }
                    };
                    var __wglOrigP = Ctor.prototype.getParameter;
                    defineFn(Ctor.prototype, 'getParameter', function (p) {
                        var nat = __wglOrigP.apply(this, arguments);
                        if (p === 37445 || p === 37446) {
                            if (!D.webgl || !D.webgl.renderer) { return nat; }
                            return p === 37445 ? D.webgl.vendor : D.webgl.renderer;
                        }
                        if (D.ios && (p === __wglEnum('VERSION')
                                      || p === __wglEnum('SHADING_LANGUAGE_VERSION'))) {
                            // F-91：与页面侧同口径，按枚举显式映射真值串。
                            var __wgl2 = (typeof WebGL2RenderingContext !== 'undefined'
                                          && Ctor === WebGL2RenderingContext);
                            if (p === __wglEnum('VERSION')) {
                                return __wgl2 ? 'WebGL 2.0' : 'WebGL 1.0';
                            }
                            return __wgl2 ? 'WebGL GLSL ES 3.00'
                                          : 'WebGL GLSL ES 1.0 (1.0)';
                        }
                        if (typeof p === 'number') {
                            for (var ln in __wglLimits) {
                                if (!Object.prototype.hasOwnProperty.call(__wglLimits, ln)) { continue; }
                                var en = __wglEnum(ln);
                                if (en !== null && en === p && nat !== null && nat !== undefined) {
                                    var lv = __wglLimits[ln];
                                    return (lv && lv.length !== undefined) ? new Int32Array(lv) : lv;
                                }
                            }
                        }
                        return nat;
                    });
                    if (!__wglDrop.length || !Ctor.prototype.getSupportedExtensions) { return; }
                    var __wglSet = {};
                    for (var wi = 0; wi < __wglDrop.length; wi++) {
                        __wglSet[String(__wglDrop[wi]).toLowerCase()] = true;
                    }
                    var __wglList = Ctor.prototype.getSupportedExtensions;
                    var __wglGet = Ctor.prototype.getExtension;
                    defineFn(Ctor.prototype, 'getSupportedExtensions', function () {
                        var all = __wglList.apply(this, arguments);
                        if (!all) { return all; }
                        var out = [];
                        for (var ai = 0; ai < all.length; ai++) {
                            if (!__wglSet[String(all[ai]).toLowerCase()]) { out.push(all[ai]); }
                        }
                        return out;
                    });
                    defineFn(Ctor.prototype, 'getExtension', function (name) {
                        if (name !== undefined && name !== null
                                && __wglSet[String(name).toLowerCase()]) {
                            return null;
                        }
                        return __wglGet.apply(this, arguments);
                    });
                    var __wglAdd = (D.webglProfile && D.webglProfile.addExtensions) || [];
                    if (__wglAdd.length && Ctor.prototype.getSupportedExtensions) {
                        var __wAddList = Ctor.prototype.getSupportedExtensions;
                        var __wAddGet = Ctor.prototype.getExtension;
                        var __wAddSet = {};
                        for (var __wj = 0; __wj < __wglAdd.length; __wj++) {
                            __wAddSet[String(__wglAdd[__wj]).toLowerCase()] = 1;
                        }
                        defineFn(Ctor.prototype, 'getSupportedExtensions', function () {
                            var all = __wAddList.apply(this, arguments) || [];
                            var out = [];
                            var seen = {};
                            for (var __wk = 0; __wk < all.length; __wk++) {
                                var nm = String(all[__wk]);
                                if (!seen[nm.toLowerCase()]) {
                                    seen[nm.toLowerCase()] = 1;
                                    out.push(all[__wk]);
                                }
                            }
                            for (var __wm = 0; __wm < __wglAdd.length; __wm++) {
                                var an = String(__wglAdd[__wm]);
                                if (!seen[an.toLowerCase()]) {
                                    seen[an.toLowerCase()] = 1;
                                    out.push(an);
                                }
                            }
                            return out;
                        });
                        defineFn(Ctor.prototype, 'getExtension', function (name) {
                            var key = (name === undefined || name === null)
                                ? '' : String(name).toLowerCase();
                            if (Object.prototype.hasOwnProperty.call(__wAddSet, key)) {
                                var ext = {};
                                if (key === 'webkit_webgl_compressed_texture_pvrtc') {
                                    ext.COMPRESSED_RGB_PVRTC_4BPPV1_IMG = 0x8C00;
                                    ext.COMPRESSED_RGB_PVRTC_2BPPV1_IMG = 0x8C01;
                                    ext.COMPRESSED_RGBA_PVRTC_4BPPV1_IMG = 0x8C02;
                                    ext.COMPRESSED_RGBA_PVRTC_2BPPV1_IMG = 0x8C03;
                                }
                                return ext;
                            }
                            return __wAddGet.apply(this, arguments);
                        });
                    }
                    if (D.ios && Ctor.prototype.getContextAttributes) {
                        var __wglAttrs = Ctor.prototype.getContextAttributes;
                        defineFn(Ctor.prototype, 'getContextAttributes', function () {
                            var attrs = __wglAttrs.apply(this, arguments);
                            if (!attrs) { return attrs; }
                            try { delete attrs.desynchronized; } catch (e) {}
                            try { delete attrs.xrCompatible; } catch (e) {}
                            return attrs;
                        });
                    }
                };
                __wglPatch((typeof WebGLRenderingContext !== 'undefined') ? WebGLRenderingContext : null);
                __wglPatch((typeof WebGL2RenderingContext !== 'undefined') ? WebGL2RenderingContext : null);
            } catch (e) {}
        }

        // ---- WebGPU：Worker 内 GPUAdapterInfo 与主文档同口径 ----
        // （Dedicated Worker 可用 navigator.gpu；宿主 Apple 适配器信息在这里
        //   同样泄漏，真机 Android Worker 报与主文档相同的 Dawn 代际口径）
        if (D.webgpu && typeof GPUAdapterInfo !== 'undefined' && GPUAdapterInfo.prototype) {
            defineGet(GPUAdapterInfo.prototype, 'vendor', D.webgpu.vendor);
            defineGet(GPUAdapterInfo.prototype, 'architecture', D.webgpu.architecture);
        }

        // ---- FontFace：裁掉宿主本地字体（F-163）----
        if (__ffWrap) {
            try {
                __maskNative(__ffWrap, 'FontFace', null, true);
                Object.defineProperty(self, 'FontFace', {
                    value: __ffWrap, writable: true,
                    enumerable: false, configurable: true,
                });
            } catch (e) {}
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
        "baseGlobals": list(spec.get("baseGlobals") or []),
        "removeWorkerGlobals": list(spec.get("removeWorkerGlobals") or []),
        "workerAdd": spec.get("workerAdd") or {},
        # F-102：原型成员差集。页面侧由 _PATCH_TEMPLATE 的 dropMembers 收口，
        # Worker 是独立全局、拿不到那一步；这里原样下发，脚本内按
        # 「Worker 全局里确实有该构造器」过滤，避免页面独有接口的项空转。
        "dropMembers": dict(spec.get("dropMembers") or {}),
        "connectionType": str(spec.get("connectionType") or ""),
        "downlinkMax": spec.get("downlinkMax"),
        "ios": bool(spec.get("ios")),
        "webgpu": spec.get("webgpu"),
        "webgl": spec.get("webgl"),
        "webglProfile": spec.get("webglProfile"),
        # F-142：Worker 侧 MediaSource.isTypeSupported 与页面同表
        "av1": bool(spec.get("av1")),
        "mseTypes": spec.get("mseTypes") or {},
        # F-163：Worker 全局同样暴露 FontFace，local() 源泄漏同一批宿主字体
        "fonts": list(spec.get("fonts") or []),
    }
    return (_WORKER_PATCH_TEMPLATE
            .replace("__MSE_SRC__", _MSE_SUPPORT_SRC)
            .replace("__WC_SRC__", _WEBCODECS_ICS_SRC)
            .replace("__MC_SRC__", _MC_SRC)
            .replace("__FF_SRC__", _FONT_FACE_SRC)
            .replace("__SPEC__", json.dumps(payload, ensure_ascii=False)))


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
        "        // Intl 成员（如 v8BreakIterator）挂在 Intl 对象自身，非 self\n"
        "        // 成员也非原型成员，需按名单独删。\n"
        "        if (typeof Intl !== 'undefined') {\n"
        "            try { delete Intl[n]; } catch (e) {}\n"
        "        }\n"
        "    }\n"
        "})();\n"
    )


_WORKER_LATE_TEMPLATE = r"""
(function () {
    var D = __SPEC__;
    var __maskNative = (function () {
        try {
            var origToString = Function.prototype.toString;
            var registry = new WeakMap();
            var faked = function toString() {
                var v = registry.get(this);
                if (v !== undefined) { return v; }
                var s = origToString.call(this);
                if (D.ios && typeof s === 'string'
                        && s.indexOf('[native code]') !== -1
                        && s.indexOf('\n') === -1) {
                    var m = /^function\s*([^(]*)\(/.exec(s);
                    return 'function ' + (m ? m[1].trim() : '') + '() {\n    [native code]\n}';
                }
                return s;
            };
            var mask = function (fn, name, src, asCtor) {
                try {
                    if (typeof fn === 'function') {
                        var s = src || (D.ios
                            ? 'function ' + name + '() {\n    [native code]\n}'
                            : 'function ' + name + '() { [native code] }');
                        registry.set(fn, s);
                        try {
                            Object.defineProperty(fn, 'name',
                                { value: name, configurable: true });
                        } catch (e2) {}
                        if (asCtor) { return fn; }
                        var w = ({ m() { return fn.apply(this, arguments); } }).m;
                        registry.set(w, s);
                        try {
                            Object.defineProperty(w, 'name',
                                { value: name, configurable: true });
                        } catch (e3) {}
                        try {
                            Object.defineProperty(w, 'length',
                                { value: fn.length, configurable: true });
                        } catch (e4) {}
                        return w;
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
__WC_SRC__
__MC_SRC__
    var __staticW = function (Ctor, name, impl, len) {
        try {
            var fn = function () { return impl(this, arguments); };
            Object.defineProperty(fn, 'length', { value: len || 0, configurable: true });
            Object.defineProperty(Ctor, name, {
                value: __maskNative(fn, name),
                writable: true, enumerable: true, configurable: true,
            });
        } catch (e) {}
    };
    ['AudioDecoder', 'AudioEncoder', 'VideoDecoder', 'VideoEncoder']
        .forEach(function (name) {
            try {
                var Ctor = self[name];
                if (!Ctor || typeof Ctor.isConfigSupported !== 'function') { return; }
                __staticW(Ctor, 'isConfigSupported',
                          __wcIcsImpl(name, __wcIcsKind(name)), 1);
            } catch (e) {}
        });
    // mediaCapabilities 原型方法（F-135）：MediaCapabilities 在
    // `Exposed=(Window,DedicatedWorker)` 里，WorkerNavigator 也有该属性。
    try {
        var __mcProto = self.MediaCapabilities && self.MediaCapabilities.prototype;
        var __mcInst = self.navigator && self.navigator.mediaCapabilities;
        if (__mcProto && __mcInst) {
            var __mcOpt = { av1: !!D.av1 };
            var __mcFn = function (kind) {
                var fn = function (config) {
                    return __mc[kind](this, arguments, __mcInst, __mcOpt);
                };
                try {
                    Object.defineProperty(fn, 'length', { value: 1, configurable: true });
                } catch (e) {}
                return __maskNative(fn, kind === 'dec' ? 'decodingInfo' : 'encodingInfo');
            };
            Object.defineProperty(__mcProto, 'decodingInfo', {
                value: __mcFn('dec'),
                writable: true, enumerable: true, configurable: true,
            });
            Object.defineProperty(__mcProto, 'encodingInfo', {
                value: __mcFn('enc'),
                writable: true, enumerable: true, configurable: true,
            });
        }
    } catch (e) {}
})();
"""


def build_worker_late_patch(spec: Dict[str, Any]) -> str:
    """Worker 首脚本执行前（插桩断点）补装的那一层。

    WebCodecs 的四个接口由内核在 Worker 首脚本开始时才安装，暂停态
    `Runtime.evaluate` 读不到，所以静态 `isConfigSupported` 的包装只能
    放在「首行执行前」的断点窗口里（与全局差集补删同一时机）。
    """
    payload = {"ios": bool(spec.get("ios")), "av1": bool(spec.get("av1"))}
    return (_WORKER_LATE_TEMPLATE
            .replace("__WC_SRC__", _WEBCODECS_ICS_SRC)
            .replace("__MC_SRC__", _MC_SRC)
            .replace("__SPEC__", json.dumps(payload, ensure_ascii=False)))


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
            spec = derive_spec(fingerprint, target_os, locale)
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
                "baseGlobals": (list(_IOS_GLOBAL_DROPS) if target_os == "ios"
                                else list(_ANDROID_GLOBAL_DROPS)),
                "removeWorkerGlobals": (list(_IOS_WORKER_DROPS) if target_os == "ios"
                                        else list(_ANDROID_WORKER_DROPS)),
                "workerAdd": _worker_add_members(target_os),
                "emptyPlugins": True,
                "connectionType": "wifi" if target_os == "android" else None,
                "downlinkMax": _ANDROID_DOWNLINK_MAX_MBPS if target_os == "android" else None,
                "codecCapability": _codec_capability(target_os),
                # F-142：MSE 真值表开关。旧调用方式拿不到机型，av1 取 false
                # （与 derive_spec 的非 AV1 机型同口径）。
                "mseTypes": {"ios": target_os == "ios", "av1": False},
                "webglProfile": _webgl_profile(target_os),
                "ios": target_os == "ios", "android": target_os == "android",
                # F-161：与 derive_spec 同口径，默认语音按 locale 选语族
                "androidVoices": _android_tts_voices(target_os, locale),
                "iosChrome": None, "webgl": None, "webgpu": None,
                "mediaVideoInputs": 0,
                "cssWidth": css_w, "cssHeight": css_h,
                "viewportHeight": css_h, "dpr": scale,
                "safeArea": None,
                "dynamicRangeHigh": False,
                "naturalLandscape": target_os != "ios",
                # F-163：旧调用方式没有指纹，按平台给完整字体表（与
                # _generate_mobile_font_subset(full=True) 同口径）
                "fonts": _generate_mobile_font_subset(target_os, full=True),
            }

        self.spec = spec
        self.ua = spec["ua"]
        self.platform = spec["platform"]
        self.max_touch_points = spec["maxTouchPoints"]
        self.ios = bool(spec.get("ios"))
        self.css_w = spec["cssWidth"]
        self.css_h = spec["cssHeight"]
        self.viewport_h = spec["viewportHeight"]
        self.dpr = spec["dpr"]
        self.safe_area = spec.get("safeArea") or None

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
        self._first_page_task: Optional[asyncio.Task] = None
        # 安全区（P2-5 后半）：已挂安全区判定的页面会话（iOS）。
        # 决策按主文档生效；跨域子帧保持 0（真机 iframe 的 env() 同样是 0）。
        self._safe_sessions: set[str] = set()
        # Accept-CH：CDP session → {源: 该源已声明的高熵提示}（Fetch 域回送）
        self._accept_ch: dict[str, dict[str, set]] = {}
        # iOS Chrome POST 导航面（B1-7）：标记 cookie 的序号，见
        # _CRIOS_POST_REQUEST_JS。
        self._crios_post_seq = 0
        # 主帧 id：sessionId → frameId。Fetch 的 Document 请求同时包含同进程
        # 子帧，据此只认主文档（重放面不进子帧）。
        self._main_frames: dict[str, str] = {}
        # F-86：UA 竞态兜底计数（sessionId → 已兜底次数），见 _guard_ua。
        # 总量单独累计：重连会清空会话级字典（会话标识全部失效），
        # 验收侧要读的是「本次运行共遇到几次」。
        self._ua_guard: dict[str, int] = {}
        self._ua_guard_busy: set[str] = set()
        self._ua_guard_total = 0
        # 传感器仿真（Android）：虚拟传感器是浏览器进程级全局对象，全会话
        # 一条读数循环即可（详见 _setup_sensors）；supported=None 表示未探测
        self._sensor_supported: Optional[bool] = None
        self._sensor_task: Optional[asyncio.Task] = None
        self._sensor_session: Optional[str] = None
        self._closed = False
        # 通道健康看门狗：CDP 连接是「仿真是否生效」的唯一凭据——客户端断开
        # 时内核会还原全部 Emulation 覆盖，文档起点补丁脚本也随会话失效：
        # 此后打开的标签页连 JS 补丁都没有（实测检测站直接读到宿主 GPU /
        # maxTouchPoints=0 / pointer:fine / UA-CH 桌面值），已存在的文档则
        # 只剩 JS 层补丁，媒体查询回落宿主值。看门狗定期探活，断开后自动
        # 重连并重新覆盖所有已存在页面（自愈），供工作台展示与审计。
        self._watchdog: Optional[asyncio.Task] = None
        self._emu_ok = False
        # 注入后读数校验结果：None = 尚未校验，False = 通道在线但仿真未生效
        self._emu_verified: Optional[bool] = None
        self._reconnects = 0
        self._last_error = ""
        self.profile_id = ""
        self.profile_name = ""
        self.on_session_event: Optional[Callable[[str, int], None]] = None

    # ---------------- 生命周期 ----------------

    async def start(self) -> None:
        """连接浏览器调试端口并应用仿真；失败会自行清理。

        连接建立后同时启动通道看门狗：CDP 连接静默断开（实例长期空闲时
        实测发生）会由看门狗自动重连并重新覆盖所有页面，避免「标签页逐步
        裸奔」——新开标签页读到宿主 GPU/触屏/桌面媒体查询的典型事故。
        """
        await self._connect(navigate=True)
        if self._watchdog is None:
            self._watchdog = asyncio.create_task(self._watchdog_loop())

    async def _connect(self, *, navigate: bool) -> None:
        """建立一条新的 CDP 连接并应用仿真（首次连接与断线重连共用）。

        navigate=False（重连路径）时不重复导航 start_url，只补仿真。
        """
        import websockets

        # 新连接 = 全新会话上下文：上一连接的 session/target 标识全部失效，
        # 必须清空去重与暂存状态，才能对已存在页面重新应用（自愈的关键）。
        if self._sensor_task is not None:
            self._sensor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._sensor_task
            self._sensor_task = None
        self._applied_targets.clear()
        self._accept_ch.clear()
        self._worker_drop_waiters.clear()
        self._safe_sessions.clear()
        self._sensor_supported = None
        self._sensor_session = None
        self._emu_verified = None
        self._ua_guard.clear()
        self._ua_guard_busy.clear()
        self._first_page_task = None

        # proxy=None：CDP 端点恒在回环，websockets 14+ 默认读系统/环境代理，
        # 走代理会让握手 502 而整条注入链静默失效（移动仿真退化成裸桌面浏览器）
        self._ws = await websockets.connect(
            self.ws_endpoint, max_size=64 * 1024 * 1024, ping_interval=None,
            proxy=None,
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

        # 已存在的页面（重连时即「因断线而裸奔」的标签页，逐个重新覆盖）
        targets = (await self._send("Target.getTargets")).get("targetInfos", [])
        for info in targets:
            if info.get("type") == "page":
                await self._attach_page(info.get("targetId", ""))
            elif info.get("type") == "iframe":
                asyncio.create_task(self._attach_frame(info.get("targetId", "")))

        # 应用完成后导航到目标页（首个真实页面加载前指纹已生效）
        if navigate and self.start_url and self.start_url != "about:blank":
            await self._navigate_start_url()
        self._emu_ok = True

    async def close(self) -> None:
        self._closed = True
        # 看门狗先停：避免关闭流程与重连竞态（否则可能出现「关闭后重连成功」
        # 把仿真重新挂上，浏览器已无人回收）
        if self._watchdog is not None:
            self._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._watchdog
            self._watchdog = None
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

    @property
    def healthy(self) -> bool:
        """仿真通道当前是否在线（工作台展示用：断线期间标签页逐步裸奔）。"""
        return bool(self._emu_ok and not self._closed)

    @property
    def emulation_verified(self) -> Optional[bool]:
        """首屏读数校验：True = 仿真确实生效，False = 在线但未生效。"""
        return self._emu_verified

    @property
    def reconnect_count(self) -> int:
        return self._reconnects

    @property
    def ua_guard_count(self) -> int:
        """F-86：UA 竞态兜底触发次数（0 = 首文档 UA 全程正确）。"""
        return self._ua_guard_total

    # ---------------- 通道看门狗 ----------------

    # 探活间隔与命令超时（秒）：Target.getTargets 极轻，内核无感；
    # 断开后 1s 起步、指数退避（上限 30s）重连，恢复后 10s 一次继续探活。
    _WATCHDOG_INTERVAL = 10.0
    _WATCHDOG_TIMEOUT = 5.0

    async def _watchdog_loop(self) -> None:
        """定期探活；发现通道断开自动重连并重新覆盖所有页面。

        覆盖三种断线形态：对端关闭（send/recv 抛 ConnectionClosed）、
        半开连接（探活命令超时）、连接被替换。重连失败按指数退避重试，
        浏览器被关闭后连接会持续失败，由工作台停止流程回收本会话。
        """
        backoff = 1.0
        while not self._closed:
            try:
                await asyncio.sleep(
                    self._WATCHDOG_INTERVAL if self._emu_ok else backoff)
                if self._closed:
                    return
                if self._emu_ok:
                    await self._send("Target.getTargets", {},
                                     timeout=self._WATCHDOG_TIMEOUT)
                    continue
                if await self._reconnect():
                    backoff = 1.0
                else:
                    backoff = min(backoff * 2, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self._last_error = f"{type(e).__name__}: {e}"
                if self._emu_ok:
                    self._emu_ok = False
                    log.warning(
                        "移动端仿真通道断开（%s）[%s]，自动重连中……",
                        e, self.profile_name or self.profile_id or "?")
                backoff = 1.0

    async def _reconnect(self) -> bool:
        """重建 CDP 连接；成功后对所有已存在页面重新应用仿真与补丁。"""
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None
        if self._ws is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ws.close()
            self._ws = None
        try:
            await self._connect(navigate=False)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            self._last_error = f"{type(e).__name__}: {e}"
            return False
        self._reconnects += 1
        log.warning(
            "移动端仿真已自动重连（第 %d 次）[%s]：已打开页面全部重新应用仿真与补丁",
            self._reconnects, self.profile_name or self.profile_id or "?")
        if self.on_session_event is not None:
            with contextlib.suppress(Exception):
                self.on_session_event("reconnect", self._reconnects)
        return True

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
        try:
            await self._ws.send(json.dumps(msg))
        except Exception:
            # 发送失败（连接已断）也要回收 pending 槽位，避免句柄泄漏
            self._pending.pop(mid, None)
            raise
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
                    pending = self._worker_drop_waiters.get(sid_evt)
                    if pending is not None and params.get("reason") == "instrumentation":
                        self._worker_drop_waiters.pop(sid_evt, None)
                        asyncio.create_task(self._worker_drop_apply(sid_evt, pending))
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
                    # 目标发现兜底：auto-attach 投递存在客户端竞争（Playwright
                    # 同时连接会抢走），发现到新目标后显式挂载（去重后无副作用）。
                    info = (msg.get("params") or {}).get("targetInfo") or {}
                    tid = info.get("targetId")
                    if tid and info.get("type") == "page":
                        asyncio.create_task(self._attach_page(tid))
                    elif tid and info.get("type") == "iframe":
                        asyncio.create_task(self._attach_frame(tid))
                elif msg.get("method") == "Page.frameNavigated":
                    # 主文档提交（同文档导航走 navigatedWithinDocument，不触发）：
                    # 安全区覆盖跨导航保持（实测），必须清零后按新文档的
                    # <meta viewport> 重新判定，否则无 cover 的页面继承上一页的值。
                    sid_evt = msg.get("sessionId") or ""
                    frame = (msg.get("params") or {}).get("frame") or {}
                    if not frame.get("parentId"):
                        if frame.get("id"):
                            self._main_frames[sid_evt] = frame["id"]
                        if sid_evt in self._safe_sessions:
                            asyncio.create_task(self._safe_area_after_nav(sid_evt))
                elif msg.get("method") == "Page.domContentEventFired":
                    sid_evt = msg.get("sessionId") or ""
                    if sid_evt in self._safe_sessions:
                        asyncio.create_task(self._sync_safe_area(sid_evt))
                    # F-86：UA 竞态兜底（覆盖与文档起点脚本双双漏过首文档时）
                    if self.ua and sid_evt:
                        asyncio.create_task(self._guard_ua(sid_evt))
                elif msg.get("method") == "Fetch.requestPaused":
                    # Accept-CH：Document 响应解析声明；已声明源的请求回送高熵头
                    asyncio.create_task(self._on_fetch_paused(msg))
        except Exception as e:  # noqa: BLE001
            # 连接关闭/取消：不在此处恢复（交给看门狗统一探活/重连），
            # 但必须留痕——静默断开是「标签页逐步裸奔」类事故的根因线索。
            if not self._closed:
                self._last_error = f"{type(e).__name__}: {e}"
                # 立即标记不健康：看门狗无需等下一次探活，1s 内进入重连
                self._emu_ok = False
                log.warning("移动端仿真 CDP 读取循环结束（连接断开）: %s", e)
        finally:
            # 正常结束（对端干净关闭）与异常结束都要标记通道失效：
            # 看门狗据此在 1s 内进入重连，而不是等下一次探活超时。
            if not self._closed:
                self._emu_ok = False
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
                # 导航要等这一页的仿真挂完（见 _navigate_start_url）：自动挂载
                # 路径下本函数由 read loop 起成独立 task，导航可能先跑到。
                self._first_page_task = asyncio.current_task()
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
                if self._emu_verified is None:
                    await self._verify_emulation(session_id)
            if current is not None and self._attach_tasks.get(target_id) is current:
                self._attach_tasks.pop(target_id, None)

    async def _verify_emulation(self, session_id: str) -> None:
        """注入后主动校验一次仿真读数：连接成功 ≠ 仿真生效（F-01）。

        走代理导致 CDP 握手失败时浏览器照常启动、/browser/start 照常返回
        成功，但一条 CDP 仿真都没挂上（宿主触屏/媒体查询）。这里读一次
        真实读数，面板据此区分「在线」与「在线但未生效」。
        """
        expr = ("({mtp: navigator.maxTouchPoints,"
                " coarse: matchMedia('(pointer: coarse)').matches})")
        try:
            res = await self._send("Runtime.evaluate", {
                "expression": expr, "returnByValue": True,
            }, session_id=session_id, timeout=5.0)
            val = ((res.get("result") or {}).get("value"))
            ok = (isinstance(val, dict) and int(val.get("mtp") or 0) > 0
                  and val.get("coarse") is True)
        except Exception as e:  # noqa: BLE001
            # 读不到（文档处于暂停态 / 命令超时）不等于仿真失效：保持未校验，
            # 留给下一个页面挂载时重试，避免瞬态误报 degraded。
            log.debug("移动端仿真读数校验未完成: %s", e)
            return
        self._emu_verified = ok
        if not ok:
            self._last_error = f"仿真读数校验失败: {val}"
            log.error("移动端仿真读数校验失败（补丁/设备仿真未生效）：%s [%s]",
                      val, self.profile_name or self.profile_id or "?")

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
            # 色域：与页面路径同口径（setEmulatedMedia 按目标生效，OOPIF 是
            # 独立目标，不继承主文档的媒体特性覆盖）。
            if self.ios:
                with contextlib.suppress(CDPError):
                    await self._send("Emulation.setEmulatedMedia", {
                        "media": "screen",
                        "features": [{"name": "color-gamut", "value": "p3"}],
                    }, session_id=session_id)
            # 不启用 Emulation.setEmitTouchEventsForMouse：实测（fp-chromium 148）
            # 该转换会吞掉 mousePressed/Released 的 CDP 应答 → Playwright 点击
            # 永久挂起，且产出序列缺兼容鼠标事件与 click。移动端自动化应改用
            # Input.dispatchTouchEvent 派发（见 docs/移动端大厂检测对抗优化方案.md
            # §事件层）。
            # 安全区不在这里下发：实测主文档的 setSafeAreaInsetsOverride 不会
            # 传播到 OOPIF 会话（跨域子帧读数恒为 0），而真机 iframe 的 env()
            # 同样按自身视口计算（也是 0）——保持一致，不做额外覆盖。
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
        # Intl 成员（v8BreakIterator）在页面表 removeGlobals 里，但 Worker 首
        # 脚本前同样需要补删——否则「页面 false、Worker true」构成跨上下文
        # 矛盾。合并进插桩断点的删除清单（build_worker_drop_patch 已按名尝试
        # `delete Intl[n]`）。
        for _n in ((self.spec.get("removeGlobals") or [])
                   + (self.spec.get("baseGlobals") or [])):
            if _n == "v8BreakIterator" and _n not in drops:
                drops.append(_n)
        injected = False
        try:
            await self._send("Runtime.evaluate", {"expression": patch},
                             session_id=session_id, timeout=3.0)
            injected = True
        except (CDPError, asyncio.TimeoutError):
            pass
        # WebCodecs 静态面（F-133）：Worker 的 VideoDecoder/AudioEncoder 等
        # 是内核在首脚本执行时才装上的，暂停态 `Runtime.evaluate` 读不到
        # （typeof self.VideoDecoder === 'undefined'）——必须走插桩断点那条
        # 「首行执行前」的窗口。因此即便没有要删的全局，也要挂断点。
        late = build_worker_late_patch(self.spec)
        if (drops or late) and injected:
            # 插桩断点：命中「脚本执行前」再补删一次全局差集、补装静态面。
            try:
                await self._send("Debugger.enable", {},
                                 session_id=session_id, timeout=3.0)
                await self._send("Debugger.setInstrumentationBreakpoint",
                                 {"instrumentation": "beforeScriptExecution"},
                                 session_id=session_id, timeout=3.0)
                self._worker_drop_waiters[session_id] = (drops, late)
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

    async def _worker_drop_apply(self, session_id: str, pending: tuple) -> None:
        """插桩断点命中（Worker 首脚本执行前）时补删全局差集、补装静态面，然后放行。"""
        drops, late = pending
        with contextlib.suppress(CDPError, asyncio.TimeoutError):
            await self._send("Runtime.evaluate",
                             {"expression": build_worker_drop_patch(drops)},
                             session_id=session_id, timeout=3.0)
        if late:
            with contextlib.suppress(CDPError, asyncio.TimeoutError):
                await self._send("Runtime.evaluate", {"expression": late},
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

    async def _enroll_crios_post_request(self, sid: str) -> None:
        """iOS Chrome：启用 POST 文档的标记通道（B1-7）。

        Fetch 只拦 Document 响应（Accept-CH 的请求级规则在 iOS 不存在），
        用途是在响应放行前种标记 cookie；Network 域用于 setCookie。
        """
        if not self.spec.get("iosChrome"):
            return
        with contextlib.suppress(CDPError):
            await self._send("Fetch.enable",
                             {"patterns": self._accept_ch_patterns(sid)},
                             session_id=sid)
        with contextlib.suppress(CDPError):
            await self._send("Network.enable", {}, session_id=sid)

    def _is_main_frame_document(self, sid: str, params: Dict[str, Any]) -> bool:
        """Fetch 的 Document 事件是否属于主帧（同进程子帧也会命中规则）。"""
        if params.get("resourceType") != "Document":
            return False
        frame_id = params.get("frameId")
        if not frame_id:
            return True
        main = self._main_frames.get(sid)
        # 主帧 id 未知时（getFrameTree 失败）按主文档处理：宁可多补一次，
        # 也不让 POST 文档漏掉重放面。
        return main is None or frame_id == main

    async def _mark_crios_post_document(self, sid: str,
                                        params: Dict[str, Any]) -> None:
        """响应级暂停里给 POST 主文档种一次性标记（请求已发出，服务端不可见）。"""
        if not self.spec.get("iosChrome"):
            return
        if not self._is_main_frame_document(sid, params):
            return
        request = params.get("request") or {}
        if str(request.get("method") or "").upper() != "POST":
            return
        # 3xx 会把导航改写成 GET、错误响应不产生文档、下载也不产生文档：
        # 这三种都没有重放文档，标了会让下一次 GET 误带重放面。
        status = params.get("responseStatusCode")
        if params.get("responseErrorReason") or not status:
            return
        if 300 <= int(status) < 400:
            return
        for h in params.get("responseHeaders") or []:
            if (h.get("name") or "").lower() == "content-disposition":
                if "attachment" in (h.get("value") or "").lower():
                    return
        url = request.get("url") or ""
        if not url:
            return
        self._crios_post_seq += 1
        with contextlib.suppress(CDPError):
            await self._send("Network.setCookie", {
                "name": f"__fpwb_pn_{self._crios_post_seq}",
                "value": "1",
                "url": url,
                # path=/：目标文档可能在任何路径上，标记必须对它可见。
                "path": "/",
                "secure": False,
                "httpOnly": False,
                # 文档起点脚本会立即删除；30s 只兜底「文档没建成」的情况。
                "expires": time.time() + 30,
            }, session_id=sid)

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
            # B1-7：POST 主文档在放行响应前种标记，文档起点脚本据此装配重放面。
            await self._mark_crios_post_document(sid, params)
            # Accept-CH 只属于 Android 口径：iOS 的 Fetch 拦截仅为 B1-7 而开，
            # 不能让响应解析顺带打开高熵头回送（真机 CriOS 没有 Sec-CH-UA*）。
            hints: set = set()
            if not self.spec.get("ios") and self.spec.get("uaCh"):
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
        # 主帧 id：Fetch 的 Document 请求同时包含同进程子帧，B1-7 的导航态
        # 判定需要区分主文档。
        if self.spec.get("iosChrome"):
            with contextlib.suppress(CDPError):
                tree = await self._send("Page.getFrameTree", {}, session_id=sid)
                fid = ((((tree or {}).get("frameTree") or {}).get("frame") or {})
                       .get("id"))
                if fid:
                    self._main_frames[sid] = fid
            await self._enroll_crios_post_request(sid)
            # 0.4 iOS Chrome：POST 标记脚本。必须排在主补丁**之前**——它在
            #     文档起点读一次性 cookie、删掉标记并把结论交给主补丁，通道
            #     与命名空间由主补丁按常规具名 getter 机制装配。详见
            #     _CRIOS_POST_REQUEST_JS 注释。
            with contextlib.suppress(CDPError):
                await self._send("Page.addScriptToEvaluateOnNewDocument",
                                 {"source": _CRIOS_POST_REQUEST_JS},
                                 session_id=sid)

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

        # 2.5 色域：iPhone 7 及以后全系是 Display P3 屏，
        #     (color-gamut: p3) 恒为 true；宿主桌面显示器多为 sRGB，内核默认
        #     报 false——「声明 iPhone 但只有 sRGB」是硬件级互斥判据。CDP 的
        #     setEmulatedMedia 可原生注入该特性，无需 JS 改 matchMedia。
        #     实测（fp-chromium 148）：立即生效、同一页面内导航后保持、同进程
        #     iframe 继承，但**不跨页面**（新 page 目标回到 sRGB）——故页面路径
        #     与 OOPIF 路径各自下发，且每次页面级仿真都重发。
        if self.ios:
            with contextlib.suppress(CDPError):
                await self._send("Emulation.setEmulatedMedia", {
                    "media": "screen",
                    "features": [{"name": "color-gamut", "value": "p3"}],
                }, session_id=sid)

        # 2.6 安全区（P2-5 后半）：真机 iOS 在 viewport-fit=cover 页面把
        #     刘海/灵动岛/Home Indicator 换算成 env(safe-area-inset-*) 计算值。
        #     CDP 覆盖不受 viewport-fit 约束、且跨导航保持（实测 fp-chromium
        #     148），因此挂载与每次主文档提交都先清零，由 _sync_safe_area
        #     按该文档 <meta viewport> 静态判定（WebKit 只在解析 meta 时
        #     决定是否避让，页面载入后改写 meta 不会重新生效）。
        if self.ios and self.safe_area:
            self._safe_sessions.add(sid)
            await self._clear_safe_area(sid)

        # 3. UA 覆盖（内核层只会按桌面平台拼 UA，必须显式覆盖为移动端 UA）
        if self.ua:
            await self._reapply_ua(sid)

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

        # 5b. 语言：Intl 默认 locale。内核 --lang/acceptLanguage 只改
        #     navigator.language，改不动 ICU 默认 locale——宿主中文机器上
        #     Intl.DateTimeFormat().resolvedOptions().locale 恒为 zh-CN，
        #     与欧美出口时区并存就是「属地与语言不符」的跨域特征。
        #     setLocaleOverride 是唯一能改 Intl 的开关（实测）。
        if self.locale:
            with contextlib.suppress(CDPError):
                await self._send("Emulation.setLocaleOverride",
                                 {"locale": self.locale}, session_id=sid)

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

    # ---------------- 安全区（iOS）----------------

    async def _clear_safe_area(self, sid: str) -> None:
        """清零安全区覆盖（非 cover 页面 / 新文档起点 / 非 iOS 页面）。"""
        if sid not in self._safe_sessions:
            return
        with contextlib.suppress(CDPError, asyncio.TimeoutError):
            # 空 insets：未给出的边按 0 计算（实测 fp-chromium 148 下
            # env() 均返回 0px，与真机非 cover 页面的计算值一致）
            await self._send("Emulation.setSafeAreaInsetsOverride",
                             {"insets": {}}, session_id=sid, timeout=5.0)

    async def _sync_safe_area(self, sid: str) -> None:
        """按当前主文档的 <meta viewport> 决定安全区数值（幂等）。

        WebKit 的真机语义：viewport-fit=cover 时页面铺到系统 UI 之下，
        env() 才为非 0；其余一律 0。内核不区分 viewport-fit，只能在此判定。
        """
        if sid not in self._safe_sessions or not self.safe_area:
            return
        try:
            res = await self._send("Runtime.evaluate", {
                "expression": _SAFE_AREA_META_JS, "returnByValue": True,
            }, session_id=sid, timeout=5.0)
        except (CDPError, asyncio.TimeoutError):
            return
        content = str(((res or {}).get("result") or {}).get("value") or "")
        # 关键字大小写不敏感、允许 "viewport-fit = cover"（空白归一化后判定）
        cover = "viewport-fit=cover" in "".join(content.lower().split())
        with contextlib.suppress(CDPError, asyncio.TimeoutError):
            await self._send("Emulation.setSafeAreaInsetsOverride", {
                # max-inset 与 inset 同值：真机 Safari 竖屏下浏览器 UI 可收缩
                # 的余量未观测到差异，取保守口径（见方案文档 §2.16.1）
                "insets": {**self.safe_area,
                           **{k + "Max": v for k, v in self.safe_area.items()}}
                if cover else {},
            }, session_id=sid, timeout=5.0)

    async def _safe_area_after_nav(self, sid: str) -> None:
        """主文档提交后：清零并按新文档重新判定。

        解析早段就取一次读数——解析器在取外部资源时会交还主线程，此时
        <head> 里的 meta 往往已解析，能让文档尾部的脚本读到真机口径；
        DOMContentLoaded 再兜底一次（见 _read_loop）。
        """
        await self._clear_safe_area(sid)
        for delay in (0.0, 0.03, 0.12):
            if delay:
                await asyncio.sleep(delay)
            await self._sync_safe_area(sid)

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
        五类读数并发下发：60Hz 下每 tick 只有 ~16ms，串行等待五次往返
        会把整个时间预算耗在 RTT 上（本地 ~0.3ms/条，尚可，但会累积漂移）。
        """
        sends = []
        for sensor_type, values in readings.items():
            if sensor_type.endswith("orientation"):
                reading = {"quaternion": values}
            else:
                reading = {"xyz": values}
            sends.append(self._send("Emulation.setSensorOverrideReadings",
                                    {"type": sensor_type, "reading": reading},
                                    session_id=sid, timeout=5.0))
        await asyncio.gather(*sends)

    async def _sensor_loop(self, simulator: _SensorSimulator) -> None:
        """按真机节奏持续更新读数（事件流随读数变化保持「活」）。"""
        failures = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        try:
            while not self._closed:
                # 更新节奏带抖动：严格固定周期会让「事件内容变化」呈精确的
                # 周期性（如连续 3 样本相同再跳变），真机传感器采样周期不固定。
                # 按绝对时刻排程而不是「发完再 sleep」：60Hz 下每 tick 只有
                # 16.7ms，sleep 的固定开销（实测 macOS 上每轮多睡 1–2ms）
                # 会把更新率拖到 ~54Hz——读数变化频率本身就是检测面。
                deadline += _SENSOR_TICK_SEC * (1.0 + _SENSOR_TICK_JITTER
                                                * (random.random() * 2 - 1))
                delay = deadline - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    # 落后超过一个周期（被挂起/阻塞）：重新对齐，避免追帧风暴
                    deadline = loop.time()
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

    async def _reapply_ua(self, sid: str) -> None:
        """下发 UA 覆盖（首次挂载与 F-86 兜底重发共用同一份参数）。"""
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

    async def _guard_ua(self, sid: str) -> None:
        """F-86：主文档提交后校验 UA，漏成内核默认形态时补发覆盖并 reload。

        竞态面：新标签页首文档偶发在 `Emulation.setUserAgentOverride` 与
        文档起点补丁双双落达之前就已提交——此时页面 UA 是内核
        `--fingerprint-platform=ios` 的默认形态。iPhone 机型该形态与真机
        同形，只有 iPad 机型会漂成 `(iPhone; CPU iPhone OS …)`（UA 与
        platform 自相矛盾，单行可判），故长期未被发现。

        处置分两层：重发 setUserAgentOverride（修复此后所有请求头与子帧），
        再对当前文档立即执行一次补丁（修复已提交文档的 JS 可读值）。不
        reload：首屏文档是用户要看的页面，为一次读数不符整页重载代价过高，
        且 reload 本身又会再触发一次本检查。计数留给验收侧断言。
        """
        if not self.ua or sid in self._ua_guard_busy:
            return
        try:
            res = await self._send("Runtime.evaluate", {
                "expression": "navigator.userAgent", "returnByValue": True,
            }, session_id=sid, timeout=5.0)
        except (CDPError, asyncio.TimeoutError):
            return
        got = ((res or {}).get("result") or {}).get("value")
        if not isinstance(got, str) or got == self.ua:
            return
        self._ua_guard_busy.add(sid)
        try:
            self._ua_guard[sid] = self._ua_guard.get(sid, 0) + 1
            self._ua_guard_total += 1
            log.warning("移动端仿真 UA 竞态（第 %d 次）：页面 %r ≠ 指纹 %r，"
                        "已重发覆盖并补丁当前文档",
                        self._ua_guard[sid], got[:48], self.ua[:48])
            self._last_error = "UA 竞态已兜底"
            await self._reapply_ua(sid)
            await self._apply_patch_now(sid)
        finally:
            self._ua_guard_busy.discard(sid)

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
        # F-86：等首屏仿真挂完再导航。自动挂载路径下 _attach_page 由 read loop
        # 起成独立 task，而首屏导航在 _connect 末尾同步发出——不等待就会出现
        # 「首个真实文档先于 setUserAgentOverride 提交」的竞态，正是 UA 漏成
        # 内核默认形态的成因之一。
        task = self._first_page_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait({task}, timeout=15.0)
        try:
            await self._send("Page.navigate", {"url": self.start_url}, session_id=sid)
        except CDPError as e:
            log.debug("移动端 start_url 导航失败: %s", e)
