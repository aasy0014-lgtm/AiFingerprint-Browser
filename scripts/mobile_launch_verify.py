#!/usr/bin/env python3
"""移动端环境真实启动验证（fp-chromium 内核 + CDP 移动仿真）

与 scripts/mobile_test.py（纯数据结构）和 scripts/test_mobile_end_to_end.py
（流程演示）不同，本脚本会**真实启动浏览器**并连接 CDP 读取页面内的实际值，
逐项核对移动端仿真是否生效：

  1. UA 为该环境指纹里的移动端 UA
  2. navigator.platform / maxTouchPoints / navigator.mobile 与指纹一致
  3. window.devicePixelRatio 为指纹 DPR
  4. screen.width/height 与 innerWidth/Height 为 CSS 逻辑像素（物理 / DPR）
  5. 触控事件可用（ontouchstart）
  6. navigator 上无 own-property 泄漏（JS 补丁改的是原型）

用独立临时数据目录运行，不污染正式数据。

用法：
    .venv/bin/python scripts/mobile_launch_verify.py [android|ios]
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

STATE_JS = """() => ({
    url: location.href,
    userAgent: navigator.userAgent,
    platform: navigator.platform,
    maxTouchPoints: navigator.maxTouchPoints,
    mobileProp: navigator.mobile,
    dpr: window.devicePixelRatio,
    screenW: screen.width, screenH: screen.height,
    innerW: innerWidth, innerH: innerHeight,
    touchEvents: 'ontouchstart' in window,
    ownProps: Object.getOwnPropertyNames(navigator).filter(
        n => ['platform','userAgent','maxTouchPoints','mobile'].includes(n)),
})"""


async def main() -> int:
    target_os = (sys.argv[1] if len(sys.argv) > 1 else "android").lower()
    if target_os not in ("android", "ios"):
        print("用法: mobile_launch_verify.py [android|ios]")
        return 2

    ok, msg = is_available()
    if not ok:
        print("❌ fp-chromium 内核未安装：", msg)
        print("   安装：bash scripts/install_fpchromium.sh")
        return 1

    ensure_dirs()
    db.init_db()

    fp = create_fingerprint(target_os, "generate")
    nav = fp["fingerprint"]["navigator"]
    screen = fp["fingerprint"]["screen"]
    dpr = screen["devicePixelRatio"]
    css_w = round(screen["width"] / dpr)
    css_h = round(screen["height"] / dpr)

    print(f"== 指纹预期（{target_os}）==")
    print("  UA        :", nav["userAgent"][:90])
    print("  platform  :", nav.get("platform"), "  touch:", nav.get("maxTouchPoints"))
    print("  物理屏幕   :", screen.get("width"), "x", screen.get("height"), "@", dpr)
    print("  CSS 屏幕  :", css_w, "x", css_h)

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
    browser = await pw.chromium.connect_over_cdp(res["ws_endpoint"])
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else await context.new_page()
    state = await page.evaluate(STATE_JS)
    print("\n== 页面实际状态（导航后）==")
    print(json.dumps(state, ensure_ascii=False, indent=1))
    await pw.stop()
    await manager.stop(profile["id"])

    checks = {
        "已按 start_url 导航": "example.com" in (state["url"] or ""),
        "UA 为指纹中的移动端 UA": state["userAgent"] == nav["userAgent"],
        "platform 一致": state["platform"] == nav.get("platform"),
        "maxTouchPoints 一致": state["maxTouchPoints"] == nav.get("maxTouchPoints"),
        "navigator.mobile = true": state["mobileProp"] is True,
        "DPR 一致": abs((state["dpr"] or 0) - dpr) < 0.02,
        "screen 为 CSS 尺寸": state["screenW"] == css_w and state["screenH"] == css_h,
        "视口为 CSS 尺寸": state["innerW"] == css_w,
        "触控事件可用": state["touchEvents"] is True,
        "navigator 无 own-property 泄漏": len(state["ownProps"]) == 0,
    }
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
