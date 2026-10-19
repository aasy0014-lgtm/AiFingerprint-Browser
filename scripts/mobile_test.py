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
from app.mobile_cdp_injector import _android_webgpu, build_mobile_patch, derive_spec
from app.mobile_fingerprints import (
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
        check(f"[{tag}] platform == Linux armv81", nav["platform"] == "Linux armv81")
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
        check(f"[{tag}] 抹除 Android 不存在的 WebHID（hid）",
              extra["removeNavigatorProps"] == ["hid"])
        check(f"[{tag}] plugins 置空标记（Android 无内置 PDF 插件）",
              extra["emptyPlugins"] is True)
    else:
        check(f"[{tag}] platform == iPhone/iPad", nav["platform"] in ("iPhone", "iPad"))
        check(f"[{tag}] vendor == Apple Computer, Inc.", nav["vendor"] == "Apple Computer, Inc.")
        check(f"[{tag}] GPU == Apple GPU", data["videoCard"]["renderer"] == "Apple GPU")
        check(f"[{tag}] GPU 厂商 == Apple Inc.", data["videoCard"]["vendor"] == "Apple Inc.")
        check(f"[{tag}] 无 UA-CH", extra.get("uaCh") is None)
        check(f"[{tag}] 无 Battery 数据", data.get("battery") is None)
        check(f"[{tag}] deviceMemory 不暴露", nav.get("deviceMemory") is None)
        check(f"[{tag}] Safari 属性差异清单完整",
              {"connection", "pdfViewerEnabled", "userAgentData",
               "getBattery", "deviceMemory", "serial", "usb", "hid", "bluetooth",
               "keyboard", "requestMIDIAccess", "presentation",
               "getInstalledRelatedApps", "vibrate"} <= set(extra["removeNavigatorProps"]))
        check(f"[{tag}] plugins 置空标记", extra["emptyPlugins"] is True)

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
                      "platform": "Linux armv81", "maxTouchPoints": 5, "deviceMemory": 12,
                      "vendor": "Google Inc.", "hardwareConcurrency": 8},
        "screen": {"width": 1440, "height": 3120, "devicePixelRatio": 3, "colorDepth": 30},
        "videoCard": {"vendor": "Qualcomm", "renderer": "Adreno (TM) 740"},
        "battery": {"level": 0.5, "charging": False, "chargingTime": 0, "dischargingTime": 1000},
    }
    spec = derive_spec(legacy, "android")
    check("旧结构：CSS 由物理/DPR 推算", spec["cssWidth"] == 480 and spec["cssHeight"] == 1040)
    check("旧结构：deviceMemory 规范化为 8", spec["deviceMemory"] == 8)
    check("旧结构：视口高回退为 CSS 高", spec["viewportHeight"] == 1040)
    check("旧结构 Android：默认抹除 WebHID", spec["removeProps"] == ["hid"])
    check("旧结构 Android：plugins 置空（平台事实，不依赖存量标记）",
          spec["emptyPlugins"] is True)
    spec_ios = derive_spec(legacy, "ios")
    check("旧结构 iOS：默认抹除 Safari 缺失属性",
          set(spec_ios["removeProps"]) == {
              "connection", "pdfViewerEnabled", "userAgentData", "getBattery",
              "deviceMemory", "serial", "usb", "hid", "bluetooth", "keyboard",
              "requestMIDIAccess", "presentation", "getInstalledRelatedApps", "vibrate"})


def main() -> None:
    print("移动端真实机型指纹库 —— 数据层测试")
    devices = test_library()
    test_android(devices)
    test_ios(devices)
    test_preset_determinism()
    test_generate_randomness(devices)
    test_engines_compat()

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
