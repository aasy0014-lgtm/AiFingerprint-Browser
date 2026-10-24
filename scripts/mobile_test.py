#!/usr/bin/env python3
"""移动端真实机型指纹库测试（数据层，不启动浏览器）

逐项校验：
  1. 机型库加载与品牌清单（/api/v1/mobile-devices 的数据源）
  2. UA 格式（Android Chrome / iOS Safari，含平板差异）
  3. 屏幕一致性（物理 / DPR / CSS / 首屏视口高）与 colorDepth=24
  4. GPU 字符串格式（Android=ANGLE 格式；iOS=Apple GPU）
  5. Android：UA-CH 与 UA 相互印证、deviceMemory 上报桶、Battery API
  6. iOS：无 UA-CH / 无 Battery、Safari 属性差异清单（运行期抹除）
  7. 机型档案印证（硬件并发 / 屏幕 / 触控 == 机型库记录）
  8. preset 确定性（系统/浏览器版本取主流组合）与 generate 随机性
  9. 体检 check_health 无告警；summarize / matrix 兼容

用法：
    .venv/bin/python scripts/mobile_test.py
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.fingerprint_engine import create_fingerprint, summarize
from app.mobile_cdp_injector import (
    _android_webgpu,
    _ua_ios_version,
    build_mobile_patch,
    derive_spec,
)
from app.mobile_fingerprints import (
    _ios_av1_capable,
    _ios_handlers,
    _kernel_browser,
    create_mobile_fingerprint,
    library_info,
    list_brands,
)

_PASS = 0
_FAIL = 0
_FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print(f"  ✅ {name}")
    else:
        _FAIL += 1
        _FAILURES.append(name)
        print(f"  ❌ {name}" + (f" —— {detail}" if detail else ""))


def section(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def _set_ios_version(fp: dict, ver: str) -> None:
    """把 profile 的 osVersion 与 UA 一起改成 ver（F-140 后 UA 是权威来源）。"""
    fp.setdefault("mobileExtra", {})["osVersion"] = ver
    nav = fp.get("navigator") or {}
    ua = str(nav.get("userAgent") or "")
    m = re.search(r"CPU (?:iPhone )?OS [\d_]+ like Mac OS X|CPU OS [\d_]+ ", ua)
    if not m:
        return
    token = ver.replace(".", "_")
    if "CPU iPhone OS" in m.group(0):
        repl = f"CPU iPhone OS {token} like Mac OS X"
    else:
        repl = f"CPU OS {token} "
    nav["userAgent"] = ua[:m.start()] + repl + ua[m.end():]
    fp["navigator"] = nav


# ------------------------------------------------------------------ 机型库
def load_device_index() -> dict:
    lib = json.loads((Path(__file__).parent.parent / "app" / "data" / "mobile_devices.json")
                     .read_text(encoding="utf-8"))
    return {d["market_name"]: d for d in lib["devices"]}


_RE_ANDROID_UA = re.compile(
    r"^Mozilla/5\.0 \(Linux; Android (?P<os>\d+(?:\.\d+)?); (?P<model>.+?)\) "
    r"AppleWebKit/537\.36 \(KHTML, like Gecko\) Chrome/(?P<major>\d+)\.0\.0\.0 "
    r"(?P<mobile>Mobile )?Safari/537\.36$"
)
_RE_IOS_UA = re.compile(
    r"^Mozilla/5\.0 \((?P<dev>iPhone|iPad); CPU (?P<prefix>iPhone )?OS (?P<os>[\d_]+) like Mac OS X\) "
    r"AppleWebKit/605\.1\.15 \(KHTML, like Gecko\) Version/(?P<ver>\d+\.\d+) Mobile/15E148 Safari/604\.1$"
)
_RE_IOS_CHROME_UA = re.compile(
    r"^Mozilla/5\.0 \((?P<dev>iPhone|iPad); CPU (?P<prefix>iPhone )?OS (?P<os>[\d_]+) like Mac OS X\) "
    r"AppleWebKit/605\.1\.15 \(KHTML, like Gecko\) CriOS/(?P<major>\d+)\.0\.0\.0 "
    r"Mobile/15E148 Safari/604\.1$"
)


def verify_fp_consistency(fp: dict, target_os: str, devices: dict) -> None:
    """对一套生成结果做全量一致性断言（每条都会计入测试结果）。"""
    data = fp["fingerprint"]
    nav = data["navigator"]
    screen = data["screen"]
    extra = data["mobileExtra"]
    ua = nav["userAgent"]
    tag = f"{fp['manufacturer']} {fp['device_model']}"

    # --- UA 格式 ---
    if target_os == "android":
        m = _RE_ANDROID_UA.match(ua)
        check(f"[{tag}] Android UA 格式", m is not None, ua[:100])
        if m:
            check(f"[{tag}] UA 机型 == 档案机型", m.group("model") == extra["model"], m.group("model"))
            check(f"[{tag}] UA 系统版本 == 档案", m.group("os") == extra["osVersion"])
            check(f"[{tag}] UA Appendix 与设备类型匹配",
                  (m.group("mobile") is not None) == (extra["deviceType"] == "phone"))
    else:
        is_crios = extra.get("browser") == "chrome_ios"
        m = (_RE_IOS_CHROME_UA if is_crios else _RE_IOS_UA).match(ua)
        check(f"[{tag}] iOS {'Chrome（CriOS）' if is_crios else 'Safari'} UA 格式",
              m is not None, ua[:110])
        if m:
            check(f"[{tag}] UA 设备类型匹配",
                  (m.group("dev") == "iPad") == (extra["deviceType"] == "tablet"))
            check(f"[{tag}] UA 系统版本 == 档案",
                  m.group("os") == extra["osVersion"].replace(".", "_"))
            if is_crios:
                check(f"[{tag}] CriOS 主版本 == 档案浏览器版本",
                      m.group("major") == extra["browserVersion"].split(".")[0])
            else:
                check(f"[{tag}] Version 取主次版本",
                      m.group("ver") == ".".join(extra["osVersion"].split(".")[:2]))
            check(f"[{tag}] iPhone 前缀正确（平板无 iPhone 前缀）",
                  (m.group("prefix") is not None) == (extra["deviceType"] == "phone"))

    # --- 屏幕一致性 ---
    dpr = screen["devicePixelRatio"]
    check(f"[{tag}] CSS 宽 == 物理宽/DPR", abs(screen["cssWidth"] - round(screen["width"] / dpr)) <= 1)
    check(f"[{tag}] CSS 高 == 物理高/DPR", abs(screen["cssHeight"] - round(screen["height"] / dpr)) <= 1)
    check(f"[{tag}] 首屏视口 < 屏幕高（浏览器 UI 占位）",
          0.5 * screen["cssHeight"] <= screen["cssViewportHeight"] < screen["cssHeight"])
    check(f"[{tag}] colorDepth/pixelDepth == 24",
          screen["colorDepth"] == 24 and screen["pixelDepth"] == 24)

    # --- 机型档案印证 ---
    rec = devices.get(data["deviceModel"])
    check(f"[{tag}] 机型在库中", rec is not None)
    if rec:
        check(f"[{tag}] 硬件并发 == 档案", nav["hardwareConcurrency"] == rec["cpu_cores"],
              f"{nav['hardwareConcurrency']} vs {rec['cpu_cores']}")
        check(f"[{tag}] 物理屏幕/DPR == 档案",
              (screen["width"], screen["height"], dpr) ==
              (rec["screen"]["w"], rec["screen"]["h"], rec["screen"]["dpr"]))
        check(f"[{tag}] CSS/视口 == 档案",
              (screen["cssWidth"], screen["cssHeight"], screen["cssViewportHeight"]) ==
              (rec["screen"]["css_w"], rec["screen"]["css_h"], rec["screen"]["viewport_h"]))
        check(f"[{tag}] GPU == 档案", data["videoCard"]["renderer"] == rec["gpu"]["renderer"])
        check(f"[{tag}] 触控点 == 档案", nav["maxTouchPoints"] == rec["max_touch_points"])
        check(f"[{tag}] 系统版本 ∈ 档案版本池",
              extra["osVersion"] in (rec.get("os_versions") or rec.get("ios_versions") or {}))

    # --- 平台语义 ---
    if target_os == "android":
        check(f"[{tag}] platform == Linux aarch64", nav["platform"] == "Linux aarch64")
        check(f"[{tag}] vendor == Google Inc.", nav["vendor"] == "Google Inc.")
        check(f"[{tag}] GPU 为 ANGLE 格式", data["videoCard"]["renderer"].startswith("ANGLE ("))
        check(f"[{tag}] deviceMemory 为上报表", nav["deviceMemory"] in (1, 2, 4, 8),
              str(nav["deviceMemory"]))
        ua_ch = extra.get("uaCh")
        check(f"[{tag}] UA-CH 存在", isinstance(ua_ch, dict))
        if ua_ch:
            check(f"[{tag}] UA-CH model == UA 机型", ua_ch["model"] == extra["model"])
            check(f"[{tag}] UA-CH platformVersion == 系统版本",
                  ua_ch["platformVersion"] == f"{extra['osVersion']}.0.0")
            check(f"[{tag}] UA-CH mobile 与设备类型匹配",
                  ua_ch["mobile"] == (extra["deviceType"] == "phone"))
            check(f"[{tag}] UA-CH 主版本 == UA 主版本",
                  all(b["version"] == ua_ch["fullVersion"].split(".")[0]
                      for b in ua_ch["brands"] if b["brand"] in ("Chromium", "Google Chrome")))
        battery = data.get("battery")
        check(f"[{tag}] Battery API 数据存在且合法",
              isinstance(battery, dict) and 0 <= battery["level"] <= 1)
        check(f"[{tag}] hasUaCh/hasBattery 标记", extra["hasUaCh"] and extra["hasBattery"])
        check(f"[{tag}] 抹除 Android 不存在的 WebHID 与协议处理器（hid / rph）",
              extra["removeNavigatorProps"] == [
                  "hid", "windowControlsOverlay",
                  "registerProtocolHandler", "unregisterProtocolHandler",
                  "setAppBadge"])
        check(f"[{tag}] plugins 置空标记（Android 无内置 PDF 插件）",
              extra["emptyPlugins"] is True)
        check(f"[{tag}] NetworkInformation.type 标记（仅 Android 实现）",
              extra.get("connectionType") == "wifi")
        check(f"[{tag}] downlinkMax == 1300（LTE 档，非 Infinity）",
              extra.get("downlinkMax") == 1300.0)
        check(f"[{tag}] WebGL 规格剔除桌面专属扩展",
              "WEBGL_compressed_texture_s3tc" in (extra.get("webglProfile") or {}).get("dropExtensions", [])
              and "EXT_texture_compression_bptc" in extra["webglProfile"]["dropExtensions"])
        check(f"[{tag}] WebGL 规格保留计时扩展（Android 驱动导出）",
              "EXT_disjoint_timer_query" not in extra["webglProfile"]["dropExtensions"])
        check(f"[{tag}] WebGL 上限为移动档",
              extra["webglProfile"]["limits"]["MAX_TEXTURE_SIZE"] == 16384
              and extra["webglProfile"]["limits"]["MAX_SAMPLES"] == 4)
        # F-165：Adreno 走 ANGLE GLES 后端，经 min(1024, native) 与
        # ANGLE_LIMIT_CAP 后落在宿主之下，可如实声明（Mali/Xclipse 不动）。
        check(f"[{tag}] WebGL 上限按 GPU 家族覆盖（Adreno 256 vectors / 14 blocks）",
              (extra["webglProfile"]["limits"]["MAX_VERTEX_UNIFORM_VECTORS"] == 256
               and extra["webglProfile"]["limits"]["MAX_VERTEX_UNIFORM_BLOCKS"] == 14
               and extra["webglProfile"]["limits"]["MAX_COMBINED_UNIFORM_BLOCKS"] == 28)
              if "Adreno" in data["videoCard"]["renderer"]
              else "MAX_VERTEX_UNIFORM_COMPONENTS" not in extra["webglProfile"]["limits"])
        check(f"[{tag}] 编解码表只校准 HEVC（ogg/webm 内核原生已可播）",
              set(extra.get("codecCapability") or {})
              == {'video/mp4; codecs="hvc1"', 'video/mp4; codecs="hvc1.1.6.l93.b0"'})
    else:
        check(f"[{tag}] platform == iPhone/iPad", nav["platform"] in ("iPhone", "iPad"))
        check(f"[{tag}] vendor == Apple Computer, Inc.", nav["vendor"] == "Apple Computer, Inc.")
        check(f"[{tag}] GPU == Apple GPU", data["videoCard"]["renderer"] == "Apple GPU")
        check(f"[{tag}] GPU 厂商 == Apple Inc.", data["videoCard"]["vendor"] == "Apple Inc.")
        check(f"[{tag}] 无 UA-CH", extra.get("uaCh") is None)
        check(f"[{tag}] 无 Battery 数据", data.get("battery") is None)
        check(f"[{tag}] deviceMemory 不暴露", nav.get("deviceMemory") is None)
        check(f"[{tag}] Safari 属性差异清单完整",
              {"connection", "userAgentData",
               "getBattery", "deviceMemory", "serial", "usb", "hid", "bluetooth",
               "keyboard", "requestMIDIAccess", "presentation",
               "getInstalledRelatedApps", "vibrate"} <= set(extra["removeNavigatorProps"]))
        check(f"[{tag}] pdfViewerEnabled 不误删（Safari 16.4+ 真实能力位）",
              "pdfViewerEnabled" not in extra["removeNavigatorProps"])
        check(f"[{tag}] plugins 置空标记", extra["emptyPlugins"] is True)
        check(f"[{tag}] 无 downlinkMax（Safari 无 NetworkInformation）",
              extra.get("downlinkMax") is None)
        check(f"[{tag}] WebGL 规格额外剔除计时扩展（Apple 物理封禁）",
              "EXT_disjoint_timer_query" in (extra.get("webglProfile") or {}).get("dropExtensions", [])
              and "EXT_disjoint_timer_query_webgl2" in extra["webglProfile"]["dropExtensions"])
        codecs = extra.get("codecCapability") or {}
        check(f"[{tag}] 编解码表：ogg/webm 整族为空串（AVFoundation 容器表无此项）",
              all(codecs.get(k) == "" for k in
                  ("audio/ogg", "audio/webm", "video/webm", "video/ogg")))
        check(f"[{tag}] 编解码表：HEVC 仅 hvc1 可播，hev1 为空串",
              codecs.get('video/mp4; codecs="hvc1"') == "probably"
              and codecs.get('video/mp4; codecs="hev1"') == "")
        # F-123：档案自带表的 av01 与机型能力位同口径（A17 Pro 起可解）
        check(f"[{tag}] 编解码表：av01 按机型能力位",
              codecs.get('video/mp4; codecs="av01.0.05m.08"') == (
                  "probably" if _ios_av1_capable(
                      str(extra.get("model") or ""),
                      str(extra.get("marketName") or "")) else ""))

    # --- 通用 ---
    check(f"[{tag}] navigator.mobile 未伪造", "mobile" not in nav)
    check(f"[{tag}] 字体非空", bool(data["fonts"]))
    check(f"[{tag}] 种子齐全", set(fp["seeds"]) == {"canvas", "audio", "font_spacing"})
    check(f"[{tag}] 体检无告警", fp["health"]["score"] == 100 and not fp["health"]["warnings"],
          str(fp["health"]))


def test_library() -> dict:
    section("1. 机型库加载")
    info = library_info()
    print(f"  版本: {info['version']}  更新: {info['updated']}  机型数: {info['count']}")
    check("机型数量充足（≥30）", info["count"] >= 30)
    brands = list_brands()
    print(f"  品牌: {brands}")
    check("Android 品牌齐全（含 Samsung/Google/Xiaomi/OnePlus/Honor）",
          {"Samsung", "Google", "Xiaomi", "OnePlus", "Honor"} <= set(brands["android"]))
    check("iOS 品牌仅 Apple", brands["ios"] == ["Apple"])
    return load_device_index()


def test_android(devices: dict) -> None:
    section("2. Android 指纹（preset / generate / 品牌筛选）")
    for mode in ("preset", "generate"):
        fp = create_mobile_fingerprint("android", mode, "Samsung")
        print(f"\n[{mode}] {fp['manufacturer']} {fp['device_model']} | "
              f"Android {fp['fingerprint']['mobileExtra']['osVersion']} | "
              f"Chrome {fp['fingerprint']['mobileExtra']['browserVersion']}")
        verify_fp_consistency(fp, "android", devices)

    print("\n[generate] 品牌 = Xiaomi")
    fp = create_mobile_fingerprint("android", "generate", "Xiaomi")
    check("品牌筛选生效", fp["manufacturer"] == "Xiaomi", fp["manufacturer"])

    print("[generate] 品牌 = 不存在的 Foo（应回退随机）")
    fp = create_mobile_fingerprint("android", "generate", "Foo")
    check("未知品牌回退到全库", fp["manufacturer"] in list_brands()["android"], fp["manufacturer"])


def test_ios(devices: dict) -> None:
    section("3. iOS 指纹（preset / generate × Safari / Chrome）")
    for mode in ("preset", "generate"):
        fp = create_mobile_fingerprint("ios", mode, "Apple")
        print(f"\n[{mode}] {fp['manufacturer']} {fp['device_model']} | "
              f"iOS {fp['fingerprint']['mobileExtra']['osVersion']}")
        verify_fp_consistency(fp, "ios", devices)
    for mode in ("preset", "generate"):
        fp = create_mobile_fingerprint("ios", mode, "Apple", browser="chrome_ios")
        extra = fp["fingerprint"]["mobileExtra"]
        print(f"\n[{mode}/chrome_ios] {fp['manufacturer']} {fp['device_model']} | "
              f"CriOS {extra['browserVersion']}")
        verify_fp_consistency(fp, "ios", devices)
        check("iOS Chrome：browser 口径落库", extra["browser"] == "chrome_ios")
        check("iOS Chrome：CriOS 版本跟随内核/版本池",
              bool(extra["browserVersion"]) and "CriOS/" in fp["fingerprint"]["navigator"]["userAgent"])
        spec = derive_spec(fp["fingerprint"], "ios")
        check("iOS Chrome：注入层识别 iosChrome", bool(spec.get("iosChrome")))
        check("iOS Chrome：仍无 UA-CH / Battery",
              spec["uaCh"] is None and spec["battery"] is None)

    # Android 口径互斥
    try:
        create_mobile_fingerprint("android", "generate", browser="chrome_ios")
        check("Android 拒绝 chrome_ios 口径", False, "未抛错")
    except ValueError:
        check("Android 拒绝 chrome_ios 口径", True)
    # 未知口径拒绝
    try:
        create_mobile_fingerprint("ios", "generate", browser="crios")
        check("iOS 拒绝未知浏览器口径", False, "未抛错")
    except ValueError:
        check("iOS 拒绝未知浏览器口径", True)


def test_preset_determinism() -> None:
    section("4. preset 确定性（版本取主流组合）")
    versions = set()
    os_versions = set()
    for _ in range(12):
        fp = create_mobile_fingerprint("android", "preset")
        extra = fp["fingerprint"]["mobileExtra"]
        versions.add(extra["browserVersion"])
        os_versions.add(extra["osVersion"])
    print(f"  Chrome 构建: {versions}")
    print(f"  Android 版本: {os_versions}")
    kernel_ver = (_kernel_browser() or {}).get("full")
    expected = {kernel_ver} if kernel_ver else {"156.0.8078.35"}
    check(f"preset 的 Chrome 构建固定（{'跟随内核真实版本 ' + kernel_ver if kernel_ver else '无内核回退库内主流 156.0.8078.35'}）",
          versions == expected, str(versions))

    # 每个机型的 preset 系统版本应为其档案中权重最高的版本
    lib = json.loads((Path(__file__).parent.parent / "app" / "data" / "mobile_devices.json")
                     .read_text(encoding="utf-8"))
    by_name = {d["market_name"]: d for d in lib["devices"]}
    ok = True
    for _ in range(20):
        fp = create_mobile_fingerprint("android", "preset")
        rec = by_name[fp["device_model"]]
        best = max(rec["os_versions"].items(), key=lambda kv: (kv[1], tuple(map(int, kv[0].split(".")))))[0]
        if fp["fingerprint"]["mobileExtra"]["osVersion"] != best:
            ok = False
    check("preset 系统版本 == 该机型权重最高版本", ok)


def test_generate_randomness(devices: dict) -> None:
    section("5. generate 随机性与批量一致性（40 套）")
    seen_ua: set[str] = set()
    seen_chrome: set[str] = set()
    bad = 0
    for i in range(20):
        for os_ in ("android", "ios"):
            fp = create_mobile_fingerprint(os_, "generate")
            seen_ua.add(fp["fingerprint"]["navigator"]["userAgent"])
            seen_chrome.add(fp["fingerprint"]["mobileExtra"]["browserVersion"])
            if fp["health"]["warnings"]:
                bad += 1
    check("40 套均无体检告警", bad == 0, f"{bad} 套告警")
    check("UA 组合多元（≥12 种）", len(seen_ua) >= 12, f"{len(seen_ua)} 种")
    print(f"  实测组合: {len(seen_ua)} 种 UA，浏览器构建 {sorted(seen_chrome)}")


def test_engines_compat() -> None:
    section("6. 引擎兼容（summarize / matrix / 注入层规格 / 旧结构兼容）")
    from app.matrix import _features

    fp = create_fingerprint("android", "preset", "Google")
    s = summarize(fp)
    check("summarize 移动端 preset 标签", s["mode"] == "真实机型预设 (移动端)", s["mode"])
    check("summarize 含机型信息", bool(s["device_model"]) and s["manufacturer"] == "Google")
    check("summarize 含浏览器口径", s.get("browser_kind") == "chrome")
    fp["id"] = "test-1"
    fp["name"] = "test-1"
    feat = _features({"id": "test-1", "name": "test-1", "target_os": "android", "fingerprint": fp})
    check("matrix 特征提取 GPU 非未知", feat["gpu"] not in (None, "未知"), str(feat["gpu"]))

    # 新版结构 -> 注入层规格
    spec = derive_spec(fp["fingerprint"], "android")
    check("注入层读取 UA-CH", spec["uaCh"] is not None)
    check("注入层读取 Battery", spec["battery"] is not None)
    check("注入层读取视口高（< CSS 高）", spec["viewportHeight"] < spec["cssHeight"])
    check("注入层 deviceMemory 为上报表", spec["deviceMemory"] in (1, 2, 4, 8))
    check("Android 无 CSS 版本门控（F-89 仅 iOS）", spec.get("cssGate") == [])

    # ---- F-140：UA 与 mobileExtra.osVersion 脱钩时以 UA 为准 ----
    # 固定造一个 18.6 的 UA，再把 osVersion 谎报成 27.0：门控必须按 UA
    # 走（26+ 的 WebGPU 族、27 的 getDisplayMedia 都不该放行）。
    _drift = create_fingerprint("ios", "generate")["fingerprint"]
    _set_ios_version(_drift, "18.6")
    _drift["mobileExtra"]["osVersion"] = "27.0"
    _drift_spec = derive_spec(_drift, "ios")
    check("F-140：osVersion 谎报 27.0 而 UA 是 18.6 → 门控按 UA（WebGPU 仍删）",
          "GPU" in _drift_spec["removeGlobals"]
          and "WebTransport" in _drift_spec["removeGlobals"])

    # ---- F-89：CSS.supports 白名单按 profile 版本门控 ----
    def _ios_spec_at(ver: str) -> dict:
        base = create_fingerprint("ios", "generate")
        # F-140：UA 是门控的权威来源，只改 osVersion 会被 UA 覆盖回去，
        # 所以这里连 UA 一起改成同版本（模拟「一致的 profile」）。
        _set_ios_version(base["fingerprint"], ver)
        return derive_spec(base["fingerprint"], "ios")

    check("iOS 15.7 profile 门控 3 项 CSS 属性（-webkit-hyphens 自 4.2 起存在，不门控）",
          sorted(_ios_spec_at("15.7")["cssGate"])
          == sorted(["-webkit-backdrop-filter", "margin-trim", "text-autospace"]))
    check("iOS 16.4 profile 只剩 18.x 档的 text-autospace 与 backdrop-filter",
          sorted(_ios_spec_at("16.4")["cssGate"])
          == sorted(["-webkit-backdrop-filter", "text-autospace"]))
    check("iOS 18.4 profile 无 CSS 门控项", _ios_spec_at("18.4")["cssGate"] == [])

    # ---- WebGPU 适配器口径（内核 148 二进制内嵌的 Dawn gpu_info 字面量实证：
    #      Qualcomm 代际 adreno-Nxx / ARM valhall·gen-5 / Samsung rdna-N /
    #      ImgTec d-series；非库内 GPU 不伪造）----
    for renderer, want in (
        ("ANGLE (Qualcomm, Adreno (TM) 750, OpenGL ES 3.2)", ("qualcomm", "adreno-7xx")),
        ("ANGLE (Qualcomm, Adreno (TM) 830, OpenGL ES 3.2)", ("qualcomm", "adreno-8xx")),
        ("ANGLE (ARM, Mali-G715-Immortalis MC10, OpenGL ES 3.2)", ("arm", "gen-5")),
        ("ANGLE (ARM, Mali-G78 MP14, OpenGL ES 3.2)", ("arm", "valhall")),
        ("ANGLE (Samsung, Xclipse 530, OpenGL ES 3.2)", ("samsung", "rdna-2")),
        ("ANGLE (Imagination Technologies, PowerVR DXT-48-1536, OpenGL ES 3.2)",
         ("img-tec", "d-series")),
    ):
        w = _android_webgpu(renderer) or {}
        check(f"WebGPU 口径 {want[0]}/{want[1]}",
              (w.get("vendor"), w.get("architecture")) == want, renderer)
    check("WebGPU 口径：库外 GPU 不伪造", _android_webgpu("ANGLE (NVIDIA, RTX 4090, ...)") is None)
    lib_all = json.loads((Path(__file__).parent.parent / "app" / "data" / "mobile_devices.json")
                         .read_text(encoding="utf-8"))["devices"]
    uncovered = [d["market_name"] for d in lib_all if d.get("platform") == "android"
                 and _android_webgpu((d.get("gpu") or {}).get("renderer", "")) is None]
    check("WebGPU 口径：库内 Android 机型全覆盖", not uncovered, str(uncovered[:4]))

    # 旧版结构（无 mobileExtra / css 字段）兼容
    legacy = {
        "navigator": {"userAgent": "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 "
                                  "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
                      "platform": "Linux aarch64", "maxTouchPoints": 5, "deviceMemory": 12,
                      "vendor": "Google Inc.", "hardwareConcurrency": 8},
        "screen": {"width": 1440, "height": 3120, "devicePixelRatio": 3, "colorDepth": 30},
        "videoCard": {"vendor": "Qualcomm", "renderer": "Adreno (TM) 740"},
        "battery": {"level": 0.5, "charging": False, "chargingTime": 0, "dischargingTime": 1000},
    }
    spec = derive_spec(legacy, "android")
    check("旧结构：CSS 由物理/DPR 推算", spec["cssWidth"] == 480 and spec["cssHeight"] == 1040)
    check("旧结构：deviceMemory 规范化为 8", spec["deviceMemory"] == 8)
    check("旧结构：视口高回退为 CSS 高", spec["viewportHeight"] == 1040)
    check("旧结构 Android：默认抹除 WebHID 与协议处理器",
          spec["removeProps"] == ["hid", "windowControlsOverlay",
                                  "registerProtocolHandler",
                                  "unregisterProtocolHandler", "setAppBadge"])
    check("旧结构 Android：plugins 置空（平台事实，不依赖存量标记）",
          spec["emptyPlugins"] is True)
    check("旧结构 Android：NetworkInformation.type 兜底", spec["connectionType"] == "wifi")
    spec_ios = derive_spec(legacy, "ios")
    check("旧结构 iOS：默认抹除 Safari 缺失属性",
          {"connection", "userAgentData", "getBattery",
           "deviceMemory", "serial", "usb", "hid", "bluetooth", "keyboard",
           "requestMIDIAccess", "presentation", "getInstalledRelatedApps", "vibrate",
           "doNotTrack", "scheduling", "storageBuckets", "windowControlsOverlay"}
          <= set(spec_ios["removeProps"]))
    check("旧结构 iOS：反向保留（真机存在的成员不得进删除清单）",
          not {"devicePosture", "pdfViewerEnabled"} & set(spec_ios["removeProps"]))
    # F-90：setAppBadge/clearAppBadge 由 AppBadgeEnabled 门控，iOS 默认关，
    # 普通浏览上下文缺席——与 Notification/Push 同属删除面。
    check("旧结构 iOS：setAppBadge/clearAppBadge 在删除清单内",
          {"setAppBadge", "clearAppBadge"} <= set(spec_ios["removeProps"]))
    legacy_pdf = dict(legacy)
    legacy_pdf["mobileExtra"] = {"removeNavigatorProps": ["connection", "pdfViewerEnabled"]}
    check("旧结构 iOS：存量档案里的 pdfViewerEnabled 运行期归一化剔除",
          derive_spec(legacy_pdf, "ios")["removeProps"] == ["connection"])
    check("旧结构 iOS：无 NetworkInformation.type（Safari 无该 API）",
          spec_ios["connectionType"] is None)

    # ---- F-103 ~ F-115：嵌套原型成员差集（§40~§43）----
    dm_ios = spec_ios["dropMembers"]
    dm_and = spec["dropMembers"]

    def _dropped(table: dict, ctor: str, names) -> bool:
        return set(names) <= set(table.get(ctor) or [])

    check("F-106：WebGL1/WebGL2 两个宿主都删 4 名（Blink 各生成一份）",
          _dropped(dm_ios, "WebGLRenderingContext",
                   ["makeXRCompatible", "unpackColorSpace",
                    "drawingBufferStorage", "drawingBufferFormat"])
          and _dropped(dm_ios, "WebGL2RenderingContext",
                       ["makeXRCompatible", "unpackColorSpace",
                        "drawingBufferStorage", "drawingBufferFormat"]))
    check("F-103：Capture Handle 双端删除；旧结构 iOS 仍删 getDisplayMedia",
          _dropped(dm_ios, "MediaDevices",
                   ["getDisplayMedia", "setCaptureHandleConfig"])
          and _dropped(dm_ios, "MediaStreamTrack",
                       ["getCaptureHandle", "oncapturehandlechange"])
          and _dropped(dm_and, "MediaDevices",
                       ["getDisplayMedia", "setCaptureHandleConfig"])
          and _dropped(dm_and, "MediaStreamTrack", ["getCaptureHandle"]))
    _ios27 = dict(legacy)
    _ios27["mobileExtra"] = {"osVersion": "27.0.1"}
    check("F-103：iOS 27 按版本只摘 getDisplayMedia，Capture Handle 仍删",
          (derive_spec(_ios27, "ios")["dropMembers"].get("MediaDevices")
           or []) == ["setCaptureHandleConfig"])
    check("F-104：iOS Canvas 2D 六项删除，filter 保留",
          _dropped(dm_ios, "CanvasRenderingContext2D",
                   ["fontKerning", "fontStretch", "fontVariantCaps",
                    "isContextLost", "lang", "textRendering"])
          and "filter" not in (dm_ios.get("CanvasRenderingContext2D") or [])
          and "CanvasRenderingContext2D" not in dm_and)
    check("F-105/F-111~F-113：零散与 HTML 元素原型差集",
          _dropped(dm_ios, "MediaMetadata", ["chapterInfo"])
          and _dropped(dm_ios, "MediaRecorder", ["audioBitrateMode"])
          and _dropped(dm_ios, "MediaKeys", ["getStatusForPolicy"])
          and _dropped(dm_ios, "Request",
                       ["isHistoryNavigation", "targetAddressSpace"])
          and _dropped(dm_ios, "HTMLIFrameElement",
                       ["credentialless", "csp", "sharedStorageWritable"])
          and _dropped(dm_ios, "HTMLAreaElement",
                       ["attributionSrc", "interestForElement"])
          and _dropped(dm_ios, "HTMLImageElement", ["sharedStorageWritable"])
          and _dropped(dm_ios, "HTMLElement",
                       ["editContext", "virtualKeyboardPolicy"]))
    check("F-105：Element.startViewTransition 删除，Document 级不受影响",
          _dropped(dm_ios, "Element", ["startViewTransition"])
          and "startViewTransition" not in (dm_ios.get("Document") or []))
    check("F-108：HTMLMediaElement 三项删除，webkit 计数器保留",
          _dropped(dm_ios, "HTMLMediaElement",
                   ["captureStream", "controlsList", "loading"])
          and not {"webkitAudioDecodedByteCount", "webkitVideoDecodedByteCount"}
              & set(dm_ios.get("HTMLMediaElement") or []))
    check("F-109：iOS 删 3 名；Android 只删 phrases/processLocally",
          _dropped(dm_ios, "webkitSpeechRecognition",
                   ["grammars", "phrases", "processLocally"])
          and _dropped(dm_and, "webkitSpeechRecognition",
                       ["phrases", "processLocally"])
          and "grammars" not in (dm_and.get("webkitSpeechRecognition") or []))
    check("F-114/F-115：HTMLElement 六名事件处理器 + Element 四名",
          _dropped(dm_ios, "HTMLElement",
                   ["onbeforexrselect", "oncontextlost", "oncontextrestored",
                    "onpointerrawupdate", "onscrollsnapchange",
                    "onscrollsnapchanging"])
          and _dropped(dm_ios, "Element",
                       ["onbeforecopy", "onbeforecut", "onbeforepaste",
                        "onsearch"])
          and "onsearch" not in (dm_ios.get("HTMLElement") or []))
    check("Android 不继承 iOS 原型删除表（只留 F-103/F-109/F-121 三族）",
          set(dm_and) == {"MediaDevices", "MediaStreamTrack",
                          "webkitSpeechRecognition", "HTMLMediaElement"})

    # ---- F-107：MediaSession.setScreenshareActive 的 add-spec ----
    add_ios = spec_ios.get("addMembers") or {}
    check("F-107：iOS 补 MediaSession.setScreenshareActive",
          any(p.get("ctor") == "MediaSession"
              and p.get("name") == "setScreenshareActive"
              and p.get("kind") == "fn" for p in (add_ios.get("protos") or [])))
    check("F-107：Android 不补（chrome_android=false）",
          not (spec.get("addMembers") or {}))

    # ---- F-115 / F-117：事件处理器落点 ----
    h_ios = {(h.get("ctor"), h.get("name")) for h in (add_ios.get("handlers") or [])}
    check("F-115：onbefore* 补到 HTMLElement（不补 Element 父类）",
          all(("HTMLElement", n) in h_ios and ("Element", n) not in h_ios
              for n in ("onbeforecopy", "onbeforecut", "onbeforepaste")))
    check("F-117：onfocusin/onfocusout 补到 Element",
          all(("Element", n) in h_ios for n in ("onfocusin", "onfocusout")))
    check("F-115：onsearch 不补（Blink 专有事件）",
          not any(n == "onsearch" for _, n in h_ios))
    check("F-118：Element 补 5 名 WebKit 专有处理器（不可枚举）",
          all(("Element", n) in h_ios for n in (
              "onbeforeload", "onwebkitneedkey",
              "onwebkitpresentationmodechanged",
              "onwebkitcurrentplaybacktargetiswirelesschanged",
              "onwebkitplaybacktargetavailabilitychanged"))
          and all(r.get("enum") is False
                  for r in (add_ios.get("handlers") or [])
                  if r.get("ctor") == "Element"))
    check("F-119：body/frameset 补 onpageswap/onpagereveal（可枚举）",
          all((c, n) in h_ios for c in ("HTMLBodyElement",
                                        "HTMLFrameSetElement")
              for n in ("onpageswap", "onpagereveal"))
          and all(r.get("enum") is not False
                  for r in (add_ios.get("handlers") or [])
                  if r.get("ctor") in ("HTMLBodyElement",
                                       "HTMLFrameSetElement")))
    check("F-119：18.2 以下不补（版本门控）",
          not any(h.get("name") in ("onpageswap", "onpagereveal")
                  for h in _ios_handlers((18, 0)))
          and any(h.get("name") == "onpageswap"
                  for h in _ios_handlers((18, 2))))
    check("F-118/F-119：Android 不补",
          not any(r.get("ctor") in ("Element", "HTMLBodyElement",
                                    "HTMLFrameSetElement")
                  for r in ((spec.get("addMembers") or {}).get("handlers") or [])))

    # ---- F-121 / F-122：HTMLMediaElement 的 sinkId 族 ----
    def _ios_drops(ver: str) -> dict:
        base = create_fingerprint("ios", "generate")
        _set_ios_version(base["fingerprint"], ver)
        return derive_spec(base["fingerprint"], "ios").get("dropMembers") or {}

    check("F-121：Android 删 setSinkId/sinkId（BCD chrome_android=false）",
          set(((derive_spec(legacy, "android").get("dropMembers") or {})
               .get("HTMLMediaElement")) or []) == {"setSinkId", "sinkId"})
    check("F-122：iOS <18.4 删 setSinkId/sinkId，18.4+ 保留",
          "setSinkId" in ((_ios_drops("17.5.1").get("HTMLMediaElement")) or [])
          and "setSinkId" not in ((_ios_drops("18.4").get("HTMLMediaElement")) or [])
          and "setSinkId" not in ((_ios_drops("26.6").get("HTMLMediaElement")) or []))
    check("F-122：低版本删除不覆盖 F-108 的三名",
          {"captureStream", "controlsList", "loading"} <=
          set(_ios_drops("17.7").get("HTMLMediaElement") or []))

    # ---- F-98：CSSStyleDeclaration 的 inline 存储 / 枚举 / computed 回读 ----
    src = Path(__file__).parent.parent / "app" / "mobile_cdp_injector.py"
    inj = src.read_text(encoding="utf-8")
    check("F-98：length / item / 数字索引三者并进存储键",
          all(k in inj for k in (
              "var __origCSLength = Object.getOwnPropertyDescriptor(",
              "__csProto.item = __maskNative(__matchLen(function (i) {",
              "var __f98IdxAttach = function (decl) {")))
    check("F-98：索引访问器按声明实例换原型（全局原型不挂数字索引）",
          "var __f98IdxAttach = function (decl) {" in inj
          and "Object.setPrototypeOf(decl, __f98IdxProto)" in inj
          and "Object.defineProperty(__csProto, String(" not in inj)
    check("F-98：-webkit-hyphens 规范名归一为 hyphens",
          "key === '-webkit-hyphens' ? 'hyphens' : key" in inj)
    check("F-98：computed 声明经 __csOwner 回落 inline 存储并归一 sticky",
          "__csOwner.set(cs, el)" in inj
          and "if (computed && key === 'position'" in inj
          and "return 'sticky';" in inj)
    check("F-98：ApplePayButtonStyle 驼峰访问器（真机大写首字母）",
          "'ApplePayButtonStyle': '-apple-pay-button-style'" in inj)

    # ---- F-99 / F-100：computed 序列化口径与表单控件 UA 默认值 ----
    check("F-99：布局长度按 1/64px 量化，物理右侧外边距例外",
          "Math.round(f * 64) / 64" in inj
          and "case 'margin-right': return 'right';" in inj
          and "var __f99PhysSide = function (key, rtl, vert, wm) {" in inj)
    check("F-99：非布局长度走 float32 + %.6f 去尾零",
          "Math.fround(v).toFixed(6)" in inj
          and "s.replace(/0+$/, '').replace(/\\.$/, '')" in inj)
    check("F-99：未参与布局的元素（none/inline/contents）不量化",
          "disp !== 'none' && disp !== 'inline'" in inj
          and "disp !== 'contents'" in inj)
    check("F-99：-webkit-locale 去引号",
          "val.slice(1, -1)" in inj and "['-webkit-locale']" in inj)
    check("F-99：简写折叠与 inline 声明不受影响",
          "var __f99Collapse = function (v) {" in inj
          and "__f99Norm(cs, k, raw, el)" in inj)
    check("F-100：表单控件 computed line-height 归一为 normal",
          "&& val === '13px'" in inj
          and "Reflect.get(t, 'fontSize', t) === '11px'" in inj)
    check("F-100：表单控件 computed cursor 归一（文本类 auto / 按钮类 default）",
          "(val === 'text' || val === 'default')" in inj
          and "__f100NoCursorAuto[ty] ? 'default' : 'auto'" in inj)

    # ---- F-130 / F-134 / F-142：ManagedMediaSource 静态 isTypeSupported ----
    mms_spec = ((spec_ios.get("addMembers") or {}).get("interfaces") or {}) \
        .get("ManagedMediaSource") or {}
    check("F-130：主世界 ManagedMediaSource 带 statics（与 Worker 表对齐）",
          mms_spec.get("statics") == ["!isTypeSupported"])
    check("F-142：页面与 Worker 共用同一张 MSE 真值表（无独立白名单残留）",
          inj.count("__mseSupports(") >= 4
          and "function __mseSupports(raw, opt) {" in inj
          and "var __wkTypes = {" not in inj)
    check("F-142：MediaSource 与 ManagedMediaSource 两处静态各自包装",
          "defineFn(MediaSource, 'isTypeSupported'" in inj
          and "defineFn(ManagedMediaSource, 'isTypeSupported'" in inj)
    check("F-142：真值表开关随平台/机型下发（ios + av1）",
          (spec_ios.get("mseTypes") or {}).get("ios") is True
          and "mseTypes" in inj)
    check("F-142：WebM 的 VP9 受 !ios 门控、legacy 拼写不豁免",
          "if (vp.vp8) { return true; }" in inj
          and "return !ios;" in inj
          and "if (vp.legacy) { return true; }" not in inj)
    check("F-130：无参抛 Not enough arguments（WebKit 文案）",
          "throw new TypeError('Not enough arguments');" in inj)

    # ---- F-135：mediaCapabilities 口径 ----
    # 只换 MediaCapabilities.prototype 两个方法，构造器/实例/SameObject 保持原生。
    check("F-135：只包 decodingInfo/encodingInfo 两个原型方法",
          "defineFn(__mcProto, 'decodingInfo'" in inj
          and "defineFn(__mcProto, 'encodingInfo'" in inj
          and inj.count("__mcProto = window.MediaCapabilities") == 1)
    check("F-135：品牌校验文案与异步 rejection 走 WebKit 口径",
          "'Can only call MediaCapabilities.' + name" in inj
          and "if (self !== inst) {" in inj)
    check("F-135：AV1 由机型能力位门控（opt.av1）",
          "var __mcOpt = { av1: !!D.av1 };" in inj)
    # AVFoundation 的扩展类型闸门问的是系统 isPlayableExtendedMIMEType，AV1
    # 那档答案随机型硬解走——漏了这条分支，能解 AV1 的机型上 file 路径仍判死。
    check("F-135：AVFoundation 闸门对 av01 走机型能力位",
          "if (__mcStarts(codec, 'av01')) { return !!(opt && opt.av1); }" in inj)
    # 闸门的两套表按「带引号 / 未加引号且后面还有分段」选，与参数名无关。
    check("F-135：扩展类型闸门按尾随分段选宽/严两套表",
          "(i === segs.length - 1) ? __mcAvfQuoted : __mcAvfUnquoted" in inj)
    check("F-135：mp4 家族小写 flac 在查表前改写成 fLaC",
          "body.split('flac').join('fLaC')" in inj)
    check("F-135：页面与 Worker 共用同一份 _MC_SRC",
          inj.count("__MC_SRC__") >= 4)


def test_api_model() -> None:
    """P1-7：移动端内核配对在模型层就拦住（MCP / 脚本绕过 HTTP 也生效）。"""
    import pydantic

    from app.models import ProfileCreate

    def err(**kw) -> str:
        try:
            ProfileCreate(name="t", **kw)
            return ""
        except pydantic.ValidationError as e:
            return e.errors()[0]["msg"]

    section("10. API 模型：移动端配对校验（P1-7）")
    check("Android + camoufox 在构造期被拒",
          "仅支持 fp-chromium" in err(target_os="android", kernel="camoufox"))
    check("iOS + chromium 在构造期被拒",
          "仅支持 fp-chromium" in err(target_os="ios", kernel="chromium"))
    check("Android + safari 在构造期被拒",
          "仅支持 chrome" in err(target_os="android", kernel="fp-chromium",
                               browser="safari"))
    check("合法移动端组合通过（Android chrome / iOS chrome_ios）",
          not err(target_os="android", kernel="fp-chromium", browser="chrome")
          and not err(target_os="ios", kernel="fp-chromium", browser="chrome_ios"))
    check("桌面端不受影响（camoufox + windows）",
          not err(kernel="camoufox", target_os="windows"))


def main() -> None:
    print("移动端真实机型指纹库 —— 数据层测试")
    devices = test_library()
    test_android(devices)
    test_ios(devices)
    test_preset_determinism()
    test_generate_randomness(devices)
    test_engines_compat()
    test_api_model()

    section(f"结果：{_PASS} 通过 / {_FAIL} 失败")
    if _FAIL:
        for name in _FAILURES:
            print(f"  ❌ {name}")
        sys.exit(1)
    print("✅ 全部通过")
    print("\n真机级运行期验证（需启动浏览器）:")
    print("  .venv/bin/python scripts/mobile_launch_verify.py android")
    print("  .venv/bin/python scripts/mobile_launch_verify.py ios")


if __name__ == "__main__":
    main()
