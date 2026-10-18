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
  6. Android：navigator.userAgentData（UA-CH）完整可用 + Sec-CH-UA* 请求头；
     architecture/bitness 恒为空字符串（Chromium 源码的 Android 手机端行为）
  7. Android：navigator.deviceMemory（上报桶）+ Battery API（数值一致）
  8. iOS：navigator.userAgentData / connection / pdfViewerEnabled 不存在，
     plugins/mimeTypes 为空，navigator.standalone 存在且为 false
  9. navigator 上无 own-property 泄漏（JS 补丁改的是原型）
 10. WebGL readPixels 纯净：复刻 BrowserScan webglNoise「纯红三角形 R==A」
     探针，任何噪声像素（内核 canvas 伪装已关闭）即为失败
 11. speechSynthesis.getVoices() 为空列表（宿主 macOS 有上百个语音，
     是「Different operating systems」服务端判定的直接输入）
 12. WebGL getParameter(UNMASKED_RENDERER) 调用吞吐与原生同量级
     （复刻 BrowserScan E() 计时器，Z<=100；纯 JS 缓存返回会被判
     "WebGL exception"）
  13. 跨域 iframe（OOPIF）：platform/screen/DPR/GPU/触控/UA-CH 与主文档
      一致，且首个文档在脚本执行前即带补丁（文档起点取值校验）
  14. Dedicated Worker（页面内 + 跨域 iframe 内）：WorkerNavigator 的
      platform / deviceMemory / userAgentData 与主文档一致

用独立临时数据目录运行，不污染正式数据。

用法：
    .venv/bin/python scripts/mobile_launch_verify.py [android|ios] [generate|preset]
"""
import asyncio
import json
import os
import re
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
    const nativeStr = (fn) => { try { return /\\[native code\\]/.test(Function.prototype.toString.call(fn)); } catch (e) { return false; } };
    const descOf = (proto, name) => { try { return Object.getOwnPropertyDescriptor(proto, name); } catch (e) { return null; } };
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
        timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
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
        integrity: {
            webglGetParameterNative: nativeStr(WebGLRenderingContext.prototype.getParameter),
            webgl2GetParameterNative: nativeStr(WebGL2RenderingContext.prototype.getParameter),
            fnToStringNative: nativeStr(Function.prototype.toString),
            canvasToDataUrlNative: nativeStr(HTMLCanvasElement.prototype.toDataURL),
            canvasGetImageDataNative: nativeStr(CanvasRenderingContext2D.prototype.getImageData),
            colorDepthGetterNative: (() => { const d = descOf(Screen.prototype, 'colorDepth'); return d && d.get ? nativeStr(d.get) : null; })(),
            deviceMemoryGetterNative: (() => { const d = descOf(Navigator.prototype, 'deviceMemory'); return d && d.get ? nativeStr(d.get) : null; })(),
            colorDepthEnumerable: (() => { const d = descOf(Screen.prototype, 'colorDepth'); return d ? d.enumerable : null; })(),
            uaDataOwnProps: nav.userAgentData ? Object.getOwnPropertyNames(nav.userAgentData) : null,
        },
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
    // ---- speechSynthesis 语音列表（BrowserScan 只取数量作为 OS 判定输入）----
    out.voices = await new Promise((resolve) => {
        try {
            if (!window.speechSynthesis) return resolve(-1);
            const s = speechSynthesis;
            const v0 = s.getVoices();
            if (v0.length) return resolve(v0.length);
            s.addEventListener('voiceschanged', () => resolve(s.getVoices().length));
            setTimeout(() => resolve(s.getVoices().length), 1200);
        } catch (e) { resolve(-2); }
    });
    // ---- BrowserScan webglNoise 复刻：纯红三角形，R 应恒等于 A ----
    out.webglNoiseSample = null;
    out.webglNoise = (() => {
        try {
            const t = document.createElement('canvas');
            t.width = 41; t.height = 41;
            const e = t.getContext('webgl2') || t.getContext('webgl');
            if (!e) return -1;
            const vsrc = "attribute vec2 attrVertex; void main () {gl_Position = vec4(attrVertex,0,1);}";
            const fsrc = "void main() {gl_FragColor = vec4(1,0,0,1);}";
            const c = e.createBuffer();
            e.bindBuffer(e.ARRAY_BUFFER, c);
            e.bufferData(e.ARRAY_BUFFER, new Float32Array([0, 1, -1, -1, 1, -1]), e.STATIC_DRAW);
            const a = e.createProgram();
            const r = e.createShader(e.VERTEX_SHADER);
            e.shaderSource(r, vsrc); e.compileShader(r);
            const E = e.createShader(e.FRAGMENT_SHADER);
            e.shaderSource(E, fsrc); e.compileShader(E);
            e.attachShader(a, r); e.attachShader(a, E);
            e.linkProgram(a); e.useProgram(a);
            const loc = e.getAttribLocation(a, 'attrVertex');
            e.enableVertexAttribArray(loc);
            e.vertexAttribPointer(loc, 2, e.FLOAT, false, 0, 0);
            e.drawArrays(e.TRIANGLES, 0, 3);
            const d = new Uint8Array(e.drawingBufferWidth * e.drawingBufferHeight * 4);
            e.readPixels(0, 0, e.drawingBufferWidth, e.drawingBufferHeight,
                         e.RGBA, e.UNSIGNED_BYTE, d);
            const sample = [];
            let bad = 0;
            for (let T = 0; T < d.length; T += 4) {
                if (d[T] !== d[T + 3]) {
                    bad += 1;
                    if (sample.length < 20) sample.push(T, d[T], d[T + 1], d[T + 2], d[T + 3]);
                }
            }
            out.webglNoiseSample = sample;
            return bad;
        } catch (err) { return -2; }
    })();
    // ---- BrowserScan E()：getParameter(UNMASKED_RENDERER) 调用吞吐计时 ----
    // Z = 5ms 窗口内调用数 / 实际毫秒；原生约 2-3，纯 JS 缓存返回每毫秒数万次。
    out.glPerfZ = (() => {
        try {
            const R = document.createElement('canvas');
            const Y = R.getContext('webgl2') || R.getContext('webgl');
            if (!Y) return -1;
            const te = Y.getExtension('WEBGL_debug_renderer_info');
            if (!te) return -1;
            var h = performance.now(), I = 0;
            while (5 > performance.now() - h) {
                for (let k = 0; k < 10; k++) { Y.getParameter(te.UNMASKED_RENDERER_WEBGL); }
                I += 10;
            }
            h = performance.now() - h;
            return Math.round(I / h);
        } catch (err) { return -2; }
    })();
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

# 竞态测试探针页：内联脚本在「文档起点」抓取指纹值（早于任何网络加载），
# 若首个文档在补丁生效前执行，这里抓到的就是原生的宿主值。
RACE_PROBE_HTML = """<!doctype html><html><head><script>
window.__probe = {
  uaMobile: /Android|iPhone/.test(navigator.userAgent),
  touch: navigator.maxTouchPoints,
  colorDepth: screen.colorDepth,
  memory: navigator.deviceMemory,
  hasUaData: !!navigator.userAgentData,
  webgl: (function () { try { var g = document.createElement('canvas').getContext('webgl');
    var e = g.getExtension('WEBGL_debug_renderer_info');
    return e ? g.getParameter(e.UNMASKED_RENDERER_WEBGL) : null; } catch (err) { return 'err'; } })(),
  tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
  id: navigator.userAgent.indexOf('PJZ110') >= 0 ? 1 : 0
};
</script></head><body>probe</body></html>"""

# ---- 跨域 iframe（OOPIF）/ Worker 探针 ----
# 父页 127.0.0.1 与子帧 localhost 属不同 site（站点隔离下必为 OOPIF）；
# 两侧都指向本脚本内置服务器，全程无外部网络依赖。
# 子帧首个文档的内联脚本在「文档起点」读取全部信号并回传父页：
# 任何字段仍是宿主值（macOS 1440x900/DPR2/横向/Linux x86_64）都会被断言捕获。
OOPIF_MAIN_HTML = """<!doctype html><html><head><script>
window.msgs = [];
window.addEventListener('message', function (e) { window.msgs.push(e.data); });
// 页面文档起点的 Worker：early 为脚本首行读取（验证注入先于首行），
// late 为 300ms 后读取（多客户端场景下 resume 竞争不影响最终一致性断言）
try {
  var code = 'postMessage({early: navigator.platform, uaP: navigator.userAgentData ? navigator.userAgentData.platform : null});'
    + 'setTimeout(function () { postMessage({late: navigator.platform, devMem: navigator.deviceMemory,'
    + ' uaP: navigator.userAgentData ? navigator.userAgentData.platform : null}); }, 300);';
  var w = new Worker(URL.createObjectURL(new Blob([code], { type: 'text/javascript' })));
  window.workerData = {};
  w.onmessage = function (ev) { for (var k in ev.data) { window.workerData[k] = ev.data[k]; } };
} catch (e) { window.workerErr = String(e); }
// 跨域 iframe（不同 site → OOPIF）
var f = document.createElement('iframe');
f.src = 'http://localhost:__PORT__/oopif-probe';
f.width = 320; f.height = 240;
document.documentElement.appendChild(f);
</script></head><body>main</body></html>"""

OOPIF_PROBE_HTML = """<!doctype html><html><head><script>
window.__early = {
  platform: navigator.platform,
  cd: screen.colorDepth,
  sw: screen.width, sh: screen.height,
  availW: screen.availWidth, availH: screen.availHeight,
  dpr: devicePixelRatio,
  maxTouch: navigator.maxTouchPoints,
  coarse: matchMedia('(pointer: coarse)').matches,
  orientation: screen.orientation ? screen.orientation.type : null,
  uaP: navigator.userAgentData ? navigator.userAgentData.platform : null,
  uaMobile: navigator.userAgentData ? navigator.userAgentData.mobile : null,
  gl: 'ERR'
};
try {
  var g = document.createElement('canvas').getContext('webgl');
  var ext = g && g.getExtension('WEBGL_debug_renderer_info');
  window.__early.gl = ext ? g.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null;
} catch (e) {}
try {
  var code = 'postMessage({early: navigator.platform});'
    + 'setTimeout(function () { postMessage({late: navigator.platform, devMem: navigator.deviceMemory,'
    + ' uaP: navigator.userAgentData ? navigator.userAgentData.platform : null}); }, 300);';
  var w = new Worker(URL.createObjectURL(new Blob([code], { type: 'text/javascript' })));
  window.__workerData = {};
  w.onmessage = function (ev) { for (var k in ev.data) { window.__workerData[k] = ev.data[k]; } };
} catch (e) { window.__workerErr = String(e); }
setTimeout(function () {
  var out = {};
  for (var k in window.__early) { out[k] = window.__early[k]; }
  out.worker = window.__workerData || null;
  out.workerErr = window.__workerErr || null;
  parent.postMessage(out, '*');
}, 1300);
</script></head><body>probe</body></html>"""

# 二次客户端（Playwright 自身会话）读取跨域 frame 的即时值：
# 验证 OOPIF 补丁对任意客户端口径均一致
FRAME_LIVE_JS = """(() => ({
  platform: navigator.platform,
  cd: screen.colorDepth,
  sw: screen.width, sh: screen.height,
  dpr: devicePixelRatio,
  maxTouch: navigator.maxTouchPoints,
  coarse: matchMedia('(pointer: coarse)').matches,
  uaP: navigator.userAgentData ? navigator.userAgentData.platform : null,
}))()"""

# Accept-CH 高熵头回送探针：/accept-ch 的响应声明高熵提示，页面同源 fetch
# 触发第二次请求；服务器端记录两次请求实际收到的头（真机 Chrome 在声明后
# 对同源后续请求附上高熵 Sec-CH-UA-*，Safari 则始终没有 UA-CH 头）
ACCEPT_CH_VALUE = (
    "Sec-CH-UA-Model, Sec-CH-UA-Platform-Version, Sec-CH-UA-Arch, "
    "Sec-CH-UA-Bitness, Sec-CH-UA-Full-Version-List, Sec-CH-UA-Full-Version, "
    "Sec-CH-UA-WoW64"
)

ACCEPT_CH_HTML = """<!doctype html><html><head><script>
fetch('/accept-ch-second').catch(function () {});
</script></head><body>accept-ch</body></html>"""

# WebGPU 适配器信息：安全上下文（localhost/127.0.0.1）下读取 GPUAdapterInfo；
# 真机 Android Chrome 为 vendor "qualcomm" / architecture "adreno-<型号>"，
# 宿主 Apple GPU（vendor "apple"/"metal-*"）会在跨 API 对照下暴露。
WEBGPU_JS = """(async () => {
  const out = { hasGpu: !!navigator.gpu };
  if (!navigator.gpu) { return out; }
  try {
    const a = await navigator.gpu.requestAdapter();
    if (!a) { out.adapter = null; return out; }
    const i = a.info || {};
    out.vendor = i.vendor;
    out.architecture = i.architecture;
    out.device = i.device;
    out.description = i.description;
    out.protoOk = Object.getPrototypeOf(i) === GPUAdapterInfo.prototype;
  } catch (e) { out.err = String(e); }
  return out;
})()"""

# 媒体设备枚举：真机 Android Chrome 枚举前后两个摄像头（videoinput ×2）；
# 未授权时 label/deviceId/groupId 全为空串（Chromium 语义）。
MEDIA_JS = """(async () => {
  const out = {};
  try {
    const ds = await navigator.mediaDevices.enumerateDevices();
    const counts = {};
    ds.forEach(d => { counts[d.kind] = (counts[d.kind] || 0) + 1; });
    out.counts = counts;
    out.total = ds.length;
    out.allMasked = ds.every(d => d.deviceId === '' && d.label === '' && d.groupId === '');
    out.protoOk = ds.every(d => d instanceof MediaDeviceInfo);
  } catch (e) { out.err = String(e); }
  return out;
})()"""


async def race_test(ws_endpoint: str) -> dict:
    """模拟用户新开标签页：目标被本会话门控 → 首个文档必须在补丁生效后执行。

    使用原始 CDP 且不引入 Playwright 客户端（Playwright 自身也使用
    waitForDebuggerOnStart 门控并会抢先 resume，会掩盖真实竞态）。
    """
    import itertools
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import websockets

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = RACE_PROBE_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: A003
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    http_port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    probe: dict = {}
    try:
        async with websockets.connect(ws_endpoint, max_size=64 * 1024 * 1024,
                                      ping_interval=None) as ws:
            ids = itertools.count(1)
            pending: dict = {}

            async def reader():
                async for raw in ws:
                    msg = json.loads(raw)
                    mid = msg.get("id")
                    if mid is None:
                        continue
                    fut = pending.pop(mid, None)
                    if fut and not fut.done():
                        fut.set_result(msg)

            rt = asyncio.create_task(reader())

            async def send(method: str, params: dict | None = None,
                           sid: str | None = None, timeout: float = 6.0) -> dict:
                mid = next(ids)
                msg: dict = {"id": mid, "method": method}
                if params:
                    msg["params"] = params
                if sid:
                    msg["sessionId"] = sid
                fut = asyncio.get_running_loop().create_future()
                pending[mid] = fut
                await ws.send(json.dumps(msg))
                try:
                    return await asyncio.wait_for(fut, timeout)
                except asyncio.TimeoutError:
                    return {"__timeout__": True}

            try:
                # 用户开新标签页（此时页面 target 被会话以 waitForDebuggerOnStart 暂停）
                res = await send("Target.createTarget", {"url": "about:blank"})
                target_id = (res.get("result") or {}).get("targetId")
                if not target_id:
                    return {}
                at = await send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
                sid = (at.get("result") or {}).get("sessionId")
                if not sid:
                    return {}
                await send("Page.enable", {}, sid=sid)
                # 门控下该命令的响应会在会话 resume 后才返回（超时可忽略）
                await send("Page.navigate", {"url": f"http://127.0.0.1:{http_port}/"},
                           sid=sid, timeout=3.0)
                for _ in range(40):
                    await asyncio.sleep(0.2)
                    r = await send("Runtime.evaluate",
                                   {"expression": "window.__probe || null",
                                    "returnByValue": True}, sid=sid, timeout=3.0)
                    val = (((r.get("result") or {}).get("result")) or {}).get("value")
                    if isinstance(val, dict):
                        probe = val
                        break
                await send("Target.closeTarget", {"targetId": target_id})
            finally:
                rt.cancel()
    finally:
        server.shutdown()
    return probe


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
            "locale": "zh-CN", "timezone": "America/Los_Angeles",
            "start_url": "https://example.com",
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

    # ---- 新标签页竞态测试（原始 CDP；Playwright 尚未连接，仅本会话门控）----
    print("\n== 竞态测试：新标签页首个文档（原始 CDP，文档起点抓取）==")
    probe = await race_test(res["ws_endpoint"])
    print(json.dumps(probe, ensure_ascii=False))

    frame_state: dict = {}
    worker_state: dict = {}
    accept_ch_state: dict = {}
    webgpu_state: dict = {}
    media_state: dict = {}

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

        # ---- 跨域 iframe（OOPIF）+ Worker：与主文档同一套信号 ----
        # 本地服务器双栈监听：127.0.0.1（父页）与 localhost（子帧）分属不同
        # site；/oopif-probe 刻意延迟 0.25s —— 本地 iframe 的加载快于 CDP
        # 往返，延迟模拟真实跨域加载时序，使「文档起点注入」可以确定性验证。
        import socket as _socket
        import threading as _threading
        import time as _time
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        oopif_state: dict = {"port": 0}

        class _OopifHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                extra_headers: dict = {}
                if self.path.startswith("/oopif-probe"):
                    _time.sleep(0.25)
                    body = OOPIF_PROBE_HTML
                elif self.path.startswith("/accept-ch-second"):
                    oopif_state.setdefault("accept_ch", {})["second"] = {
                        k.lower(): v for k, v in self.headers.items()}
                    body = "<html>ok</html>"
                elif self.path.startswith("/accept-ch"):
                    oopif_state.setdefault("accept_ch", {})["first"] = {
                        k.lower(): v for k, v in self.headers.items()}
                    body = ACCEPT_CH_HTML
                    extra_headers["Accept-CH"] = ACCEPT_CH_VALUE
                else:
                    body = OOPIF_MAIN_HTML
                data = body.replace("__PORT__", str(oopif_state["port"])).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                for hname, hvalue in extra_headers.items():
                    self.send_header(hname, hvalue)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # noqa: A003
                pass

        class _DualStackServer(ThreadingHTTPServer):
            address_family = _socket.AF_INET6

            def server_bind(self):
                self.socket.setsockopt(_socket.IPPROTO_IPV6, _socket.IPV6_V6ONLY, 0)
                super().server_bind()

        oopif_server = _DualStackServer(("::", 0), _OopifHandler)
        oopif_state["port"] = oopif_server.server_address[1]
        _threading.Thread(target=oopif_server.serve_forever, daemon=True).start()
        try:
            await page.goto(f"http://127.0.0.1:{oopif_state['port']}/oopif-main",
                            wait_until="domcontentloaded")
            bundle: dict = {}
            for _ in range(60):
                bundle = await page.evaluate(
                    "() => ({msgs: window.msgs || [],"
                    " worker: window.workerData || null,"
                    " workerErr: window.workerErr || null})")
                wk = bundle.get("worker") or {}
                if bundle.get("msgs") and "late" in wk:
                    break
                await asyncio.sleep(0.25)
            worker_state = bundle.get("worker") or {}
            msgs = bundle.get("msgs") or []
            if msgs:
                frame_state = msgs[0]
            # 二次客户端读取跨域 frame 即时值（Playwright 自建 OOPIF 会话）
            frame_live: dict = {}
            frames = [fr for fr in page.frames if "localhost" in (fr.url or "")]
            if frames:
                try:
                    frame_live = await frames[0].evaluate(FRAME_LIVE_JS)
                except Exception as exc:  # noqa: BLE001
                    frame_live = {"err": str(exc)[:100]}
            frame_state["live"] = frame_live

            print("\n== 跨域 iframe（OOPIF，文档起点取值）==")
            print(json.dumps({k: v for k, v in frame_state.items() if k != "worker"},
                             ensure_ascii=False))
            print("\n== 跨域 iframe 内 Worker ==")
            print(json.dumps(frame_state.get("worker") or {}, ensure_ascii=False))
            print("\n== 页面 Worker（文档起点）==")
            print(json.dumps(worker_state, ensure_ascii=False))

            # ---- Accept-CH 高熵头回送：声明后同源请求应带高熵头（真机行为）----
            await page.goto(f"http://127.0.0.1:{oopif_state['port']}/accept-ch",
                            wait_until="domcontentloaded")
            await asyncio.sleep(1.0)
            accept_ch_state = dict(oopif_state.get("accept_ch") or {})
            print("\n== Accept-CH 高熵头回送（两次请求实收头）==")
            print(json.dumps(accept_ch_state, ensure_ascii=False, indent=1))

            # ---- WebGPU 适配器信息（安全上下文；Android 应为 Qualcomm Adreno）----
            webgpu_state = await page.evaluate(WEBGPU_JS) or {}
            print("\n== WebGPU 适配器信息 ==")
            print(json.dumps(webgpu_state, ensure_ascii=False))

            # ---- 媒体设备枚举（真机 Android 前后双摄形态）----
            media_state = await page.evaluate(MEDIA_JS) or {}
            print("\n== 媒体设备枚举 ==")
            print(json.dumps(media_state, ensure_ascii=False))
        finally:
            oopif_server.shutdown()
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
        "时区覆盖生效（launch.timezone）": state.get("timezone") == "America/Los_Angeles",
        # ---- 竞态：新标签页首个文档（含内联脚本）必须已是补丁值 ----
        "竞态：首文档 UA 为移动端": probe.get("uaMobile") is True,
        "竞态：首文档色深 24": probe.get("colorDepth") == 24,
        "竞态：首文档触控已启用": int(probe.get("touch") or 0) >= 5,
        "竞态：首文档时区已对齐": probe.get("tz") == "America/Los_Angeles",
        "竞态：首文档 GPU 已替换": (probe.get("webgl") or "") == vc.get("renderer"),
        # ---- 补丁函数原生性（BrowserScan「Canvas Tampering」的触发点）----
        "补丁函数 toString 呈原生": all([
            (state.get("integrity") or {}).get("webglGetParameterNative") is True,
            (state.get("integrity") or {}).get("webgl2GetParameterNative") is True,
            (state.get("integrity") or {}).get("fnToStringNative") is True,
            (state.get("integrity") or {}).get("canvasToDataUrlNative") is True,
            (state.get("integrity") or {}).get("canvasGetImageDataNative") is True,
        ]),
        "colorDepth getter 原生且 enumerable": (
            (state.get("integrity") or {}).get("colorDepthGetterNative") is True
            and (state.get("integrity") or {}).get("colorDepthEnumerable") is True),
        # ---- BrowserScan 服务端 OS 判定输入：语音列表必须为空 ----
        "speechSynthesis 语音列表为空": state.get("voices") == 0,
        # ---- BrowserScan webglNoise：readPixels 纯净（内核 canvas 伪装已关闭）----
        "WebGL readPixels 无噪声（R==A）": state.get("webglNoise") == 0,
        # ---- BrowserScan E()：getParameter 耗时须与原生同量级（Z<=100）----
        "WebGL getParameter 吞吐原生量级（Z<=100）": (
            isinstance(state.get("glPerfZ"), int) and 0 <= state.get("glPerfZ") <= 100),
    }

    # ---- 跨域 iframe（OOPIF）：reCAPTCHA 等三方沙箱内与主文档同源 ----
    fr = frame_state if isinstance(frame_state, dict) else {}
    fr_live = fr.get("live") if isinstance(fr.get("live"), dict) else {}
    fr_worker = fr.get("worker") if isinstance(fr.get("worker"), dict) else {}
    wk_main = worker_state if isinstance(worker_state, dict) else {}
    # ---- Accept-CH：声明后同源请求实际收到的头（真机 Chrome 补高熵）----
    ac_first = (accept_ch_state or {}).get("first") or {}
    ac_second = (accept_ch_state or {}).get("second") or {}
    # ---- WebGPU：适配器信息（安全上下文实测；真机 Android 为 Qualcomm Adreno）----
    wg = webgpu_state if isinstance(webgpu_state, dict) else {}
    _wg_m = re.search(r"Adreno\s*(?:\(TM\))?\s*(\d{3,4})", str(vc.get("renderer") or ""))
    wg_arch = ("adreno-" + _wg_m.group(1)) if _wg_m else None
    # ---- 媒体设备枚举（类型计数）----
    media_counts = (media_state or {}).get("counts") or {}
    checks.update({
        "跨域 iframe：platform 一致（文档起点）": fr.get("platform") == nav.get("platform"),
        "跨域 iframe：colorDepth == 24（文档起点）": fr.get("cd") == 24,
        "跨域 iframe：screen/avail == 主文档": (
            fr.get("sw") == state["screenW"] and fr.get("sh") == state["screenH"]
            and fr.get("availW") == state["screenW"] and fr.get("availH") == state["screenH"]),
        "跨域 iframe：DPR 一致（文档起点）": abs((fr.get("dpr") or 0) - dpr) < 0.02,
        "跨域 iframe：GPU == 指纹（文档起点）": (fr.get("gl") or "") == vc.get("renderer"),
        "跨域 iframe：maxTouchPoints 一致": fr.get("maxTouch") == nav.get("maxTouchPoints"),
        "跨域 iframe：粗指针（coarse）": fr.get("coarse") is True,
        "跨域 iframe：朝向 portrait-primary": fr.get("orientation") == "portrait-primary",
        "跨域 iframe：二次客户端读取一致": (
            fr_live.get("platform") == nav.get("platform")
            and fr_live.get("cd") == 24
            and fr_live.get("coarse") is True),
        "Worker（页面）：platform 一致": wk_main.get("late") == nav.get("platform"),
        "Worker（跨域 iframe）：platform 一致": fr_worker.get("late") == nav.get("platform"),
    })

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
            "UA-CH 高熵 architecture/bitness 为空串（Android 手机端行为）": bool(high) and (
                high.get("architecture") == "" and high.get("bitness") == ""),
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
            "竞态：首文档 UA-CH/内存已注入": bool(probe.get("hasUaData"))
                and probe.get("memory") == mem_class,
            "userAgentData 无 own property": (
                (state.get("integrity") or {}).get("uaDataOwnProps")) == [],
            "deviceMemory getter 原生": (
                (state.get("integrity") or {}).get("deviceMemoryGetterNative")) is True,
            "跨域 iframe：UA-CH platform/mobile == 指纹": (
                fr.get("uaP") == (ua_ch or {}).get("platform")
                and fr.get("uaMobile") is True),
            "Worker（页面）：UA-CH platform == Android": (
                wk_main.get("uaP") == (ua_ch or {}).get("platform")),
            "Worker（页面）：deviceMemory == 内存桶": (
                wk_main.get("devMem") == state.get("deviceMemory")),
            "Worker（跨域 iframe）：UA-CH platform == Android": (
                fr_worker.get("uaP") == (ua_ch or {}).get("platform")),
            # ---- Accept-CH：站点声明后回送高熵头（真机 Chrome 行为）----
            "Accept-CH：首个文档仅低熵三头（无高熵）": (
                "sec-ch-ua" in ac_first and "sec-ch-ua-model" not in ac_first),
            "Accept-CH：声明后同源请求补齐高熵头": (
                ac_second.get("sec-ch-ua-model") == f'"{ua_ch.get("model") or ""}"'
                and ac_second.get("sec-ch-ua-platform-version") == f'"{ua_ch.get("platformVersion") or ""}"'
                and ac_second.get("sec-ch-ua-arch") == '""'
                and ac_second.get("sec-ch-ua-bitness") == '""'
                and ac_second.get("sec-ch-ua-wow64") == "?0"),
            "Accept-CH：fullVersion/全版本列表 == 指纹": (
                ac_second.get("sec-ch-ua-full-version") == f'"{ua_ch.get("fullVersion") or ""}"'
                and ac_second.get("sec-ch-ua-full-version-list") == ", ".join(
                    f'"{b["brand"]}";v="{b["version"]}"'
                    for b in (ua_ch.get("fullVersionList") or []))),
            "Accept-CH：低熵三头与 UA 在回送中保持": (
                ac_second.get("sec-ch-ua") == sec_ch_headers(ua_ch or {}).get("Sec-CH-UA")
                and ac_second.get("sec-ch-ua-mobile") == "?1"
                and ac_second.get("sec-ch-ua-platform") == f'"{ua_ch.get("platform") or "Android"}"'
                and ac_second.get("user-agent") == nav["userAgent"]),
            # ---- WebGPU：适配器信息（真机 Android Chrome 为 Qualcomm Adreno）----
            "WebGPU：适配器 vendor == qualcomm（非宿主 apple）": (
                wg.get("hasGpu") is True and wg.get("vendor") == "qualcomm"),
            "WebGPU：architecture == adreno-<WebGL 型号>（跨 API 一致）": (
                bool(wg_arch) and wg.get("architecture") == wg_arch),
            "WebGPU：GPUAdapterInfo 原型链/空 device/description 保持原生形态": (
                wg.get("protoOk") is True and wg.get("device") == ""
                and wg.get("description") == ""),
            # ---- 媒体设备枚举（真机 Android Chrome 前后双摄）----
            "媒体设备：videoinput == 2（前后摄）、audioinput/audiooutput 各 1": (
                media_counts.get("videoinput") == 2
                and media_counts.get("audioinput") == 1
                and media_counts.get("audiooutput") == 1),
            "媒体设备：未授权时 label/deviceId/groupId 全空 + instanceof 成立": (
                (media_state or {}).get("allMasked") is True
                and (media_state or {}).get("protoOk") is True),
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
            "竞态：首文档无 UA-CH（Safari 行为）": probe.get("hasUaData") is False,
            "跨域 iframe：UA-CH 不存在（Safari 行为）": fr.get("uaP") is None,
            "Worker（页面）：UA-CH 不存在（Safari 行为）": wk_main.get("uaP") is None,
            "Worker（页面）：deviceMemory 不暴露（Safari 行为）": wk_main.get("devMem") is None,
            "Worker（跨域 iframe）：UA-CH 不存在（Safari 行为）": fr_worker.get("uaP") is None,
            # ---- Accept-CH：Safari 无 UA-CH，声明后也没有任何 Sec-CH-UA* 头 ----
            "Accept-CH：Safari 声明前后均无 UA-CH 头": (
                not any(k.startswith("sec-ch") for k in ac_first)
                and not any(k.startswith("sec-ch") for k in ac_second)),
            # ---- WebGPU：iOS 保持宿主 Apple 适配器（真机 iPhone 同为 Apple GPU）----
            "WebGPU：iOS 适配器 vendor == apple（真机一致）": (
                wg.get("hasGpu") is True and wg.get("vendor") == "apple"),
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
