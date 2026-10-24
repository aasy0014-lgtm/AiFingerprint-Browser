#!/usr/bin/env python3
"""移动端整机链路回归（P2-9）：API 建环境 → 启动 → 经代理连接 → 页面读数比对。

与另外两个脚本的分工：

  scripts/mobile_test.py          数据层，不启动浏览器
  scripts/mobile_launch_verify.py 直接构造会话，逐面深挖（跳过 API / 数据库 / 代理）
  本脚本                          走用户实际路径，只做「页面读数 == 存储指纹」的
                                  确定性比对，用于发现链路级回归（建库字段丢失、
                                  启动参数没透传、代理没挂上、数据库往返丢字段）

不访问第三方检测站：分数是黑盒，失败无法定位；这里断言的是可复现的读数差异。

用法：
    .venv/bin/python scripts/mobile_e2e_test.py            # 三口径全跑
    .venv/bin/python scripts/mobile_e2e_test.py android    # 只跑 Android
    .venv/bin/python scripts/mobile_e2e_test.py ios        # iOS Safari
    .venv/bin/python scripts/mobile_e2e_test.py ios chrome_ios
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必须在导入 app.config 之前重定位数据目录（不污染正式数据）
os.environ["FPWB_HOME"] = tempfile.mkdtemp(prefix="fpwb_mobile_e2e_")

from app.kernels.fp_chromium_kernel import is_available  # noqa: E402

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


# 页面读数：只取「与存储指纹可逐项比对」的确定性字段。
# 刻意不含时序类读数（传感器间隔、拖拽剖面）——那些由 launch_verify 负责，
# 放进整机链路会让本脚本变成不稳定的性能测试。
READ_JS = r"""() => {
  const nav = navigator;
  const scr = screen;
  const gl = (() => {
    try {
      const c = document.createElement('canvas');
      const e = c.getContext('webgl') || c.getContext('experimental-webgl');
      if (!e) return null;
      const dbg = e.getExtension('WEBGL_debug_renderer_info');
      return {
        vendor: dbg ? e.getParameter(dbg.UNMASKED_VENDOR_WEBGL) : null,
        renderer: dbg ? e.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : null,
      };
    } catch (err) { return null; }
  })();
  return {
    ua: nav.userAgent,
    platform: nav.platform,
    maxTouchPoints: nav.maxTouchPoints,
    hardwareConcurrency: nav.hardwareConcurrency,
    deviceMemory: ('deviceMemory' in nav) ? nav.deviceMemory : null,
    dpr: window.devicePixelRatio,
    screenW: scr.width,
    screenH: scr.height,
    innerW: innerWidth,
    innerH: innerHeight,
    colorDepth: scr.colorDepth,
    uaCh: nav.userAgentData ? {
      brands: nav.userAgentData.brands,
      mobile: nav.userAgentData.mobile,
      platform: nav.userAgentData.platform,
    } : null,
    hasConnection: !!nav.connection,
    hasGetBattery: typeof nav.getBattery === 'function',
    pdfViewer: ('pdfViewerEnabled' in nav) ? nav.pdfViewerEnabled : null,
    gl,
  };
}"""

# Battery 读数必须 await（getBattery 返回 Promise）；单独一段避免污染同步探针
BATTERY_JS = r"""(async () => {
  try {
    if (typeof navigator.getBattery !== 'function') return null;
    const b = await navigator.getBattery();
    return {level: b.level, charging: b.charging};
  } catch (e) { return {error: String(e && e.message || e)}; }
})()"""


# 输入序列：Playwright 的鼠标点击必须经代理变成触摸。
# 页面只记录 pointerType，避免把兼容鼠标事件（真机也有）误判为桌面痕迹。
POINTER_JS = r"""() => {
  window.__ptrs = [];
  document.addEventListener('pointerdown', (e) => {
    window.__ptrs.push({type: e.pointerType, x: Math.round(e.clientX)});
  }, true);
  document.addEventListener('touchstart', () => { window.__touchStarts = (window.__touchStarts || 0) + 1; }, true);
  return true;
}"""

_PAGE_HTML = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1">
</head><body style="margin:0;height:3000px">
<button id="b" style="width:200px;height:80px;margin:0">tap</button>
</body></html>"""


def _start_page_server() -> int:
    """本地页面：必须声明 width=device-width，移动仿真才按 CSS 宽排版。"""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = _PAGE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: A003
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1]


async def run_one(target_os: str, browser: str | None) -> None:
    from httpx import ASGITransport, AsyncClient
    from playwright.async_api import async_playwright

    from app import db
    from app.config import ensure_dirs
    from app.main import app as fastapi_app

    label = f"{target_os}" + (f"/{browser}" if browser else "")
    print("\n" + "=" * 70)
    print(f"整机链路：{label}")
    print("=" * 70)

    ensure_dirs()
    db.init_db()

    payload = {
        "name": f"e2e-{label}",
        "kernel": "fp-chromium",
        "target_os": target_os,
        "fingerprint_mode": "generate",
        "browser": browser,
        "launch": {"headless": True, "geoip": False, "humanize": False,
                   "start_url": "about:blank"},
    }

    async with AsyncClient(transport=ASGITransport(app=fastapi_app),
                           base_url="http://test") as client:
        # ---- 1. 建环境（走真实 API 与数据库）----
        resp = await client.post("/api/v1/profiles", json=payload)
        check("API 建环境成功（201/200）", resp.status_code < 400, resp.text[:200])
        if resp.status_code >= 400:
            return
        profile = resp.json()["data"]
        pid = profile["id"]
        stored = profile["fingerprint"]
        fpd = stored["fingerprint"]
        nav = fpd["navigator"]
        scr = fpd["screen"]
        extra = fpd["mobileExtra"]
        vc = fpd["videoCard"]

        check("数据库往返保留移动端指纹（UA/屏幕/GPU）",
              bool(nav.get("userAgent")) and bool(scr.get("cssWidth"))
              and bool(vc.get("renderer")))

        # ---- 2. 启动 ----
        resp = await client.post("/api/v1/browser/start",
                                 json={"profile_id": pid, "headless": True})
        check("启动成功", resp.status_code < 400, resp.text[:200])
        if resp.status_code >= 400:
            return
        info = resp.json()["data"]
        ws_endpoint = info["ws_endpoint"]
        ws_direct = info.get("ws_endpoint_direct")

        # ---- 3. 代理接线 ----
        check("ws_endpoint 指向输入代理（与内核直连地址不同）",
              bool(ws_direct) and ws_endpoint != ws_direct,
              f"{ws_endpoint} / {ws_direct}")

        try:
            # 等仿真会话建立 + 首个导航完成
            await asyncio.sleep(5)

            # 本地页面：about:blank 没有 viewport meta，移动仿真会按 980px
            # 虚拟视口排版（与真机访问无 viewport 的页面一致）；要核对
            # innerWidth/视口高，必须让页面声明 width=device-width。
            port = _start_page_server()

            pw = await async_playwright().start()
            try:
                # 客户端一律经代理连接：这条链路本身也是被测对象
                br = await pw.chromium.connect_over_cdp(ws_endpoint)
                ctx = br.contexts[0]
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                await asyncio.sleep(0.4)
                state = await page.evaluate(READ_JS)
                battery = await page.evaluate(BATTERY_JS)

                # ---- 4. 页面读数 vs 存储指纹 ----
                check("UA == 存储指纹", state["ua"] == nav["userAgent"],
                      f"{state['ua'][:60]} != {str(nav.get('userAgent'))[:60]}")
                check("platform == 存储指纹",
                      state["platform"] == nav.get("platform"),
                      f"{state['platform']} != {nav.get('platform')}")
                check("maxTouchPoints == 存储指纹",
                      state["maxTouchPoints"] == nav.get("maxTouchPoints"),
                      f"{state['maxTouchPoints']} != {nav.get('maxTouchPoints')}")
                check("hardwareConcurrency == 存储指纹",
                      state["hardwareConcurrency"] == nav.get("hardwareConcurrency"),
                      f"{state['hardwareConcurrency']} != {nav.get('hardwareConcurrency')}")
                check("devicePixelRatio == 存储指纹",
                      abs((state["dpr"] or 0) - scr["devicePixelRatio"]) < 0.02,
                      f"{state['dpr']} != {scr['devicePixelRatio']}")
                check("screen 为 CSS 尺寸",
                      state["screenW"] == scr["cssWidth"]
                      and state["screenH"] == scr["cssHeight"],
                      f"{state['screenW']}x{state['screenH']} != "
                      f"{scr['cssWidth']}x{scr['cssHeight']}")
                check("innerWidth == CSS 宽度",
                      state["innerW"] == scr["cssWidth"],
                      f"{state['innerW']} != {scr['cssWidth']}")
                check("innerHeight == 首屏视口高",
                      state["innerH"] == scr["cssViewportHeight"],
                      f"{state['innerH']} != {scr['cssViewportHeight']}")
                check("colorDepth == 24（宿主为 30）", state["colorDepth"] == 24,
                      str(state["colorDepth"]))
                check("WebGL 渲染器 == 存储指纹",
                      (state.get("gl") or {}).get("renderer") == vc.get("renderer"),
                      f"{(state.get('gl') or {}).get('renderer')} != {vc.get('renderer')}")

                if target_os == "android":
                    check("UA-CH 存在且 mobile=true",
                          bool(state["uaCh"]) and state["uaCh"].get("mobile") is True,
                          json.dumps(state["uaCh"], ensure_ascii=False)[:120])
                    check("UA-CH platform == Android",
                          (state["uaCh"] or {}).get("platform") == "Android",
                          str((state["uaCh"] or {}).get("platform")))
                    check("deviceMemory 为上报表",
                          state["deviceMemory"] in (1, 2, 4, 8),
                          str(state["deviceMemory"]))
                    check("Battery API 可用且数值与指纹一致",
                          isinstance(battery, dict) and "error" not in battery
                          and abs(battery.get("level", -1)
                                  - fpd["battery"]["level"]) < 0.01
                          and battery.get("charging") == fpd["battery"]["charging"],
                          json.dumps(battery, ensure_ascii=False)[:120])
                    check("NetworkInformation 存在",
                          state["hasConnection"] is True)
                else:
                    check("iOS 无 UA-CH", state["uaCh"] is None,
                          json.dumps(state["uaCh"], ensure_ascii=False)[:120])
                    check("iOS 无 getBattery（WKWebView 无该 API）",
                          state["hasGetBattery"] is False
                          and battery is None, json.dumps(battery)[:80])
                    check("iOS 无 NetworkInformation",
                          state["hasConnection"] is False)
                    check("iOS pdfViewerEnabled === true（Safari 16.4+ 能力位）",
                          state["pdfViewer"] is True, str(state["pdfViewer"]))
                    check("iOS UA 口径正确",
                          ("CriOS/" in state["ua"]) if browser == "chrome_ios"
                          else ("CriOS/" not in state["ua"] and "Safari/" in state["ua"]),
                          state["ua"][:80])

                # ---- 5. 输入链路：Playwright 鼠标 → 代理 → 触摸 ----
                await page.goto(f"http://127.0.0.1:{port}/input", wait_until="load")
                await page.evaluate(POINTER_JS)
                await page.mouse.click(60, 40)
                await asyncio.sleep(0.3)
                ptrs = await page.evaluate("() => window.__ptrs || []")
                touches = await page.evaluate("() => window.__touchStarts || 0")
                types = [p.get("type") for p in ptrs]
                check("鼠标点击经代理变成 pointerType=touch",
                      "touch" in types, str(types))
                check("鼠标点击不产生 pointerType=mouse",
                      "mouse" not in types, str(types))
                check("鼠标点击派发 touchstart", touches >= 1, str(touches))

                await br.close()
            finally:
                await pw.stop()
        finally:
            # ---- 6. 收尾 ----
            await client.post("/api/v1/browser/stop", json={"profile_id": pid})
            await client.delete(f"/api/v1/profiles/{pid}")


async def main() -> int:
    ok, msg = is_available()
    if not ok:
        print("❌ fp-chromium 内核未安装：", msg)
        print("   安装：bash scripts/install_fpchromium.sh")
        return 1

    target = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    browser_arg = (sys.argv[2] if len(sys.argv) > 2 else "").lower() or None
    cases = ([("android", None)] if target == "android"
             else [("ios", "chrome_ios")] if target == "ios" and browser_arg == "chrome_ios"
             else [("ios", "safari")] if target == "ios"
             else [("android", None), ("ios", "safari"), ("ios", "chrome_ios")])

    for target_os, browser in cases:
        await run_one(target_os, browser)

    print("\n" + "=" * 70)
    print(f"结果：{_PASS} 通过 / {_FAIL} 失败")
    if _FAIL:
        for name in _FAILURES:
            print(f"  ❌ {name}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
