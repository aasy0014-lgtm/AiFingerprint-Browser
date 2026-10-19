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
  8. iOS Safari：navigator.userAgentData / connection / pdfViewerEnabled 不存在，
     plugins/mimeTypes 为空，navigator.standalone 存在且为 false，
     无 __gCrWeb / webkit.messageHandlers
  8b. iOS Chrome（CriOS）：UA 含 CriOS、__gCrWeb（windowId + 成员族）、
     webkit.messageHandlers、被改写的 history/print/scrollTo（toString 痕迹）
     均在文档起点生效；standalone 不存在；两口径均无 window.chrome
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
    .venv/bin/python scripts/mobile_launch_verify.py [android|ios] [generate|preset] [safari|chrome_ios]
"""
import asyncio
import json
import math
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
from app.mobile_cdp_injector import derive_spec, sec_ch_headers

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
        // ---- iOS Chrome（CriOS）检测面（报告 §4.4 / 补丁方案 §2.3）----
        hasGCrWeb: typeof window.__gCrWeb !== 'undefined',
        gCrWebWindowId: (window.__gCrWeb && window.__gCrWeb.windowId) || null,
        gCrWebKeys: window.__gCrWeb ? Object.keys(window.__gCrWeb) : [],
        hasWK: !!(window.webkit && window.webkit.messageHandlers),
        wkHandlers: window.webkit && window.webkit.messageHandlers
            ? Object.keys(window.webkit.messageHandlers) : [],
        historyPatched: /invokeOnHost/.test(String(window.history.pushState)),
        printPatched: /invokeOnHost/.test(String(window.print)),
        scrollPatched: /setWebViewScrollViewIsDragging/.test(String(window.scrollTo)),
        hasChromeObj: typeof window.chrome !== 'undefined',
        isCriOS: /CriOS\\/[\\d.]+/.test(nav.userAgent),
        paymentRequest: String(window.PaymentRequest || ''),
        pluginsLen: nav.plugins.length,
        mimeTypesLen: nav.mimeTypes.length,
        dpr: window.devicePixelRatio,
        screenW: screen.width, screenH: screen.height,
        colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth,
        innerW: innerWidth, innerH: innerHeight,
        touchEvents: 'ontouchstart' in window,
        // ---- 平台属性面：桌面 Chromium 独有 API 的保留/抹除校验 ----
        hasSerial: 'serial' in nav,
        hasUsb: 'usb' in nav,
        hasHid: 'hid' in nav,
        hasBluetooth: 'bluetooth' in nav,
        hasKeyboard: 'keyboard' in nav,
        hasMidiAccess: typeof nav.requestMIDIAccess === 'function',
        hasPresentation: 'presentation' in nav,
        hasRelatedApps: typeof nav.getInstalledRelatedApps === 'function',
        hasVibrate: typeof nav.vibrate === 'function',
        hasOpenFilePicker: typeof window.showOpenFilePicker === 'function',
        hasPerfMemory: typeof performance.memory !== 'undefined',
        pdfViewerValue: nav.pdfViewerEnabled,
        connTypeIn: !!(nav.connection && ('type' in nav.connection)),
        connType: (nav.connection && ('type' in nav.connection)) ? nav.connection.type : null,
        // ---- iOS WebKit 专有手势事件家族（Chromium 全平台无）----
        hasGestureEvent: typeof window.GestureEvent === 'function',
        hasGestureHandler: 'ongesturestart' in window,
        gestureProtoOk: (() => {
            try {
                return typeof window.GestureEvent === 'function'
                    && Object.getPrototypeOf(window.GestureEvent.prototype) === UIEvent.prototype;
            } catch (e) { return false; }
        })(),
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
  ua: navigator.userAgent,
  touch: navigator.maxTouchPoints,
  colorDepth: screen.colorDepth,
  memory: navigator.deviceMemory,
  hasUaData: !!navigator.userAgentData,
  // iOS Chrome（CriOS）检测面：文档起点即应存在（init 脚本先于页面脚本）
  gcr: typeof window.__gCrWeb !== 'undefined',
  gcrWindowId: (window.__gCrWeb && window.__gCrWeb.windowId) || null,
  wk: !!(window.webkit && window.webkit.messageHandlers),
  hist: /invokeOnHost/.test(String(window.history.pushState)),
  scroll: /setWebViewScrollViewIsDragging/.test(String(window.scrollTo)),
  chromeType: typeof window.chrome,
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
    + ' connType: (navigator.connection && ("type" in navigator.connection)) ? navigator.connection.type : "<none>",'
    + ' uaP: navigator.userAgentData ? navigator.userAgentData.platform : null}); }, 300);';
  var w = new Worker(URL.createObjectURL(new Blob([code], { type: 'text/javascript' })));
  window.workerData = {};
  w.onmessage = function (ev) { for (var k in ev.data) { window.workerData[k] = ev.data[k]; } };
} catch (e) { window.workerErr = String(e); }
// 跨域 iframe（不同 site → OOPIF）
// allow 委托传感器权限：devicemotion 受 accelerometer/gyroscope Permissions
// Policy 门控（默认 allowlist=self），未委托的跨域子帧真机同样拿不到事件。
var f = document.createElement('iframe');
f.src = 'http://localhost:__PORT__/oopif-probe';
f.width = 320; f.height = 240;
f.setAttribute('allow', 'accelerometer; gyroscope; magnetometer');
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
  // iOS Chrome：init 脚本对子帧同样生效，CriOS 检测面在 OOPIF 内也应成立
  gcr: typeof window.__gCrWeb !== 'undefined',
  wk: !!(window.webkit && window.webkit.messageHandlers),
  hist: /invokeOnHost/.test(String(window.history.pushState)),
  chromeType: typeof window.chrome,
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
    + ' connType: (navigator.connection && ("type" in navigator.connection)) ? navigator.connection.type : "<none>",'
    + ' uaP: navigator.userAgentData ? navigator.userAgentData.platform : null}); }, 300);';
  var w = new Worker(URL.createObjectURL(new Blob([code], { type: 'text/javascript' })));
  window.__workerData = {};
  w.onmessage = function (ev) { for (var k in ev.data) { window.__workerData[k] = ev.data[k]; } };
} catch (e) { window.__workerErr = String(e); }
// 传感器面：跨域 iframe（OOPIF 进程）内 devicemotion 数值流（父页已按
// Permissions Policy 声明 allow 委托；未委托时内核抑制，与真机同规则）
window.__dmCount = 0;
window.__dmNumeric = 0;
try {
  window.addEventListener('devicemotion', function (e) {
    window.__dmCount++;
    var g = e.accelerationIncludingGravity;
    if (g && (typeof g.x === 'number' || typeof g.y === 'number' || typeof g.z === 'number')) {
      window.__dmNumeric++;
    }
  }, true);
} catch (e) {}
setTimeout(function () {
  var out = {};
  for (var k in window.__early) { out[k] = window.__early[k]; }
  out.worker = window.__workerData || null;
  out.workerErr = window.__workerErr || null;
  out.dm = window.__dmCount;
  out.dmNumeric = window.__dmNumeric;
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
  gcr: typeof window.__gCrWeb !== 'undefined',
  wk: !!(window.webkit && window.webkit.messageHandlers),
  hist: /invokeOnHost/.test(String(window.history.pushState)),
  chromeType: typeof window.chrome,
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
# 真机 Android Chrome 报内核 Dawn gpu_info 映射的代际级架构名（qualcomm/
# adreno-Nxx、arm/valhall·gen-5、samsung/rdna-N、img-tec/d-series，见
# _android_webgpu），宿主 Apple GPU（"apple"/"metal-*"）会在跨 API 对照下暴露。
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


# 输入事件层探针：记录页面实际收到的事件类型（pointer 事件带 pointerType）。
# 移动端自动化的真机等价路径是 CDP 触摸派发（Input.dispatchTouchEvent）：
# 真机点击序列 = pointerdown:touch → touchstart → pointerup:touch → touchend
# →（兼容鼠标）mousedown → mouseup → click:touch。桌面鼠标序列在手机上不存在，
# 是行为型风控（Cloudflare/DataDome/极验等）最直接的一层证据。
EVENT_CAPTURE_JS = r"""() => {
  window.__ev = [];
  const rec = (e) => window.__ev.push(e.type + (e.pointerType !== undefined ? ':' + e.pointerType : ''));
  for (const t of ['pointerover','pointerenter','pointermove','pointerdown','mousedown','touchstart',
                   'mousemove','touchmove','pointerup','mouseup','touchend','click']) {
    document.addEventListener(t, rec, true);
  }
  let hit = document.getElementById('__mobile_input_probe');
  if (!hit) {
    hit = document.createElement('div');
    hit.id = '__mobile_input_probe';
    hit.style.cssText = 'position:fixed;inset:0;z-index:2147483647;background:transparent';
    document.body.appendChild(hit);
  }
  return 'ok';
}"""


async def _mouse_click(page) -> None:
    await page.mouse.move(120, 300)
    await page.mouse.down()
    await asyncio.sleep(0.05)
    await page.mouse.up()


async def input_event_probe(page) -> dict:
    """真机输入语义探针（移动端自动化路径）。

    1) CDP 触摸派发（Input.dispatchTouchEvent）——移动端自动化的真机等价
       路径：应产生完整触摸序列 + 兼容鼠标事件 + click（pointerType=touch）。
       注意 `page.touchscreen.tap` 在 connect_over_cdp 的默认上下文不可用
       （无 hasTouch），CDP 原始派发才是可用路径。
    2) Playwright 鼠标点击——应答必须正常返回：内核的
       Emulation.setEmitTouchEventsForMouse 转换会吞掉 mousePressed/Released
       的 CDP 应答（fp-chromium 148 实测），一旦启用，Playwright 点击永久挂起。
       本项即该回归的守卫（详见 docs/移动端大厂检测对抗优化方案.md 事件层）。
    """
    out: dict = {"touch": [], "mouse": [], "mouse_ok": False}
    await page.evaluate(EVENT_CAPTURE_JS)
    try:
        cdp = await page.context.new_cdp_session(page)
        await cdp.send("Input.dispatchTouchEvent", {
            "type": "touchStart",
            "touchPoints": [{"x": 160, "y": 420, "radiusX": 1, "radiusY": 1,
                             "force": 1, "id": 1}],
        })
        await cdp.send("Input.dispatchTouchEvent",
                       {"type": "touchEnd", "touchPoints": []})
        await asyncio.sleep(0.3)
        out["touch"] = await page.evaluate("() => (window.__ev || []).splice(0)")
    except Exception as exc:  # noqa: BLE001
        out["touch_error"] = f"{type(exc).__name__}: {exc}"[:160]
    try:
        await asyncio.wait_for(_mouse_click(page), 5)
        out["mouse_ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["mouse_error"] = type(exc).__name__
    await asyncio.sleep(0.25)
    out["mouse"] = await page.evaluate("() => (window.__ev || []).splice(0)")
    await page.evaluate(
        "() => { const el = document.getElementById('__mobile_input_probe');"
        " if (el) el.remove(); }")
    return out


# 传感器面探针：真机 Chrome Android 有持续数值事件流（devicemotion 60Hz 级、
# deviceorientation 随传感器更新派发）；桌面内核只在注册监听时派发一次全 null
# 的「无传感器」哨兵事件——连续数值流是模拟器/桌面装手机最直接的判据之一。
# iOS 侧另有 WebKit 专有的 requestPermission 静态方法（iOS 14.5+）与
# DeviceOrientationEvent.absolute 缺失、deviceorientationabsolute 不存在三个面。
SENSOR_PROBE_JS = r"""async () => {
  const S = window.__sensor = {
    requestPermission: {
      motion: typeof (window.DeviceMotionEvent || {}).requestPermission,
      orientation: typeof (window.DeviceOrientationEvent || {}).requestPermission,
    },
    orientationAbsoluteInProto: (typeof DeviceOrientationEvent === 'function')
      && ('absolute' in DeviceOrientationEvent.prototype),
    onDeviceOrientationAbsolute: 'ondeviceorientationabsolute' in window,
    counts: { motion: 0, motionNumeric: 0, orientation: 0, orientationNumeric: 0,
              orientationAbsolute: 0, orientationAbsoluteNumeric: 0 },
    lastMotion: null, lastOrientation: null, lastOrientationAbsolute: null,
  };
  const num = (v) => typeof v === 'number' && isFinite(v);
  window.addEventListener('devicemotion', (e) => {
    S.counts.motion++;
    const aig = e.accelerationIncludingGravity, acc = e.acceleration, rr = e.rotationRate;
    if (aig && (num(aig.x) || num(aig.y) || num(aig.z))) S.counts.motionNumeric++;
    S.lastMotion = { trusted: e.isTrusted, interval: e.interval,
      aig: aig ? [aig.x, aig.y, aig.z] : null,
      acc: acc ? [acc.x, acc.y, acc.z] : null,
      rr: rr ? [rr.alpha, rr.beta, rr.gamma] : null };
  }, true);
  const orient = (e) => ({ trusted: e.isTrusted, a: e.alpha, b: e.beta, g: e.gamma,
                           abs: e.absolute });
  window.addEventListener('deviceorientation', (e) => {
    S.counts.orientation++;
    if (num(e.alpha) || num(e.beta) || num(e.gamma)) S.counts.orientationNumeric++;
    S.lastOrientation = orient(e);
  }, true);
  window.addEventListener('deviceorientationabsolute', (e) => {
    S.counts.orientationAbsolute++;
    if (num(e.alpha) || num(e.beta) || num(e.gamma)) S.counts.orientationAbsoluteNumeric++;
    S.lastOrientationAbsolute = orient(e);
  }, true);
  await new Promise((r) => setTimeout(r, 1500));
  return S;
}"""


# requestPermission 语义探针（仅 iOS 有该方法）。
# 必须在「无自动化激活污染」的独立页面 + 裸 CDP Runtime.evaluate 上测：
# Playwright 的 evaluate 默认以 userGesture=true 执行（实现细节），会给
# 文档授予瞬时激活——同一文档内永远测不到「无手势」分支。两段语义：
#   - 无手势调用 → 真机 reject NotAllowedError（WebKit 原文案）；
#   - CDP 触摸点击按钮（真实手势）→ 真机弹系统授权框、Promise 保持
#     pending 直到用户作答；仿真不弹框也不伪造授权结果，同样保持 pending。
REQUEST_PERMISSION_CALL_JS = (
    "(() => { const r = window.__rpNoGesture = { state: 'pending' };"
    " const ua = navigator.userActivation;"
    " r.activation = ua ? { active: ua.isActive, sticky: ua.hasBeenActive } : null;"
    " DeviceMotionEvent.requestPermission().then("
    "  (v) => { r.state = 'resolved:' + v; },"
    "  (e) => { r.state = 'rejected:' + (e && e.name); });"
    " return 'called'; })()")


REQUEST_PERMISSION_GESTURE_JS = (
    "(() => { const btn = document.createElement('button');"
    " btn.id = '__rp_probe'; btn.textContent = 'rp';"
    " btn.style.cssText = 'position:fixed;left:0;top:0;width:240px;height:240px;'"
    "  + 'z-index:2147483647;background:transparent;border:0';"
    " window.__rpGesture = { state: 'pending' };"
    " btn.addEventListener('click', () => { const ua = navigator.userActivation;"
    "  window.__rpGesture.activation = ua ? { active: ua.isActive, sticky: ua.hasBeenActive } : null;"
    "  DeviceMotionEvent.requestPermission().then("
    "   (v) => { window.__rpGesture.state = 'resolved:' + v; },"
    "   (e) => { window.__rpGesture.state = 'rejected:' + (e && e.name); }); });"
    " document.body.appendChild(btn); return 'ok'; })()")


async def sensor_probe(page) -> dict:
    """传感器面：Android 连续数值流 + iOS WebKit 权限面/语义。"""
    out: dict = await page.evaluate(SENSOR_PROBE_JS)
    if (out.get("requestPermission") or {}).get("motion") == "function":
        out.update(await _request_permission_probe(page))
    return out


async def _request_permission_probe(page) -> dict:
    """iOS requestPermission 两段语义（独立页面 + 裸 CDP，见上注）。"""
    out: dict = {}
    fresh = None
    try:
        fresh = await page.context.new_page()
        cdp = await fresh.context.new_cdp_session(fresh)
        await cdp.send("Page.enable")
        await cdp.send("Page.navigate", {"url": page.url})
        await asyncio.sleep(1.0)

        async def raw(expr):
            res = await cdp.send("Runtime.evaluate",
                                 {"expression": expr, "returnByValue": True})
            return res.get("result", {}).get("value")

        out["freshPage"] = await raw("location.href")
        out["noGestureCalls"] = await raw(REQUEST_PERMISSION_CALL_JS)
        await asyncio.sleep(0.6)
        out["noGesture"] = await raw("window.__rpNoGesture")
        await raw(REQUEST_PERMISSION_GESTURE_JS)
        await cdp.send("Input.dispatchTouchEvent", {
            "type": "touchStart",
            "touchPoints": [{"x": 120, "y": 120, "radiusX": 1, "radiusY": 1,
                             "force": 1, "id": 1}],
        })
        await cdp.send("Input.dispatchTouchEvent",
                       {"type": "touchEnd", "touchPoints": []})
        await asyncio.sleep(0.5)
        out["gesture"] = await raw("window.__rpGesture")
    except Exception as exc:  # noqa: BLE001
        out["requestPermissionError"] = f"{type(exc).__name__}: {exc}"[:160]
    finally:
        if fresh is not None:
            try:
                await fresh.close()
            except Exception:  # noqa: BLE001
                pass
    return out


def _sensor_magnitude(values) -> float | None:
    """向量模长；含 None/非数值时返回 None。"""
    if not isinstance(values, list) or not values:
        return None
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               for v in values):
        return None
    return math.sqrt(sum(float(v) ** 2 for v in values))


def _aig_from_orientation(beta_deg: float, gamma_deg: float) -> tuple:
    """真机自洽关系：由 deviceorientation 的 beta/gamma 反推重力方向。

    aig = (−g·cosβ·sinγ, g·sinβ, g·cosβ·cosγ)，与注入层的姿态生成同源。
    """
    beta = math.radians(float(beta_deg))
    gamma = math.radians(float(gamma_deg))
    return (-9.80665 * math.cos(beta) * math.sin(gamma),
            9.80665 * math.sin(beta),
            9.80665 * math.cos(beta) * math.cos(gamma))


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
    browser = (sys.argv[3] if len(sys.argv) > 3 else None)
    if target_os not in ("android", "ios") or (
            browser is not None
            and browser not in (("chrome",) if target_os == "android" else ("safari", "chrome_ios"))):
        print("用法: mobile_launch_verify.py [android|ios] [generate|preset] [safari|chrome_ios]")
        return 2

    ok, msg = is_available()
    if not ok:
        print("❌ fp-chromium 内核未安装：", msg)
        print("   安装：bash scripts/install_fpchromium.sh")
        return 1

    ensure_dirs()
    db.init_db()

    fp = create_fingerprint(target_os, mode, browser=browser)
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
    event_state: dict = {}
    sensor_state: dict = {}

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

        # ---- 输入事件层：触摸派发（真机路径）/ 鼠标派发（应答守卫）----
        event_state = await input_event_probe(page)
        print("\n== 输入事件层（触摸派发 / 鼠标派发）==")
        print(json.dumps(event_state, ensure_ascii=False))

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

            # ---- WebGPU 适配器信息（安全上下文；Android 按 Dawn 代际口径）----
            webgpu_state = await page.evaluate(WEBGPU_JS) or {}
            print("\n== WebGPU 适配器信息 ==")
            print(json.dumps(webgpu_state, ensure_ascii=False))

            # ---- 媒体设备枚举（真机 Android 前后双摄形态）----
            media_state = await page.evaluate(MEDIA_JS) or {}
            print("\n== 媒体设备枚举 ==")
            print(json.dumps(media_state, ensure_ascii=False))

            # ---- 传感器面：必须在信任来源（127.0.0.1/localhost）上测量。
            #      真机 Chrome 在非安全上下文同样不派发传感器事件（probe.test
            #      不可用），此处页面已在 127.0.0.1 的本地服务上。----
            sensor_state = await sensor_probe(page)
            print("\n== 传感器面（devicemotion / deviceorientation）==")
            print(json.dumps(sensor_state, ensure_ascii=False))
        finally:
            oopif_server.shutdown()
    finally:
        await pw.stop()
        await manager.stop(profile["id"])

    gl = state.get("webgl") or {}
    is_android = target_os == "android"
    ev_touch = [str(e) for e in (event_state.get("touch") or [])]
    ev_mouse = [str(e) for e in (event_state.get("mouse") or [])]

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
        # ---- 输入事件层（行为型风控）：触摸派发 = 移动端自动化的真机路径 ----
        "事件层：触摸派发 → touchstart/touchend": (
            "touchstart" in ev_touch and "touchend" in ev_touch),
        "事件层：触摸派发 → pointerdown:touch": "pointerdown:touch" in ev_touch,
        "事件层：触摸派发 → 兼容鼠标事件 + click:touch": (
            "mousedown" in ev_touch and "mouseup" in ev_touch
            and any(s.startswith("click") for s in ev_touch)),
        "事件层：鼠标点击应答正常（未启用 emit-touch 转换）": (
            event_state.get("mouse_ok") is True
            and any(s.split(":", 1)[0] == "mousedown" for s in ev_mouse)),
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
        "竞态：首文档 UA 为移动端": probe.get("ua") == nav["userAgent"],
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
    # ---- WebGPU：适配器信息（安全上下文实测；口径与注入规格同源，Dawn 代际级）----
    wg = webgpu_state if isinstance(webgpu_state, dict) else {}
    _wg_expect = (derive_spec(fpd, target_os).get("webgpu") or {}) if is_android else {}
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

    # ---- 传感器面：数值流 / 自洽性 / WebKit 权限面 ----
    sensor = sensor_state if isinstance(sensor_state, dict) else {}
    sensor_counts = sensor.get("counts") or {}
    last_motion = sensor.get("lastMotion") or {}
    last_do = sensor.get("lastOrientation") or {}
    last_doa = sensor.get("lastOrientationAbsolute") or {}
    rp = sensor.get("requestPermission") or {}
    rp_no_gesture = sensor.get("noGesture") or {}
    rp_gesture = sensor.get("gesture") or {}
    aig_mag = _sensor_magnitude(last_motion.get("aig"))
    lin_mag = _sensor_magnitude(last_motion.get("acc"))
    gyro_mag = _sensor_magnitude(last_motion.get("rr"))
    gravity_delta = None
    if (isinstance(last_do.get("b"), (int, float))
            and isinstance(last_do.get("g"), (int, float))
            and isinstance(last_motion.get("aig"), list) and aig_mag is not None):
        expected = _aig_from_orientation(last_do["b"], last_do["g"])
        gravity_delta = max(abs(expected[i] - float(last_motion["aig"][i]))
                            for i in range(3))

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
            "plugins/mimeTypes 为空（Android Chrome 无内置 PDF 插件）": (
                state["pluginsLen"] == 0 and state["mimeTypesLen"] == 0),
            "pdfViewerEnabled == false（Android 构建口径）": state["pdfViewerValue"] is False,
            "WebHID 不存在（Android Chrome 未实现）": state["hasHid"] is False,
            "WebUSB 存在（Chrome Android 148 支持）": state["hasUsb"] is True,
            "Web Serial 存在（Chrome Android 148 支持）": state["hasSerial"] is True,
            "Web Bluetooth 存在（Chrome Android 支持）": state["hasBluetooth"] is True,
            "Web MIDI 存在（Chrome Android 支持）": state["hasMidiAccess"] is True,
            "performance.memory 存在（Chrome 非标准扩展，Android 亦有）": (
                state["hasPerfMemory"] is True),
            "无 GestureEvent/ongesturestart（Chromium 无 WebKit 手势面）": (
                state["hasGestureEvent"] is False and state["hasGestureHandler"] is False),
            "connection 存在（Chrome Android 特性）": state["hasConnection"] is True,
            "connection.type == 'wifi'（仅 Android 实现；桌面内核无该属性）": (
                state["connTypeIn"] is True and state["connType"] == "wifi"),
            "Worker（页面）：connection.type == 'wifi'": wk_main.get("connType") == "wifi",
            "Worker（跨域 iframe）：connection.type == 'wifi'": fr_worker.get("connType") == "wifi",
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
            # ---- WebGPU：适配器信息（口径与注入规格同源；非宿主 apple）----
            "WebGPU：适配器 vendor == 规格（非宿主 apple）": (
                wg.get("hasGpu") is True and wg.get("vendor") == _wg_expect.get("vendor")),
            "WebGPU：architecture == Dawn 代际口径（跨 API 一致）": (
                wg.get("architecture") == _wg_expect.get("architecture")),
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
            # ---- 传感器面：真机 Android 持续数值事件流（桌面内核只有 null 哨兵）----
            "传感器：devicemotion 连续数值流（60Hz 级）": (
                sensor_counts.get("motionNumeric", 0) >= 30),
            "传感器：无全 null 哨兵事件（桌面内核无传感器特征）": (
                sensor_counts.get("motion", 0) == sensor_counts.get("motionNumeric", 0)),
            "传感器：事件可信（isTrusted）且 interval 合理": (
                last_motion.get("trusted") is True
                and 0 < (last_motion.get("interval") or 0) <= 100),
            "传感器：重力矢量量级 ≈ 9.8 m/s²（手持姿态）": (
                aig_mag is not None and 9.2 <= aig_mag <= 10.1),
            "传感器：线性加速度量级合理（去重力 |a| ≤ 2）": (
                lin_mag is not None and lin_mag <= 2.0),
            "传感器：角速度近似静止（|ω| ≤ 5 deg/s）": (
                gyro_mag is not None and gyro_mag <= 5.0),
            "传感器：重力矢量与 deviceorientation 自洽（交叉校验）": (
                gravity_delta is not None and gravity_delta <= 0.35),
            "传感器：deviceorientation 事件流且 absolute == false": (
                sensor_counts.get("orientationNumeric", 0) >= 10
                and last_do.get("abs") is False),
            "传感器：deviceorientationabsolute 事件流且 absolute == true": (
                sensor_counts.get("orientationAbsoluteNumeric", 0) >= 10
                and last_doa.get("abs") is True),
            "传感器：无 requestPermission（Chromium 152 才实现，148 无）": (
                rp.get("motion") == "undefined" and rp.get("orientation") == "undefined"),
            "传感器：跨域 iframe（OOPIF，已委托 allow）内 devicemotion 同样有流": (
                int(fr.get("dm") or 0) >= 20),
        })
    else:
        is_ios_chrome = extra.get("browser") == "chrome_ios"
        checks.update({
            "UA-CH 不存在（Safari/CriOS 均无该 API）": state["uaData"] == "ABSENT",
            "getBattery 不存在（WKWebView 无该 API）": state["hasGetBattery"] is False,
            "deviceMemory 不暴露（WKWebView 无该 API）": state["deviceMemory"] is None,
            "connection 不存在（Safari/CriOS 均无该 API）": state["hasConnection"] is False,
            "Worker（页面）：无 connection.type（WKWebView 行为）": wk_main.get("connType") == "<none>",
            "pdfViewerEnabled 不存在（Safari/CriOS 均无该 API）": state["hasPdfViewer"] is False,
            "plugins/mimeTypes 为空（WKWebView 行为）": state["pluginsLen"] == 0 and state["mimeTypesLen"] == 0,
            # ---- 桌面 Chromium 独有 API 的抹除（一条 `'serial' in navigator` 即可判假）----
            "Web Serial/USB/WebHID 均不存在（Safari 未实现）": (
                state["hasSerial"] is False and state["hasUsb"] is False
                and state["hasHid"] is False),
            "Web Bluetooth 不存在（Safari 未实现）": state["hasBluetooth"] is False,
            "Keyboard 不存在（Safari 未实现）": state["hasKeyboard"] is False,
            "Web MIDI 不存在（Safari 未实现）": state["hasMidiAccess"] is False,
            "Presentation 不存在（Safari 未实现）": state["hasPresentation"] is False,
            "getInstalledRelatedApps 不存在（Safari 未实现）": state["hasRelatedApps"] is False,
            "navigator.vibrate 不存在（Safari 未实现）": state["hasVibrate"] is False,
            "showOpenFilePicker 不存在（Safari 未实现）": state["hasOpenFilePicker"] is False,
            "performance.memory 不存在（WebKit 未实现 MemoryInfo）": state["hasPerfMemory"] is False,
            # ---- iOS WebKit 专有手势事件家族（Chromium 装 iOS 的最快反证）----
            "GestureEvent 构造器存在（iOS WebKit 专有）": state["hasGestureEvent"] is True,
            "ongesturestart 处理属性存在": state["hasGestureHandler"] is True,
            "GestureEvent 原型链 == UIEvent": state["gestureProtoOk"] is True,
            "无 Sec-CH-UA 请求头（WKWebView 不发）": not any(k.startswith("sec-ch") for k in seen_headers),
            "请求头 User-Agent == 指纹 UA": seen_headers.get("user-agent") == nav["userAgent"],
            "竞态：首文档无 UA-CH（WKWebView 行为）": probe.get("hasUaData") is False,
            "跨域 iframe：UA-CH 不存在（WKWebView 行为）": fr.get("uaP") is None,
            "Worker（页面）：UA-CH 不存在（WKWebView 行为）": wk_main.get("uaP") is None,
            "Worker（页面）：deviceMemory 不暴露（WKWebView 行为）": wk_main.get("devMem") is None,
            "Worker（跨域 iframe）：UA-CH 不存在（WKWebView 行为）": fr_worker.get("uaP") is None,
            # ---- Accept-CH：无 UA-CH，声明后也没有任何 Sec-CH-UA* 头 ----
            "Accept-CH：声明前后均无 UA-CH 头": (
                not any(k.startswith("sec-ch") for k in ac_first)
                and not any(k.startswith("sec-ch") for k in ac_second)),
            # ---- WebGPU：iOS 保持宿主 Apple 适配器（真机 iPhone 同为 Apple GPU）----
            "WebGPU：iOS 适配器 vendor == apple（真机一致）": (
                wg.get("hasGpu") is True and wg.get("vendor") == "apple"),
            # ---- iOS 两口径通用：普通页面无 window.chrome（报告 §4.4 / B1-5）----
            "window.chrome 为 undefined（iOS 普通页面无该对象）": state["hasChromeObj"] is False,
            # ---- 传感器权限面（WebKit 专有）：requestPermission 与授权语义 ----
            "传感器：requestPermission 存在（iOS 14.5+ 专有静态方法）": (
                rp.get("motion") == "function" and rp.get("orientation") == "function"),
            "传感器：无手势调用 → NotAllowedError（WebKit 语义）": (
                rp_no_gesture.get("state") == "rejected:NotAllowedError"
                and (rp_no_gesture.get("activation") or {}).get("active") is False),
            "传感器：手势内调用保持 pending（不伪造授权结果）": (
                rp_gesture.get("state") == "pending"
                and (rp_gesture.get("activation") or {}).get("active") is True),
            "传感器：DeviceOrientationEvent 无 absolute（iOS Safari 口径）": (
                sensor.get("orientationAbsoluteInProto") is False),
            "传感器：无 ondeviceorientationabsolute（WebKit 无该事件）": (
                sensor.get("onDeviceOrientationAbsolute") is False),
            "传感器：未授权不派发数值事件（真机零事件口径）": (
                sensor_counts.get("motionNumeric", 0) == 0
                and sensor_counts.get("orientationNumeric", 0) == 0
                and sensor_counts.get("orientationAbsoluteNumeric", 0) == 0),
        })
        if is_ios_chrome:
            checks.update({
                # ---- CriOS 检测面（补丁方案 §2.3 断言组）----
                "CriOS：UA 含 CriOS/<version>": state["isCriOS"] is True,
                "CriOS：UA 版本 == 指纹 browserVersion": (
                    f"CriOS/{str(extra.get('browserVersion') or '').split('.')[0]}.0.0.0"
                    in nav["userAgent"]),
                "CriOS：window.__gCrWeb 存在": state["hasGCrWeb"] is True,
                "CriOS：__gCrWeb.windowId 非空": bool(state["gCrWebWindowId"]),
                "CriOS：__gCrWeb 成员族齐全": (
                    {"message", "autofill", "suggestion", "contextualSearch",
                     "languageDetection", "translate", "findInPage", "plugin",
                     "findPasswordForms", "dispatchPopstateEvent", "replaceWebViewURL"}
                    <= set(state["gCrWebKeys"] or [])),
                "CriOS：window.webkit.messageHandlers 存在": state["hasWK"] is True,
                "CriOS：messageHandlers 含已确认通道": (
                    {"POSTSuccessHandler", "POSTErrorHandler"}
                    <= set(state["wkHandlers"] or [])),
                "CriOS：history.pushState toString 含 invokeOnHost": state["historyPatched"] is True,
                "CriOS：print toString 含 invokeOnHost": state["printPatched"] is True,
                "CriOS：scrollTo toString 含 setWebViewScrollViewIsDragging": (
                    state["scrollPatched"] is True),
                "CriOS：vendor == Apple Computer, Inc.": (
                    state["vendor"] == "Apple Computer, Inc."),
                # 文档起点：init 脚本先于页面脚本，竞态下也必须已补齐
                "竞态：首文档 __gCrWeb 已存在": probe.get("gcr") is True,
                "竞态：首文档 __gCrWeb.windowId 非空": bool(probe.get("gcrWindowId")),
                "竞态：首文档 webkit.messageHandlers 已存在": probe.get("wk") is True,
                "竞态：首文档 history 改写已生效": probe.get("hist") is True,
                "竞态：首文档 scrollTo 改写已生效": probe.get("scroll") is True,
                "竞态：首文档无 window.chrome": probe.get("chromeType") == "undefined",
                # 子帧（OOPIF）随 init 脚本同一份补丁：检测面不应只在顶层成立
                "跨域 iframe：CriOS 检测面一致": (
                    fr.get("gcr") is True and fr.get("wk") is True
                    and fr.get("hist") is True and fr.get("chromeType") == "undefined"),
                # 二次客户端（Playwright 自建 OOPIF 会话）实时读取同一补丁
                "跨域 iframe：二次客户端 CriOS 检测面一致": (
                    fr_live.get("gcr") is True and fr_live.get("wk") is True
                    and fr_live.get("hist") is True
                    and fr_live.get("chromeType") == "undefined"),
            })
        else:
            checks.update({
                "Safari：UA 不含 CriOS": state["isCriOS"] is False,
                "Safari：无 __gCrWeb（真机 Safari 无该对象）": state["hasGCrWeb"] is False,
                "Safari：无 window.webkit.messageHandlers": state["hasWK"] is False,
                "Safari：history/print/scrollTo 保持原生外观": (
                    state["historyPatched"] is False
                    and state["printPatched"] is False
                    and state["scrollPatched"] is False),
                "Safari：standalone 存在且为 false（iOS 专有属性）": (
                    state["hasStandalone"] is True and state["standalone"] is False),
                "跨域 iframe：无 CriOS 检测面（Safari 口径）": (
                    fr.get("gcr") is False and fr.get("wk") is False
                    and fr.get("hist") is False and fr.get("chromeType") == "undefined"),
                "跨域 iframe：二次客户端无 CriOS 检测面": (
                    fr_live.get("gcr") is False and fr_live.get("wk") is False
                    and fr_live.get("hist") is False
                    and fr_live.get("chromeType") == "undefined"),
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
