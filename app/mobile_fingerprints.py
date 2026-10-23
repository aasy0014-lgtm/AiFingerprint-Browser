"""移动端真实机型指纹库（Android/iOS）

数据文件: app/data/mobile_devices.json
- 机型档案: 屏幕(物理/CSS/视口)、DPR、GPU(真实 WebGL 上报字符串)、CPU/内存、
  可运行系统版本及在网权重、机型标识（Android UA 可见）
- 版本库: Chrome 主版本 → 完整构建号（Android UA/UA-CH 使用）

生成链路：按权重抽取机型 → 按机型可运行版本配对系统版本 → 配对浏览器版本，
保证 UA / UA-CH / 屏幕 / DPR / GPU / 硬件并发 全部相互印证。

模式:
- preset:  真实机型预设（系统与浏览器版本取该机型的当前主流组合，不随机化）
- generate: 基于真实机型库的合成（系统/浏览器版本按在网权重随机）

运行期（fp-chromium 内核 + CDP 注入）：
- 屏幕/D如下 PR/触控由 CDP 设备仿真落地
- GPU 字符串、Android UA-CH、deviceMemory、Battery、iOS 特性差异由 JS 注入层落地
  （见 app/mobile_cdp_injector.py，注入值全部取自本模块生成的 fingerprint 数据）

目标 OS: android, ios
"""
import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional

_LIBRARY_PATH = Path(__file__).resolve().parent / "data" / "mobile_devices.json"

_SEED_MAX = 4_294_967_294


# ------------------------------------------------------------------ 数据加载
def _load_library() -> Dict[str, Any]:
    try:
        with open(_LIBRARY_PATH, "r", encoding="utf-8") as fh:
            lib = json.load(fh)
    except FileNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(f"移动端机型库缺失: {_LIBRARY_PATH}") from exc
    devices = lib.get("devices") or []
    if not devices:
        raise RuntimeError(f"移动端机型库为空: {_LIBRARY_PATH}")
    for dev in devices:
        missing = [k for k in ("brand", "platform", "type", "model", "screen", "gpu", "weight") if k not in dev]
        if missing:
            raise RuntimeError(f"机型记录缺字段 {missing}: {dev.get('market_name', dev)}")
        screen = dev["screen"]
        for k in ("w", "h", "dpr", "css_w", "css_h", "viewport_h"):
            if k not in screen:
                raise RuntimeError(f"机型 {dev['market_name']} 屏幕字段缺失: {k}")
    return lib


_LIBRARY: Dict[str, Any] = _load_library()

_DEVICES: List[Dict[str, Any]] = list(_LIBRARY["devices"])
_ANDROID_BROWSERS: List[Dict[str, Any]] = list(_LIBRARY.get("android_browsers") or [])


# ------------------------------------------------------------------ 工具
def _weighted_choice(weights: Dict[str, Any]) -> str:
    """按 {值: 权重} 抽取（保持 JSON 中的插入顺序作为同权重兜底顺序）"""
    keys = list(weights.keys())
    w = [max(0.0001, float(weights[k])) for k in keys]
    return random.choices(keys, weights=w, k=1)[0]


def _version_key(v: str) -> tuple:
    return tuple(int(x) if x.isdigit() else 0 for x in str(v).split("."))


def _memory_class(ram_gb: int) -> int:
    """Chrome navigator.deviceMemory 上报桶: 取不超过物理内存的 2 的幂，上限 8。"""
    for cap in (8, 4, 2, 1):
        if ram_gb >= cap:
            return cap
    return 1


def _pick_device(target_os: str, manufacturer: Optional[str]) -> Dict[str, Any]:
    """按平台 + 可选品牌过滤后按权重抽取机型；品牌无匹配时回退到平台全池。"""
    pool = [d for d in _DEVICES if d["platform"] == target_os]
    if not pool:
        raise ValueError(f"机型库中没有平台 {target_os} 的设备")
    if manufacturer:
        matched = [d for d in pool if str(d["brand"]).lower() == manufacturer.lower()]
        if matched:
            pool = matched
    return random.choices(pool, weights=[max(0.0001, float(d["weight"])) for d in pool], k=1)[0]


def _pick_os_version(device: Dict[str, Any], mode: str) -> str:
    versions = device.get("os_versions") or device.get("ios_versions") or {}
    if not versions:
        raise RuntimeError(f"机型 {device['market_name']} 缺少系统版本池")
    if mode == "preset":
        # 当前主流：权重最高；同权重取版本号更高者（更新更常见）
        return max(versions.items(), key=lambda kv: (float(kv[1]), _version_key(kv[0])))[0]
    return _weighted_choice(versions)


def _kernel_browser() -> Optional[Dict[str, Any]]:
    """内核实测的 Chromium 版本（首选）。

    UA / UA-CH 声称的 Chrome 版本必须与内核引擎一致：检测站会做
    feature 探测（引擎实际版本）与 UA 版本比对，声称 156 而引擎是 148
    会直接命中「浏览器版本不同」。内核版本随安装包变化，这里动态读取，
    未安装内核时回退到库内版本池（仅用于无内核环境生成指纹）。
    """
    try:
        from .kernels import fp_chromium_kernel
        full = fp_chromium_kernel.chrome_version()
    except Exception:
        full = None
    if not full:
        return None
    return {"major": full.split(".")[0], "full": full, "weight": 1.0,
            "source": "kernel"}


def _pick_android_browser(mode: str) -> Dict[str, Any]:
    kernel = _kernel_browser()
    if kernel:
        return kernel
    if not _ANDROID_BROWSERS:
        return {"major": "156", "full": "156.0.8078.35", "weight": 1}
    if mode == "preset":
        return max(_ANDROID_BROWSERS, key=lambda b: (float(b["weight"]), _version_key(b["full"])))
    return random.choices(
        _ANDROID_BROWSERS,
        weights=[max(0.0001, float(b["weight"])) for b in _ANDROID_BROWSERS],
        k=1,
    )[0]


def _pick_ios_browser() -> Dict[str, Any]:
    """Chrome iOS（CriOS）版本：与内核实测引擎一致（与 Android 侧同法）。

    Chrome iOS 的大版本与桌面 Chrome 同步演进（120 ↔ 120），因此直接复用
    「内核版本优先」策略，避免 UA 声称版本与引擎特性错位被检测站命中。
    未安装内核时回退到库内 Chrome 版本池（仅用于无内核环境生成指纹）。
    """
    kernel = _kernel_browser()
    if kernel:
        return kernel
    if _ANDROID_BROWSERS:
        fb = max(_ANDROID_BROWSERS, key=lambda b: (float(b["weight"]), _version_key(b["full"])))
        return {"major": fb["major"], "full": fb["full"], "weight": 1.0, "source": "library"}
    return {"major": "156", "full": "156.0.8078.35", "weight": 1.0, "source": "fallback"}


def _normalize_browser(target_os: str, browser: Optional[str]) -> str:
    """归一化浏览器口径：Android 恒为 chrome；iOS 为 safari / chrome_ios。"""
    value = (browser or "").strip().lower()
    if target_os == "android":
        if value in ("", "chrome"):
            return "chrome"
        raise ValueError(f"Android 端浏览器口径仅支持 chrome（收到 {browser}）")
    if value in ("", "safari"):
        return "safari"
    if value in ("chrome", "chrome_ios"):
        return "chrome_ios"
    raise ValueError(f"iOS 端浏览器口径仅支持 safari / chrome_ios（收到 {browser}）")


def _android_ua_ch(device: Dict[str, Any], os_version: str, browser: Dict[str, Any]) -> Dict[str, Any]:
    """Chrome Android 的 navigator.userAgentData（含高熵值）。"""
    major = str(browser["major"])
    full = str(browser["full"])
    grease = _LIBRARY.get("grease_brand") or {"brand": "Not/A)Brand", "version": "99"}
    grease_brand = {"brand": grease["brand"], "version": str(grease["version"])}
    grease_full = {"brand": grease["brand"], "version": f"{grease['version']}.0.0.0"}
    return {
        "brands": [
            {"brand": "Chromium", "version": major},
            {"brand": "Google Chrome", "version": major},
            dict(grease_brand),
        ],
        "fullVersionList": [
            {"brand": "Chromium", "version": full},
            {"brand": "Google Chrome", "version": full},
            dict(grease_full),
        ],
        "fullVersion": full,
        "platform": "Android",
        "platformVersion": f"{os_version}.0.0",
        # Chromium 源码行为：Android 手机端 architecture / bitness 恒为空字符串
        # （components/embedder_support/user_agent_utils.cc：GetCpuArchitecture /
        # GetCpuBitness 在 IS_ANDROID 且非桌面/XR 设备上返回 ""）。
        # 填 "arm" 会与真机组合不符，服务端 OS 一致性检查可据此判定为桌面系统。
        "architecture": "",
        "bitness": "",
        "model": str(device["model"]),
        "mobile": device["type"] == "phone",
        # F-97：Chrome 124+ 的 Sec-CH-UA-Form-Factors 高熵提示。
        "formFactors": ["Tablet"] if device["type"] == "tablet" else ["Mobile"],
        "wow64": False,
    }


# ------------------------------------------------------------------ 字体
_MOBILE_FONTS = {
    "ios": {
        "essential": [
            'Helvetica Neue', 'Arial', 'Times New Roman', 'Courier New',
            'Georgia', 'Verdana', 'Tahoma', 'Palatino', 'Garamond',
            'Bookman', 'Comic Sans MS', 'Trebuchet MS', 'Lucida Console',
            'Menlo', 'Monaco', 'Lucida Grande', 'PingFang SC', 'PingFang HK',
        ],
        "extra": [
            'SF Pro Text', 'SF Pro Icons', 'Helvetica', 'Chalkboard SE',
            'Didot', 'ITC Avant Garde Gothic', 'Century Gothic', 'Sand',
            'System Font', 'Times', 'Arial MT', 'Georgia MT',
        ],
    },
    "android": {
        "essential": [
            'Droid Sans', 'Droid Serif', 'Roboto', 'Open Sans', 'Montserrat',
            'Noto Sans', 'Noto Sans SC', 'Source Sans Pro', 'Lato',
        ],
        "extra": [
            'Merriweather', 'Oxygen', 'PT Sans', 'PT Serif', 'Fira Sans',
            'Raleway', 'Ubuntu', 'Karumbi', 'Poppins', 'Josefin Sans',
        ],
    },
}


def _generate_mobile_font_subset(target_os: str, *, full: bool = False) -> List[str]:
    """移动端字体子集：preset 用完整集合（固定），generate 随机子集。"""
    table = _MOBILE_FONTS.get(target_os, _MOBILE_FONTS["android"])
    essential, extra = table["essential"], table["extra"]
    if full or not extra:
        return list(essential) + list(extra)
    picked = random.sample(extra, min(len(extra), random.randint(5, len(extra))))
    return list(essential) + picked


# ------------------------------------------------------------------ 辅助数据
def _generate_battery_api() -> Dict[str, Any]:
    """Android Chrome navigator.getBattery() 的初始值（iOS 无该 API，返回 None）。"""
    level = round(random.uniform(0.2, 1.0), 2)
    charging = random.random() < 0.15
    return {
        "level": level,
        "charging": charging,
        "chargingTime": 0 if not charging else random.randint(1800, 7200),
        "dischargingTime": random.randint(14400, 43200),
    }


_MOBILE_GEOLOCATION = {
    "geolocation": {
        "accessibility": True,
        "mocked": False,
    },
}


def _generate_camera_api() -> Dict[str, Any]:
    return {
        "hasCamera": True,
        "rearCamera": True,
        "frontCamera": True,
        "videoCapture": True,
        "imageCapture": True,
    }


_MOBILE_WEBRTC = {
    "webrtc": {
        "supported": True,
        "privacyMode": False,
    },
}

# iOS Safari 不存在、运行期需要从内核（桌面 Chromium）上抹掉的 Navigator 属性。
# 首项是早期的 Safari/桌面差异（connection 等）；其余为 WebKit 从未实现的
# 桌面 Chromium API（MDN BCD，2026-10 核实）：Web Serial / WebUSB / WebHID /
# Web Bluetooth / Keyboard Lock / Web MIDI / Presentation /
# getInstalledRelatedApps / navigator.vibrate。桌面内核全部携带，不抹除时
# `'serial' in navigator` 这类一行探测即可判定「Chromium 装 iOS」。
# pdfViewerEnabled 不在此列：BCD safari_ios=16.4，真机恒 true（与插件集合
# 为空不矛盾），运行期显式置 true。
_IOS_REMOVE_NAV_PROPS = [
    "connection", "userAgentData", "getBattery", "deviceMemory",
    "serial", "usb", "hid", "bluetooth", "keyboard", "requestMIDIAccess",
    "presentation", "getInstalledRelatedApps", "vibrate",
    # F-04：WebKit 27.2 实测已无、桌面 Chromium 148 仍带的成员。前两项
    # 在 BCD 里记的是「移除」（doNotTrack 7、getUserMedia 12），后三项
    # 是 Chromium 专有面（Scheduling API / Storage Buckets / PWA 标题栏）。
    "doNotTrack", "getUserMedia", "webkitGetUserMedia",
    "scheduling", "storageBuckets", "windowControlsOverlay",
    # F-69：NavigatorLogin / NavigatorManagedData 对应的 nav 属性面（与 window
    # 类名是两套暴露面，只删类名仍可 `'login' in navigator` 判假）
    "login", "managed",
    # F-70：WebKit 的协议处理器实现早已移除（Bugzilla 仅 2017 GTK 回归记录），
    # 真机 Safari 无。
    "registerProtocolHandler", "unregisterProtocolHandler",
    # F-90：徽标 API 由 AppBadgeEnabled 门控，该 pref 的 defaultValue 为 false
    # （UnifiedWebPreferences.yaml，无 iOS 覆盖），WebKit 27.2 实测
    # navigator.setAppBadge/clearAppBadge 均为 undefined。BCD 记的 16.4 是
    # Home Screen Web App 口径，普通浏览上下文不暴露。
    "setAppBadge", "clearAppBadge",
]

# Android Chrome 不存在、需从桌面内核上抹掉的 Navigator 属性（WebHID 仅桌面，
# 见 BCD chrome_android=false；WebUSB/Web Serial 在 Chrome Android 148 已支持，
# 不可误删——版本相关差异必须逐项核对，不能按「桌面 API」一概而论）。
_ANDROID_REMOVE_NAV_PROPS = [
    "hid",
    # F-96：Window Controls Overlay 是桌面 PWA 专有（BCD chrome_android=false）；
    # F-74 只删了构造函数/全局，Navigator 上的 getter 仍在。
    "windowControlsOverlay",
    # F-78：crbug 40964464 —— Chrome Android 无协议处理器入口；
    # setAppBadge 仅 set 缺席（clearAppBadge BCD=81 保留，勿成对删）。
    "registerProtocolHandler", "unregisterProtocolHandler", "setAppBadge",
]


# --------------------------------------------------------------------------
# 媒体可播放能力表（HTMLMediaElement.prototype.canPlayType）
# --------------------------------------------------------------------------
# 内核是桌面 Chromium（自建 FFmpeg 管线），canPlayType 对 ogg/webm/opus 一律
# "probably"；真机 iOS Safari 走系统 AVFoundation，容器白名单里**没有** ogg/webm，
# 单行 `canPlayType('audio/ogg; codecs="vorbis"')` 即可判「不是 iOS」。
#
# 机制取证（WebKit main，2026-10）：
#   Source/WebCore/platform/graphics/avfoundation/objc/AVAssetMIMETypeCache.mm
#   的 isStaticContainerType 白名单仅 30 项（3gpp/aac/aiff/basic/mp3/mp4/mpeg/
#   wav/x-m4a/x-wav/mpegurl/mp2t… + video/3gpp/3gpp2/mp4/mpeg/quicktime/x-m4v），
#   无平台条件分支——ogg/webm 不在其中，故 iOS 上落到 AVFoundation 扩展查询并
#   被拒；macOS AVFoundation 自带 WebM/ogg 解码器，同一份 WebKit 会返回
#   "probably"（实测 Playwright WebKit 26.5：ogg/webm 全 "probably"）——
#   macOS WebKit **不能**当作 iOS 基线，本表按 iOS 真机口径写死。
#   hvc1 在 iOS 走 VideoToolbox 硬件解码（A10 起）→ "probably"；hev1 不被接受
#   → ""。内核 ffmpeg 的 hvc1 支持与容器串形态相关（'hvc1' → ""、
#   'hvc1.1.6.L93.B0' → "probably"），一并收敛为 iOS 口径。
#
# 本表是**覆盖层**，不是兜底：未命中的串由注入层的 __cptIos 按 WebKit
# MIMETypeCache::canDecodeType 算法判定（30 项 AVFoundation 静态容器白名单 +
# iOS 动态项 + codecs 谓词，见 mobile_cdp_injector._CPT_IOS_SRC）。F-143
# 之前这里是「表外透传宿主原生」，会让 `audio/mp4; codecs="opus"` 继承
# Blink 的 "probably"（真机 iOS 为 ''）。
# 判定顺序：先剥离 `; codecs=` 后按整串匹配，再按 codecs 逐个匹配（大小写不敏感）。
#
# Android Chrome 侧为**显式豁免**：内核 ffmpeg 对 hvc1 的返回值形态不一致
# （裸 'hvc1' 为 ""），真机 Android Chrome 有 HEVC 硬件解码（芯片级），
# 故 hvc1 家族单列校准为 "probably"；ogg/webm/opus 内核已与真机同口径，不动。
_CODEC_CAPABILITY_IOS: Dict[str, str] = {
    # --- AVFoundation 容器白名单外：拒绝 ---
    "audio/ogg": "",
    'audio/ogg; codecs="vorbis"': "",
    'audio/ogg; codecs="opus"': "",
    "audio/webm": "",
    'audio/webm; codecs="opus"': "",
    'audio/webm; codecs="vorbis"': "",
    "video/webm": "",
    'video/webm; codecs="vp8"': "",
    'video/webm; codecs="vp8, vorbis"': "",
    'video/webm; codecs="vp9"': "",
    'video/webm; codecs="vp09.00.10.08"': "",
    "video/ogg": "",
    'video/ogg; codecs="theora"': "",
    # --- Apple VideoToolbox：HEVC 硬件解码 ---
    'video/mp4; codecs="hvc1"': "probably",
    'video/mp4; codecs="hvc1.1.6.L93.B0"': "probably",
    'video/mp4; codecs="hev1"': "",
    # --- F-19：容器白名单内、未带 codecs 的查询 ---
    # WebKit MIMETypeCache::canDecodeType（Source/WebCore/platform/graphics/
    # MIMETypeCache.cpp:74-100）：容器命中而 codecs 为空 → MayBeSupported
    # （"maybe"）；带 codecs 时只有 canDecodeExtendedType 成功才 IsSupported
    # （"probably"）。桌面 Chromium 的 FFmpeg 管线对 audio/mpeg|mp3|aac|flac
    # 直接给 "probably"，一行 canPlayType 即可判假。
    "audio/mpeg": "maybe",
    "audio/mp3": "maybe",
    "audio/aac": "maybe",
    "audio/flac": "maybe",
    "video/quicktime": "maybe",
    # 带 codecs 的常见查询走 canDecodeExtendedType：'mp3' 命中 WebKit 的
    # shouldOverrideExtendedType 特例（MIMETypeCache.cpp:163-175，直接
    # IsSupported）；flac 只在 MP4 容器内被改写成 MP4RA 的 'fLaC' 后再问
    # AVFoundation（AVAssetMIMETypeCache.mm:108-112）→ audio/flac 容器带
    # codecs="flac" 反而不可播（WebKit 实测 ""），audio/mp4 带 flac 可播。
    'audio/mpeg; codecs="mp3"': "probably",
    'audio/flac; codecs="flac"': "",
    'audio/mp4; codecs="flac"': "probably",
    # Dolby Vision：A12（iPhone XS/XR）及以后全系可解（dvh1 → HEVC 主层），
    # 指纹库内机型均晚于该代 → "probably"。未列入的 av01 由 _codec_capability
    # 按机型能力位补充（A17 Pro 起才可解）。
    'video/mp4; codecs="dvh1.05.06"': "probably",
    # AV1：A17 Pro 之前无硬解，Safari 报 ""（而非 Chromium 的 "probably"）。
    'video/mp4; codecs="av01.0.05M.08"': "",
}
_CODEC_CAPABILITY_ANDROID: Dict[str, str] = {
    # --- 真机 Android Chrome：HEVC 硬件解码（内核裸 'hvc1' 形态不一致）---
    'video/mp4; codecs="hvc1"': "probably",
    'video/mp4; codecs="hvc1.1.6.L93.B0"': "probably",
}


# AV1 硬解机型（A17 Pro / A18 / A19 / M3+ 家族）。按机型名匹配指纹库里的
# market_name：iPhone 15 Pro(Pro Max)、iPhone 16 全系（含 16e）、iPhone 17
# 全系、iPhone Air（A19 Pro）、iPad mini (A17 Pro)、iPad Air (M3)、
# iPad Pro (M4/M5)。真机门控是 VideoToolbox 的 VTIsHardwareDecodeSupported
# ('av01')（AV1UtilitiesCocoa.mm:198-205），与 CPU 代次一一对应。
_IOS_AV1_MARKET_TOKENS = (
    "iPhone 15 Pro", "iPhone 16", "iPhone 17", "iPhone Air", "A17 Pro",
    "(M3)", "(M4)", "(M5)",
)


def _ios_av1_capable(model: str, market_name: str = "") -> bool:
    name = f"{model} {market_name}"
    return any(tok in name for tok in _IOS_AV1_MARKET_TOKENS)


def _codec_capability(target_os: str, *, av1: bool = False) -> Dict[str, str]:
    """目标平台相对宿主内核需要校准的 canPlayType 返回值（键为小写 MIME）。"""
    table = dict(_CODEC_CAPABILITY_IOS if target_os == "ios" else _CODEC_CAPABILITY_ANDROID)
    if target_os == "ios" and av1:
        table['video/mp4; codecs="av01.0.05M.08"'] = "probably"
    return {k.lower(): v for k, v in table.items()}


# --------------------------------------------------------------------------
# NetworkInformation.downlinkMax（仅 Android，单位 Mbps）
# --------------------------------------------------------------------------
# 与 type / ontypechange 同属 NetInfoDownlinkMax 运行期开关，BCD
# api.NetworkInformation.downlinkMax：chrome_android=38，桌面 chrome 仅
# ChromeOS（partial）→ 桌面内核上 'downlinkMax' in navigator.connection 为
# false，是与 type 同源的「桌面装 Android」判据。
#
# 取值不是编造的常量，而是内核自己的连接子类型表（net/base/
# network_change_notifier.cc: GetMaxBandwidthMbpsForConnectionSubtype）：
# 真机 Android 的 subtype 来自 ConnectivityManager（WiFi/Ethernet 走
# NetworkInfo.getSubtype，蜂窝走 cellularSubtypeFromKbps(下行带宽)），
# 再按该表折算。type 已声明 wifi，故取该表的 Wi-Fi 条目值域——
# 11 / 54 / 600 / 1300 / 7000（b/g/n/ac/ad），真机取值落在这个集合内。
# 取 1300（Wi-Fi 5 / 802.11ac）：2023 年后的主流机型与路由器均为 ac，
# 且与「5G 手机」的代数口径一致；ax 机型该值仍是常见读数（内核表未区分 ax）。
_ANDROID_DOWNLINK_MAX_MBPS = 1300.0


# --------------------------------------------------------------------------
# WebGL 参数上限与扩展集（按 GPU 家族，非按机型）
# --------------------------------------------------------------------------
# 宿主是 Apple M 系列（Metal/ANGLE）：MAX_TEXTURE_SIZE=16384、
# MAX_VIEWPORT_DIMS=[16384,16384]、MAX_SAMPLES=4，扩展集带桌面专属
# EXT_texture_compression_bptc / rgtc / WEBGL_polygon_mode /
# EXT_clip_control / WEBGL_blend_func_extended 等——「渲染器字符串是
# Adreno 710、参数上限却是 M 系列」的三元组矛盾，gpuinfo.org 数据集比对
# 可直接命中（方案 P2-1）。
#
# 取值依据：移动 GPU 的 OpenGL ES 上限受硬件约束，且各家族长期稳定——
#   * Adreno（Qualcomm，A6xx/A7xx/A8xx 全系）：MAX_TEXTURE_SIZE 16384、
#     MAX_VIEWPORT_DIMS 16384×16384、MAX_RENDERBUFFER_SIZE 16384、
#     MAX_CUBE_MAP_TEXTURE_SIZE 16384、MAX_SAMPLES 4、MAX_DRAW_BUFFERS 8
#   * Mali（ARM，Valhall/5th Gen）：MAX_TEXTURE_SIZE 16384（G715/G615 段），
#     MAX_SAMPLES 4
#   * Xclipse（Samsung/RDNA）、PowerVR（ImgTec）：同为 16384 档
# 这些上限与桌面 Apple M 系列**数值相同**，所以单看 MAX_TEXTURE_SIZE 并不
# 穿帮；真正穿帮的是**扩展集**——S3TC/BPTC/RGTC/polygon_mode 是桌面
# OpenGL/D3D 纹理与光栅化扩展，移动 GPU 驱动不导出（iOS Metal 更是绝不
# 支持 S3TC，检测站对 iOS 目标直接按 15 分计）。故本表的核心是扩展集
# 白名单，MAX_* 只覆盖确有家族差异的项。
#
# 扩展集来源：内核在移动 GPU 上会导出的 WebGL1 扩展（Chrome 148 实测集
# 合中剔除桌面专属项）。缺失项由注入层从 getSupportedExtensions() 过滤，
# getExtension() 对过滤项返回 null（与「驱动不导出」同语义）。
_GL_EXT_DESKTOP_ONLY = (
    # 桌面 D3D/OpenGL 纹理压缩（DXT/BC 家族）——移动 GPU 不导出
    "WEBGL_compressed_texture_s3tc",
    "WEBGL_compressed_texture_s3tc_srgb",
    "EXT_texture_compression_bptc",
    "EXT_texture_compression_rgtc",
    # 桌面光栅化/着色器扩展
    "WEBGL_polygon_mode",
    "EXT_clip_control",
    "EXT_depth_clamp",
    "EXT_polygon_offset_clamp",
    "WEBGL_blend_func_extended",
    "NV_shader_noperspective_interpolation",
    "EXT_texture_mirror_clamp_to_edge",
    "EXT_render_snorm",
    "EXT_texture_norm16",
)
# iOS（Metal/ANGLE-Metal）额外：Spectre 侧信道计时扩展在 Apple 物理封禁。
# F-23：此前这里把整套桌面扩展（S3TC/BPTC/RGTC/clip_control/polygon_mode…）
# 也一并剔除，但 iOS Safari 自 15 起同样走 ANGLE-Metal，扩展集与 macOS
# WebKit 一致——真机**有**这 16 项，剔除反而制造「声明 iOS 却缺 iOS 扩展」的
# 反向判据。iOS 只保留计时扩展的剔除。
_GL_EXT_DESKTOP_ONLY_IOS = (
    "EXT_disjoint_timer_query",
    "EXT_disjoint_timer_query_webgl2",
)
# iOS 真机有、桌面 Chromium 不导出的扩展（F-23）：PVRTC 是 PowerVR 家族的
# 压缩纹理扩展，WebKit 在 Apple 平台仍作为 WEBKIT_ 前缀面导出（iOS GPU 家族
# 特征位），Chromium 从不提供。
_GL_EXT_IOS_ADD = (
    "WEBKIT_WEBGL_compressed_texture_pvrtc",
)

_GL_LIMITS_MOBILE: Dict[str, Any] = {
    # 各家族一致的移动端上限（覆盖宿主同值项，语义上仍是「声明值」）
    "MAX_TEXTURE_SIZE": 16384,
    "MAX_RENDERBUFFER_SIZE": 16384,
    "MAX_CUBE_MAP_TEXTURE_SIZE": 16384,
    "MAX_VIEWPORT_DIMS": [16384, 16384],
    "MAX_SAMPLES": 4,
    "MAX_DRAW_BUFFERS": 8,
    "MAX_VERTEX_ATTRIBS": 16,
    "MAX_TEXTURE_IMAGE_UNITS": 16,
    "MAX_VERTEX_TEXTURE_IMAGE_UNITS": 16,
    "MAX_COMBINED_TEXTURE_IMAGE_UNITS": 32,
    "MAX_VERTEX_UNIFORM_VECTORS": 1024,
    "MAX_FRAGMENT_UNIFORM_VECTORS": 1024,
    "MAX_VARYING_VECTORS": 30,
    "ALIASED_POINT_SIZE_RANGE": [1, 511],
    "ALIASED_LINE_WIDTH_RANGE": [1, 1],
}


def _webgl_profile(target_os: str) -> Dict[str, Any]:
    """目标平台 WebGL 面校准规格：上限覆盖 + 扩展集剔除/补充（按 GPU 家族）。"""
    if target_os == "ios":
        return {
            "limits": dict(_GL_LIMITS_MOBILE),
            "dropExtensions": list(_GL_EXT_DESKTOP_ONLY_IOS),
            "addExtensions": list(_GL_EXT_IOS_ADD),
        }
    return {
        "limits": dict(_GL_LIMITS_MOBILE),
        "dropExtensions": list(_GL_EXT_DESKTOP_ONLY),
    }


# --------------------------------------------------------------------------
# 平台 API 差集（窗口 / 文档 / navigator 三处统一按名删除）
# --------------------------------------------------------------------------
# 桌面内核会暴露一批「所属平台真机没有」的 API，属一行判假面。逐项证据 =
# MDN BCD（/bcd/api/v0/current/）的 safari_ios / chrome_android 支持值；
# WebKit 从未实现的家族（传感器 / 隐私沙盒）同时有 WebKit 源码面佐证。
# 删除时同步清 window / Window.prototype / Document.prototype / document /
# Navigator.prototype / navigator，并按需在原型上兜底 defineGet(undefined)。
#
# ---- iOS（Safari 与 CriOS 同为 WebKit 内核，口径一致）----
_IOS_REMOVE_GLOBALS = [
    # 通用传感器 API：WebKit 从未实现（BCD api.Accelerometer /
    # api.Gyroscope / api.GravitySensor / api.LinearAccelerationSensor /
    # api.AbsoluteOrientationSensor / api.RelativeOrientationSensor /
    # api.SensorErrorEvent / api.Sensor: safari_ios=false）。
    # Chrome Android 自 67 起具备，Android 侧保持（注入层由 CDP 虚拟
    # 传感器喂数，读数与事件流同源）。
    "Sensor", "SensorErrorEvent", "Accelerometer", "Gyroscope",
    "LinearAccelerationSensor", "GravitySensor",
    "AbsoluteOrientationSensor", "RelativeOrientationSensor",
    # Chromium 专有、Safari 无（BCD 逐项 safari_ios=false）：
    # EyeDropper(95/desktop-only) / IdleDetector(94) / WebSocketStream(124) /
    # ImageDecoder(94) / FileSystemObserver(133) / queryLocalFonts(103) /
    # documentPictureInPicture(116) / launchQueue(102) / Scheduler(94) /
    # OTPCredential(93) / Ink(94) / MediaStreamTrackGenerator(94)
    "EyeDropper", "IdleDetector", "WebSocketStream", "ImageDecoder",
    "FileSystemObserver", "queryLocalFonts", "documentPictureInPicture",
    "launchQueue", "Scheduler", "scheduler", "OTPCredential", "Ink",
    "MediaStreamTrackGenerator",
    # 隐私沙盒（WebKit 明确拒绝实现）：Shared Storage / Fenced Frames /
    # Topics / Protected Audience（BCD api.Window.sharedStorage /
    # api.Window.fence / api.HTMLFencedFrameElement / api.Document.browsingTopics
    # / api.Navigator.runAdAuction 等：safari_ios=false）
    "sharedStorage", "SharedStorage", "fence", "Fence",
    "HTMLFencedFrameElement", "browsingTopics",
    "runAdAuction", "joinAdInterestGroup", "leaveAdInterestGroup",
    "deprecatedReplaceInURN", "updateAdInterestGroups", "protectedAudience",
    # File System Access 的「选择器」面：Safari 只实现 OPFS
    # （FileSystemHandle 家族 Safari 15.2+ 具备、不可误删），
    # show{,Open,Save}FilePicker / showDirectoryPicker 三项均为
    # Chromium 专有（BCD safari_ios=false）
    "showSaveFilePicker", "showDirectoryPicker",
    # navigator 成员：WebXR / VirtualKeyboard / Ink（BCD safari_ios=false）
    "xr", "virtualKeyboard", "ink",
    # V8 专有、WebKit 从未实现的两项。检测站各占一档独立判据：
    #   Intl.v8BreakIterator —— V8 的旧版 ICU 断词器，WebKit 的 Intl 无此成员；
    #   navigator.webkitTemporaryStorage / webkitPersistentStorage —— Blink
    #   分叉时遗留的配额 API，WebKit main 的 NavigatorStorage.idl 只声明
    #   `storage`（Source/WebCore/page/NavigatorStorage.idl，2026-10 核实），
    #   真机 Safari/CriOS 均无。桌面内核两者俱全，一行 `in` 即可判假。
    "v8BreakIterator", "webkitTemporaryStorage", "webkitPersistentStorage",
    # F-05 / F-09 / F-10 / F-21：WebKit 27.2 实测缺席、桌面内核仍带的入口
    # 接口对象与 Blink 遗留别名。整族只收「入口」——家族成员的 `in` 随
    # 入口缺席而消失。webkit* 前缀族是 Chromium 的遗留别名，真机 iOS 用
    # 的是完全不同的命名（如 SpeechRecognition 家族），比裸名更刺眼。
    # 反例（iOS 真支持，勿加）：styleMedia / BarcodeDetector / PaymentRequest /
    # SpeechRecognition / MediaStreamTrackProcessor / DigitalCredential /
    # DevicePosture / DeviceMotionEventAcceleration / DeviceMotionEventRotationRate。
    "External", "external", "crashReport", "viewport", "credentialless",
    "InputDeviceCapabilities", "isExtended",
    "NavigatorUAData", "NetworkInformation",
    "BackgroundFetchManager", "SyncManager", "BeforeInstallPromptEvent",
    "WindowControlsOverlay", "requestIdleCallback", "cancelIdleCallback",
    "EditContext", "TextFormat", "TextUpdateEvent", "CharacterBoundsUpdateEvent",
    "CropTarget", "DelegatedInkTrailPresenter", "ChapterInformation",
    "MediaStreamTrackAudioStats", "MediaStreamTrackVideoStats", "AudioSinkInfo",
    "FencedFrameConfig", "SharedStorageWorklet", "Sanitizer",
    "LaunchQueue", "LaunchParams", "TimelineTrigger", "NotRestoredReasons",
    "WebSocketError", "WebGLObject", "DOMError", "QuotaExceededError",
    "FeaturePolicy", "AsyncDisposableStack", "DisposableStack",
    "SuppressedError", "CrashReportContext",
    "webkitMediaStream", "webkitRTCPeerConnection",
    "webkitSpeechRecognitionError", "webkitSpeechRecognitionEvent",
    "webkitSpeechGrammar", "webkitSpeechGrammarList",
    # Blink 专有事件处理器属性（F-08）：WebKit 用的是 webglcontextlost /
    # webglcontextrestored 等另一套命名，命名不同即一行判假。
    "onsearch", "oncontextlost", "oncontextrestored", "onbeforeinstallprompt",
    "onappinstalled",
    # F-10：Blink 前缀可见性别名（WebKit 14 起已移除该前缀面）
    "webkitHidden", "webkitVisibilityState",
    # F-139：Device Posture 在 WebKit 里由 `DevicePostureAPIEnabled` 偏好
    # 门控，`UnifiedWebPreferences.yaml` 记 `status: testable` +
    # `defaultValue: false`——出货版 Safari 不开，普通浏览上下文恒缺席，
    # 与 iOS 版本无关（BCD 的 safari_ios 条目本身就是 preference flag）。
    # 放版本门控表会在 27+ 档漏出，故按无条件删除处理。
    "DevicePosture", "devicePosture",
    # ---- F-62 / F-63 / F-65 / F-66 / F-67 / F-68 / F-75：Chromium 专有家族 ----
    # 证据：原生 fp-chromium 148（https 页面，零注入）对 WebKit 基线取差集，
    # 再逐族过 BCD safari_ios=false（取证见清单 §23/§24/§25）。整族按名删除
    # 而非只删入口——Chromium 的家族成员是独立全局，入口删掉成员仍在，
    # `'HIDDevice' in window` 依旧为 true。
    # 硬件直连（Web Bluetooth / WebHID / WebUSB / Web Serial / Web MIDI /
    # Keyboard Lock）：WebKit 一个都没实现。BluetoothUUID 一并删——真机
    # 没有该命名空间对象，留着等于留下整族的指纹。
    "Bluetooth", "BluetoothCharacteristicProperties", "BluetoothDevice",
    "BluetoothRemoteGATTCharacteristic", "BluetoothRemoteGATTDescriptor",
    "BluetoothRemoteGATTServer", "BluetoothRemoteGATTService",
    "BluetoothUUID",
    "HID", "HIDConnectionEvent", "HIDDevice", "HIDInputReportEvent",
    "USB", "USBAlternateInterface", "USBConfiguration", "USBConnectionEvent",
    "USBDevice", "USBEndpoint", "USBInTransferResult", "USBInterface",
    "USBIsochronousInTransferPacket", "USBIsochronousInTransferResult",
    "USBIsochronousOutTransferPacket", "USBIsochronousOutTransferResult",
    "USBOutTransferResult",
    "Serial", "SerialPort",
    "MIDIAccess", "MIDIConnectionEvent", "MIDIInput", "MIDIInputMap",
    "MIDIMessageEvent", "MIDIOutput", "MIDIOutputMap", "MIDIPort",
    "Keyboard",
    # WebXR：Safari 从不支持（navigator.xr 已在上面删除，类名整族 50 个同删）。
    "XRAnchor", "XRAnchorSet", "XRBoundedReferenceSpace",
    "XRCPUDepthInformation", "XRCamera", "XRCompositionLayer", "XRCubeLayer",
    "XRCylinderLayer", "XRDOMOverlayState", "XRDepthInformation",
    "XREquirectLayer", "XRFrame", "XRHand", "XRHitTestResult",
    "XRHitTestSource", "XRInputSource", "XRInputSourceArray",
    "XRInputSourceEvent", "XRInputSourcesChangeEvent", "XRJointPose",
    "XRJointSpace", "XRLayer", "XRLayerEvent", "XRLightEstimate",
    "XRLightProbe", "XRPlane", "XRPlaneSet", "XRPose", "XRProjectionLayer",
    "XRQuadLayer", "XRRay", "XRReferenceSpace", "XRReferenceSpaceEvent",
    "XRRenderState", "XRRigidTransform", "XRSession", "XRSessionEvent",
    "XRSpace", "XRSubImage", "XRSystem", "XRTransientInputHitTestResult",
    "XRTransientInputHitTestSource", "XRView", "XRViewerPose", "XRViewport",
    "XRVisibilityMaskChangeEvent", "XRWebGLBinding", "XRWebGLDepthInformation",
    "XRWebGLLayer", "XRWebGLSubImage",
    # 广告 / 隐私沙盒残项（本体已在上面，这些是同族的方法对象与子类）
    "ProtectedAudience", "SharedStorageAppendMethod", "SharedStorageClearMethod",
    "SharedStorageDeleteMethod", "SharedStorageModifierMethod",
    "SharedStorageSetMethod", "InterestEvent", "Subscriber",
    "RestrictionTarget", "NotRestoredReasonDetails",
    # 呈现 / 后台同步 / 存储分桶（三类 WebKit 均未实现，见下方 F-90 段落对
    # 「BCD 记 16.4」的辨析：那是 Home Screen Web App 口径）
    "Presentation", "PresentationConnection", "PresentationConnectionAvailableEvent",
    "PresentationConnectionCloseEvent", "PresentationConnectionList",
    "PresentationReceiver", "PresentationRequest",
    # F-92：同族的两个类名漏项。入口 navigator.presentation /
    # navigator.storageBuckets 已在 _IOS_REMOVE_NAV_PROPS 删除，但类名是
    # 独立全局，`'StorageBucket' in window` 仍为 true。三方证据：
    # BCD safari_ios=false；Playwright WebKit 27.2 两者均 undefined；
    # 原生 fp-chromium 148 两者均暴露（StorageBucket 原型带
    # name/indexedDB/caches/estimate/expires/persisted/setExpires/persist）。
    "PresentationAvailability", "StorageBucket",
    "PeriodicSyncManager", "StorageBucketManager",
    # F-92：Gamepad 震动面。WebKit GamepadHapticActuator.idl 受
    # EnabledBySetting=GamepadVibrationActuatorEnabled 门控，而该开关默认值
    # 取自 defaultGamepadVibrationActuatorEnabled()
    # （Source/WebKit/Shared/WebPreferencesDefaultValues.cpp:246）：
    # 仅 HAVE(WIDE_GAMECONTROLLER_SUPPORT) / WPE / GTK 为 true，而
    # HAVE_WIDE_GAMECONTROLLER_SUPPORT 只在 PLATFORM(MAC) 下定义
    # （Source/WTF/wtf/PlatformHave.h:607）→ iOS 恒 false。
    # 故 iOS 既无该类名，也无 Gamepad.prototype.vibrationActuator
    # （后者见 _IOS_DROP_PROTO_MEMBERS）。BCD safari_ios=false 与此一致；
    # Playwright WebKit 显示存在是 macOS 构建口径，不可直接套 iOS。
    "GamepadHapticActuator",
    # 调度器 / 性能观测（safari_ios 全 false；IdleDeadline 仅 preview）
    "Scheduling", "TaskController", "TaskSignal", "TaskPriorityChangeEvent",
    "TaskAttributionTiming", "Observable", "IdleDeadline", "LayoutShift",
    "LayoutShiftAttribution", "Profiler", "PerformanceLongTaskTiming",
    "PerformanceScriptTiming", "PerformanceLongAnimationFrameTiming",
    "PerformanceElementTiming", "PerformanceTimingConfidence",
    # 视口 / 虚拟键盘（KeyboardLayoutMap 已在上面）
    "Viewport", "VirtualKeyboard", "VirtualKeyboardGeometryChangeEvent",
    # 凭证 / 联邦身份（Credential / CredentialsContainer 保留；DigitalCredential
    # 是 iOS 26+ 才有的版本门控项，见 _IOS_MIN_VERSIONS）
    "FederatedCredential", "PasswordCredential", "NavigatorLogin",
    "NavigatorManagedData",
    # F-75 剩余零散面
    "BackgroundFetchRecord", "BackgroundFetchRegistration",
    "BatteryManager", "BrowserCaptureMediaStreamTrack",
    "NavigationPrecommitController", "GPUError", "VisibilityStateEntry",
    "CSSMarginRule", "CSSPositionValue", "ClipboardChangeEvent", "SnapEvent",
    "HTMLGeolocationElement", "IntegrityViolationReportBody", "PaymentManager",
    "TextFormatUpdateEvent", "WindowControlsOverlayGeometryChangeEvent",
    "MediaStreamEvent", "TimelineTriggerRange", "TimelineTriggerRangeList",
    "SpeechGrammar", "SpeechGrammarList", "SpeechRecognitionPhrase",
    "webkitRequestFileSystem", "webkitResolveLocalFileSystemURL",
    # ---- F-64 / F-65 / F-71：对象成员与 Document 原型面 ----
    # F-64：window.chrome 是 Chrome 的经典探测向量，iOS Safari 恒无。
    "chrome",
    # F-65：FLEDGE / Protected Audience 的 Navigator 方法面（类名在上）。
    # 拼写以 Blink 实际导出名为准（F-83 实测：真名是
    # deprecatedRun**AdAuction**EnforcesKAnonymity，此前误写成
    # deprecatedRunAuction…，delete 全部落空——18.6 上该 boolean 暴露为 false）。
    "adAuctionComponents", "canLoadAdAuctionFencedFrame",
    "clearOriginJoinedAdInterestGroups", "createAuctionNonce",
    "deprecatedRunAdAuctionEnforcesKAnonymity", "deprecatedURNToURL",
    "getInterestGroupAdAuctionData",
    # F-71：Document.prototype 的指针锁 / Page Lifecycle / Scroll Snap /
    # 隐私沙盆成员。pointerLock 族 BCD safari=10.1 但 safari_ios=false。
    # 全屏族 13 名（标准 5 + webkit 别名 8）按 F-76 翻转保留，不在此列；
    # ariaNotify 是 iOS 27+ 的版本门控项，见 _IOS_MIN_VERSIONS。
    "pointerLockElement", "exitPointerLock", "prerendering", "wasDiscarded",
    "featurePolicy", "hasPrivateToken", "hasRedemptionRecord",
    "hasUnpartitionedCookieAccess", "requestStorageAccessFor", "moveBefore",
    "onbeforexrselect", "onfreeze", "onpointerlockchange",
    "onpointerlockerror", "onpointerrawupdate", "onprerenderingchange",
    "onresume", "onscrollsnapchange", "onscrollsnapchanging",
    # 真机复核队列的两项：BCD safari=false 且 WebKit Bugzilla 零记录，
    # 按删除处理（若真机复核推翻，从本表移除即可）
    "AnimationTrigger", "FontFaceSetLoadEvent",
    # F-21：第三轮新出现的 Chromium 专有入口（BCD safari_ios=false）。
    # Chrome 内置 AI（138+）：WebKit 无 LLM/翻译/摘要任何一面。
    "LanguageDetector", "LanguageModel", "Translator", "Summarizer",
    # 画中画 / 编辑
    "DocumentPictureInPicture", "DocumentPictureInPictureEvent",
    # 屏幕 / 窗口管理（Window Management API 全是 Chromium 专有）
    "getScreenDetails", "ScreenDetailed", "ScreenDetails", "CookieStoreManager",
    # 采集 / 传感器 / 输入
    "CaptureController", "CreateMonitor", "PressureObserver", "PressureRecord",
    "OrientationSensor", "KeyboardLayoutMap",
    # 媒体
    "ImageTrack", "ImageTrackList", "AudioPlaybackStats", "FontData",
    # Privacy Sandbox / 归因
    "IdentityCredential", "IdentityProvider", "IdentityCredentialError",
    "fetchLater", "FetchLaterResult",
    # 其它 Chromium 新增面
    "NotRestoredReasonsDetails",
    # ---- F-90：Web Notifications / Push / 徽标（iOS 普通浏览上下文全无）----
    # WebKit 的这族能力全部由 WebPreferences 开关门控，而 iOS 的默认值是关：
    #   NotificationsEnabled      defaultValue PLATFORM(IOS_FAMILY)=false
    #   PushAPIEnabled            defaultValue false（embedder 未打开）
    #   AppBadgeEnabled           defaultValue false
    #   NotificationEventEnabled  defaultValue ENABLE(NOTIFICATION_EVENT) &&
    #                             !PLATFORM(IOS_FAMILY)（见
    #                             Source/WTF/Scripts/Preferences/UnifiedWebPreferences.yaml）
    # 对应 IDL 全部带 EnabledBySetting（Notification.idl / PushManager.idl /
    # ServiceWorkerRegistration+PushAPI.idl / ServiceWorkerRegistration.idl 的
    # showNotification、getNotifications / NavigatorBadge.idl），所以普通页
    # 上这些名字与成员**整体缺席**，不是「存在但被拒」。
    # 旁证：iOS TestExpectations「# Web Notifications are not supported on
    # iOS.」+ b/240579 对 shownotification-* / getnotifications* /
    # http/tests/push-api 整片 skip；BCD safari_ios 对 Notification 记
    # partial_implementation（"undefined, unless the page is a web app saved
    # to the home screen"），PushManager 16.4 同款 Home Screen 口径，
    # Window.pushManager 则是 18.4。
    # Playwright WebKit 27.2（macOS 构建）这 9 名全 undefined，与上述一致。
    # 本仓库没有 standalone / manifest 判定（navigator.standalone 恒 false），
    # 因此所有 iOS profile 都按普通浏览上下文处理。
    #
    # F-90 勘误（推翻 F-66 的「PushManager 禁删」与 F-102 备注的「勿连带删除」）：
    # 那两条判断依据的是 BCD 的 16.4 条目，而 BCD 对 PushManager /
    # PushSubscription / PushSubscriptionOptions / NotificationEvent 的
    # safari_ios 条目都带同一条 notes——"Notifications are supported in web
    # apps saved to the home screen"。PushManager.idl 与
    # ServiceWorkerRegistration+PushAPI.idl 双双标 EnabledBySetting=PushAPIEnabled，
    # 而该 pref 的 defaultValue 为 false（status: embedder，WebKit 未在
    # iOS 上打开）；NotificationsEnabled 更是显式 PLATFORM(IOS_FAMILY)=false。
    # 因此普通浏览上下文里整族缺席，与 Notification 同因同源，必须同删。
    # Window.pushManager（18.4）同属 PushAPIEnabled 门控，一并覆盖。
    "Notification", "NotificationEvent", "NotificationPermission",
    "PushManager", "PushSubscription", "PushSubscriptionOptions",
    "PushEvent", "PushMessageData", "PushPermissionState",
]

# ---- Android Chrome（真实 Chrome Android 无、桌面内核有）----
# BCD chrome_android=false：EyeDropper / FileSystemObserver（133）/
# queryLocalFonts（103）/ documentPictureInPicture（116）/ launchQueue（102）。
# 注意反向：WebHID 已由 _ANDROID_REMOVE_NAV_PROPS 处理；WebUSB/Web Serial/
# FSA 选择器（chrome_android=132）/ 传感器家族 / IdleDetector / OTPCredential /
# Ink 等在 Chrome Android 真实存在，不得误删（回归断言锁定）。
_ANDROID_REMOVE_GLOBALS = [
    "EyeDropper", "FileSystemObserver", "queryLocalFonts",
    "documentPictureInPicture", "launchQueue",
    # F-78（BCD chrome_android=false + 运行时仍暴露，双证据）：WebHID 族 /
    # Window Controls Overlay / Document PiP / Chrome 内置 AI / 杂项。
    "HID", "HIDDevice", "HIDConnectionEvent", "HIDInputReportEvent",
    "WindowControlsOverlay", "WindowControlsOverlayGeometryChangeEvent",
    "DocumentPictureInPicture", "DocumentPictureInPictureEvent",
    "RestrictionTarget", "SpeechRecognitionPhrase",
    "LanguageModel", "Translator", "Summarizer",
]

# ---- Worker 全局作用域内的同名差集 ----
# 内核在 Worker 里同样暴露（实测）：IdleDetector / WebSocketStream /
# ImageDecoder / FileSystemObserver / Scheduler / scheduler（对象）。
# WebKit 全部未实现（BCD safari_ios=false）；Chrome Android 侧除
# FileSystemObserver 外均存在（chrome_android=false → Android 也删）。
_IOS_REMOVE_WORKER_GLOBALS = [
    "IdleDetector", "WebSocketStream", "ImageDecoder", "FileSystemObserver",
    "Scheduler", "scheduler",
]
_ANDROID_REMOVE_WORKER_GLOBALS = ["FileSystemObserver"]

# ---- Worker 全局面：iOS 真机有、fp-chromium Worker 没有的接口（F-37 / F-38）----
# 证据：Playwright WebKit 27.2 的 Worker 作用域实测（`/tmp/wkworker/probe.py`、
# `kinds.py`、`vals.py`、`vals2.py`）对 fp-chromium 148 的同一探针
# （`/tmp/wkworker/chprobe.py`）取差集——WebKit 多 40 项、Chromium 多 31 项。
#
# 只登记「真机有、仿真缺」方向；反方向的 31 项走删除清单（见
# _IOS_WORKER_GLOBAL_DROPS）。字段口径与页面 _IOS_ADD_SPEC["interfaces"] 一致：
#   construct  'illegal' = 带不带 new 都抛 Illegal constructor
#              'args'    = 必须 new，缺参抛 Not enough arguments
#              'ok'      = 必须 new，可零参
#   extends    真机原型链父接口（Worker 作用域内同名，实测 chain）
#   init       实例初值；members '!' = 方法、'=' = 可写属性、裸名 = 只读；
#              方法名后接 ':N' 表示真机 Function#length 为 N（缺省 0）
#   statics    构造器静态成员：'!name' = 方法（按真机返回值）
#   values     静态方法的固定返回（isConfigSupported 类，见注入器）
#   brandErr   'webkit' = 真机文案（The X.m getter can only be used on
#              instances of X / Can only call X.m on instances of X）；
#              缺省沿用注入器既有的 DOM 文案 'Illegal invocation'
#   noBrand    真机不做接收者校验的成员（传 {} 也照常返回，实测）
_IOS_WORKER_ADD_INTERFACES: Dict[str, Any] = {
    # ---- WebCodecs（WebKit 的 Worker 侧比 Chromium 多 Audio/VideoDecoder+Encoder）----
    "AudioDecoder": {"construct": "args", "length": 1, "extends": "EventTarget",
                     "init": {"state": "unconfigured", "decodeQueueSize": 0},
                     "members": ["state", "decodeQueueSize", "=ondequeue",
                                 "!configure:1", "!decode:1", "!flush",
                                 "!reset", "!close"],
                     "noBrand": ["flush"],
                     "statics": ["!isConfigSupported"]},
    "AudioEncoder": {"construct": "args", "length": 1, "extends": "EventTarget",
                     "init": {"state": "unconfigured", "encodeQueueSize": 0},
                     "members": ["state", "encodeQueueSize", "=ondequeue",
                                 "!configure:1", "!encode:1", "!flush",
                                 "!reset", "!close"],
                     "noBrand": ["flush"],
                     "statics": ["!isConfigSupported"]},
    "VideoDecoder": {"construct": "args", "length": 1, "extends": "EventTarget",
                     "init": {"state": "unconfigured", "decodeQueueSize": 0},
                     "members": ["state", "decodeQueueSize", "=ondequeue",
                                 "!configure:1", "!decode:1", "!flush",
                                 "!reset", "!close"],
                     "noBrand": ["flush"],
                     "statics": ["!isConfigSupported"]},
    "VideoEncoder": {"construct": "args", "length": 1, "extends": "EventTarget",
                     "init": {"state": "unconfigured", "encodeQueueSize": 0},
                     "members": ["state", "encodeQueueSize", "=ondequeue",
                                 "!configure:1", "!encode:1", "!flush",
                                 "!reset", "!close"],
                     "noBrand": ["flush"],
                     "statics": ["!isConfigSupported"]},
    # ---- 媒体轨道配置字典 ----
    "AudioTrackConfiguration": {"construct": "illegal", "length": 0,
                                "members": ["codec", "sampleRate",
                                            "numberOfChannels", "bitrate"]},
    "VideoTrackConfiguration": {"construct": "illegal", "length": 0,
                                "members": ["codec", "width", "height",
                                            "bitrate", "framerate", "colorSpace"]},
    # ---- Managed Media Source（iOS 17.1+）----
    "ManagedMediaSource": {"construct": "ok", "length": 0,
                           "extends": "MediaSource",
                           "init": {"quality": "high", "streaming": False},
                           "members": ["quality", "=onqualitychange",
                                       "streaming", "=onstartstreaming",
                                       "=onendstreaming"],
                           "statics": ["!isTypeSupported"]},
    "ManagedSourceBuffer": {"construct": "illegal", "length": 0,
                            "extends": "SourceBuffer",
                            "members": ["=onbufferedchange"]},
    "BufferedChangeEvent": {"construct": "args", "length": 2,
                            "extends": "Event",
                            "members": ["addedRanges", "removedRanges"]},
    # ---- MediaStreamTrack 家族 ----
    "MediaStreamTrack": {"construct": "illegal", "length": 0,
                         "extends": "EventTarget",
                         "init": {"enabled": True, "muted": False,
                                  "readyState": "live", "contentHint": ""},
                         "members": ["kind", "id", "label", "=enabled",
                                     "=contentHint", "muted", "=onmute",
                                     "=onunmute", "=onconfigurationchange",
                                     "readyState", "=onended", "!clone", "!stop",
                                     "!getCapabilities", "!getConstraints",
                                     "!getSettings", "!applyConstraints"],
                         "noBrand": ["applyConstraints"]},
    "MediaStreamTrackProcessor": {"construct": "args", "length": 1,
                                  "members": ["readable"]},
    "VideoTrackGenerator": {"construct": "ok", "length": 0,
                            "init": {"muted": False},
                            "members": ["writable", "=muted", "track"]},
    # ---- 规范内、Chromium Worker 侧缺失的杂项 ----
    "NavigationPreloadManager": {"construct": "illegal", "length": 0,
                                 "members": ["!enable", "!disable",
                                             "!setHeaderValue:1", "!getState"],
                                 "noBrand": ["enable", "disable",
                                             "setHeaderValue", "getState"]},
    "Report": {"construct": "illegal", "length": 0,
               "members": ["type", "url", "body", "!toJSON"]},
    "FontFaceSet": {"construct": "illegal", "length": 1, "noNew": "args",
                    "extends": "EventTarget",
                    "members": ["size", "=onloading", "=onloadingdone",
                                "=onloadingerror", "ready", "status",
                                "!has:1", "!add:1", "!delete:1", "!clear",
                                "!load:1", "!check:1", "!entries", "!keys",
                                "!values", "!forEach:1"],
                    "noBrand": ["ready", "load"]},
    "StylePropertyMapReadOnly": {
        "construct": "illegal", "length": 0,
        "members": ["size", "!get:1", "!getAll:1", "!has:1", "!entries",
                    "!keys", "!values", "!forEach:1"]},
    # ---- CSS Typed OM（Worker 里整族都在，Chromium Worker 只有 CSSSkewX/Y）----
    "CSSStyleValue": {"construct": "illegal", "length": 0,
                      "members": ["!toString"]},
    "CSSImageValue": {"construct": "illegal", "length": 0,
                      "extends": "CSSStyleValue", "members": []},
    "CSSKeywordValue": {"construct": "args", "length": 1,
                        "extends": "CSSStyleValue", "members": ["=value"]},
    "CSSNumericValue": {"construct": "illegal", "length": 0,
                        "extends": "CSSStyleValue",
                        "members": ["!add", "!sub", "!mul", "!div", "!min",
                                    "!max", "!equals", "!to:1", "!toSum",
                                    "!type"]},
    "CSSUnitValue": {"construct": "args", "length": 2,
                     "extends": "CSSNumericValue",
                     "members": ["=value", "unit"]},
    "CSSUnparsedValue": {"construct": "args", "length": 1,
                         "extends": "CSSStyleValue",
                         "members": ["length", "!entries", "!forEach",
                                     "!keys", "!values"],
                         "noBrand": ["entries", "keys", "values", "forEach"]},
    "CSSVariableReferenceValue": {"construct": "args", "length": 1,
                                  "members": ["=variable", "fallback"]},
    "CSSNumericArray": {"construct": "illegal", "length": 0,
                        "members": ["length", "!entries", "!forEach",
                                    "!keys", "!values"],
                        "noBrand": ["entries", "keys", "values", "forEach"]},
    "CSSMathValue": {"construct": "illegal", "length": 0,
                     "extends": "CSSNumericValue", "members": ["operator"]},
    "CSSMathSum": {"construct": "args", "length": 0,
                   "extends": "CSSMathValue",
                   "init": {"operator": "sum"}, "members": ["values"]},
    "CSSMathProduct": {"construct": "args", "length": 0,
                       "extends": "CSSMathValue",
                       "init": {"operator": "product"}, "members": ["values"]},
    "CSSMathMin": {"construct": "args", "length": 0,
                   "extends": "CSSMathValue",
                   "init": {"operator": "min"}, "members": ["values"]},
    "CSSMathMax": {"construct": "args", "length": 0,
                   "extends": "CSSMathValue",
                   "init": {"operator": "max"}, "members": ["values"]},
    "CSSMathNegate": {"construct": "args", "length": 1,
                      "extends": "CSSMathValue",
                      "init": {"operator": "negate"}, "members": ["value"]},
    "CSSMathInvert": {"construct": "args", "length": 1,
                      "extends": "CSSMathValue",
                      "init": {"operator": "invert"}, "members": ["value"]},
    "CSSMathClamp": {"construct": "args", "length": 3,
                     "extends": "CSSMathValue",
                     "init": {"operator": "clamp"},
                     "members": ["lower", "value", "upper"]},
    "CSSTransformComponent": {"construct": "illegal", "length": 0,
                              "init": {"is2D": True},
                              "members": ["=is2D", "!toMatrix", "!toString"]},
    "CSSTransformValue": {"construct": "args", "length": 1,
                          "extends": "CSSStyleValue",
                          "members": ["length", "is2D", "!toMatrix",
                                      "!entries", "!forEach", "!keys",
                                      "!values"],
                          "noBrand": ["entries", "keys", "values", "forEach"]},
    "CSSTranslate": {"construct": "args", "length": 2,
                     "extends": "CSSTransformComponent",
                     "members": ["=x", "=y", "=z"]},
    "CSSRotate": {"construct": "args", "length": 1,
                  "extends": "CSSTransformComponent",
                  "init": {"x": 0, "y": 0, "z": 1},
                  "members": ["=x", "=y", "=z", "=angle"]},
    "CSSScale": {"construct": "args", "length": 2,
                 "extends": "CSSTransformComponent",
                 "init": {"z": 1},
                 "members": ["=x", "=y", "=z"]},
    "CSSSkew": {"construct": "args", "length": 2,
                "extends": "CSSTransformComponent",
                "members": ["=ax", "=ay"]},
    "CSSPerspective": {"construct": "args", "length": 1,
                       "extends": "CSSTransformComponent",
                       "members": ["=length"]},
    "CSSMatrixComponent": {"construct": "args", "length": 1,
                           "extends": "CSSTransformComponent",
                           "members": ["=matrix"]},
    # ---- ServiceWorker 家族（F-77 D3）----
    # 三个接口的 IDL 都是 Exposed=(Window,Worker)，真机 WebKit 的 Worker
    # 全局三者俱全；Chromium 的 Worker 只有 ServiceWorkerRegistration。
    # 接收者校验文案实测（/tmp/f77p/swbrand.py wk）：
    #   「The X.m getter can only be used on instances of X」
    #   「Can only call X.m on instances of X」
    # 与注入器 __wkBrandErrW 的默认文案一致，故不写 brandErr / noBrand。
    "ServiceWorker": {
        "construct": "illegal", "length": 0, "extends": "EventTarget",
        "members": ["scriptURL", "state", "=onstatechange", "=onerror",
                    "!postMessage:1"]},
    "ServiceWorkerContainer": {
        "construct": "illegal", "length": 0, "extends": "EventTarget",
        "init": {"controller": None},
        "members": ["controller", "ready", "=oncontrollerchange",
                    "=onmessage", "=onmessageerror", "!register:1",
                    "!getRegistration", "!getRegistrations",
                    "!startMessages"]},
    # repair：Chromium 的 Worker 已有该接口，构造文案是 Blink 形态
    # （`Failed to construct 'ServiceWorkerRegistration': Illegal constructor`），
    # 换成真机的裸文案并把 constructor 挪回原型首位即可。成员不动——多出的
    # paymentManager / backgroundFetch / pushManager 等由 embedder 设置门控，
    # Playwright 基线与真机 Safari 本就不同源，页面侧保持同一口径，不在
    # Worker 里单删（否则跨上下文自相矛盾）。
    "ServiceWorkerRegistration": {
        "construct": "illegal", "length": 0, "repair": True, "members": []},
}

# Worker 全局面反方向：fp-chromium Worker 有、真机 WebKit Worker 没有。
# 与页面 _IOS_REMOVE_GLOBALS 是两套表（两边的缺口不同）：页面里被删的
# PushManager / SyncManager / NetworkInformation 等在这里同样要删，而
# 页面存在的 BarcodeDetector / URLPattern 在 Worker 里两侧都有、不能删。
# Worker 的 WebKit 侧删除清单 = 本表 + _IOS_REMOVE_WORKER_GLOBALS（旧表，
# 实测 WebKit Worker 同样没有 IdleDetector / WebSocketStream / ImageDecoder /
# FileSystemObserver / Scheduler）。
_IOS_WORKER_GLOBAL_DROPS = [
    # Blink 存储/后台同步/推送（WebKit Worker 均无；PushManager 只挂在页面
    # ServiceWorkerRegistration 上，不在 Worker 全局）
    # Notification：WebKit 的 IDL 虽是 Exposed=(Window,DedicatedWorker,
    # ServiceWorker)，但 NotificationsEnabled 在 PLATFORM(IOS_FAMILY) 默认
    # false，Worker 全局同样缺席；Blink 的 DedicatedWorker 有 Notification。
    "Notification",
    "PushManager", "PushSubscription", "PushSubscriptionOptions",
    "SyncManager", "PeriodicSyncManager",
    "BackgroundFetchManager", "BackgroundFetchRegistration",
    "BackgroundFetchRecord",
    # Blink 调度/任务优先级
    "TaskController", "TaskSignal", "TaskPriorityChangeEvent",
    # 传感器与压力观测
    "PressureObserver", "PressureRecord",
    # 媒体
    "SourceBufferList", "WebSocketError",
    # 显式资源管理（V8 专有，iOS 无）
    "AsyncDisposableStack", "DisposableStack", "SuppressedError",
    # Blink 杂项
    "CropTarget", "RestrictionTarget", "UserActivation", "Observable",
    "Subscriber", "QuotaExceededError", "WebGLObject",
    # UA-CH / Network Information：真机 iOS Worker 的 WorkerNavigator 没有
    # 这两个接口（页面侧 iOS 同样删除，见 _IOS_REMOVE_GLOBALS）
    "NavigatorUAData", "NetworkInformation",
    # webkit* 前缀的 FileSystem 遗留别名：真机 iOS 页面有
    # webkitRequestFileSystem，但 Worker 里 WebKit 一个都不暴露
    "webkitRequestFileSystem", "webkitRequestFileSystemSync",
    "webkitResolveLocalFileSystemURL", "webkitResolveLocalFileSystemSyncURL",
]

# Worker 里真机 WebKit 的 WorkerNavigator 原型面（WebKit 27.2 Worker 内实测）：
# 只有这 16 个成员（全部 enumerable getter），Chromium 的 WorkerNavigator 多
# deviceMemory / userAgentData / connection 等。删除后 Worker 内
# `'deviceMemory' in navigator` 才与真机一致（页面侧已有的 removeProps
# 复用同表，这里只补 Worker 独有的项）。
#
# 注意 globalPrivacyControl **不在**真机列表里：WebKit 的
# NavigatorGlobalPrivacyControl.idl 用 [EnabledBySetting=GlobalPrivacyControlEnabled]
# 门控，未由 embedder 打开时属性不出现（WebKit commit c7b5fcf1c5 / bug 319630）。
# Worker 侧的 WorkerNavigator 也不 include NavigatorGlobalPrivacyControl
# （Navigator.idl 才 include，见 WebKit main），所以 Worker 里应当缺席，
# 而不是恒 false——注入器不再合成该成员。
_IOS_WORKER_NAV_PROTO = [
    "hardwareConcurrency", "gpu", "mediaCapabilities", "permissions", "locks",
    "appCodeName", "appName", "appVersion", "platform", "product", "userAgent",
    "language", "languages", "onLine", "serviceWorker", "storage",
]

# Worker 独有的全局项（真机 WebKit Worker 有、Chromium Worker 全局没有）。
# WebKit 把 ServiceWorker 家族暴露在 Worker 全局（WorkerGlobalScope.idl 的
# ServiceWorkerContainer 与 ServiceWorker 注册表），而 Chromium 的 Worker
# 只有 ServiceWorkerRegistration。
#   serviceWorker  —— WorkerNavigator.prototype 上的 getter（enumerable true、
#                     get.length 0、无 setter），返回容器实例；
#   ServiceWorkerContainer —— 容器构造器：Illegal constructor、length 0、
#                     extends EventTarget，实例零自有属性、无公开构造路径。
# 接口本体在 _IOS_WORKER_ADD_INTERFACES 里（同一套补建机制），这里只登记
# 「实例 + 原型 getter」的接法。
_IOS_WORKER_ADD_GLOBALS: Dict[str, Any] = {
    "serviceWorker": {"instance": "ServiceWorkerContainer"},
}


# --------------------------------------------------------------------------
# 原型级成员差集（F-26 / F-27）
# --------------------------------------------------------------------------
# 现有删除机制只处理「全局名 / navigator 名」。一批 Chromium 残留挂在
# **接口原型**上，`'addStream' in RTCPeerConnection.prototype` 这类一行探测
# 即可判假；它们不能靠删全局（构造器本身是真机也有的），只能按成员删。
#
# 证据：BCD safari_ios 逐项 + WebKit 27.2 运行期实测（/tmp/batch2_members.py）。
# 判定「该删」的条件是 **BCD safari_ios=false（或记录为已移除）且 WebKit
# 运行期实测缺席**——方向存疑项（GestureEvent.rotation/scale、
# DeviceOrientationEvent.webkitCompass*）一律不动，避免反向制造判据。
#
# 键 = window 上的接口名，值 = 该接口原型上要删的成员名。
# 顺序无意义；删除后若仍在（configurable:false），注入层以 undefined getter
# 兜底（与 removeProp 同语义）。
_IOS_DROP_PROTO_MEMBERS: Dict[str, List[str]] = {
    # F-26：iOS 12 起移除的 WebRTC 遗留面（BCD addStream：safari_ios 11 →
    # removed 12），Chromium 至今保留。
    "RTCPeerConnection": [
        "addStream", "removeStream", "createDTMFSender",
        "getLocalStreams", "getRemoteStreams",
        "onaddstream", "onremovestream",
    ],
    # F-27：FontFace 的 Chromium 描述符扩展。WebKit FontFace.idl 只有
    # ascent/descent/lineGapOverride（且由 CSSFontFaceMetricOverrideDescriptors
    # 开关门控，iOS 未开）、没有 variationSettings/variant。
    # 注意 width 是反向项（WebKit 有、仿真缺）——见 _IOS_ADD_PROTO_MEMBERS。
    "FontFace": [
        "ascentOverride", "descentOverride", "lineGapOverride",
        "variationSettings", "variant",
    ],
    # BCD incremental：safari_ios removed 17.2
    "HTMLInputElement": ["incremental"],
    # BCD pointerevents.persistentDeviceId：safari_ios=false
    "PointerEvent": ["persistentDeviceId"],
    # BCD 均 safari_ios=false
    "IntersectionObserver": ["delay", "trackVisibility"],
    "XMLHttpRequest": ["setAttributionReporting", "setPrivateToken"],
    "Element": [
        "setHTML", "moveBefore", "elementTiming", "fakeShadowRoot",
        "activeViewTransition",
        # F-105：Element.startViewTransition 是 Chrome 147 新增的重载；
        # Document.prototype 上那名自 iOS 18 起真机就有，**保留**。
        "startViewTransition",
        # F-115：Blink 把 onbeforecopy/cut/paste 挂在 Element，WebKit 挂在
        # HTMLElement（下方 add-spec 补回）；onsearch 是 Blink 专有事件。
        "onbeforecopy", "onbeforecut", "onbeforepaste", "onsearch",
    ],
    "Document": [
        "exitPointerLock", "requestStorageAccessFor", "featurePolicy",
        "hasPrivateToken", "hasRedemptionRecord", "hasUnpartitionedCookieAccess",
        "wasDiscarded", "prerendering", "pointerLockElement",
    ],
    # isExtended 是 F-09 的同一项；onchange 为 WebKit 未实现的 Screen 事件
    "Screen": ["isExtended", "onchange"],
    "OffscreenCanvas": ["oncontextlost", "oncontextrestored"],
    "ShadowRoot": ["getSelection", "pointerLockElement", "setHTML"],
    "VideoFrame": ["flip", "rotation", "metadata"],
    # F-102：Blink 的 ServiceWorkerRegistration 扩展成员。前五项是 BCD
    # safari_ios=false 的 Blink 专有面；pushManager / cookies 同为 pref 门控
    # （PushAPIEnabled 默认 false、CookieStoreManagerEnabled 默认 false），
    # 在 iOS 普通浏览上下文里同样缺席——Playwright WebKit 27.2 的
    # has_touch+is_mobile 上下文（走 iOS_FAMILY 默认值）实测这六项全部 absent，
    # 与页面侧一致。navigationPreload（默认 true）不在此列。
    "ServiceWorkerRegistration": [
        "sync", "periodicSync", "backgroundFetch", "paymentManager",
        "cookies", "pushManager",
    ],
    "TrustedTypePolicyFactory": ["getTypeMapping"],
    "HighlightRegistry": ["highlightsFromPoint"],
    "ImageData": ["pixelFormat"],
    "VisualViewport": ["onscrollend"],
    "WebGLRenderingContext": [
        # 常量 RGB8/RGBA8 只存在于 WebGL2RenderingContext（WebKit 实测），
        # 但 WebIDL 常量在接口对象与原型上都是不可配置属性，删不掉；这里
        # 只处理可删/可遮蔽的原型成员。
        # F-106：Blink 在 WebGL1/2 两个接口上各生成一份自有成员（WebGL2
        # 原型并不继承 WebGL1 的生成面），所以两个宿主都要删；只改键会把
        # WebGL1 重新露出来。
        "makeXRCompatible", "unpackColorSpace",
        "drawingBufferStorage", "drawingBufferFormat",
    ],
    "WebGL2RenderingContext": [
        "makeXRCompatible", "unpackColorSpace",
        "drawingBufferStorage", "drawingBufferFormat",
    ],
    "File": ["lastModifiedDate"],
    "EventTarget": ["when"],
    # Privacy Sandbox 归因属性（HTMLAnchor/Image/Button/Script 同族）
    "HTMLAnchorElement": [
        "attributionSrc", "interestForElement", "browsingTopics",
        "sharedStorageWritable",
    ],
    "HTMLImageElement": [
        "attributionSrc", "interestForElement", "browsingTopics",
        # F-113：F-27 的「同族」注释写了但名单漏了这名。
        "sharedStorageWritable",
    ],
    "HTMLButtonElement": ["interestForElement"],
    "HTMLScriptElement": ["attributionSrc"],
    # F-112：<area> 上的归因/兴趣组族（BCD safari_ios=false）。
    "HTMLAreaElement": ["attributionSrc", "interestForElement"],
    # F-111：iframe 的 Blink 专有 + Privacy Sandbox 反射属性。
    # loading 是 iOS 16.4+ 真机面，不在名单里。
    "HTMLIFrameElement": [
        "adAuctionHeaders", "allowPaymentRequest", "browsingTopics",
        "credentialless", "csp", "featurePolicy", "privateToken",
        "sharedStorageWritable",
    ],
    # F-103：Capture Handle 是桌面 Chrome 专有面——BCD 对 safari_ios 与
    # chrome_android 都是 false，因此这是少数「Android 也漏」的成员。
    "MediaStreamTrack": ["getCaptureHandle", "oncapturehandlechange"],
    # F-104：Canvas 2D 的 Blink 专有属性面。filter 自 iOS 18 起真机有
    # （BCD safari_ios=18），不在此列。
    "CanvasRenderingContext2D": [
        "fontKerning", "fontStretch", "fontVariantCaps", "isContextLost",
        "lang", "textRendering",
    ],
    # F-105：零散 Blink 原型成员。Element.ariaNotify 自 iOS 27 起真机有，
    # 不在此列；Element 与 Document 的 startViewTransition 见上方。
    "MediaMetadata": ["chapterInfo"],
    "MediaRecorder": ["audioBitrateMode"],
    "MediaKeys": ["getStatusForPolicy"],
    "Request": ["isHistoryNavigation", "targetAddressSpace"],
    # F-108：Blink 的媒体元素扩展。webkit*Decoded* 计数器是真机面，勿删。
    "HTMLMediaElement": ["captureStream", "controlsList", "loading"],
    # F-109：webkitSpeechRecognition 构造器本身 iOS 真机有，只清成员；
    # grammars 在 Chrome Android 33 起是真机面，Android 侧另表处理。
    "webkitSpeechRecognition": ["grammars", "phrases", "processLocally"],
    # F-114：HTMLElement 落点的事件处理器（F-08/F-71 只删了 window/Document）。
    # ontouch*/ongesture* 是 iOS 真机面，勿删。
    "HTMLElement": [
        "onbeforexrselect", "oncontextlost", "oncontextrestored",
        "onpointerrawupdate", "onscrollsnapchange", "onscrollsnapchanging",
        # F-105：Chromium 的 EditContext / 虚拟键盘策略反射属性。
        "editContext", "virtualKeyboardPolicy",
    ],
    # F-17 附带项：Chromium 的 AudioContext 输出设备选择 / 播放统计面
    # （BCD safari_ios=false）。注意这些成员实际定义在 BaseAudioContext /
    # AudioContext 各自的原型上，两边都尝试删。
    "AudioContext": ["playbackStats", "setSinkId", "sinkId", "onsinkchange"],
    "BaseAudioContext": ["playbackStats"],
    # 真机实测（/tmp/wkprobe/f25all.py）：RTCDataChannel.reliable 是 Chromium
    # 专有（WebKit 原型无此成员）。
    "RTCDataChannel": ["reliable"],
    # ---- F-40 / F-49 / F-51：对象子属性与原型方法缺口 ----
    # 键既可以是接口构造器（删原型成员），也可以是命名空间对象（删自身成员，
    # 注入器按 `window[key].prototype || window[key]` 取宿主）。
    # F-40：BCD getDisplayMedia safari_ios 27、chrome_android=false——iOS 26.x
    # 与全量 Android 真机均无此入口，Chromium 内核有（实测原生
    # `MediaDevices.prototype.getDisplayMedia` = function）。
    # F-103：setCaptureHandleConfig 是桌面 Chrome 专有面（BCD safari_ios 与
    # chrome_android 均为 false），与 getDisplayMedia 同宿主。
    "MediaDevices": ["getDisplayMedia", "setCaptureHandleConfig"],
    # F-49：CSS Paint API 从未进入 Safari；paintWorklet 是 CSS 对象自身成员
    # （实测 `Object.getOwnPropertyNames(CSS)` 含 paintWorklet），删全局名无效。
    "CSS": ["paintWorklet"],
    # F-51：HTMLSelectElement.showPicker 是 Chromium 121+ 新增（BCD
    # safari_ios=false）；input.showPicker 真机 iOS 18.4+ 有，**禁删**。
    "HTMLSelectElement": ["showPicker"],
    # F-58：Blink 把 pageX/pageY/layerX/layerY 挪到 MouseEvent.prototype，
    # UIEvent 实例上不再有（实测 `'pageX' in new UIEvent('x')` = false）；
    # WebKit 的 UIEvent.prototype 仍是 8 名口径。sourceCapabilities 是
    # Blink 专有（WebKit 无）。
    "UIEvent": ["sourceCapabilities"],
    # F-92：iOS 的 Gamepad 不带震动扩展（开关默认值见
    # _IOS_REMOVE_GLOBALS 里 GamepadHapticActuator 的取证）。WebKit 是
    # partial 接口声明，成员落在 Gamepad.prototype 上；Blink 同位置，按名删。
    "Gamepad": ["vibrationActuator"],
}


# --------------------------------------------------------------------------
# 权限查询口径（F-20）
# --------------------------------------------------------------------------
# navigator.permissions.query({name}) 的真机 iOS 结果面与桌面 Chromium 差异
# 很大，且两个方向都有。表值语义：
#   "granted" / "prompt" / "denied" → Promise.resolve({state: <值>})
#   "NotSupportedError" / "TypeError" → Promise.reject(对应 DOMException)
# 未列入的名称一律回退内核原生结果（避免把真机本就一致的面改坏）。
# 证据：BCD safari_ios 逐项 + WebKit 源码头文件（Permissions.cpp 只实现
# geolocation/notifications/push/…；未实现的名称抛 TypeError）。
_IOS_PERMISSIONS: Dict[str, str] = {
    # WebKit 从未实现的权限名 → DOMException NotSupportedError
    "accelerometer": "NotSupportedError",
    "gyroscope": "NotSupportedError",
    "magnetometer": "NotSupportedError",
    "midi": "NotSupportedError",
    "background-fetch": "NotSupportedError",
    "display-capture": "NotSupportedError",
    # 权限枚举里根本没有的名称 → TypeError（WebIDL 枚举转换失败）
    "background-sync": "TypeError",
    "clipboard-read": "TypeError",
    "clipboard-write": "TypeError",
    "persistent-storage": "TypeError",
    "local-fonts": "TypeError",
    "window-management": "TypeError",
    "payment-handler": "TypeError",
    # 真机为 prompt（未授权），桌面内核 headless 给 granted
    "screen-wake-lock": "prompt",
    "storage-access": "prompt",
    # 反向项：iOS 16.4+ 支持 Web Push，首访应为 prompt（内核桌面版无
    # push 权限名 → NotSupportedError）。UA 声明 >=16.4 时才可用，注入层
    # 无条件声明 16.4+ 机型（指纹库最低 iOS 版本已高于 16.4）。
    "push": "prompt",
}


def _ios_permissions(target_os: str) -> Dict[str, str]:
    return dict(_IOS_PERMISSIONS) if target_os == "ios" else {}


# --------------------------------------------------------------------------
# Android 的 TTS 语音表（F-45）
# --------------------------------------------------------------------------
# 真机 Chrome Android 预装 Google TTS，`speechSynthesis.getVoices()` 返回
# 数十条；宿主 macOS 上 Blink 返回的是宿主语音（婷婷/Albert/Samantha…），
# 两者都不是 Android 真机形态。表内取自真机 Chrome Android（Pixel 8,
# Android 14, 2026-10 采样）的常见子集：字段顺序为
#   [voiceURI, lang, localService, default]
# voiceURI 用 Google TTS 的 "Google <语言名>" 形态（真机也是这个名字），
# 每条都是本地引擎（localService=true，离线 TTS），只有 default 一条为 true。
_ANDROID_TTS_VOICES: List[List[Any]] = [
    ["Google 中文（简体）", "zh-CN", True, True],
    ["Google 普通话（中国大陆）", "cmn-Hans-CN", True, False],
    ["Google 粤語（香港）", "yue-HK", True, False],
    ["Google 中文（繁體）", "zh-TW", True, False],
    ["Google English (United States)", "en-US", True, False],
    ["Google English (United Kingdom)", "en-GB", True, False],
    ["Google English (India)", "en-IN", True, False],
    ["Google 日本語", "ja-JP", True, False],
    ["Google 한국어", "ko-KR", True, False],
    ["Google Deutsch", "de-DE", True, False],
    ["Google français", "fr-FR", True, False],
    ["Google español (España)", "es-ES", True, False],
    ["Google español (Estados Unidos)", "es-US", True, False],
    ["Google italiano", "it-IT", True, False],
    ["Google português (Brasil)", "pt-BR", True, False],
    ["Google português (Portugal)", "pt-PT", True, False],
    ["Google русский", "ru-RU", True, False],
    ["Google ภาษาไทย", "th-TH", True, False],
    ["Google Tiếng Việt", "vi-VN", True, False],
    ["Google Bahasa Indonesia", "id-ID", True, False],
    ["Google हिन्दी", "hi-IN", True, False],
    ["Google Türkçe", "tr-TR", True, False],
    ["Google Nederlands", "nl-NL", True, False],
    ["Google polski", "pl-PL", True, False],
    ["Google العربية", "ar-XA", True, False],
]


def _android_tts_voices(target_os: str) -> List[List[Any]]:
    """Android 身份的 TTS 语音表；iOS 保持空列表（真机 Safari 无手势时为空）。"""
    if target_os != "android":
        return []
    return [list(v) for v in _ANDROID_TTS_VOICES]


def _drop_proto_members(target_os: str,
                        os_version: Any = None) -> Dict[str, List[str]]:
    """目标平台需要从接口原型上删除的成员（F-26/F-27）。

    F-40：getDisplayMedia 是唯一一项**两端都要删**的成员——BCD
    `chrome_android=false`（72–88 曾暴露但恒 NotAllowedError，视为无）、
    `safari_ios=27`（iOS 27 起才有），而设备库 Android 机型全量、iOS 机型
    绝大多数是 26.x。iOS 27 的身份由调用方按版本回填（derive_spec 里
    直接 pop 掉 MediaDevices 项）。
    """
    if target_os != "ios":
        # F-103 / F-109：这两族在 chrome_android 上同样是 false——桌面
        # Chromium 原型被原样带进 Android 仿真。grammars 是 Chrome
        # Android 33 起的真机面，保留。
        # F-121：setSinkId/sinkId 是桌面 Chrome 49 起的音频输出选择面，
        # BCD chrome_android=false——真机 Chrome Android 不暴露。
        return {
            "MediaDevices": ["getDisplayMedia", "setCaptureHandleConfig"],
            "MediaStreamTrack": ["getCaptureHandle", "oncapturehandlechange"],
            "webkitSpeechRecognition": ["phrases", "processLocally"],
            "HTMLMediaElement": ["setSinkId", "sinkId"],
        }
    table = {k: list(v) for k, v in _IOS_DROP_PROTO_MEMBERS.items()}
    md = table.setdefault("MediaDevices", [])
    if "getDisplayMedia" not in md:
        md.append("getDisplayMedia")
    # F-122：BCD safari_ios=18.4 才有这两名，静态表不能承载版本门控
    # （iOS 18.4+ 必须保留）。18.4 以下按档案动态追加。
    version = _parse_os_version(os_version)
    if version is not None and version < (18, 4):
        hm = table.setdefault("HTMLMediaElement", [])
        hm.extend(n for n in ("setSinkId", "sinkId") if n not in hm)
    return table


# --------------------------------------------------------------------------
# 补成员清单（F-25 / F-36）：真机 WebKit 有、Chromium 内核没有的面
# --------------------------------------------------------------------------
# 取值口径全部来自 macOS WebKit 27 实测（`/tmp/wkprobe/f25v.py`），与 iPhone
# Safari 共用同一 WebKit 源码；Chromium 侧缺口用同一探针在 fp-chromium 上
# 复核（`/tmp/wkprobe/f25ch.py`）。只登记「仿真缺、真机有」的项，方向存疑的
# 一律不登记（GestureEvent.rotation、DeviceOrientationEvent.webkitCompass*、
# SpeechRecognitionAlternative/Result 在 macOS WebKit 亦缺，故不补）。
#
# protos 的 kind：fn = 方法（固定返回值），get = 只读属性，rw = 可写属性。
# interfaces 的 members：'!m' 前缀为方法，其余为属性 getter。
_IOS_ADD_SPEC: Dict[str, Any] = {
    # 复杂语义的成员（Canvas 状态改写、媒体轨道对象、事件访问器）在注入器里
    # 逐条实现，这里只放「固定返回值」的简单成员。
    "protos": [
        {"ctor": "Range", "name": "compareNode", "kind": "fn"},
        {"ctor": "TextMetrics", "name": "emHeightAscent", "kind": "get",
         "value": 0},
        {"ctor": "TextMetrics", "name": "emHeightDescent", "kind": "get",
         "value": 0},
        {"ctor": "FontFace", "name": "width", "kind": "get", "value": "normal"},
        {"ctor": "CSSStyleDeclaration", "name": "cssRules", "kind": "get",
         "value": None},
        {"ctor": "CSSStyleDeclaration", "name": "getPropertyCSSValue",
         "kind": "fn"},
        {"ctor": "MediaStreamTrack", "name": "onconfigurationchange",
         "kind": "rw", "value": None},
        {"ctor": "Worker", "name": "onmessageerror", "kind": "rw", "value": None},
        # 真机 VideoPlaybackQuality 原型比 Chromium 多 totalFrameDelay
        # （实测真机初值 0）。
        {"ctor": "VideoPlaybackQuality", "name": "totalFrameDelay",
         "kind": "get", "value": 0},
        # F-14 / F-77 D3：真机 Safari 的 Navigator.prototype.globalPrivacyControl
        # 是只读 getter（enumerable true、get.length 0、值 false）。WebKit 的
        # NavigatorGlobalPrivacyControl.idl 用 [EnabledBySetting=…] 门控、且
        # UnifiedWebPreferences.yaml 把默认值交给 embedder——Playwright WebKit
        # 因此看不到它，而 Safari 打开该设置。Blink 侧完全没有这个成员。
        {"ctor": "Navigator", "name": "globalPrivacyControl", "kind": "get",
         "value": False},
        # F-84：真机 WebKit 的 AudioSession 是 Singleton 双暴露面——window 上是
        # 接口对象，navigator.audioSession 是同一实例（实测 /tmp/f77p/audio.py：
        # 原型只读 getter、enumerable true、零自有属性、type 初值 "auto"、
        # `navigator.audioSession instanceof AudioSession` 为真、赋值被静默忽略）。
        {"ctor": "Navigator", "name": "audioSession", "singleton": "AudioSession",
         "init": {"type": "auto"}},
        # F-107：真机 Safari 18.4 起 MediaSession 有 setScreenshareActive
        # （WebKit 26.5 实测：原型自有、enumerable/writable、length 1、返回
        # undefined）；Chromium 内核没有，且 BCD chrome_android=false，只补 iOS。
        {"ctor": "MediaSession", "name": "setScreenshareActive", "kind": "fn",
         "value": None},
    ],
    "statics": [
        {"ctor": "DataView", "name": "BYTES_PER_ELEMENT", "value": 1},
        {"ctor": "RegExp", "name": "multiline", "value": False},
    ],
    "constants": [
        {"ctor": "MouseEvent", "name": "WEBKIT_FORCE_AT_MOUSE_DOWN", "value": 1},
        {"ctor": "MouseEvent", "name": "WEBKIT_FORCE_AT_FORCE_MOUSE_DOWN",
         "value": 2},
        {"ctor": "Range", "name": "NODE_BEFORE", "value": 0},
        {"ctor": "Range", "name": "NODE_AFTER", "value": 1},
        {"ctor": "Range", "name": "NODE_BEFORE_AND_AFTER", "value": 2},
        {"ctor": "Range", "name": "NODE_INSIDE", "value": 3},
        # CSS 遗留值常量：这些接口在 Chromium 里整体不存在，原型上的常量
        # 也要补（真机实测：构造函数与原型两侧同值）。
        {"ctor": "CSSValue", "name": "CSS_INHERIT", "value": 0},
        {"ctor": "CSSValue", "name": "CSS_PRIMITIVE_VALUE", "value": 1},
        {"ctor": "CSSValue", "name": "CSS_VALUE_LIST", "value": 2},
        {"ctor": "CSSValue", "name": "CSS_CUSTOM", "value": 3},
        {"ctor": "SVGViewSpec", "name": "SVG_ZOOMANDPAN_UNKNOWN", "value": 0},
        {"ctor": "SVGViewSpec", "name": "SVG_ZOOMANDPAN_DISABLE", "value": 1},
        {"ctor": "SVGViewSpec", "name": "SVG_ZOOMANDPAN_MAGNIFY", "value": 2},
        # F-155 残项：WebKit 的 MediaError 多一个 MEDIA_ERR_ENCRYPTED=5
        # （/tmp/f404 实测：构造函数与原型两侧都有、可枚举、不可写）。
        {"ctor": "MediaError", "name": "MEDIA_ERR_ENCRYPTED", "value": 5},
    ] + (
        # CSSPrimitiveValue / SVGPathSeg 的枚举族：真机实测为连续 0..N，
        # 顺序即 WebKit CSSPrimitiveValue.idl / SVGPathSeg.idl 的声明顺序。
        [{"ctor": "CSSPrimitiveValue", "name": n, "value": i}
         for i, n in enumerate([
             "CSS_UNKNOWN", "CSS_NUMBER", "CSS_PERCENTAGE", "CSS_EMS",
             "CSS_EXS", "CSS_PX", "CSS_CM", "CSS_MM", "CSS_IN", "CSS_PT",
             "CSS_PC", "CSS_DEG", "CSS_RAD", "CSS_GRAD", "CSS_MS", "CSS_S",
             "CSS_HZ", "CSS_KHZ", "CSS_DIMENSION", "CSS_STRING", "CSS_URI",
             "CSS_IDENT", "CSS_ATTR", "CSS_COUNTER", "CSS_RECT",
             "CSS_RGBCOLOR"])]
        + [{"ctor": "SVGPathSeg", "name": n, "value": i}
           for i, n in enumerate([
               "PATHSEG_UNKNOWN", "PATHSEG_CLOSEPATH", "PATHSEG_MOVETO_ABS",
               "PATHSEG_MOVETO_REL", "PATHSEG_LINETO_ABS",
               "PATHSEG_LINETO_REL", "PATHSEG_CURVETO_CUBIC_ABS",
               "PATHSEG_CURVETO_CUBIC_REL", "PATHSEG_CURVETO_QUADRATIC_ABS",
               "PATHSEG_CURVETO_QUADRATIC_REL", "PATHSEG_ARC_ABS",
               "PATHSEG_ARC_REL", "PATHSEG_LINETO_HORIZONTAL_ABS",
               "PATHSEG_LINETO_HORIZONTAL_REL", "PATHSEG_LINETO_VERTICAL_ABS",
               "PATHSEG_LINETO_VERTICAL_REL",
               "PATHSEG_CURVETO_CUBIC_SMOOTH_ABS",
               "PATHSEG_CURVETO_CUBIC_SMOOTH_REL",
               "PATHSEG_CURVETO_QUADRATIC_SMOOTH_ABS",
               "PATHSEG_CURVETO_QUADRATIC_SMOOTH_REL"])]
    ),
    # 事件处理属性。真机实测（`/tmp/f77p/hosts.py`，WebKit 27.2 / has_touch）：
    # copy/cut/paste + webkitmouseforce* + ontouchforcechange + ontouch* 在
    # Window / Document / HTMLElement / SVGElement 四处都有；HTMLBodyElement
    # 继承这些成员（不单独补）。
    # 可枚举性：copy/cut/paste 为 enumerable:true，其余九个（Force Touch 五个 +
    # ontouch 四个）为 enumerable:false —— 写成 `enum: False` 的行走后者，
    # 其余沿用默认 true。ontouch* 在内核里由触控仿真原生安装且是可枚举的，
    # 注入器对「已存在但可枚举性不符」的自有访问器只重定义 enumerable。
    "handlers": (
        [{"ctor": c, "name": n, "enum": n in ("oncopy", "oncut", "onpaste")}
         for c in ("window", "Document", "HTMLElement", "SVGElement")
         for n in ("oncopy", "oncut", "onpaste",
                   "onwebkitmouseforcechanged", "onwebkitmouseforcedown",
                   "onwebkitmouseforceup", "onwebkitmouseforcewillbegin",
                   "ontouchforcechange",
                   "ontouchstart", "ontouchmove", "ontouchend", "ontouchcancel")]
        # F-115 / F-117：WebKit 把 onbeforecopy/cut/paste 放在 HTMLElement、
        # onfocusin/onfocusout 放在 Element（WebKit Element.idl 对后者是
        # [NotEnumerable]），Blink 的落点不同或整体缺失。WebKit 26.5 实测这
        # 五名均为 enumerable:false 的访问器。
        # 落点只写 WebKit 真实宿主：Element 是 HTMLElement 的父类，补在父类
        # 会让 document.documentElement 也命中，反向制造判据。
        + [{"ctor": "HTMLElement", "name": n, "enum": False}
           for n in ("onbeforecopy", "onbeforecut", "onbeforepaste")]
        + [{"ctor": "Element", "name": n, "enum": False}
           for n in ("onfocusin", "onfocusout")]
        # F-118：WebKit 的 Element.idl 里另有 5 名非标准处理器，全部
        # [NotEnumerable]（WebKit 26.5 实测 enumerable:false）。HTMLBodyElement
        # / SVGElement / MathMLElement 按继承得到，不重复补。
        # 条件项（onwebkitneedkey 依赖 LegacyEncryptedMediaAPIEnabled、
        # presentationmode 依赖 VIDEO_PRESENTATION_MODE、AirPlay 两项依赖
        # WIRELESS_PLAYBACK_TARGET）在 WebKit 26.5 运行期全部暴露。
        + [{"ctor": "Element", "name": n, "enum": False}
           for n in ("onbeforeload", "onwebkitneedkey",
                     "onwebkitpresentationmodechanged",
                     "onwebkitcurrentplaybacktargetiswirelesschanged",
                     "onwebkitplaybacktargetavailabilitychanged")]
    ),
    # F-29 / F-30：表单控件默认样式。真机 iOS WebKit 的 UA 样式表把控件
    # 字体定为 system-ui（macOS 宿主 Chromium 是 Arial），控件高度
    # button 18 / input 21 / select 20 / textarea 32（实测
    # /tmp/wkprobe/forms.py）。只作用于作者未显式设过的控件。
    # F-29 / F-30：表单控件默认样式（真机 iOS WebKit 实测 /tmp/wkprobe/forms2.py）。
    # 整段放进 @layer：层内规则一律低于任何未分层作者规则，所以站点自己写的
    # `button{...}` 仍然覆盖，只有「作者没设过」的属性才落到这里的真机口径。
    # 不用 !important —— 那会连作者的普通规则一起压掉，反而暴露。
    # line-height:13px 是控件高度的真正来源：WebKit 的 button 高度 = 4 border
    # + 1 padding + 13 行高 = 18px。Chromium 默认行高按字体族解析（CJK 文本
    # 会到 16px 而撑成 21px），钉死行高后 button/textarea 与真机逐像素一致。
    "uaFormCss": (
        "@layer wkua{"
        "button,input,select,textarea{font-family:system-ui;font-size:11px;"
        "line-height:13px}"
        # F-46：Blink UA sheet 的两处 iOS 穿帮面。真机 WebKit 实测
        # （/tmp/f40wk/probe5.py）：input overflow=visible（Blink 是 clip）、
        # select font-family=system-ui（Blink 落到宿主 Arial）；textarea 的
        # monospace 由上面的 font-family 规则覆盖。
        "input{overflow:visible}"
        # F-47：真机 iOS Safari 正文默认不可选（长按不弹选择菜单），computed
        # `-webkit-user-select` 为 none；内核宿主是 auto。放 @layer 里，站点
        # 自己写的 user-select 仍然优先。表单控件显式回 text（真机控件内可选中）。
        "body{-webkit-user-select:none;user-select:none}"
        "input,textarea{-webkit-user-select:text;user-select:text}"
        "button,input[type=button],input[type=submit],input[type=reset]{"
        "padding:0 6px 1px;border:2px outset rgb(192,192,192);"
        "border-radius:0;background-color:rgb(192,192,192);"
        "color:rgba(0,0,0,0.847)}"
        "input:not([type=button]):not([type=submit]):not([type=reset])"
        ":not([type=checkbox]):not([type=radio]):not([type=range])"
        ":not([type=color]):not([type=file]){padding:2px 5px;"
        "border:2px inset rgb(128,128,128);border-radius:0;"
        "background-color:rgb(255,255,255);color:rgb(0,0,0)}"
        "select{border:1px solid rgba(0,0,0,0.847);border-radius:5px;"
        "background-color:rgb(255,255,255);color:rgba(0,0,0,0.847);height:20px}"
        "textarea{border:1px solid rgb(0,0,0);border-radius:0;"
        "background-color:rgb(255,255,255);color:rgb(0,0,0);padding:2px 5px}"
        "}"
    ),
    # 真机控件高度（实测），供验证脚本比对。
    "uaHeights": {"button": 18, "input": 21, "select": 20, "textarea": 32},
    # 全局项（WebKit 27.2 实测 /tmp/f77p/shape2.py、odb5.py）：
    #   webkitIndexedDB —— 只读 getter（无 setter）、enumerable true、
    #     SameObject（=== indexedDB）；
    #   openDatabase —— get/set 访问器、enumerable false；未赋值读作
    #     "undefined"（真机 getter 返回 JSC 的内部函数对象，typeof 亦为
    #     "undefined"；V8 无法制造该对象，退化为真 undefined）；
    #   safari —— 可写数据属性（w/e/c 全 true），值 {pushNotification}，
    #     其 pushNotification 自带 toString → "[object SafariRemoteNotification]"。
    "globals": {
        "webkitIndexedDB": {"alias": "indexedDB"},
        "openDatabase": {"accessor": "undefined"},
        "safari": {"object": {"pushNotification": {
            "toStringTag": "SafariRemoteNotification"}}},
    },
    # 接口对象：construct='ok' 表示真机可直接 new（其余抛 Illegal constructor），
    # length 为构造器参数个数；members 为原型成员（'!' 前缀 = 方法，'=' 前缀
    # = 可写属性，其余为只读 getter）；extends 为真机原型链父接口（实测
    # /tmp/wkprobe/f25chain.py）；init 为实例初值，argInit 为「构造参数 →
    # 成员」的位置映射（'' = 该位不映射）。
    "interfaces": {
        "AudioSession": {"construct": "illegal", "length": 0,
                         "extends": "EventTarget",
                         "members": ["=type"]},
        "MediaController": {"construct": "ok", "length": 0,
                            "extends": "EventTarget",
                            # 真机 new MediaController() 的初值（实测）。
                            "init": {"buffered": "@TimeRanges",
                                     "seekable": "@TimeRanges",
                                     "played": "@TimeRanges",
                                     "duration": 0, "currentTime": 0,
                                     "paused": False,
                                     "playbackState": "waiting",
                                     "defaultPlaybackRate": 1,
                                     "playbackRate": 1, "volume": 1,
                                     "muted": False},
                            "members": ["buffered", "seekable", "duration",
                                        "=currentTime", "paused", "played",
                                        "playbackState", "=defaultPlaybackRate",
                                        "=playbackRate", "=volume", "=muted",
                                        "!play", "!pause", "!unpause"]},
        "XMLHttpRequestProgressEvent": {
            "construct": "illegal", "length": 0,
            "extends": "ProgressEvent",
            "members": ["position", "totalSize"]},
        "SVGViewSpec": {"construct": "illegal", "length": 0,
                        "members": ["transform", "viewBoxString",
                                    "preserveAspectRatioString",
                                    "transformString", "viewBox",
                                    "preserveAspectRatio", "=zoomAndPan"]},
        "CSSValue": {"construct": "illegal", "length": 0,
                     "members": ["=cssText", "cssValueType"]},
        "CSSPrimitiveValue": {
            "construct": "illegal", "length": 0, "extends": "CSSValue",
            "members": ["primitiveType", "!setFloatValue", "!getFloatValue",
                        "!setStringValue", "!getStringValue", "!getCounterValue",
                        "!getRectValue", "!getRGBColorValue"]},
        "CSSValueList": {"construct": "illegal", "length": 0,
                         "extends": "CSSValue",
                         "members": ["length", "!item"]},
        "RGBColor": {"construct": "illegal", "length": 0,
                     "members": ["red", "green", "blue", "alpha"]},
        "Rect": {"construct": "illegal", "length": 0,
                 "members": ["top", "right", "bottom", "left"]},
        "Counter": {"construct": "illegal", "length": 0,
                    "members": ["identifier", "listStyle", "separator"]},
        "SVGPathSeg": {"construct": "illegal", "length": 0,
                       "members": ["pathSegType", "pathSegTypeAsLetter"]},
        "MathMLMathElement": {"construct": "illegal", "length": 0,
                              "extends": "MathMLElement", "members": []},
        # 真机 type 取第 4 个参数（实测 new DataCue(5, 9, 'V', 'text/plain')
        # → value 'V'、type 'text/plain'；缺省为 ''）。
        "DataCue": {"construct": "args", "length": 3,
                    "extends": "TextTrackCue",
                    "init": {"value": "", "type": "", "data": None},
                    "argInit": ["", "", "value", "type"],
                    "members": ["=value", "type", "=data"]},
        "VTTRegion": {"construct": "ok", "length": 0,
                      # 真机 new VTTRegion() 的初值（实测）。
                      "init": {"id": "", "width": 100, "lines": 3,
                               "regionAnchorX": 0, "regionAnchorY": 100,
                               "viewportAnchorX": 0, "viewportAnchorY": 100,
                               "scroll": ""},
                      "members": ["=id", "=width", "=lines", "=regionAnchorX",
                                  "=regionAnchorY", "=viewportAnchorX",
                                  "=viewportAnchorY", "=scroll"]},
        "ManagedMediaSource": {"construct": "ok", "length": 0,
                               "extends": "MediaSource",
                               # 实测：quality 'high'、streaming false。
                               # 构造参数 MediaSourceInit 在 WebKit 里被完全忽略
                               # （new ManagedMediaSource({quality:'low'}) 仍读
                               # 'high'），故 init 只作成员默认值，不进 seed。
                               "init": {"quality": "high", "streaming": False},
                               # F-152：实例必须建在真实 MediaSource 上，
                               # 否则继承来的 readyState/duration/sourceBuffers
                               # 全是 Illegal invocation，URL.createObjectURL
                               # 也会因过不了 Blink 内部类型检查而失败。
                               "realBase": True,
                               # F-152：字典参数遇到非对象 → TypeError('Type error')。
                               "dictArg": True,
                               "members": ["quality", "=onqualitychange",
                                           "streaming", "=onstartstreaming",
                                           "=onendstreaming"],
                               # F-130：WebKit 里 ManagedMediaSource 继承
                               # MediaSource，静态 isTypeSupported 必须存在
                               # （主世界表此前漏了这一项，Worker 表有）。
                               "statics": ["!isTypeSupported"]},
        "ManagedSourceBuffer": {"construct": "illegal", "length": 0,
                                "extends": "SourceBuffer",
                                "members": ["=onbufferedchange"]},
        "BufferedChangeEvent": {"construct": "args", "length": 2,
                                "extends": "Event",
                                "members": ["addedRanges", "removedRanges"]},
        "VideoTrack": {"construct": "illegal", "length": 0,
                       "members": ["id", "kind", "label", "language",
                                   "=selected", "configuration", "sourceBuffer"]},
        "AudioTrack": {"construct": "illegal", "length": 0,
                       "members": ["id", "kind", "label", "language",
                                   "=enabled", "configuration", "sourceBuffer"]},
        "VideoTrackList": {"construct": "illegal", "length": 0,
                           "extends": "EventTarget",
                           "members": ["length", "selectedIndex", "=onchange",
                                       "=onaddtrack", "=onremovetrack", "!item",
                                       "!getTrackById"]},
        "AudioTrackList": {"construct": "illegal", "length": 0,
                           "extends": "EventTarget",
                           "members": ["length", "=onchange", "=onaddtrack",
                                       "=onremovetrack", "!item",
                                       "!getTrackById"]},
        # F-154：Blink 的 TextTrackList / SourceBufferList 缺 item()，
        # 且成员的接收者校验走内部槽（真机 Object.create(原型) 不算实例）。
        # 这里只补缺的成员，既有成员由页面脚本的 __wkListSetup 统一接管。
        "TextTrackList": {"construct": "illegal", "length": 0,
                          "repair": True, "extends": "EventTarget",
                          "members": ["length", "=onaddtrack", "=onchange",
                                      "=onremovetrack", "!item",
                                      "!getTrackById"]},
        "SourceBufferList": {"construct": "illegal", "length": 0,
                             "repair": True, "extends": "EventTarget",
                             "members": ["length", "=onaddsourcebuffer",
                                         "=onremovesourcebuffer", "!item"]},
        # F-155 残项：真机 MediaError 比 Blink 多 MEDIA_ERR_ENCRYPTED（=5），
        # 构造文案是裸 Illegal constructor，接收者校验也走真机文案
        # （/tmp/f404）。常量走 constants 表；原型成员顺序因 Blink 的
        # 常量不可配置而无法重排，属既有 own-name 顺序残差。
        "MediaError": {"construct": "illegal", "length": 0,
                       "repair": True,
                       "members": ["code", "message"]},
        "AudioTrackConfiguration": {
            "construct": "illegal", "length": 0,
            "members": ["codec", "sampleRate", "numberOfChannels", "bitrate"]},
        "MediaRecorderErrorEvent": {"construct": "args", "length": 2,
                                    "extends": "Event",
                                    "members": ["error"]},
        "OverconstrainedErrorEvent": {"construct": "args", "length": 1,
                                      "extends": "Event",
                                      "members": ["error"]},
        "FileSystemEntry": {"construct": "illegal", "length": 0,
                            "members": ["isFile", "isDirectory", "name",
                                        "fullPath", "filesystem", "!getParent"]},
        "FileSystemDirectoryEntry": {
            "construct": "illegal", "length": 0,
            "extends": "FileSystemEntry",
            "members": ["!createReader", "!getFile", "!getDirectory"]},
        "FileSystemFileEntry": {"construct": "illegal", "length": 0,
                                "extends": "FileSystemEntry",
                                "members": ["!file"]},
        "FileSystemDirectoryReader": {"construct": "illegal", "length": 0,
                                      "members": ["!readEntries"]},
        "Report": {"construct": "illegal", "length": 0,
                   "members": ["type", "url", "body", "!toJSON"]},
        # 混合体：不带 new 是 args 文案，带 new 是裸 Illegal constructor
        # （实测 /tmp/wkprobe/f25nonew.py）。
        "FontFaceSet": {"construct": "illegal", "length": 1,
                        "noNew": "args",
                        "extends": "EventTarget",
                        "members": ["size", "=onloading", "=onloadingdone",
                                    "=onloadingerror", "ready", "status",
                                    "!has", "!add", "!delete", "!clear",
                                    "!load", "!check", "!entries", "!keys",
                                    "!values", "!forEach"]},
        # repair=True：Chromium 已有同名接口，但构造语义是 Blink 文案
        # （`Failed to construct 'X': …`），需要改成 WebKit 的裸文案；
        # 且缺 entries/keys/values/forEach 四个迭代方法。
        "StylePropertyMapReadOnly": {
            "construct": "illegal", "length": 0, "repair": True,
            "members": ["size", "!get", "!getAll", "!has", "!entries",
                        "!keys", "!values", "!forEach"]},
        "CSSMathValue": {"construct": "illegal", "length": 0, "repair": True,
                         "extends": "CSSNumericValue",
                         "members": ["operator"]},
        "CSSNumericValue": {"construct": "illegal", "length": 0, "repair": True,
                            "extends": "CSSStyleValue",
                            "members": ["!add", "!sub", "!mul", "!div", "!min",
                                        "!max", "!equals", "!to", "!toSum",
                                        "!type"]},
        "CSSUnitValue": {"construct": "args", "length": 2, "repair": True,
                         "extends": "CSSNumericValue",
                         "members": ["=value", "unit"]},
        "CSSKeywordValue": {"construct": "args", "length": 1, "repair": True,
                            "extends": "CSSStyleValue",
                            "members": ["=value"]},
        "CSSImageValue": {"construct": "illegal", "length": 0, "repair": True,
                          "extends": "CSSStyleValue",
                          "members": []},
        "CSSTransformComponent": {"construct": "illegal", "length": 0,
                                  "repair": True,
                                  "members": ["=is2D", "!toMatrix",
                                              "!toString"]},
        "CSSStyleValue": {"construct": "illegal", "length": 0, "repair": True,
                          "members": ["!toString"]},
        "SpeechRecognitionResultList": {
            "construct": "illegal", "length": 0,
            "members": ["length", "!item"]},
        # F-77 D3：WebKit 27.2 实测（/tmp/f77p/shape2.py）三者的形态——
        # 均为 Illegal constructor、length 0、原型直挂 Object.prototype。
        # SpeechRecognitionResult.item 是 writable 数据属性（length 1），
        # 其余成员是只读 getter（enumerable true）。
        "SpeechRecognitionResult": {
            "construct": "illegal", "length": 0,
            "members": ["length", "isFinal", "!item"]},
        "SpeechRecognitionAlternative": {
            "construct": "illegal", "length": 0,
            "members": ["transcript", "confidence"]},
        # Apple Pay JS：ApplePayError 可 new（length 1，缺参抛 Not enough
        # arguments），四个成员均为 get/set（enumerable true）。真机还校验
        # code / contactField / domain 的枚举值，补丁只查参数个数——检测站
        # 极少构造 ApplePayError，该差异可接受（清单 §28 D3 备忘）。
        # 1 参构造的初值实测 {contactField:null, message:"", domain:null}。
        "ApplePayError": {
            "construct": "args", "length": 1,
            "init": {"contactField": None, "message": "", "domain": None},
            "argInit": ["code", "contactField", "message", "domain"],
            "members": ["=code", "=contactField", "=message", "=domain"]},
        # 旧 WebKit 文件系统入口（与 FileSystemEntry 族的 File and Directory
        # Entries 是不同 API）：Illegal constructor，原型 name / root 只读。
        "FileSystem": {"construct": "illegal", "length": 0,
                       "members": ["name", "root"]},
        # 真机 TextTrack 比 Chromium 多 inBandMetadataTrackDispatchType 与
        # sourceBuffer 两个属性。
        "TextTrack": {"construct": "illegal", "length": 0, "repair": True,
                      "extends": "EventTarget",
                      "members": ["kind", "label", "language", "id",
                                  "inBandMetadataTrackDispatchType", "=mode",
                                  "cues", "activeCues", "=oncuechange",
                                  "sourceBuffer", "!addCue", "!removeCue"]},
        # 真机是可 new 的（length 3，缺参抛 Not enough arguments），
        # Chromium 里 TextTrackCue 是 Illegal constructor 且 length 0。
        "TextTrackCue": {"construct": "args", "length": 3, "repair": True,
                         "extends": "EventTarget",
                         "members": ["track", "=id", "=startTime", "=endTime",
                                     "=pauseOnExit", "=onenter", "=onexit",
                                     "!getCueAsHTML"]},
    },
}


def _worker_add_members(target_os: str) -> Dict[str, Any]:
    """Worker 作用域的补成员清单（F-37 / F-38）；非 iOS 返回空表。

    Worker 是独立 JS 全局，页面的 addMembers 到不了（见
    mobile_cdp_injector.build_worker_patch）。表结构与页面版本一致，但
    extends 指向 Worker 作用域内的同名接口——同一份表在页面里会解析到
    window，在 Worker 里解析到 self。
    """
    if target_os != "ios":
        return {}
    return {
        "interfaces": {k: dict(v) for k, v in _IOS_WORKER_ADD_INTERFACES.items()},
        "globalDrops": list(_IOS_WORKER_GLOBAL_DROPS),
        "navProto": list(_IOS_WORKER_NAV_PROTO),
        # Worker 独有的全局项：真机 WebKit Worker 的 ServiceWorkerContainer
        # 是规范内接口（Chromium 的 Worker 全局没有它，见 navProto 的
        # serviceWorker 成员），必须在这里补建，否则 navProto 里的
        # serviceWorker getter 读不到容器对象。
        "globals": dict(_IOS_WORKER_ADD_GLOBALS),
    }


def _parse_os_version(value: Any) -> Optional[tuple]:
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


# F-119：WebKit 把 WindowEventHandlers 转发到 body/frameset，BCD Window
# 事件记 Safari 18.2，故按 profile 版本门控；低于 18.2 不补。
_PAGESWAP_MIN = (18, 2)


def _ios_handlers(version: Optional[tuple]) -> List[Dict[str, Any]]:
    rows = [dict(r) for r in _IOS_ADD_SPEC["handlers"]]
    if version is None or version >= _PAGESWAP_MIN:
        rows += [{"ctor": c, "name": n}
                 for c in ("HTMLBodyElement", "HTMLFrameSetElement")
                 for n in ("onpageswap", "onpagereveal")]
    return rows


def _add_members(target_os: str, os_version: Any = None) -> Dict[str, Any]:
    if target_os != "ios":
        return {}
    version = _parse_os_version(os_version)
    return {
        "protos": [dict(r) for r in _IOS_ADD_SPEC["protos"]],
        "statics": [dict(r) for r in _IOS_ADD_SPEC["statics"]],
        "constants": [dict(r) for r in _IOS_ADD_SPEC["constants"]],
        "handlers": _ios_handlers(version),
        "globals": dict(_IOS_ADD_SPEC["globals"]),
        "uaFormCss": _IOS_ADD_SPEC["uaFormCss"],
        "uaHeights": dict(_IOS_ADD_SPEC["uaHeights"]),
        "interfaces": {k: {"construct": v["construct"], "length": v["length"],
                           "noNew": v.get("noNew"),
                           "repair": bool(v.get("repair")),
                           "extends": v.get("extends"),
                           "realBase": bool(v.get("realBase")),
                           "dictArg": bool(v.get("dictArg")),
                           "init": dict(v.get("init") or {}),
                           "argInit": list(v.get("argInit") or []),
                           "statics": list(v.get("statics") or []),
                           "members": list(v["members"])}
                       for k, v in _IOS_ADD_SPEC["interfaces"].items()},
    }



# ------------------------------------------------------------------ 指纹生成
def generate_mobile_fingerprint(
    target_os: str,
    manufacturer: Optional[str] = None,
    mode: str = "generate",
    browser: Optional[str] = None,
) -> Dict[str, Any]:
    """生成一套移动端指纹（基于真实机型库）。

    Parameters:
        target_os: "android" or "ios"
        manufacturer: 可选品牌（Samsung/Google/Xiaomi/OnePlus/Honor/Apple 等），
                      无匹配时回退为该平台全库，实际结果见返回的 manufacturer
        mode: "generate"（版本随机）/ "preset"（版本取机型当前主流组合）
        browser: 浏览器口径。Android 仅 "chrome"（默认）；iOS 可选
                 "safari"（默认）/"chrome_ios"（Chrome iOS，CriOS）。
    """
    if target_os not in ("android", "ios"):
        raise ValueError(f"不支持的移动端目标 OS: {target_os}")
    mode = "preset" if mode == "preset" else "generate"
    browser_choice = _normalize_browser(target_os, browser)

    device = _pick_device(target_os, manufacturer)
    screen_meta = device["screen"]
    css_w, css_h = int(screen_meta["css_w"]), int(screen_meta["css_h"])
    viewport_h = int(screen_meta["viewport_h"])
    dpr = float(screen_meta["dpr"])

    os_version = _pick_os_version(device, mode)
    is_tablet = device["type"] == "tablet"
    max_touch_points = int(device.get("max_touch_points") or 5)
    memory_class = _memory_class(int(device.get("ram_gb") or 4))

    if target_os == "android":
        android_browser = _pick_android_browser(mode)
        major = str(android_browser["major"]) 
        mobile_token = "" if is_tablet else "Mobile "
        user_agent = (
            f"Mozilla/5.0 (Linux; Android {os_version}; {device['model']}) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 "
            f"{mobile_token}Safari/537.36"
        )
        # F-41：真机 Chrome Android 的 navigator.platform 按 ABI 取值——
        # 32 位 armv8l、64 位 aarch64（Chrome 111 起）。现役机型全 arm64，
        # 故默认 aarch64。旧值 "Linux armv81" 不匹配任何真机/构建串。
        nav_platform = "Linux aarch64"
        vendor = "Google Inc."
        ua_ch: Optional[Dict[str, Any]] = _android_ua_ch(device, os_version, android_browser)
        browser_version = str(android_browser["full"])
        platform_version_ch = f"{os_version}.0.0"
        battery: Optional[Dict[str, Any]] = _generate_battery_api()
        device_memory: Optional[int] = memory_class
        remove_props: List[str] = list(_ANDROID_REMOVE_NAV_PROPS)
        # Android 构建无内置 PDF 插件（Chromium 构建开关 enable_pdf=!is_android）：
        # 真机 Chrome Android 的 plugins/mimeTypes 为空、pdfViewerEnabled 恒 false。
        # 宿主桌面内核是 5 个 PDF 插件 + true，是「桌面装手机」的单行级证据。
        empty_plugins = True
        # NetworkInformation.type：仅 Android 实现（MDN BCD chrome_android=38；
        # 桌面 chrome 仅 ChromeOS 有）——桌面内核 'type' in navigator.connection
        # 为 false，同样是单行级证据。真机取值 wifi/cellular/ethernet/…（枚举）。
        # 环境经代理出网、无蜂窝信息，取最常见且稳定的 'wifi'。
        connection_type: Optional[str] = "wifi"
        # downlinkMax 与 type 同属 NetInfoDownlinkMax 开关，仅 Android 实现；
        # 取值见 _ANDROID_DOWNLINK_MAX_MBPS（内核子类型表的 Wi-Fi 值域）。
        downlink_max: Optional[float] = _ANDROID_DOWNLINK_MAX_MBPS
    else:
        os_token = os_version.replace(".", "_")
        safari_version = ".".join(os_version.split(".")[:2])  # iOS 27.0.1 → Safari 27.0
        if browser_choice == "chrome_ios":
            # Chrome iOS（CriOS）：WKWebView 壳 + 系统 WebKit。UA 为 reduced
            # 形态 CriOS/<major>.0.0.0（与 Android 侧 Chrome/<major>.0.0.0
            # 同法），版本随内核实测引擎；无 UA-CH / Battery（WKWebView 无
            # 这些 API）。页面世界检测面差异（__gCrWeb / 改写痕迹 /
            # webkit.messageHandlers / 无 window.chrome）由运行期注入层
            # （mobile_cdp_injector 的 iosChrome 段）补齐。
            ios_browser = _pick_ios_browser()
            crios_major = str(ios_browser["major"])
            browser_version = str(ios_browser["full"])
            ua_segment = f"CriOS/{crios_major}.0.0.0"
        else:
            browser_version = safari_version
            ua_segment = f"Version/{safari_version}"
        if is_tablet:
            user_agent = (
                f"Mozilla/5.0 (iPad; CPU OS {os_token} like Mac OS X) "
                f"AppleWebKit/605.1.15 (KHTML, like Gecko) {ua_segment} "
                f"Mobile/15E148 Safari/604.1"
            )
            nav_platform = "iPad"
        else:
            user_agent = (
                f"Mozilla/5.0 (iPhone; CPU iPhone OS {os_token} like Mac OS X) "
                f"AppleWebKit/605.1.15 (KHTML, like Gecko) {ua_segment} "
                f"Mobile/15E148 Safari/604.1"
            )
            nav_platform = "iPhone"
        vendor = "Apple Computer, Inc."
        ua_ch = None  # Safari / CriOS 均不暴露 UA-CH
        platform_version_ch = os_version if os_version.count(".") >= 2 else f"{os_version}.0"
        battery = None  # WKWebView 无 Battery API
        device_memory = None
        remove_props = list(_IOS_REMOVE_NAV_PROPS)
        empty_plugins = True
        connection_type = None  # Safari / CriOS 均无 navigator.connection（含 type）
        downlink_max = None

    app_version = user_agent.split("Mozilla/", 1)[-1]

    screen = {
        "availHeight": int(screen_meta["h"]),
        "availWidth": int(screen_meta["w"]),
        "availTop": 0,
        "availLeft": 0,
        "colorDepth": 24,
        "height": int(screen_meta["h"]),
        "pixelDepth": 24,
        "width": int(screen_meta["w"]),
        "devicePixelRatio": dpr,
        # 浏览器可见的 CSS 尺寸（物理 / DPR）与首屏视口高度（扣除浏览器 UI）
        "cssWidth": css_w,
        "cssHeight": css_h,
        "cssViewportHeight": viewport_h,
    }

    navigator = {
        "userAgent": user_agent,
        "userAgentData": ua_ch,
        "doNotTrack": None,
        "appCodeName": "Mozilla",
        "appName": "Netscape",
        "appVersion": app_version,
        "oscpu": None,  # Chrome/Safari 均不暴露 oscpu（Firefox 专有属性）
        "webdriver": False,
        "language": "zh-CN",
        "languages": ["zh-CN", "zh", "en-US", "en"],
        "platform": nav_platform,
        "hardwareConcurrency": int(device.get("cpu_cores") or 8),
        "deviceMemory": device_memory,
        "product": "Gecko",
        "productSub": "20030107",
        "vendor": vendor,
        "vendorSub": "",
        "maxTouchPoints": max_touch_points,
        "extraProperties": {},
        "offline": False,
        "cookieEnabled": True,
    }

    video_card = {
        "vendor": device["gpu"]["vendor"],
        "renderer": device["gpu"]["renderer"],
    }

    audio_codecs = {
        "aac": "audio/mp4a-enc;codecs=aac",
        "flac": "audio/flac;codecs=flac",
        "mp3": "audio/mpeg;codecs=mp3",
        "opus": "audio/ogg;codecs=opus",
        "vorbis": "audio/ogg;codecs=vorbis",
    }

    video_codecs = {
        "av1": "video/mp4;codecs=av01.0.05M.08",
        "h264": "video/mp4;codecs=avc1.42E01E",
        "vp8": "video/webm;codecs=vp8",
        "vp9": "video/webm;codecs=vp09.00.10.08",
    }

    plugins_data = {
        "pdfViewer": "application/pdf",
        "widevine": "video/mp4",
        "nacl": "application/x-nacl",
    }

    multimedia_devices = [
        "microphone",
        "speaker",
        "camera-rear",
        "camera-front",
        "video-output",
    ]

    fonts = _generate_mobile_font_subset(target_os, full=(mode == "preset"))

    fingerprint = {
        "screen": screen,
        "navigator": navigator,
        "headers": {},  # 由 HeadersGenerator 填充
        "videoCodecs": video_codecs,
        "audioCodecs": audio_codecs,
        "pluginsData": plugins_data,
        "battery": battery,
        "videoCard": video_card,
        "multimediaDevices": multimedia_devices,
        "fonts": fonts,
        "mockWebRTC": False,
        "slim": False,
        # 实际配对结果（供 create_mobile_fingerprint / 摘要使用）
        "manufacturer": device["brand"],
        "deviceModel": device["market_name"],
        # 移动端特有扩展：运行期注入层（mobile_cdp_injector）据此落地
        "mobileExtra": {
            "model": str(device["model"]),
            "marketName": device["market_name"],
            "deviceType": device["type"],
            "released": device.get("released"),
            "osVersion": os_version,
            "browser": browser_choice,
            "browserVersion": browser_version,
            "platformVersionCh": platform_version_ch,
            "memoryClass": memory_class,
            "hasUaCh": ua_ch is not None,
            "hasBattery": battery is not None,
            "emptyPlugins": empty_plugins,
            "connectionType": connection_type,
            "downlinkMax": downlink_max,
            # F-123：机型能力位随档案落库（与 derive_spec 同口径），否则 A17
            # Pro 档的导出/API 面表里 av01 仍是 ''。
            "codecCapability": _codec_capability(
                target_os,
                av1=_ios_av1_capable(str(device["model"]), device["market_name"])),
            "webglProfile": _webgl_profile(target_os),
            "removeNavigatorProps": remove_props,
            "uaCh": ua_ch,
            "maxTouchPoints": max_touch_points,
            "geolocation": _MOBILE_GEOLOCATION,
            "camera": _generate_camera_api(),
            "webrtc": _MOBILE_WEBRTC,
        },
    }

    return fingerprint


def seeds_to_config(fingerprint_data: dict) -> dict:
    """把固定噪声种子转成 camoufox launch_options 的 config 参数。"""
    seeds = fingerprint_data.get("seeds", {})
    return {
        "canvas:seed": int(seeds.get("canvas", 1)),
        "audio:seed": int(seeds.get("audio", 1)),
        "fonts:spacing_seed": int(seeds.get("font_spacing", 1)),
    }


def create_mobile_fingerprint(
    target_os: str,
    mode: str = "generate",
    manufacturer: Optional[str] = None,
    browser: Optional[str] = None,
) -> dict:
    """创建移动端环境指纹（与桌面端 create_fingerprint 接口一致）。

    - mode=preset:  真实机型预设（版本组合取机型当前主流值）
    - mode=generate: 基于真实机型库的合成（版本随机）
    - browser: Android 恒为 chrome；iOS 可选 safari（默认）/ chrome_ios
    """
    if target_os not in ("android", "ios"):
        raise ValueError(f"不支持的移动端目标 OS: {target_os}")
    mode = "preset" if mode == "preset" else "generate"

    data = {
        "mode": mode,
        "fingerprint": None,
        "preset": None,
        "seeds": {
            "canvas": random.randint(1, _SEED_MAX),
            "audio": random.randint(1, _SEED_MAX),
            "font_spacing": random.randint(1, _SEED_MAX),
        },
        "fonts": [],
        "voices": None,  # 移动端通常没有 TTS
        "health": {"score": 100, "warnings": []},
        "target_os": target_os,
        "manufacturer": manufacturer,
        "device_model": None,
    }

    raw_fp = generate_mobile_fingerprint(target_os, manufacturer, mode, browser)
    data["fingerprint"] = raw_fp

    # 厂商 / 机型以实际机型库配对结果为准（未指定 manufacturer 时为按权重抽取结果）
    data["manufacturer"] = raw_fp.get("manufacturer") or manufacturer
    data["device_model"] = raw_fp.get("deviceModel")

    # 字体子集直接取用指纹内的同一份结果，避免两处随机不一致
    data["fonts"] = list(raw_fp.get("fonts") or [])

    from app.fingerprint_engine import check_health
    data["health"] = check_health(data, target_os)

    return data


# ------------------------------------------------------------------ 库查询（API/UI）
def list_brands(target_os: Optional[str] = None) -> Dict[str, List[str]]:
    """返回各平台可用品牌（用于面板品牌选择器）。"""
    result: Dict[str, List[str]] = {"android": [], "ios": []}
    for dev in _DEVICES:
        if target_os and dev["platform"] != target_os:
            continue
        brand = dev["brand"]
        if brand not in result.setdefault(dev["platform"], []):
            result[dev["platform"]].append(brand)
    return {k: sorted(v) for k, v in result.items()}


def library_devices(target_os: Optional[str] = None) -> List[Dict[str, Any]]:
    """机型清单（摘要字段，供 API/UI 展示）。"""
    out = []
    for dev in _DEVICES:
        if target_os and dev["platform"] != target_os:
            continue
        out.append({
            "brand": dev["brand"],
            "platform": dev["platform"],
            "type": dev["type"],
            "model": dev["model"],
            "market_name": dev["market_name"],
            "released": dev.get("released"),
            "screen": f"{dev['screen']['css_w']}x{dev['screen']['css_h']}@{dev['screen']['dpr']}",
            "gpu": dev["gpu"]["renderer"],
            "weight": dev["weight"],
        })
    return out


def library_info() -> Dict[str, Any]:
    return {
        "version": _LIBRARY.get("version"),
        "updated": _LIBRARY.get("updated"),
        "notes": _LIBRARY.get("notes"),
        "count": len(_DEVICES),
        "brands": list_brands(),
    }


if __name__ == "__main__":
    print("=== 机型库 ===")
    print(json.dumps(library_info(), ensure_ascii=False, indent=1))

    print("\n=== Android preset ===")
    android_fp = create_mobile_fingerprint("android", "preset")
    fp = android_fp["fingerprint"]
    print(f"UA: {fp['navigator']['userAgent']}")
    print(f"机型: {android_fp['manufacturer']} {android_fp['device_model']}")
    print(f"Screen: {fp['screen']['width']}x{fp['screen']['height']} @{fp['screen']['devicePixelRatio']} "
          f"(css {fp['screen']['cssWidth']}x{fp['screen']['cssHeight']}, vp {fp['screen']['cssViewportHeight']})")
    print(f"GPU: {fp['videoCard']['vendor']} / {fp['videoCard']['renderer']}")
    print(f"UA-CH model: {(fp['mobileExtra'].get('uaCh') or {}).get('model')}")
    print(f"health: {android_fp['health']}")

    print("\n=== iOS generate ===")
    ios_fp = create_mobile_fingerprint("ios", "generate")
    fp = ios_fp["fingerprint"]
    print(f"UA: {fp['navigator']['userAgent']}")
    print(f"机型: {ios_fp['manufacturer']} {ios_fp['device_model']}")
    print(f"Screen: {fp['screen']['width']}x{fp['screen']['height']} @{fp['screen']['devicePixelRatio']} "
          f"(css {fp['screen']['cssWidth']}x{fp['screen']['cssHeight']}, vp {fp['screen']['cssViewportHeight']})")
    print(f"GPU: {fp['videoCard']['vendor']} / {fp['videoCard']['renderer']}")
    print(f"remove props: {fp['mobileExtra']['removeNavigatorProps']}")
    print(f"health: {ios_fp['health']}")
