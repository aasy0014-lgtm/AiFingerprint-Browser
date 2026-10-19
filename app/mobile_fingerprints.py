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

# iOS Safari 不存在、运行期需要从内核（桌面 Chromium）上抹掉的 Navigator 属性
_IOS_REMOVE_NAV_PROPS = ["connection", "pdfViewerEnabled", "userAgentData", "getBattery", "deviceMemory"]


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
        remove_props: List[str] = []
        empty_plugins = False
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
