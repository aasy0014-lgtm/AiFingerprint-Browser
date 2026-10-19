"""指纹引擎：环境指纹的双模式生成、一致性体检与启动参数装配。

模式:
- generate: BrowserForge 合成指纹（默认，无限独特组合）
- preset:   Camoufox 内置的真实设备指纹预设库（真实采集数据）

移动端 (Android/iOS):
- generate: 基于真实机型库（app/data/mobile_devices.json）的合成指纹
- preset:   真实机型预设（机型库中系统/浏览器版本的主流组合）

统一存储格式（fingerprint_json）:
{
  "mode": "generate" | "preset",
  "fingerprint": {...BrowserForge dump / 移动端机型指纹...} | null,
  "preset": {...camoufox preset dict...} | null,   # 仅桌面 preset 模式
  "seeds": {"canvas": int, "audio": int, "font_spacing": int},
  "fonts": [...该环境固定的字体子集...],
  "voices": [...] | null,        # preset 自带 speechVoices 时为 null（用预设的）
  "health": {"score": int, "warnings": [...]},
  "target_os": "windows" | "macos" | "linux" | "android" | "ios",
  "manufacturer": ...              # 仅移动端有效（Samsung/Apple/Google 等）
}
"""
import random
import re
from typing import Any, Optional

from . import fingerprints as fp_lib

_MAX_SEED = 4_294_967_295

_GPU_OS_HINTS = {
    "windows": ("Direct3D", "D3D11", "ANGLE (", "Google Inc."),
    "macos": ("Apple", "ANGLE (Apple", "Metal"),
    "linux": ("Mesa", "llvmpipe", "X11", "DRI"),
    # 移动端 GPU（Android: Adreno/Mali/PowerVR/Xclipse/Maleoon；iOS: Apple GPU）
    "android": ("Adreno", "Mali", "PowerVR", "Immortalis", "Xclipse", "Maleoon"),
    "ios": ("Apple", "Metal", "PVR", "Imagination"),
}


def create_fingerprint(target_os: str, mode: str = "generate", manufacturer: Optional[str] = None,
                       browser: Optional[str] = None) -> dict[str, Any]:
    """创建一套环境指纹（生成模式或真实预设模式）。

    manufacturer 仅移动端使用（机型库品牌筛选，留空则按权重随机）。
    browser 仅 iOS 移动端使用（"safari" 默认 / "chrome_ios"，见 mobile_fingerprints）。
    """
    # 移动端指纹使用专用引擎
    if target_os in ("android", "ios"):
        from .mobile_fingerprints import create_mobile_fingerprint
        return create_mobile_fingerprint(target_os, mode, manufacturer, browser=browser)
    
    data: dict[str, Any] = {
        "mode": mode,
        "fingerprint": None,
        "preset": None,
        "seeds": {
            "canvas": random.randint(1, _MAX_SEED),
            "audio": random.randint(1, _MAX_SEED),
            "font_spacing": random.randint(1, _MAX_SEED),
        },
        "fonts": [],
        "voices": None,
        "health": {"score": 100, "warnings": []},
    }

    if mode == "preset":
        preset = _random_preset(target_os)
        if preset is None:  # 预设库不可用时回退到合成模式
            data["mode"] = "generate"
        else:
            preset = _fix_preset_webgl(preset, target_os)
            data["preset"] = _align_preset_ua(preset)

    if data["mode"] == "generate":
        data["fingerprint"] = fp_lib.generate_profile_fingerprint(target_os)["fingerprint"]

    # 字体子集固定（两种模式都需要，否则每次启动随机导致度量漂移）
    try:
        from camoufox.fingerprints import _generate_random_font_subset
        data["fonts"] = _generate_random_font_subset(target_os)
    except Exception:
        pass

    # preset 模式且预设自带 speechVoices 时不注入生成的语音列表（用预设真实值）
    if not (data["mode"] == "preset" and data["preset"].get("speechVoices")):
        try:
            from camoufox.fingerprints import _generate_random_voice_subset
            data["voices"] = _generate_random_voice_subset(target_os)
        except Exception:
            pass

    data["health"] = check_health(data, target_os)
    return data


def _random_preset(target_os: str) -> Optional[dict]:
    try:
        from camoufox.fingerprints import get_random_preset
        return get_random_preset(os=target_os)
    except Exception:
        return None


def _fix_preset_webgl(preset: dict, target_os: str) -> dict:
    """部分真实预设的 WebGL 是泛化的 "Mozilla"（该设备的隐私设置所致），
    camoufox 的 WebGL 参数库无法为其采样，启动会报
    "No WebGL data found"。创建时替换为该 OS 的真实 GPU 参数并固定存储。
    """
    webgl = dict(preset.get("webgl") or {})
    vendor = str(webgl.get("unmaskedVendor") or "")
    renderer = str(webgl.get("unmaskedRenderer") or "")
    if vendor and vendor != "Mozilla" and renderer and renderer != "Mozilla":
        return preset
    try:
        from camoufox.webgl import sample_webgl

        os_key = {"windows": "win", "macos": "mac", "linux": "lin"}.get(target_os, "win")
        sampled = sample_webgl(os_key)
        webgl["unmaskedVendor"] = sampled.get("webGl:vendor", vendor)
        webgl["unmaskedRenderer"] = sampled.get("webGl:renderer", renderer)
        preset = dict(preset)
        preset["webgl"] = webgl
    except Exception:
        pass
    return preset


def _align_preset_ua(preset: dict) -> dict:
    """把预设 UA 的 Firefox 版本号对齐到已安装内核（camoufox 启动时也会做同样处理）。"""
    preset = dict(preset)
    nav = dict(preset.get("navigator") or {})
    ua = nav.get("userAgent")
    if ua:
        try:
            from camoufox.pkgman import installed_verstr
            major = installed_verstr().split(".", 1)[0]
            ua = re.sub(r"Firefox/\d+\.0", f"Firefox/{major}.0", ua)
            ua = re.sub(r"rv:\d+\.0", f"rv:{major}.0", ua)
            nav["userAgent"] = ua
        except Exception:
            pass
    preset["navigator"] = nav
    return preset


def build_launch_kwargs(fingerprint_data: dict[str, Any]) -> dict[str, Any]:
    """把存储的指纹转成 AsyncNewBrowser 的指纹相关 kwargs。"""
    target_os = fingerprint_data.get("target_os", "windows")
    
    # 移动端使用专用启动参数装配
    if target_os in ("android", "ios"):
        from .mobile_fingerprints import seeds_to_config
        config = seeds_to_config(fingerprint_data)
        
        # 移动端不注入 preset 或 BrowserForge 指纹（camoufox 不支持移动平台）
        # 返回空 kwargs，由 launcher 层处理移动端特殊启动逻辑
        return {
            "config": config,
            "fonts": fingerprint_data.get("fonts", []),
            "mobile_target_os": target_os,
        }
    
    mode = fingerprint_data.get("mode", "generate")
    kwargs: dict[str, Any] = {}

    config: dict[str, Any] = {}
    seeds = fingerprint_data.get("seeds") or {}
    if seeds.get("canvas"):
        config["canvas:seed"] = int(seeds["canvas"])
    if seeds.get("audio"):
        config["audio:seed"] = int(seeds["audio"])
    if seeds.get("font_spacing"):
        config["fonts:spacing_seed"] = int(seeds["font_spacing"])
    voices = fingerprint_data.get("voices")
    if voices:
        config["voices"] = voices

    if mode == "preset" and fingerprint_data.get("preset"):
        # 预设由 camoufox 转换（合并不覆盖既有键，我们的种子优先）
        kwargs["fingerprint_preset"] = fingerprint_data["preset"]
    else:
        kwargs["fingerprint"] = fp_lib.load_fingerprint(fingerprint_data)

    kwargs["config"] = config
    if fingerprint_data.get("fonts"):
        kwargs["fonts"] = fingerprint_data["fonts"]
    return kwargs


# ---------------------------------------------------------------- 一致性体检

def check_health(fingerprint_data: dict[str, Any], target_os: str) -> dict[str, Any]:
    """静态一致性体检：跨信号矛盾是反检测的头号破绽，创建/更新时检查并落库。"""
    warnings: list[str] = []
    is_mobile = target_os in ("android", "ios")

    if is_mobile:
        # 移动端数据始终存放在 fingerprint 字段（preset 仅是模式标签）
        fp = fingerprint_data.get("fingerprint") or {}
        nav = fp.get("navigator") or {}
        screen = fp.get("screen") or {}
        vc = fp.get("videoCard") or {}
        extra = fp.get("mobileExtra") or {}
        ua, platform = nav.get("userAgent", ""), nav.get("platform", "")
        renderer = str(vc.get("renderer", ""))
        hw = nav.get("hardwareConcurrency")
    elif fingerprint_data.get("mode") == "preset" and (fingerprint_data.get("preset") or {}):
        nav = (fingerprint_data.get("preset") or {}).get("navigator") or {}
        screen = (fingerprint_data.get("preset") or {}).get("screen") or {}
        webgl = (fingerprint_data.get("preset") or {}).get("webgl") or {}
        ua, platform = nav.get("userAgent", ""), nav.get("platform", "")
        renderer = str(webgl.get("unmaskedRenderer", ""))
        hw = nav.get("hardwareConcurrency")
    else:
        fp = fingerprint_data.get("fingerprint") or {}
        nav = fp.get("navigator") or {}
        screen = fp.get("screen") or {}
        vc = fp.get("videoCard") or {}
        ua, platform = nav.get("userAgent", ""), nav.get("platform", "")
        renderer = str(vc.get("renderer", ""))
        hw = nav.get("hardwareConcurrency")

    # UA 与 platform 与目标 OS 三方一致
    # 注意：iPad UA 含 "like Mac OS X"，必须先判移动端标记再判 macOS
    if "Android" in ua:
        ua_os = "android"
    elif any(tok in ua for tok in ("iPhone", "iPad", "iPod")):
        ua_os = "ios"
    elif "Windows NT" in ua:
        ua_os = "windows"
    elif "Mac OS X" in ua:
        ua_os = "macos"
    elif "X11; Linux" in ua:
        ua_os = "linux"
    else:
        ua_os = "unknown"
    if ua_os != "unknown" and ua_os != target_os:
        warnings.append(f"UA 声称 {ua_os} 但目标是 {target_os}")
    expected_platform = {
        "windows": {"Win32"},
        "macos": {"MacIntel"},
        "linux": {"Linux x86_64"},
        # 移动端 JS 层 navigator.platform 的真机典型值
        "android": {"Linux armv81", "Linux aarch64", "Linux armv7l"},
        "ios": {"iPhone", "iPad", "iPod"},
    }
    if platform and platform not in expected_platform.get(target_os, {platform}):
        warnings.append(f"platform={platform} 与目标 OS {target_os} 不匹配")

    # GPU 渲染器与 OS 大方向匹配（宽松检查，只报明显矛盾）
    if renderer:
        hints = _GPU_OS_HINTS.get(target_os, ())
        if hints and not any(h.lower() in renderer.lower() for h in hints):
            warnings.append(f"WebGL 渲染器与 {target_os} 常见硬件不符: {renderer[:50]}")

    # 屏幕合理性
    try:
        w, h = int(screen.get("width", 0)), int(screen.get("height", 0))
        if w and (w > 7680 or h > 4320 or w < 640 or h < 480):
            warnings.append(f"屏幕尺寸异常: {w}x{h}")
        if screen.get("colorDepth") not in (None, 24, 30):
            warnings.append(f"色深异常: {screen.get('colorDepth')}")
        if target_os in ("android", "ios"):
            dpr = float(screen.get("devicePixelRatio") or 0)
            if not 1.5 <= dpr <= 4.5:
                warnings.append(f"移动端 devicePixelRatio 异常: {screen.get('devicePixelRatio')}")
            elif w and not 300 <= w / dpr <= 1400:
                warnings.append(f"移动端 CSS 视口宽度异常: {w / dpr:.0f}px")
    except (TypeError, ValueError):
        warnings.append("屏幕字段类型异常")

    # 硬件并发数
    try:
        if hw is not None and not 1 <= int(hw) <= 64:
            warnings.append(f"hardwareConcurrency 异常: {hw}")
    except (TypeError, ValueError):
        warnings.append(f"hardwareConcurrency 非数值: {hw}")

    # ---------------- 移动端专项检查 ----------------
    if is_mobile:
        extra = (fingerprint_data.get("fingerprint") or {}).get("mobileExtra") or {}
        model = str(extra.get("model") or "")
        os_version = str(extra.get("osVersion") or "")

        # 屏幕 / DPR / CSS / 视口 相互印证
        try:
            phys_w = int(screen.get("width", 0))
            dpr = float(screen.get("devicePixelRatio") or 0)
            css_w = int(screen.get("cssWidth") or 0)
            css_h = int(screen.get("cssHeight") or 0)
            vp_h = int(screen.get("cssViewportHeight") or 0)
            if phys_w and dpr and css_w and abs(round(phys_w / dpr) - css_w) > 1:
                warnings.append(f"物理分辨率/DPR 与 CSS 宽度不一致: {phys_w}/{dpr} vs {css_w}")
            if css_h and vp_h and not 0.5 * css_h <= vp_h <= 0.95 * css_h:
                warnings.append(f"首屏视口高度异常: {vp_h}（屏幕 CSS 高度 {css_h}）")
            if css_w and vp_h and vp_h > 0 and not 0.2 <= css_w / vp_h <= 1.6:
                warnings.append(f"视口纵横比异常: {css_w}x{vp_h}")
        except (TypeError, ValueError):
            warnings.append("移动端屏幕字段类型异常")

        # UA ↔ 机型 ↔ 系统版本 相互印证
        if model and target_os == "android":
            # 机型名可能含空格（Pixel 8 Pro），做整名匹配
            if model not in ua:
                warnings.append(f"UA 中未见机型标识 {model}")
        if os_version:
            if target_os == "android" and f"Android {os_version}" not in ua:
                warnings.append(f"UA 中 Android 版本与档案不一致（应为 {os_version}）")
            if target_os == "ios" and f"OS {os_version.replace('.', '_')}" not in ua:
                warnings.append(f"UA 中 iOS 版本与档案不一致（应为 {os_version}）")

        # UA-CH 语义：Android Chrome 必有，iOS Safari 必无
        ua_ch = extra.get("uaCh")
        if target_os == "android":
            if not ua_ch:
                warnings.append("Android 缺少 UA-CH 元数据")
            elif model and str(ua_ch.get("model") or "") != model:
                warnings.append("UA-CH model 与 UA 机型标识不一致")
        elif ua_ch:
            warnings.append("iOS 不应携带 UA-CH 元数据")

        # Battery API 语义：Android Chrome 有，iOS Safari 无
        battery = (fingerprint_data.get("fingerprint") or {}).get("battery")
        if target_os == "android" and not (extra.get("hasBattery") and battery):
            warnings.append("Android 缺少 Battery API 数据")
        if target_os == "ios" and battery:
            warnings.append("iOS 不应携带 Battery API 数据")

        # GPU 字符串格式（按平台真实上报格式）
        if target_os == "ios":
            if renderer and renderer != "Apple GPU":
                warnings.append(f"iOS WebGL 渲染器应为 Apple GPU: {renderer[:40]}")
            if str((fingerprint_data.get("fingerprint") or {}).get("videoCard", {}).get("vendor") or "") != "Apple Inc.":
                warnings.append(f"iOS WebGL 厂商应为 Apple Inc.: {(fingerprint_data.get('fingerprint') or {}).get('videoCard', {}).get('vendor')}")
        else:
            if renderer and not renderer.startswith("ANGLE ("):
                warnings.append(f"Android WebGL 渲染器应为 ANGLE 格式: {renderer[:40]}")

    # 固定要素齐全
    seeds = fingerprint_data.get("seeds") or {}
    for key in ("canvas", "audio", "font_spacing"):
        if not seeds.get(key):
            warnings.append(f"噪声种子缺失: {key}")
    if not fingerprint_data.get("fonts"):
        warnings.append("字体子集缺失")

    score = max(50, 100 - 12 * len(warnings))
    return {"score": score, "warnings": warnings}


def summarize(fingerprint_data: dict[str, Any]) -> dict[str, Any]:
    """列表页摘要：UA、平台、屏幕、GPU、模式与健康分。"""
    target_os = fingerprint_data.get("target_os", "windows")
    
    # 移动端指纹摘要
    if target_os in ("android", "ios"):
        fp = fingerprint_data.get("fingerprint", {}) or {}
        nav = fp.get("navigator", {}) or {}
        screen = fp.get("screen", {}) or {}
        extra = fp.get("mobileExtra", {}) or {}

        # 从 videoCard 获取 GPU（移动端可能不存在）
        vc = fp.get("videoCard") or {}
        renderer = vc.get("renderer")

        mode = fingerprint_data.get("mode", "generate")
        screen_desc = None
        if screen.get("width"):
            screen_desc = f"{screen.get('width')}x{screen.get('height')}@{screen.get('colorDepth')}"
            if screen.get("cssWidth"):
                screen_desc += f" (css {screen['cssWidth']}x{screen.get('cssViewportHeight') or screen.get('cssHeight')})"

        return {
            "mode": "真实机型预设 (移动端)" if mode == "preset" else "合成生成 (移动端)",
            "user_agent": nav.get("userAgent"),
            "platform": nav.get("platform"),
            "languages": nav.get("languages"),
            "hardware_concurrency": nav.get("hardwareConcurrency"),
            "device_memory": nav.get("deviceMemory"),
            "max_touch_points": nav.get("maxTouchPoints"),
            "screen": screen_desc,
            "webgl_renderer": renderer,
            "manufacturer": fingerprint_data.get("manufacturer", "Unknown"),
            "device_model": fingerprint_data.get("device_model"),
            # 浏览器口径（safari / chrome_ios），供面板回显；browser 字段为带版本号的展示串
            "browser_kind": extra.get("browser"),
            "os_version": extra.get("osVersion"),
            "browser": f"{extra.get('browser')} {extra.get('browserVersion')}".strip() if extra.get("browser") else None,
            "fonts_count": len(fp.get("fonts", []) or []),
            "seeds": fingerprint_data.get("seeds"),
            "health": fingerprint_data.get("health", {"score": 100, "warnings": []}),
        }
    
    # 桌面端指纹摘要（原有逻辑）
    mode = fingerprint_data.get("mode", "generate")
    if mode == "preset":
        nav = (fingerprint_data.get("preset") or {}).get("navigator") or {}
        screen = (fingerprint_data.get("preset") or {}).get("screen") or {}
        webgl = (fingerprint_data.get("preset") or {}).get("webgl") or {}
        renderer = webgl.get("unmaskedRenderer")
        fonts_count = None
    else:
        fp = fingerprint_data.get("fingerprint") or {}
        nav = fp.get("navigator") or {}
        screen = fp.get("screen") or {}
        renderer = (fp.get("videoCard") or {}).get("renderer")
        fonts_count = len(fp.get("fonts", []) or [])
    return {
        "mode": "真实预设" if mode == "preset" else "合成生成",
        "user_agent": nav.get("userAgent"),
        "platform": nav.get("platform"),
        "languages": nav.get("languages"),
        "hardware_concurrency": nav.get("hardwareConcurrency"),
        "screen": (
            f"{screen.get('width')}x{screen.get('height')}@{screen.get('colorDepth')}"
            if screen.get("width") else None
        ),
        "webgl_renderer": renderer,
        "fonts_count": fonts_count if fonts_count is not None else len(fingerprint_data.get("fonts", []) or []),
        "seeds": fingerprint_data.get("seeds"),
        "health": fingerprint_data.get("health", {"score": 100, "warnings": []}),
    }
