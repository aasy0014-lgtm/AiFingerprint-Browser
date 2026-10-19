#!/usr/bin/env python3
"""大厂检测信号实证探针（诊断用）。

对运行中的移动端仿真环境（fp-chromium + CDP 注入后的真实运行态）做全量
「真机面」信号读写：媒体查询 / 传感器 / 触控 / 屏幕几何 / API 面 / WebGL
参数 / 自动化事件层，输出 JSON 供与真机行为逐项比对。
详细方法、读数矩阵与缺口清单见 docs/移动端大厂检测对抗优化方案.md。

用法: .venv/bin/python scripts/mobile_bigtech_probe.py [android|ios] [market_name]
"""
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
os.environ.setdefault("FPWB_HOME", tempfile.mkdtemp(prefix="fpwb_probe_"))

target_os = sys.argv[1] if len(sys.argv) > 1 else "android"
device_name = sys.argv[2] if len(sys.argv) > 2 else (
    "Xiaomi 15" if target_os == "android" else "iPhone 17")

lib = json.loads((REPO / "app/data/mobile_devices.json").read_text(encoding="utf-8"))
DEV = next(d for d in lib["devices"] if d["market_name"] == device_name)

import app.mobile_fingerprints as mf  # noqa: E402

mf._pick_device = lambda target_os, manufacturer: copy.deepcopy(DEV)

from app.config import ensure_dirs  # noqa: E402
from app.fingerprint_engine import create_fingerprint  # noqa: E402
from app.launcher import LaunchManager  # noqa: E402

BATTERY_JS = r"""(async () => {
  const out = {};
  const safeA = async (fn) => { try { return await fn(); } catch (e) { return 'ERR:' + ((e && e.name) || e); } };
  const safe = (fn) => { try { return fn(); } catch (e) { return 'ERR:' + ((e && e.name) || e); } };
  const mm = (q) => safe(() => matchMedia(q).matches);

  out.href = location.href;
  out.isSecureContext = isSecureContext;
  out.userAgent = navigator.userAgent;
  out.platform = navigator.platform;
  out.vendor = navigator.vendor;
  out.languages = navigator.languages;
  out.hardwareConcurrency = navigator.hardwareConcurrency;
  out.deviceMemory = navigator.deviceMemory === undefined ? '<undef>' : navigator.deviceMemory;
  out.maxTouchPoints = navigator.maxTouchPoints;
  out.uaChType = typeof navigator.userAgentData;
  out.uaChMobile = safe(() => navigator.userAgentData ? navigator.userAgentData.mobile : '<missing>');
  out.uaChFull = await safeA(async () => {
    if (!navigator.userAgentData) return '<missing>';
    return await navigator.userAgentData.getHighEntropyValues(
      ['platform','platformVersion','architecture','bitness','model','uaFullVersion','fullVersionList','wow64']);
  });

  out.screen = {
    w: screen.width, h: screen.height, dpr: devicePixelRatio,
    availW: screen.availWidth, availH: screen.availHeight,
    availLeft: screen.availLeft, availTop: screen.availTop,
    colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth,
    isExtended: screen.isExtended,
  };
  out.screenOrientation = safe(() => ({ type: screen.orientation.type, angle: screen.orientation.angle }));
  out.win = {
    innerW: innerWidth, innerH: innerHeight, outerW: outerWidth, outerH: outerHeight,
    screenX: screenX, screenY: screenY, screenLeft: screenLeft, screenTop: screenTop,
    legacyOrientation: typeof window.orientation === 'undefined' ? '<undef>' : window.orientation,
  };
  out.visualViewport = safe(() => ({
    w: visualViewport.width, h: visualViewport.height, scale: visualViewport.scale,
    top: visualViewport.offsetTop, left: visualViewport.offsetLeft,
  }));

  out.mq = {
    pointerCoarse: mm('(pointer: coarse)'), pointerFine: mm('(pointer: fine)'),
    anyPointerCoarse: mm('(any-pointer: coarse)'), anyPointerFine: mm('(any-pointer: fine)'),
    hoverNone: mm('(hover: none)'), hoverHover: mm('(hover: hover)'),
    anyHoverNone: mm('(any-hover: none)'), anyHoverHover: mm('(any-hover: hover)'),
  };
  out.touch = {
    ontouchstart: 'ontouchstart' in window,
    touchEvent: typeof TouchEvent,
    ontouchmove: 'ontouchmove' in window,
    gesturestart: 'ongesturestart' in window,
    gestureEvent: typeof GestureEvent,
  };
  out.sensors = {
    doe: typeof DeviceOrientationEvent,
    dme: typeof DeviceMotionEvent,
    ondeviceorientation: 'ondeviceorientation' in window,
    ondevicemotion: 'ondevicemotion' in window,
    reqPerm: safe(() => typeof DeviceOrientationEvent.requestPermission),
  };
  out.sensors.accelPerm = await safeA(async () => (await navigator.permissions.query({ name: 'accelerometer' })).state);
  out.sensors.gyroPerm = await safeA(async () => (await navigator.permissions.query({ name: 'gyroscope' })).state);

  out.connection = safe(() => {
    const c = navigator.connection;
    if (!c) return '<missing>';
    return { type: c.type === undefined ? '<undef>' : c.type,
             typeIn: 'type' in c,
             effectiveType: c.effectiveType, rtt: c.rtt, downlink: c.downlink, saveData: c.saveData };
  });
  out.pdfViewerEnabled = safe(() => navigator.pdfViewerEnabled === undefined ? '<undef>' : navigator.pdfViewerEnabled);
  out.plugins = safe(() => Array.from(navigator.plugins).map((p) => p.name));
  out.mimeTypesLen = safe(() => navigator.mimeTypes.length);
  out.perfMemory = safe(() => performance.memory
      ? { limitGB: Math.round(performance.memory.jsHeapSizeLimit / 1e9 * 10) / 10 }
      : '<missing>');

  out.nav = {
    serial: 'serial' in navigator, usb: 'usb' in navigator, hid: 'hid' in navigator,
    bluetooth: 'bluetooth' in navigator, keyboard: 'keyboard' in navigator,
    wakeLock: 'wakeLock' in navigator, presentation: 'presentation' in navigator,
    getInstalledRelatedApps: typeof navigator.getInstalledRelatedApps,
    setAppBadge: typeof navigator.setAppBadge, vibrate: typeof navigator.vibrate,
    share: typeof navigator.share, mediaSession: 'mediaSession' in navigator,
    getGamepads: typeof navigator.getGamepads, requestMIDIAccess: typeof navigator.requestMIDIAccess,
    webdriver: navigator.webdriver,
  };
  out.showOpenFilePicker = typeof showOpenFilePicker;
  out.webkit = safe(() => typeof window.webkit);
  out.webkitMessageHandlers = safe(() => !!(window.webkit && window.webkit.messageHandlers));
  out.chrome = safe(() => window.chrome ? Object.keys(window.chrome) : '<missing>');
  out.cdcKeys = safe(() => Object.keys(window).filter((k) => /cdc_|selenium|webdriver|puppeteer|playwright/i.test(k)));
  out.notification = safe(() => ({ permission: Notification.permission, maxActions: Notification.maxActions }));
  out.battery = await safeA(async () => {
    const b = await navigator.getBattery();
    return { level: b.level, charging: b.charging };
  });

  out.media = safe(() => {
    const a = document.createElement('audio');
    const v = document.createElement('video');
    return {
      ogg: a.canPlayType('audio/ogg; codecs="vorbis"'),
      opusWebm: a.canPlayType('audio/webm; codecs="opus"'),
      webmVp9: v.canPlayType('video/webm; codecs="vp9"'),
      h264: v.canPlayType('video/mp4; codecs="avc1.42E01E"'),
      hevc: v.canPlayType('video/mp4; codecs="hvc1"'),
    };
  });
  out.storage = await safeA(async () => {
    const e = await navigator.storage.estimate();
    return { quotaGB: Math.round(e.quota / 1e9 * 10) / 10, usageMB: Math.round(e.usage / 1e6) };
  });

  out.css = {
    touchCallout: CSS.supports('-webkit-touch-callout', 'none'),
    overflowScrolling: CSS.supports('-webkit-overflow-scrolling', 'touch'),
    envSupports: CSS.supports('padding-top: env(safe-area-inset-top)'),
    pointerCoarse: CSS.supports('(pointer: coarse)'),
  };
  out.envSafeArea = safe(() => {
    const d = document.createElement('div');
    d.style.cssText = 'position:fixed;top:0;left:0;padding-top:env(safe-area-inset-top);'
      + 'padding-bottom:env(safe-area-inset-bottom);padding-left:env(safe-area-inset-left)';
    document.body.appendChild(d);
    const cs = getComputedStyle(d);
    const r = { top: cs.paddingTop, bottom: cs.paddingBottom, left: cs.paddingLeft };
    d.remove();
    return r;
  });

  out.webgl = safe(() => {
    const c = document.createElement('canvas');
    const gl = c.getContext('webgl');
    if (!gl) return '<no webgl>';
    const g = (p) => { try { return gl.getParameter(p); } catch (e) { return 'ERR'; } };
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    const vd = g(gl.MAX_VIEWPORT_DIMS);
    return {
      vendor: g(gl.VENDOR), renderer: g(gl.RENDERER),
      unmaskedVendor: ext ? g(ext.UNMASKED_VENDOR_WEBGL) : null,
      unmaskedRenderer: ext ? g(ext.UNMASKED_RENDERER_WEBGL) : null,
      version: g(gl.VERSION), glsl: g(gl.SHADING_LANGUAGE_VERSION),
      MAX_TEXTURE_SIZE: g(gl.MAX_TEXTURE_SIZE),
      MAX_CUBE_SIZE: g(gl.MAX_CUBE_MAP_TEXTURE_SIZE),
      MAX_RENDERBUFFER_SIZE: g(gl.MAX_RENDERBUFFER_SIZE),
      MAX_VIEWPORT_DIMS: vd && vd.length !== undefined ? Array.from(vd) : vd,
      MAX_VERTEX_UNIFORM_VECTORS: g(gl.MAX_VERTEX_UNIFORM_VECTORS),
      MAX_FRAGMENT_UNIFORM_VECTORS: g(gl.MAX_FRAGMENT_UNIFORM_VECTORS),
      MAX_VERTEX_TEXTURE_IMAGE_UNITS: g(gl.MAX_VERTEX_TEXTURE_IMAGE_UNITS),
      MAX_TEXTURE_IMAGE_UNITS: g(gl.MAX_TEXTURE_IMAGE_UNITS),
      MAX_COMBINED_TEXTURE_IMAGE_UNITS: g(gl.MAX_COMBINED_TEXTURE_IMAGE_UNITS),
      extCount: (gl.getSupportedExtensions() || []).length,
      webgl2: !!document.createElement('canvas').getContext('webgl2'),
    };
  });
  out.webglExts = safe(() => {
    const gl = document.createElement('canvas').getContext('webgl');
    if (!gl) return '<no webgl>';
    return (gl.getSupportedExtensions() || []).sort().join(',');
  });

  return out;
})()"""

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


SENSOR_JS = r"""async () => {
  // 传感器事件流读数：真机 Chrome Android 有持续数值流（devicemotion 60Hz
  // 级、orientation 随读数更新派发）；iOS 未授权时零事件（内核只在注册
  // 监听时派发一次全 null 哨兵，numeric 计数器为 0 可辨别）。
  const num = (v) => typeof v === 'number' && isFinite(v);
  const out = {
    counts: { motion: 0, motionNumeric: 0, orientation: 0, orientationNumeric: 0,
              orientationAbsolute: 0, orientationAbsoluteNumeric: 0 },
    requestPermission: {
      motion: typeof (window.DeviceMotionEvent || {}).requestPermission,
      orientation: typeof (window.DeviceOrientationEvent || {}).requestPermission },
    orientationAbsoluteInProto: (typeof DeviceOrientationEvent === 'function')
      && ('absolute' in DeviceOrientationEvent.prototype),
    onDeviceOrientationAbsolute: 'ondeviceorientationabsolute' in window,
    intervals: { motion: null },
    samples: { motion: [], orientation: [] },
    sensorApi: null,
  };
  window.addEventListener('devicemotion', (e) => {
    out.counts.motion++;
    const g = e.accelerationIncludingGravity, a = e.acceleration, r = e.rotationRate;
    out.intervals.motion = e.interval;
    if (g && (num(g.x) || num(g.y) || num(g.z))) {
      out.counts.motionNumeric++;
      if (out.samples.motion.length < 4) {
        out.samples.motion.push({
          trusted: e.isTrusted, interval: e.interval,
          aig: [g.x, g.y, g.z],
          acc: a ? [a.x, a.y, a.z] : null,
          rr: r ? [r.alpha, r.beta, r.gamma] : null,
        });
      }
    }
  }, true);
  window.addEventListener('deviceorientation', (e) => {
    out.counts.orientation++;
    if (num(e.alpha) || num(e.beta) || num(e.gamma)) {
      out.counts.orientationNumeric++;
      if (out.samples.orientation.length < 3) {
        out.samples.orientation.push({ a: e.alpha, b: e.beta, g: e.gamma, abs: e.absolute });
      }
    }
  }, true);
  window.addEventListener('deviceorientationabsolute', (e) => {
    out.counts.orientationAbsolute++;
    if (num(e.alpha) || num(e.beta) || num(e.gamma)) out.counts.orientationAbsoluteNumeric++;
  }, true);
  // Sensor API（Chrome Android）＝同一虚拟传感器的另一读取面
  if (typeof Accelerometer === 'function') {
    try {
      const acc = new Accelerometer({ frequency: 60 });
      const got = [];
      acc.addEventListener('reading', () => {
        if (got.length < 4) got.push([acc.x, acc.y, acc.z]);
      });
      acc.start();
      await new Promise((r) => setTimeout(r, 600));
      try { acc.stop(); } catch (e2) {}
      out.sensorApi = { supported: true, samples: got };
    } catch (e) {
      out.sensorApi = { supported: false, error: String(e).slice(0, 120) };
    }
  }
  await new Promise((r) => setTimeout(r, 900));
  return out;
}"""


async def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"

    fp = create_fingerprint(target_os, "generate")
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
            signals = await page.evaluate(BATTERY_JS)

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
            sensor = await page.evaluate(SENSOR_JS)

            out = {
                "target_os": target_os,
                "device": device_name,
                "signals": signals,
                "sensor": sensor,
                "input_mouse_click_events": events_mouse,
                "input_playwright_touchscreen_tap": tap_api,
                "input_touch_tap_events": events_touch,
                "input_touch_scroll": {"before": scroll_before, "after": scroll_after,
                                       "events": events_scroll},
            }
            print(json.dumps(out, ensure_ascii=False, indent=1))
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
