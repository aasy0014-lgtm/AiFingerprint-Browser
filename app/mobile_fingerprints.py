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
    grease = _LIBRARY.get("grease_brand") or {"brand": "Not_A Brand", "version": "8"}
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
# 前 5 项是早期的 Safari/桌面差异（connection/pdfViewerEnabled 等）；其余为
# WebKit 从未实现的桌面 Chromium API（MDN BCD，2026-10 核实）：Web Serial /
# WebUSB / WebHID / Web Bluetooth / Keyboard Lock / Web MIDI / Presentation /
# getInstalledRelatedApps / navigator.vibrate。桌面内核全部携带，不抹除时
# `'serial' in navigator` 这类一行探测即可判定「Chromium 装 iOS」。
_IOS_REMOVE_NAV_PROPS = [
    "connection", "pdfViewerEnabled", "userAgentData", "getBattery", "deviceMemory",
    "serial", "usb", "hid", "bluetooth", "keyboard", "requestMIDIAccess",
    "presentation", "getInstalledRelatedApps", "vibrate",
]

# Android Chrome 不存在、需从桌面内核上抹掉的 Navigator 属性（WebHID 仅桌面，
# 见 BCD chrome_android=false；WebUSB/Web Serial 在 Chrome Android 148 已支持，
# 不可误删——版本相关差异必须逐项核对，不能按「桌面 API」一概而论）。
_ANDROID_REMOVE_NAV_PROPS = ["hid"]


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
# 未列入本表的 MIME 一律回退宿主原生结果（safari 与桌面 Chromium 在 mp4/aac/
# mpeg 容器上本就一致，无需伪造——表越小越不容易自相矛盾）。
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
}
_CODEC_CAPABILITY_ANDROID: Dict[str, str] = {
    # --- 真机 Android Chrome：HEVC 硬件解码（内核裸 'hvc1' 形态不一致）---
    'video/mp4; codecs="hvc1"': "probably",
    'video/mp4; codecs="hvc1.1.6.L93.B0"': "probably",
}


def _codec_capability(target_os: str) -> Dict[str, str]:
    """目标平台相对宿主内核需要校准的 canPlayType 返回值（键为小写 MIME）。"""
    table = _CODEC_CAPABILITY_IOS if target_os == "ios" else _CODEC_CAPABILITY_ANDROID
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
# iOS（Metal/ANGLE-Metal）额外：Spectre 侧信道计时扩展在 Apple 物理封禁，
# 且移动 Safari 从不导出 WebGL1 的 draw_buffers/depth_texture 之外的桌面面。
_GL_EXT_DESKTOP_ONLY_IOS = _GL_EXT_DESKTOP_ONLY + (
    "EXT_disjoint_timer_query",
    "EXT_disjoint_timer_query_webgl2",
    "WEBGL_debug_shaders",
    "WEBGL_multi_draw",
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
    """目标平台 WebGL 面校准规格：上限覆盖 + 扩展集剔除（按 GPU 家族）。"""
    drop = list(_GL_EXT_DESKTOP_ONLY_IOS if target_os == "ios" else _GL_EXT_DESKTOP_ONLY)
    return {
        "limits": dict(_GL_LIMITS_MOBILE),
        "dropExtensions": drop,
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
        nav_platform = "Linux armv81"
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
            "codecCapability": _codec_capability(target_os),
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
