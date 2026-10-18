#!/usr/bin/env python3
"""移动端环境真实启动验证（fp-chromium 内核 + CDP 移动仿真）

与 scripts/mobile_test.py（纯数据结构）和 scripts/test_mobile_end_to_end.py
（流程演示）不同，本脚本会**真实启动浏览器**并连接 CDP 读取页面内的实际值，
逐项核对移动端真实化仿真是否生效：

  1. UA / platform / maxTouchPoints / hardwareConcurrency 与存储指纹一致
  2. navigator.mobile 不存在（不伪造非标准属性）
  3. DPR / screen 为 CSS 逻辑像素；innerHeight == 首屏视口高度（浏览器 UI 占位）
  4. screen.colorDepth == 24（宿主 macOS 报告 30，属穿帮点）
  5. WebGL 非遮蔽厂商/渲染器 == 指纹中的真机 GPU 字符串
  6. Android：navigator.userAgentData（UA-CH）完整可用 + Sec-CH-UA* 请求头
  7. Android：navigator.deviceMemory（上报桶）+ Battery API（数值一致）
  8. iOS：navigator.userAgentData / connection / pdfViewerEnabled 不存在，
     plugins/mimeTypes 为空，navigator.standalone 存在且为 false
  9. navigator 上无 own-property 泄漏（JS 补丁改的是原型）

用独立临时数据目录运行，不污染正式数据。

用法：
    .venv/bin/python scripts/mobile_launch_verify.py [android|ios] [generate|preset]
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必须在导入 app.config 之前重定位数据目录
os.environ["FPWB_HOME"] = tempfile.mkdtemp(prefix="fpwb_mobile_verify_")

from app import db
from app.config import ensure_dirs
from app.fingerprint_engine import create_fingerprint
from app.kernels.fp_chromium_kernel import is_available
from app.launcher import LaunchManager
from app.mobile_cdp_injector import sec_ch_headers

STATE_JS = """async () => {
    const nav = navigator;
    const out = {
        url: location.href,
        userAgent: nav.userAgent,
        platform: nav.platform,
        maxTouchPoints: nav.maxTouchPoints,
        mobileProp: nav.mobile,
        hasMobileProp: 'mobile' in nav,
        vendor: nav.vendor,
        deviceMemory: nav.deviceMemory,
        hardwareConcurrency: nav.hardwareConcurrency,
        language: nav.language,
        languages: nav.languages,
        uaData: 'ABSENT',
        uaDataHigh: null,
        battery: null,
        hasGetBattery: typeof nav.getBattery === 'function',
        hasConnection: 'connection' in nav,
        hasPdfViewer: 'pdfViewerEnabled' in nav,
        hasStandalone: 'standalone' in nav,
        standalone: nav.standalone,
        pluginsLen: nav.plugins.length,
        mimeTypesLen: nav.mimeTypes.length,
        dpr: window.devicePixelRatio,
        screenW: screen.width, screenH: screen.height,
        colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth,
        innerW: innerWidth, innerH: innerHeight,
        touchEvents: 'ontouchstart' in window,
        ownProps: Object.getOwnPropertyNames(nav).filter((n) =>
            ['platform','userAgent','maxTouchPoints','mobile','deviceMemory',
             'userAgentData','vendor','getBattery','standalone','connection'].includes(n)),
    };
    if (nav.userAgentData) {
        out.uaData = {
            brands: nav.userAgentData.brands,
            mobile: nav.userAgentData.mobile,
            platform: nav.userAgentData.platform,
        };
        try {
            out.uaDataHigh = await nav.userAgentData.getHighEntropyValues(
                ['architecture','bitness','model','platformVersion','uaFullVersion','fullVersionList','wow64']);
        } catch (e) { out.uaDataHigh = 'error: ' + e.message; }
    }
    if (typeof nav.getBattery === 'function') {
        try {
            const b = await nav.getBattery();
            out.battery = { level: b.level, charging: b.charging,
                            listenable: typeof b.addEventListener === 'function' };
        } catch (e) { out.battery = 'error: ' + e.message; }
    }
    const gl = (() => {
        try {
            const c = document.createElement('canvas');
            const g = c.getContext('webgl2') || c.getContext('webgl');
            if (!g) return null;
            const dbg = g.getExtension('WEBGL_debug_renderer_info');
            return {
                vendor: dbg ? g.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
                renderer: dbg ? g.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
            };
        } catch (e) { return { error: String(e) }; }
    })();
    out.webgl = gl;
    return out;
}"""


async def main() -> int:
    target_os = (sys.argv[1] if len(sys.argv) > 1 else "android").lower()
    mode = (sys.argv[2] if len(sys.argv) > 2 else "generate").lower()
    if target_os not in ("android", "ios"):
        print("用法: mobile_launch_verify.py [android|ios] [generate|preset]")
        return 2

    ok, msg = is_available()
    if not ok:
        print("❌ fp-chromium 内核未安装：", msg)
        print("   安装：bash scripts/install_fpchromium.sh")
        return 1

    ensure_dirs()
    db.init_db()

    fp = create_fingerprint(target_os, mode)
    fpd = fp["fingerprint"]
    nav = fpd["navigator"]
    screen = fpd["screen"]
    extra = fpd["mobileExtra"]
    vc = fpd["videoCard"]
    ua_ch = extra.get("uaCh")
    dpr = screen["devicePixelRatio"]
    css_w, css_h = screen["cssWidth"], screen["cssHeight"]
    vp_h = screen["cssViewportHeight"]

    print(f"== 指纹预期（{target_os} / {mode}）==")
    print("  机型      :", fp["manufacturer"], fp["device_model"])
    print("  系统/浏览器:", extra.get("osVersion"), extra.get("browser"), extra.get("browserVersion"))
    print("  UA        :", nav["userAgent"][:100])
    print("  platform  :", nav.get("platform"), " touch:", nav.get("maxTouchPoints"),
          " hw:", nav.get("hardwareConcurrency"), " mem:", nav.get("deviceMemory"))
    print("  物理屏幕   :", screen.get("width"), "x", screen.get("height"), "@", dpr,
          " 色深:", screen.get("colorDepth"))
    print("  CSS 屏幕  :", f"{css_w}x{css_h}", " 首屏视口高:", vp_h)
    print("  GPU       :", vc.get("vendor"), "/", vc.get("renderer"))
    print("  UA-CH     :", "有" if ua_ch else "无", " Battery:", "有" if fpd.get("battery") else "无")
    print("  iOS 抹除属性:", extra.get("removeNavigatorProps"), " 空插件:", extra.get("emptyPlugins"))
    print("  体检      :", fp["health"])

    profile = {
        "id": f"verify-{target_os}",
        "name": f"verify-{target_os}",
        "kernel": "fp-chromium",
        "target_os": target_os,
        "fingerprint": fp,
        "launch": {
            "headless": True, "geoip": False, "humanize": False,
            "locale": "zh-CN", "start_url": "https://example.com",
        },
        "proxy": None,
    }

    manager = LaunchManager()
    res = await manager.start(profile)
    print("\n== 启动结果 ==")
    for key in ("kernel", "mobile_viewport", "mobile_dpr", "mobile_platform",
                "mobile_max_touch", "mobile_start_url"):
        print(f"  {key}: {res.get(key)}")

    await asyncio.sleep(6)  # 等仿真会话建立 + start_url 导航完成

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(res["ws_endpoint"])
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()
        state = await page.evaluate(STATE_JS)

        # 请求头核对：拦截一个假域名，读取浏览器实际发出的请求头
        seen_headers: dict = {}

        async def on_route(route):
            for k, v in route.request.headers.items():
                lk = k.lower()
                if lk.startswith("sec-ch") or lk == "user-agent":
                    seen_headers[lk] = v
            await route.fulfill(status=200, content_type="text/html",
                                body="<html><body>probe</body></html>")

        await page.route("http://probe.test/**", on_route)
        await page.goto("http://probe.test/", wait_until="domcontentloaded")

        print("\n== 页面实际状态（导航后）==")
        print(json.dumps(state, ensure_ascii=False, indent=1))
        print("\n== 实际请求头（ua-ch 相关）==")
        print(json.dumps(seen_headers, ensure_ascii=False, indent=1))
    finally:
        await pw.stop()
        await manager.stop(profile["id"])

    gl = state.get("webgl") or {}
    is_android = target_os == "android"

    checks: dict[str, bool] = {
        "已按 start_url 导航": "example.com" in (state["url"] or ""),
        "UA 为指纹中的移动端 UA": state["userAgent"] == nav["userAgent"],
        "platform 一致": state["platform"] == nav.get("platform"),
        "maxTouchPoints 一致": state["maxTouchPoints"] == nav.get("maxTouchPoints"),
        "hardwareConcurrency 一致": state["hardwareConcurrency"] == nav.get("hardwareConcurrency"),
        "navigator.mobile 不存在（不伪造）": state["hasMobileProp"] is False,
        "DPR 一致": abs((state["dpr"] or 0) - dpr) < 0.02,
        "screen 为 CSS 尺寸": state["screenW"] == css_w and state["screenH"] == css_h,
        "innerWidth == CSS 宽度": state["innerW"] == css_w,
        "innerHeight == 首屏视口高（非屏幕全高）": state["innerH"] == vp_h,
        "触控事件可用": state["touchEvents"] is True,
        "navigator 无 own-property 泄漏": len(state["ownProps"]) == 0,
        "colorDepth/pixelDepth == 24": state["colorDepth"] == 24 and state["pixelDepth"] == 24,
        "navigator.vendor 一致": state["vendor"] == nav.get("vendor"),
        "WebGL 厂商 == 指纹": (gl.get("vendor") or "") == vc.get("vendor"),
        "WebGL 渲染器 == 指纹": (gl.get("renderer") or "") == vc.get("renderer"),
        "GPU 字符串格式正确": (
            str(gl.get("renderer") or "").startswith("ANGLE (") if is_android
            else gl.get("renderer") == "Apple GPU"
        ),
        "languages 以 zh-CN 开头": bool(state["languages"]) and state["languages"][0] == "zh-CN",
    }

    if is_android:
        ua_data = state.get("uaData") if isinstance(state.get("uaData"), dict) else None
        high = state.get("uaDataHigh") if isinstance(state.get("uaDataHigh"), dict) else None
        battery = state.get("battery") if isinstance(state.get("battery"), dict) else None
        mem_class = extra.get("memoryClass")
        checks.update({
            "UA-CH 存在": ua_data is not None,
            "UA-CH brands == 指纹": bool(ua_data) and ua_data.get("brands") == (ua_ch or {}).get("brands"),
            "UA-CH mobile/platform == 指纹": bool(ua_data) and (
                ua_data.get("mobile") == (ua_ch or {}).get("mobile")
                and ua_data.get("platform") == (ua_ch or {}).get("platform")),
            "UA-CH 高熵 model == 指纹": bool(high) and high.get("model") == (ua_ch or {}).get("model"),
            "UA-CH 高熵 platformVersion == 指纹": bool(high) and (
                high.get("platformVersion") == (ua_ch or {}).get("platformVersion")),
            "UA-CH 高熵 fullVersionList == 指纹": bool(high) and (
                high.get("fullVersionList") == (ua_ch or {}).get("fullVersionList")),
            "deviceMemory == 内存桶": state.get("deviceMemory") == mem_class,
            "Battery API 可用且数值一致": bool(battery)
                and abs(battery.get("level", -1) - fpd["battery"]["level"]) < 0.01
                and battery.get("charging") == fpd["battery"]["charging"],
            "Sec-CH-UA 请求头存在": "sec-ch-ua" in seen_headers,
            "Sec-CH-UA 请求头 == 指纹": seen_headers.get("sec-ch-ua") == sec_ch_headers(ua_ch or {}).get("Sec-CH-UA"),
            "Sec-CH-UA-Mobile/Platform == 指纹": (
                seen_headers.get("sec-ch-ua-mobile") == sec_ch_headers(ua_ch or {}).get("Sec-CH-UA-Mobile")
                and seen_headers.get("sec-ch-ua-platform") == sec_ch_headers(ua_ch or {}).get("Sec-CH-UA-Platform")),
            "请求头 User-Agent == 指纹 UA": seen_headers.get("user-agent") == nav["userAgent"],
            "plugins 非空（Chrome Android 有内置插件）": state["pluginsLen"] > 0,
            "connection 存在（Chrome Android 特性）": state["hasConnection"] is True,
            "standalone 不存在（iOS 专有属性）": state["hasStandalone"] is False,
        })
    else:
        checks.update({
            "UA-CH 不存在（Safari 无该 API）": state["uaData"] == "ABSENT",
            "getBattery 不存在（Safari 无该 API）": state["hasGetBattery"] is False,
            "deviceMemory 不暴露（Safari 无该 API）": state["deviceMemory"] is None,
            "connection 不存在（Safari 无该 API）": state["hasConnection"] is False,
            "pdfViewerEnabled 不存在（Safari 无该 API）": state["hasPdfViewer"] is False,
            "standalone 存在且为 false（iOS 专有属性）": state["hasStandalone"] is True and state["standalone"] is False,
            "plugins/mimeTypes 为空（Safari 行为）": state["pluginsLen"] == 0 and state["mimeTypesLen"] == 0,
            "无 Sec-CH-UA 请求头（Safari 不发）": not any(k.startswith("sec-ch") for k in seen_headers),
            "请求头 User-Agent == 指纹 UA": seen_headers.get("user-agent") == nav["userAgent"],
        })

    print("\n== 检查项 ==")
    ok_all = True
    for name, passed in checks.items():
        print(("  ✅ " if passed else "  ❌ ") + name)
        ok_all = ok_all and passed
    print("\n" + ("✅ 全部通过" if ok_all else "❌ 存在失败项"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        import shutil
        shutil.rmtree(os.environ.get("FPWB_HOME", ""), ignore_errors=True)
    sys.exit(code)
