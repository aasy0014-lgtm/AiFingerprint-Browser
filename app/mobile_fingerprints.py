"""移动端设备指纹生成器（Android/iOS）

由于 BrowserForge 官方不支持移动平台，本项目实现一套基于真实移动设备指纹数据的
合成生成引擎。数据来源于：
1. UA 字符串库（https://user-agents.net/）
2. Android/iOS 设备特征数据库（屏幕分辨率、DPR、GPU、硬件并发数等）
3. CreepJS / Pixelscan 的真实采集样本

一致性原则：UA 与设备规格必须相互印证——iPhone UA 只配手机屏、iPad UA 只配平板屏，
屏幕物理分辨率 / DPR / GPU / 核心数均取对应机型真实规格；Android UA 中机型可见时按
机型精确配对，iOS UA 不含机型则按手机/平板类别配对。

目标 OS: android, ios
"""
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.fingerprints import _dump_dataclass


# ------------------------------------------------------------------ UA 库
@dataclass
class UAEntry:
    """单条 UA 记录"""
    user_agent: str
    platform: str
    browser: str
    browser_version: str
    os_version: str
    device_type: str  # mobile/tablet
    manufacturer: Optional[str]  # Samsung/Apple/etc., None for generic
    device_model: Optional[str] = None  # UA 中可识别的机型标识（iOS UA 不含机型，为 None）


# 移动 UA 样本库（保留旧名 _ANDROID_UA_POOL 以兼容历史引用）
_ANDROID_UA_POOL: List[UAEntry] = [
    UAEntry(
        "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        platform="Android", browser="Chrome", browser_version="120.0.0.0",
        os_version="13", device_type="mobile", manufacturer="Samsung",
        device_model="SM-S918B"
    ),
    UAEntry(
        "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
        platform="Android", browser="Chrome", browser_version="121.0.0.0",
        os_version="14", device_type="mobile", manufacturer="Google",
        device_model="Pixel 8 Pro"
    ),
    UAEntry(
        "Mozilla/5.0 (Linux; Android 13; 2210132G) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        platform="Android", browser="Chrome", browser_version="120.0.0.0",
        os_version="13", device_type="mobile", manufacturer="Xiaomi",
        device_model="2210132G"
    ),
    UAEntry(
        "Mozilla/5.0 (Linux; Android 13; CPH2449) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        platform="Android", browser="Chrome", browser_version="120.0.0.0",
        os_version="13", device_type="mobile", manufacturer="OnePlus",
        device_model="CPH2449"
    ),
    UAEntry(
        "Mozilla/5.0 (Linux; Android 12; ALN-AL00) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
        platform="Android", browser="Chrome", browser_version="120.0.0.0",
        os_version="12", device_type="mobile", manufacturer="Huawei",
        device_model="ALN-AL00"
    ),
    # 平板样本（Android 平板 UA 不含 "Mobile" 标记）
    UAEntry(
        "Mozilla/5.0 (Linux; Android 13; SM-X910) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        platform="Android", browser="Chrome", browser_version="120.0.0.0",
        os_version="13", device_type="tablet", manufacturer="Samsung",
        device_model="SM-X910"
    ),
    UAEntry(
        "Mozilla/5.0 (Linux; Android 13; Lenovo Tab P12) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        platform="Android", browser="Chrome", browser_version="120.0.0.0",
        os_version="13", device_type="tablet", manufacturer="Lenovo",
        device_model="Lenovo Tab P12"
    ),
    # iOS 样本（Safari UA 不暴露机型，只能按手机/平板类别配对）
    UAEntry(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        platform="iPhone", browser="Safari", browser_version="17.5",
        os_version="17.5", device_type="mobile", manufacturer="Apple"
    ),
    UAEntry(
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Mobile/15E148 Safari/604.1",
        platform="iPhone", browser="Safari", browser_version="17.4.1",
        os_version="17.4.1", device_type="mobile", manufacturer="Apple"
    ),
    UAEntry(
        "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
        platform="iPad", browser="Safari", browser_version="17.0",
        os_version="17.0", device_type="tablet", manufacturer="Apple"
    ),
]


def _pick_ua_entry(target_os: str, manufacturer: Optional[str]) -> UAEntry:
    """按平台与（可选）制造商挑选 UA。

    制造商无匹配时回退到该平台全池（绝不产生 Apple UA ↔ Android 这类跨类配对）；
    实际配对结果以返回的 UA 为准，调用方可从 manufacturer 字段读取。
    """
    if target_os == "ios":
        pool = [e for e in _ANDROID_UA_POOL if e.manufacturer == "Apple"]
    else:
        pool = [e for e in _ANDROID_UA_POOL if e.manufacturer != "Apple"]
    if manufacturer:
        matched = [e for e in pool if e.manufacturer == manufacturer]
        if matched:
            pool = matched
    return random.choice(pool)


# ------------------------------------------------------------------ 设备特征库
@dataclass
class DeviceEntry:
    """单条设备特征记录（真实机型的典型采集值）

    width/height 为物理像素；浏览器可见的 CSS 像素 = 物理 / dpr。
    """
    manufacturer: str
    model: str          # 机型标识（Android UA 中可见；iOS 用市场名）
    display_name: str   # 市场名称，用于摘要展示
    device_type: str    # mobile/tablet
    width: int
    height: int
    dpr: float
    color_depth: int
    gpu_vendor: str
    gpu_renderer: str
    hw_cores: int
    memory: int         # GB


_DEVICE_DATABASE: Dict[str, List[DeviceEntry]] = {
    "Samsung": [
        DeviceEntry("Samsung", "SM-S918B", "Galaxy S23 Ultra", "mobile",
                    1440, 3088, 3.0, 24, "Adreno", "Adreno 740", 8, 12),
        DeviceEntry("Samsung", "SM-X910", "Galaxy Tab S9", "tablet",
                    1600, 2560, 2.5, 24, "Adreno", "Adreno 740", 8, 12),
    ],
    "Google": [
        DeviceEntry("Google", "Pixel 8 Pro", "Pixel 8 Pro", "mobile",
                    1344, 2992, 2.625, 24, "Mali", "Immortalis-G715 MC10", 8, 12),
    ],
    "Xiaomi": [
        DeviceEntry("Xiaomi", "2210132G", "Xiaomi 13", "mobile",
                    1080, 2400, 2.75, 24, "Adreno", "Adreno 740", 8, 12),
    ],
    "OnePlus": [
        DeviceEntry("OnePlus", "CPH2449", "OnePlus 11", "mobile",
                    1440, 3216, 3.0, 24, "Adreno", "Adreno 740", 8, 16),
    ],
    "Huawei": [
        DeviceEntry("Huawei", "ALN-AL00", "Mate 60 Pro", "mobile",
                    1260, 2720, 2.75, 24, "Mali", "Maleoon 910", 12, 12),
    ],
    "Lenovo": [
        DeviceEntry("Lenovo", "Lenovo Tab P12", "Lenovo Tab P12", "tablet",
                    1840, 2880, 2.0, 24, "Mali", "Mali-G68 MC4", 8, 8),
    ],
    # iOS UA 不含机型，按 device_type 在 iPhone / iPad 之间随机配对
    "Apple": [
        DeviceEntry("Apple", "iPhone 15 Pro Max", "iPhone 15 Pro Max", "mobile",
                    1290, 2796, 3.0, 24, "Apple", "Apple A17 Pro GPU", 6, 8),
        DeviceEntry("Apple", "iPhone 15", "iPhone 15", "mobile",
                    1179, 2556, 3.0, 24, "Apple", "Apple A16 Bionic GPU", 6, 6),
        DeviceEntry("Apple", "iPad Pro 12.9", "iPad Pro 12.9\"", "tablet",
                    2048, 2732, 2.0, 24, "Apple", "Apple M2 GPU", 8, 16),
        DeviceEntry("Apple", "iPad Air", "iPad Air", "tablet",
                    1640, 2360, 2.0, 24, "Apple", "Apple M1 GPU", 8, 8),
    ],
}

# 未知制造商兜底（理论上不可达：所有 UA 都绑定在库内机型上）
_FALLBACK_DEVICE = DeviceEntry(
    "Unknown", "generic-android", "通用 Android 机型", "mobile",
    1080, 2400, 2.75, 24, "Adreno", "Adreno 640", 8, 6,
)


def _pick_device(ua_entry: UAEntry) -> DeviceEntry:
    """为 UA 配对同类设备：UA 中机型可见时精确配对，否则按手机/平板类别配对。"""
    devices = _DEVICE_DATABASE.get(ua_entry.manufacturer or "", [])
    if ua_entry.device_model:
        exact = [d for d in devices if d.model == ua_entry.device_model]
        if exact:
            return random.choice(exact)
    same_type = [d for d in devices if d.device_type == ua_entry.device_type]
    if same_type:
        return random.choice(same_type)
    if devices:
        return random.choice(devices)
    return _FALLBACK_DEVICE


# ------------------------------------------------------------------ 辅助数据
def _chrome_brands(ua_entry: UAEntry) -> List[Dict[str, str]]:
    """Chrome UA-CH brands（真实 Chrome 会混入 Grease 品牌并随机排序，这里固定次序）"""
    major = ua_entry.browser_version.split(".")[0]
    return [
        {"brand": "Chromium", "version": major},
        {"brand": "Google Chrome", "version": major},
        {"brand": "Not_A Brand", "version": "8"},
    ]


# Battery API（移动端特有）
def _generate_battery_api() -> Dict[str, Any]:
    """navigator.battery 对象"""
    level = random.uniform(0.2, 1.0)
    charging = random.random() < 0.15  # 15% 概率在充电

    return {
        "level": level,
        "charging": charging,
        "chargingTime": 0 if not charging else random.randint(1800, 7200),
        "dischargingTime": random.randint(14400, 43200),
    }


# Geolocation API（默认可访问，不伪装）
_MOBILE_GEOLOCATION = {
    "geolocation": {
        "accessibility": True,
        "mocked": False,
    },
}


# Camera API
def _generate_camera_api() -> Dict[str, Any]:
    """navigator.camera 能力检测"""
    return {
        "hasCamera": True,
        "rearCamera": True,
        "frontCamera": True,
        "videoCapture": True,
        "imageCapture": True,
    }


# WebRTC（移动端通常可用）
_MOBICE_WEBRTC = {
    "webrtc": {
        "supported": True,
        "privacyMode": False,
    },
}


# ------------------------------------------------------------------ 指纹生成
def generate_mobile_fingerprint(target_os: str, manufacturer: Optional[str] = None) -> Dict[str, Any]:
    """
    生成移动端指纹（Android/iOS）。

    Parameters:
        target_os: "android" or "ios"
        manufacturer: 可选，指定设备制造商（如 "Samsung", "Apple", "Google"）
                     留空则随机选择；无匹配时自动回退，实际结果见返回值的 manufacturer

    Returns:
        包含完整浏览器指纹的字典，结构与 BrowserForge 输出兼容；
        额外包含 manufacturer / deviceModel 字段记录实际配对结果
    """
    ua_entry = _pick_ua_entry(target_os, manufacturer)
    device = _pick_device(ua_entry)

    if target_os == "ios":
        # 真机 navigator.platform：iPhone 报 "iPhone"，iPad 报 "iPad"
        nav_platform = "iPad" if ua_entry.device_type == "tablet" else "iPhone"
        vendor = "Apple Computer, Inc."
        user_agent_data = None  # iOS Safari 不暴露 navigator.userAgentData
    else:
        nav_platform = "Linux armv81"  # Android ARM64 真机典型值
        vendor = "Google Inc."
        user_agent_data = {
            "brands": _chrome_brands(ua_entry),
            "platform": "Android",
            "mobile": ua_entry.device_type == "mobile",
        }

    # 真机上 appVersion = UA 去掉 "Mozilla/" 前缀（Chrome/Safari 一致）
    app_version = ua_entry.user_agent.split("Mozilla/", 1)[-1]

    # 触控点：手机 5，平板 5/10（真实设备常见值）
    max_touch_points = 5 if device.device_type == "mobile" else random.choice([5, 10])

    # Screen 对象（只保留 Screen 的真实属性；window 尺寸由 CDP 仿真层决定）
    screen = {
        "availHeight": device.height,
        "availWidth": device.width,
        "availTop": 0,
        "availLeft": 0,
        "colorDepth": device.color_depth,
        "height": device.height,
        "pixelDepth": device.color_depth,
        "width": device.width,
        "devicePixelRatio": device.dpr,
    }

    # Navigator 对象
    navigator = {
        "userAgent": ua_entry.user_agent,
        "userAgentData": user_agent_data,
        "doNotTrack": None,
        "appCodeName": "Mozilla",
        "appName": "Netscape",
        "appVersion": app_version,
        "oscpu": None,  # Chrome/Safari 均不暴露 oscpu（Firefox 专有属性）
        "webdriver": False,
        "language": "zh-CN",
        "languages": ["zh-CN", "zh", "en-US", "en"],
        "platform": nav_platform,
        "hardwareConcurrency": device.hw_cores,
        "deviceMemory": device.memory,
        "product": "Gecko",
        "productSub": "20030107",
        "vendor": vendor,
        "vendorSub": "",
        "maxTouchPoints": max_touch_points,
        "extraProperties": {},
        "offline": False,
        "cookieEnabled": True,
    }

    # 视频卡（GPU）
    video_card = {
        "renderer": device.gpu_renderer,
        "vendor": device.gpu_vendor,
    }

    # 音频编解码器
    audio_codecs = {
        "aac": "audio/mp4a-enc;codecs=aac",
        "flac": "audio/flac;codecs=flac",
        "mp3": "audio/mpeg;codecs=mp3",
        "opus": "audio/ogg;codecs=opus",
        "vorbis": "audio/ogg;codecs=vorbis",
    }

    # 视频编解码器
    video_codecs = {
        "av1": "video/mp4;codecs=av01.0.05M.08",
        "h264": "video/mp4;codecs=avc1.42E01E",
        "vp8": "video/webm;codecs=vp8",
        "vp9": "video/webm;codecs=vp09.00.10.08",
    }

    # 插件列表（Android Chrome 典型的 plugin 数组）
    plugins_data = {
        "pdfViewer": "application/pdf",
        "widevine": "video/mp4",
        "nacl": "application/x-nacl",
    }

    # 多媒体设备
    multimedia_devices = [
        "microphone",
        "speaker",
        "camera-rear",
        "camera-front",
        "video-output",
    ]

    # 字体子集（移动端常用字体）
    fonts = _generate_mobile_font_subset(target_os, ua_entry.manufacturer)

    # 合并所有数据
    fingerprint = {
        "screen": screen,
        "navigator": navigator,
        "headers": {},  # 由 HeadersGenerator 填充
        "videoCodecs": video_codecs,
        "audioCodecs": audio_codecs,
        "pluginsData": plugins_data,
        "battery": _generate_battery_api(),
        "videoCard": video_card,
        "multimediaDevices": multimedia_devices,
        "fonts": fonts,
        "mockWebRTC": False,
        "slim": False,
        # 实际配对结果（供 create_mobile_fingerprint / 摘要使用）
        "manufacturer": ua_entry.manufacturer or device.manufacturer,
        "deviceModel": device.display_name,
        # 移动端特有扩展
        "mobileExtra": {
            "maxTouchPoints": navigator["maxTouchPoints"],
            "geolocation": _MOBILE_GEOLOCATION,
            "camera": _generate_camera_api(),
            "webrtc": _MOBICE_WEBRTC,
        },
    }

    return fingerprint


def _generate_mobile_font_subset(target_os: str, manufacturer: Optional[str]) -> List[str]:
    """生成移动端字体子集"""

    if target_os == "ios":
        essential = [
            'Helvetica Neue', 'Arial', 'Times New Roman', 'Courier New',
            'Georgia', 'Verdana', 'Tahoma', 'Palatino', 'Garamond',
            'Bookman', 'Comic Sans MS', 'Trebuchet MS', 'Lucida Console',
            'Menlo', 'Monaco', 'Lucida Grande', 'PingFang SC', 'PingFang HK',
        ]
        non_essential = [
            'SF Pro Text', 'SF Pro Icons', 'Helvetica', 'Chalkboard SE',
            'Didot', 'ITC Avant Garde Gothic', 'Century Gothic', 'Sand',
            'System Font', 'Times', 'Arial MT', 'Georgia MT',
        ]
    else:  # android
        essential = [
            'Droid Sans', 'Droid Serif', 'Roboto', 'Open Sans', 'Montserrat',
            'Noto Sans', 'Noto Sans SC', 'Source Sans Pro', 'Lato',
        ]
        non_essential = [
            'Merriweather', 'Oxygen', 'PT Sans', 'PT Serif', 'Fira Sans',
            'Raleway', 'Ubuntu', 'Karumbi', 'Poppins', 'Josefin Sans',
        ]

    # 移动端字体数量较少（约 30-60 个）
    selected_non_essential = random.sample(non_essential, min(len(non_essential), random.randint(5, 15)))

    return essential + selected_non_essential


def seeds_to_config(fingerprint_data: dict) -> dict:
    """
    把固定噪声种子转成 camoufox launch_options 的 config 参数。
    移动端需注入 Canvas/Audio/字体间距种子。
    """
    seeds = fingerprint_data.get("seeds", {})
    return {
        "canvas:seed": int(seeds.get("canvas", 1)),
        "audio:seed": int(seeds.get("audio", 1)),
        "fonts:spacing_seed": int(seeds.get("font_spacing", 1)),
    }


def create_mobile_fingerprint(target_os: str, mode: str = "generate", manufacturer: Optional[str] = None) -> dict:
    """
    创建移动端指纹（与桌面端 create_fingerprint 接口一致）。

    Parameters:
        target_os: "android" or "ios"
        mode: 兼容参数；移动端无真实预设库，一律按 "generate" 处理
        manufacturer: 设备制造商（可选）

    Returns:
        标准指纹数据结构（与桌面端格式兼容）
    """
    data = {
        # 移动端只有合成生成模式（浏览器厂商均未提供移动端真实预设库）
        "mode": "generate",
        "fingerprint": None,
        "preset": None,
        "seeds": {
            "canvas": random.randint(1, 4294967294),
            "audio": random.randint(1, 4294967294),
            "font_spacing": random.randint(1, 4294967294),
        },
        "fonts": [],
        "voices": None,  # 移动端通常没有 TTS
        "health": {"score": 100, "warnings": []},
        "target_os": target_os,
        "manufacturer": manufacturer,
        "device_model": None,
    }

    # 生成移动端指纹
    raw_fp = generate_mobile_fingerprint(target_os, manufacturer)
    data["fingerprint"] = _dump_dataclass(raw_fp)

    # 厂商 / 机型以实际生成结果为准（未指定 manufacturer 时会随机选取）
    data["manufacturer"] = raw_fp.get("manufacturer") or manufacturer
    data["device_model"] = raw_fp.get("deviceModel")

    # 字体子集直接取用指纹内的同一份结果，避免两处随机不一致
    data["fonts"] = list(raw_fp.get("fonts") or [])

    # 一致性体检
    from app.fingerprint_engine import check_health
    data["health"] = check_health(data, target_os)

    return data


if __name__ == "__main__":
    # 测试：生成 Android 指纹
    print("=== Android 指纹 ===")
    android_fp = create_mobile_fingerprint("android", "generate", "Samsung")
    print(f"UA: {android_fp['fingerprint']['navigator']['userAgent']}")
    print(f"Screen: {android_fp['fingerprint']['screen']['width']}x{android_fp['fingerprint']['screen']['height']}")
    print(f"GPU: {android_fp['fingerprint']['videoCard']['vendor']} {android_fp['fingerprint']['videoCard']['renderer']}")
    print(f"Battery: {android_fp['fingerprint']['battery']}")

    print("\n=== iOS 指纹 ===")
    ios_fp = create_mobile_fingerprint("ios", "generate", "Apple")
    print(f"UA: {ios_fp['fingerprint']['navigator']['userAgent']}")
    print(f"Screen: {ios_fp['fingerprint']['screen']['width']}x{ios_fp['fingerprint']['screen']['height']}")
    print(f"GPU: {ios_fp['fingerprint']['videoCard']['vendor']} {ios_fp['fingerprint']['videoCard']['renderer']}")
