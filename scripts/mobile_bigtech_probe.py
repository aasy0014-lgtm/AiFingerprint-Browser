#!/usr/bin/env python3
"""大厂检测信号实证探针（诊断用）。

对运行中的移动端仿真环境（fp-chromium + CDP 注入后的真实运行态）做全量
「真机面」信号读写：媒体查询 / 传感器 / 触控 / 屏幕几何 / API 面 / WebGL
参数 / 自动化事件层，输出 JSON 供与真机行为逐项比对。
详细方法、读数矩阵与缺口清单见 docs/移动端大厂检测对抗优化方案.md。

读数 JS 本身在 scripts/mobile_probe_js.py（真机采集页共用同一份），
本脚本只负责「起仿真 → 依次取数 → 打 JSON」。

用法:
    .venv/bin/python scripts/mobile_bigtech_probe.py [android|ios] [market_name]
    .venv/bin/python scripts/mobile_bigtech_probe.py ios "iPhone 16" --json /tmp/sim.json --quiet

真机侧：.venv/bin/python scripts/mobile_real_collect.py，然后用
scripts/mobile_probe_compare.py 与 --json 产物逐项对拍。
"""
import argparse
import asyncio
import copy
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
os.environ.setdefault("FPWB_HOME", tempfile.mkdtemp(prefix="fpwb_probe_"))

import mobile_probe_js  # noqa: E402

_ap = argparse.ArgumentParser(add_help=True)
_ap.add_argument("target_os", nargs="?", default="android", choices=["android", "ios"])
_ap.add_argument("market_name", nargs="?")
_ap.add_argument("--browser", choices=["safari", "chrome_ios"],
                help="iOS 浏览器口径（CriOS 才有 __gCrWeb 面）")
_ap.add_argument("--json", dest="json_out", help="把读数写进文件（stdout 仍是 JSON）")
_ap.add_argument("--quiet", action="store_true", help="不打印读数，只写 --json / 提示")
_args = _ap.parse_args()

target_os = _args.target_os
device_name = _args.market_name or ("Xiaomi 15" if target_os == "android" else "iPhone 17")

lib = json.loads((REPO / "app/data/mobile_devices.json").read_text(encoding="utf-8"))
DEV = next(d for d in lib["devices"] if d["market_name"] == device_name)

import app.mobile_fingerprints as mf  # noqa: E402

mf._pick_device = lambda target_os, manufacturer: copy.deepcopy(DEV)

from app.config import ensure_dirs  # noqa: E402
from app.fingerprint_engine import create_fingerprint  # noqa: E402
from app.launcher import LaunchManager  # noqa: E402

PAGE = ("<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>probe</title></head><body>probe"
        "<div style='height:3000px'></div></body></html>")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


EVENT_CAPTURE_JS = r"""() => {
  window.__ev = [];
  const rec = (e) => window.__ev.push(e.type + (e.pointerType !== undefined ? ':' + e.pointerType : ''));
  for (const t of ['pointerover','pointerenter','pointermove','pointerdown','mousedown','touchstart',
                   'mousemove','touchmove','pointerup','mouseup','touchend','click']) {
    document.addEventListener(t, rec, true);
  }
  const d = document.createElement('div');
  d.id = 'hit';
  d.style.cssText = 'position:fixed;inset:0;z-index:99999';
  document.body.appendChild(d);
  return 'ok';
}"""


async def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"

    fp = create_fingerprint(target_os, "generate", browser=_args.browser)
    scr = fp["fingerprint"]["screen"]
    print(f"== 机型: {fp['manufacturer']} {fp['device_model']} | css {scr['cssWidth']}x{scr['cssHeight']} "
          f"vp {scr['cssViewportHeight']} @{scr['devicePixelRatio']} ==", file=sys.stderr)

    profile = {
        "id": f"probe-{target_os}",
        "name": f"probe-{target_os}",
        "kernel": "fp-chromium",
        "target_os": target_os,
        "fingerprint": fp,
        "launch": {
            "headless": True, "geoip": False, "humanize": False,
            "locale": "zh-CN", "timezone": "America/Los_Angeles",
            "start_url": url,
        },
        "proxy": None,
    }
    manager = LaunchManager()
    try:
        res = await manager.start(profile)
        await asyncio.sleep(6)

        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(res["ws_endpoint"])
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception as exc:
                print("goto warn:", exc, file=sys.stderr)
            main_part = await page.evaluate(mobile_probe_js.eval_expr('main'))
            signals = main_part['parts']['main']

            await page.evaluate(EVENT_CAPTURE_JS)
            try:
                await page.mouse.move(120, 400)
                await page.mouse.down()
                await asyncio.sleep(0.05)
                await page.mouse.up()
                await asyncio.sleep(0.25)
                events_mouse = await page.evaluate("() => window.__ev.splice(0)")
            except Exception as exc:
                events_mouse = f"ERR: {type(exc).__name__}: {exc}"

            # Playwright 的 touchscreen API（记录限制：connect_over_cdp 默认上下文无 hasTouch）
            try:
                await page.touchscreen.tap(160, 420)
                await asyncio.sleep(0.25)
                tap_api = "ok"
            except Exception as exc:
                tap_api = f"ERR: {type(exc).__name__}: {str(exc)[:120]}"
            await page.evaluate("() => window.__ev.splice(0)")

            # CDP 触摸派发 = 移动端自动化的真机等价路径（app/mobile_input.py 同源）
            cdp = await context.new_cdp_session(page)
            try:
                await cdp.send("Input.dispatchTouchEvent", {
                    "type": "touchStart",
                    "touchPoints": [{"x": 160, "y": 420, "radiusX": 1, "radiusY": 1,
                                     "force": 1, "id": 1}]})
                await asyncio.sleep(0.06)
                await cdp.send("Input.dispatchTouchEvent",
                               {"type": "touchEnd", "touchPoints": []})
                await asyncio.sleep(0.3)
                events_touch = await page.evaluate("() => window.__ev.splice(0)")
            except Exception as exc:
                events_touch = f"ERR: {type(exc).__name__}: {exc}"

            # 触摸拖拽滚动（wheel 事件在真机上不存在）
            scroll_before = await page.evaluate("() => scrollY")
            view = await page.evaluate("() => ({w: innerWidth, h: innerHeight})")
            try:
                sx, sy = view["w"] / 2, view["h"] * 0.72
                await cdp.send("Input.dispatchTouchEvent", {
                    "type": "touchStart",
                    "touchPoints": [{"x": sx, "y": sy, "radiusX": 1, "radiusY": 1,
                                     "force": 1, "id": 1}]})
                for i in range(1, 9):
                    await cdp.send("Input.dispatchTouchEvent", {
                        "type": "touchMove",
                        "touchPoints": [{"x": sx, "y": sy - (sy - 20) * i / 8,
                                         "radiusX": 1, "radiusY": 1, "force": 1, "id": 1}]})
                    await asyncio.sleep(0.012)
                await cdp.send("Input.dispatchTouchEvent",
                               {"type": "touchEnd", "touchPoints": []})
                await asyncio.sleep(0.35)
            except Exception as exc:
                scroll_err = f"ERR: {type(exc).__name__}: {exc}"
            scroll_after = await page.evaluate("() => scrollY")
            events_scroll = await page.evaluate("() => window.__ev.splice(0)")

            # 传感器事件流（页面在 127.0.0.1 = 信任来源；真机非安全上下文
            # 同样不派发，故必须以本地服务页为准）
            sensor = (await page.evaluate(mobile_probe_js.eval_expr('sensor')))['parts']['sensor']
            # 引擎面读数（全屏 / 视频 webkit 全屏 / WebKit CSS / 传感器构造器）
            engine = (await page.evaluate(mobile_probe_js.eval_expr('engine')))['parts']['engine']
            # 平台 API 差集读数（窗口/文档/navigator + Worker 全局）
            platform = (await page.evaluate(mobile_probe_js.eval_expr('platform')))['parts']['platform']
            # CriOS 宿主壳面（__gCrWeb 成员集 / windowId 形态 / 改写源码文本）
            crios = (await page.evaluate(mobile_probe_js.eval_expr('crios')))['parts']['crios']

            out = {
                "target_os": target_os,
                "device": device_name,
                "probe_version": mobile_probe_js.PROBE_VERSION,
                "collected_at": main_part["collected_at"],
                "parts": {"main": signals, "sensor": sensor, "engine": engine,
                          "platform": platform, "crios": crios},
                "input_mouse_click_events": events_mouse,
                "input_playwright_touchscreen_tap": tap_api,
                "input_touch_tap_events": events_touch,
                "input_touch_scroll": {"before": scroll_before, "after": scroll_after,
                                       "events": events_scroll},
            }
            text = json.dumps(out, ensure_ascii=False, indent=1)
            if _args.json_out:
                Path(_args.json_out).write_text(text + "\n", encoding="utf-8")
                print(f"读数已写入 {_args.json_out}", file=sys.stderr)
            if not _args.quiet:
                print(text)
            try:
                await browser.close()
            except Exception:
                pass
        finally:
            await pw.stop()
    finally:
        try:
            await manager.stop(profile["id"])
        except Exception:
            pass
        server.shutdown()
    return 0


if __name__ == "__main__":
    ensure_dirs()
    raise SystemExit(asyncio.run(main()))
