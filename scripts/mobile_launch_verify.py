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
  8. iOS Safari：navigator.userAgentData / connection 不存在，
     plugins/mimeTypes 为空、pdfViewerEnabled 为 true（Safari 16.4+），navigator.standalone 存在且为 false，
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
from app.mobile_cdp_injector import (
    _parse_ios_version,
    derive_spec,
    sec_ch_headers,
)
from app.mobile_fingerprints import _ios_av1_capable, _IOS_WORKER_NAV_PROTO

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
        intlLocale: Intl.DateTimeFormat().resolvedOptions().locale,
        // ---- 伪造接收者防护 ----
        // 原生 DOM getter 由 C++ 实现，会校验接收者内部槽：在 Object.create(实例)
        // 上调用必须抛 TypeError: Illegal invocation。JS 模拟 getter 若忽略 this
        // 直接返回闭包值就被一眼看穿（检测报告「人工 JS 模拟对象」）。
        brandGuard: (function () {
            var bad = Object.create(navigator);
            var res = {};
            var names = ['userAgent', 'appVersion', 'platform', 'vendor',
                         'maxTouchPoints', 'deviceMemory', 'plugins', 'mimeTypes',
                         'pdfViewerEnabled'];
            for (var i = 0; i < names.length; i++) {
                var d = descOf(Navigator.prototype, names[i]);
                if (!d || typeof d.get !== 'function') { continue; }
                try { res[names[i]] = 'LEAK:' + String(d.get.call(bad)).slice(0, 20); }
                catch (e) { res[names[i]] = e.name + ':' + e.message; }
            }
            ['width', 'height', 'colorDepth'].forEach(function (p) {
                var d = descOf(Screen.prototype, p);
                if (!d || typeof d.get !== 'function') { return; }
                try { res['screen.' + p] = 'LEAK:' + String(d.get.call(Object.create(screen))).slice(0, 20); }
                catch (e) { res['screen.' + p] = e.name + ':' + e.message; }
            });
            ['devicePixelRatio', 'outerWidth', 'outerHeight'].forEach(function (p) {
                var d = Object.getOwnPropertyDescriptor(window, p);
                if (!d || typeof d.get !== 'function') { return; }
                try { res['window.' + p] = 'LEAK:' + String(d.get.call(Object.create(window))).slice(0, 20); }
                catch (e) { res['window.' + p] = e.name + ':' + e.message; }
            });
            // 方法同样校验接收者：原生 m.call({}) 亦抛 Illegal invocation。
            // 例外见 brandLenient（Blink 有少数方法不校验接收者，加校验反而出错）。
            [['Navigator.prototype', typeof Navigator !== 'undefined' ? Navigator.prototype : null, 'getBattery'],
             ['Document.prototype', typeof Document !== 'undefined' ? Document.prototype : null, 'createEvent'],
             ['SpeechSynthesis.prototype',
              typeof SpeechSynthesis !== 'undefined' ? SpeechSynthesis.prototype : null, 'getVoices']
            ].forEach(function (t) {
                var fn = t[1] && t[1][t[2]];
                if (typeof fn !== 'function') { return; }
                try { res['fn.' + t[2]] = 'LEAK'; fn.call(Object.create({}), 'x'); }
                catch (e) { res['fn.' + t[2]] = e.name + ':' + e.message; }
            });
            return res;
        })(),
        // 原生不校验接收者的方法：必须保持同样的「宽松」行为
        brandLenient: (function () {
            var P = typeof MediaDevices !== 'undefined' ? MediaDevices.prototype : null;
            var fn = P && P.enumerateDevices;
            if (typeof fn !== 'function') { return { enumerateDevices: 'absent' }; }
            try { fn.call(Object.create({})); return { enumerateDevices: 'lenient' }; }
            catch (e) { return { enumerateDevices: e.name + ':' + e.message }; }
        })(),
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
        // 只具名 getter 语义：通道名可读不可枚举（supportedPropertyNames 为空）
        wkHandlerOwn: window.webkit && window.webkit.messageHandlers
            ? Object.getOwnPropertyNames(window.webkit.messageHandlers) : [],
        wkHandlerIn: !!(window.webkit && window.webkit.messageHandlers
            && ('crwebinvoke' in window.webkit.messageHandlers)),
        wkHandlerUnknown: (function () {
            var mh = window.webkit && window.webkit.messageHandlers;
            if (!mh) { return null; }
            try { return typeof mh.__no_such_handler__; } catch (e) { return 'throw:' + e.name; }
        })(),
        wkHandlerDesc: (function () {
            var mh = window.webkit && window.webkit.messageHandlers;
            if (!mh) { return null; }
            var d = Object.getOwnPropertyDescriptor(mh, 'crwebinvoke');
            return d ? [d.writable, d.enumerable, d.configurable] : null;
        })(),
        wkBuffersTag: (window.webkit && window.webkit.buffers)
            ? Object.prototype.toString.call(window.webkit.buffers) : null,
        wkBuffersCtorName: (function () {
            var b = window.webkit && window.webkit.buffers;
            if (!b) { return null; }
            var p = Object.getPrototypeOf(b);
            return (p && p.constructor) ? p.constructor.name : null;
        })(),
        wkBuffersGlobal: typeof window.WebKitBufferNamespace,
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
        // F-87：无手柄时真机 WebKit 返回 []（length 0），Blink 固定 4 槽位。
        gamepads: (function () {
            try {
                var g = nav.getGamepads();
                return [Array.isArray(g), g.length,
                        Array.prototype.map.call(g, function (x) {
                            return x === null ? 'null' : typeof x; })[0] || null,
                        nav.getGamepads.length];
            } catch (e) { return 'err'; }
        })(),
        hasVibrate: typeof nav.vibrate === 'function',
        // F-92：iOS 无 Gamepad 震动扩展（WebKit 开关默认 false）
        gamepadVibActuator: (typeof Gamepad === 'undefined')
            ? false : ('vibrationActuator' in Gamepad.prototype),
        hasOpenFilePicker: typeof window.showOpenFilePicker === 'function',
        hasPerfMemory: typeof performance.memory !== 'undefined',
        // ---- F-04 / F-05 / F-08 / F-09 / F-10：桌面 Chromium 残留面 ----
        hasDoNotTrack: 'doNotTrack' in nav,
        hasScheduling: 'scheduling' in nav,
        hasStorageBuckets: 'storageBuckets' in nav,
        hasWco: 'windowControlsOverlay' in nav,
        // 反向保留：iOS 真机存在的成员不得误删
        // F-90：setAppBadge/clearAppBadge 由 AppBadgeEnabled 门控，iOS 默认关，
        // 普通浏览上下文缺席（Android 侧 setAppBadge 亦缺席，见 F-78）。
        hasSetAppBadge: typeof nav.setAppBadge === 'function',
        hasClearAppBadge: typeof nav.clearAppBadge === 'function',
        hasDevicePosture: 'devicePosture' in nav,
        gExternal: typeof window.External,
        gInputDeviceCaps: typeof window.InputDeviceCapabilities,
        gStyleMedia: typeof window.styleMedia,
        gCrashReport: typeof window.crashReport,
        gViewport: typeof window.viewport,
        gCredentialless: typeof window.credentialless,
        gUAData: typeof window.NavigatorUAData,
        gNetworkInfo: typeof window.NetworkInformation,
        gBackgroundFetch: typeof window.BackgroundFetchManager,
        gSyncManager: typeof window.SyncManager,
        gBeforeInstall: typeof window.BeforeInstallPromptEvent,
        gWco: typeof window.WindowControlsOverlay,
        gIdleCb: typeof window.requestIdleCallback,
        gEditContext: typeof window.EditContext,
        gSanitizer: typeof window.Sanitizer,
        gLaunchQueue: typeof window.LaunchQueue,
        gSuppressedError: typeof window.SuppressedError,
        gDisposableStack: typeof window.DisposableStack,
        gWebSocketError: typeof window.WebSocketError,
        gWebkitMediaStream: typeof window.webkitMediaStream,
        gWebkitRTCPeer: typeof window.webkitRTCPeerConnection,
        gWebkitSpeechGrammar: typeof window.webkitSpeechGrammar,
        // 反向保留：iOS 真机支持的接口对象
        gBarcodeDetector: typeof window.BarcodeDetector,
        gPaymentRequest: typeof window.PaymentRequest,
        gSpeechRecognition: typeof window.SpeechRecognition,
        gTrackProcessor: typeof window.MediaStreamTrackProcessor,
        gDigitalCredential: typeof window.DigitalCredential,
        gDevicePostureCtor: typeof window.DevicePosture,
        // F-82：WebGPU 整族按 iOS 版本门控（BCD safari_ios=26）。抽样三类：
        // navigator 成员、接口对象、常量命名空间；再给一个整族在册计数。
        hasNavigatorGpu: 'gpu' in nav,
        gGPU: typeof window.GPU,
        gGPUAdapter: typeof window.GPUAdapter,
        gGPUCanvasContext: typeof window.GPUCanvasContext,
        gGPUBufferUsage: typeof window.GPUBufferUsage,
        gpuFamilyCount: (function () {
            var ns = ["GPU", "GPUAdapter", "GPUAdapterInfo", "GPUBindGroup",
                "GPUBindGroupLayout", "GPUBuffer", "GPUBufferUsage",
                "GPUCanvasContext", "GPUColorWrite", "GPUCommandBuffer",
                "GPUCommandEncoder", "GPUCompilationInfo", "GPUCompilationMessage",
                "GPUComputePassEncoder", "GPUComputePipeline", "GPUDevice",
                "GPUDeviceLostInfo", "GPUExternalTexture", "GPUInternalError",
                "GPUMapMode", "GPUOutOfMemoryError", "GPUPipelineError",
                "GPUPipelineLayout", "GPUQuerySet", "GPUQueue", "GPURenderBundle",
                "GPURenderBundleEncoder", "GPURenderPassEncoder", "GPURenderPipeline",
                "GPUSampler", "GPUShaderModule", "GPUShaderStage",
                "GPUSupportedFeatures", "GPUSupportedLimits", "GPUTexture",
                "GPUTextureUsage", "GPUTextureView", "GPUUncapturedErrorEvent",
                "GPUValidationError", "WGSLLanguageFeatures"];
            var n = 0;
            for (var i = 0; i < ns.length; i++) { if (typeof window[ns[i]] !== 'undefined') { n++; } }
            return n;
        })(),
        // F-85 余量族（WebTransport / Trusted Types / Navigation / WebCodecs
        // 音频侧 / CSS Position-Try / Streams BYOB / 单丁）——全部 BCD 26.x。
        gated26: (function () {
            var ns = ["WebTransport", "WebTransportBidirectionalStream",
                "WebTransportDatagramDuplexStream", "WebTransportError",
                "TrustedHTML", "TrustedScript", "TrustedScriptURL",
                "TrustedTypePolicy", "TrustedTypePolicyFactory", "trustedTypes",
                "Navigation", "NavigateEvent", "NavigationActivation",
                "NavigationCurrentEntryChangeEvent", "NavigationDestination",
                "NavigationHistoryEntry", "NavigationTransition", "navigation",
                "AudioData", "AudioDecoder", "AudioEncoder", "EncodedAudioChunk",
                "CSSPositionTryDescriptors", "CSSPositionTryRule",
                "ReadableByteStreamController", "ReadableStreamBYOBReader",
                "ReadableStreamBYOBRequest", "CaretPosition", "CommandEvent",
                "EventCounts", "FileSystemWritableFileStream",
                "LargestContentfulPaint", "Origin", "PerformanceEventTiming",
                "ScrollTimeline", "URLPattern", "ViewTimeline"];
            var out = {};
            for (var i = 0; i < ns.length; i++) {
                out[ns[i]] = typeof window[ns[i]];
            }
            return out;
        })(),
        // ---- F-40 / F-49 / F-51 / F-52 / F-55 / F-56：对象子属性与返回值面 ----
        mdGdm: typeof (navigator.mediaDevices || {}).getDisplayMedia,
        paintWorklet: typeof (window.CSS && CSS.paintWorklet),
        selectShowPicker: typeof HTMLSelectElement.prototype.showPicker,
        inputShowPicker: typeof HTMLInputElement.prototype.showPicker,
        canvasWebp: (function () {
            try {
                var c = document.createElement('canvas');
                c.width = 8; c.height = 8;
                return c.toDataURL('image/webp').slice(0, 16);
            } catch (e) { return 'err'; }
        })(),
        canvasAvif: (function () {
            try {
                var c = document.createElement('canvas');
                c.width = 8; c.height = 8;
                return c.toDataURL('image/avif').slice(0, 16);
            } catch (e) { return 'err'; }
        })(),
        mrWebm: MediaRecorder.isTypeSupported('video/webm'),
        mrVp8: MediaRecorder.isTypeSupported('video/webm;codecs=vp8'),
        mrVp9Opus: MediaRecorder.isTypeSupported('video/webm;codecs=vp9,opus'),
        mrWebmH264: MediaRecorder.isTypeSupported('video/webm;codecs=h264'),
        mrMp4: MediaRecorder.isTypeSupported('video/mp4'),
        ctx2dAttrs: (function () {
            try {
                var c = document.createElement('canvas');
                return Object.keys(c.getContext('2d').getContextAttributes()).sort();
            } catch (e) { return null; }
        })(),
        // ---- F-58 / F-59 / F-60 / F-61：事件 legacy 面与命令面 ----
        uiEventProto: Object.getOwnPropertyNames(UIEvent.prototype).sort(),
        uiPageX: (function () {
            try { return 'pageX' in new UIEvent('x'); } catch (e) { return null; }
        })(),
        animHasAnimation: (function () {
            try { return 'animation' in new AnimationEvent('x'); } catch (e) { return null; }
        })(),
        transHasAnimation: (function () {
            try { return 'animation' in new TransitionEvent('x'); } catch (e) { return null; }
        })(),
        execCopyNoSel: (function () {
            try { return document.execCommand('copy'); } catch (e) { return null; }
        })(),
        acStateNoGesture: (function () {
            try {
                var C = window.AudioContext || window.webkitAudioContext;
                return C ? new C().state : null;
            } catch (e) { return 'err'; }
        })(),
        qcsPaste: document.queryCommandSupported('paste'),
        // ---- F-43：zh/ja 排版口径（星期空格 / 时区名空格 / dayPeriod）----
        intlZh: (function () {
            try {
                var tz = 'America/Los_Angeles';
                // 固定 UTC 时刻：断言不随宿主时区漂移
                var d = new Date(Date.UTC(2026, 9, 20, 6, 5, 9)); // 23:05 LA
                var mk = function (loc, o) {
                    return new Intl.DateTimeFormat(loc,
                        Object.assign({ timeZone: tz }, o));
                };
                var full = mk('zh-CN', { dateStyle: 'full', timeStyle: 'full' });
                var dateFull = mk('zh-CN', { dateStyle: 'full' });
                var timeFull = mk('zh-CN', { timeStyle: 'full' });
                var h12 = mk('zh-CN', { dateStyle: 'full', timeStyle: 'full',
                                        hour12: true });
                var out = {
                    full: full.format(d),
                    dateFull: dateFull.format(d),
                    timeFull: timeFull.format(d),
                    h12: h12.format(d),
                    tls: d.toLocaleString('zh-CN',
                        { timeZone: tz, dateStyle: 'full', timeStyle: 'full' }),
                    join: full.format(d) === full.formatToParts(d)
                        .map(function (p) { return p.value; }).join(''),
                    tldErr: (function () {
                        try {
                            d.toLocaleDateString('zh-CN',
                                { timeZone: tz, timeStyle: 'full' });
                            return false;
                        } catch (e) { return e instanceof TypeError; }
                    })(),
                    fmtGetter: typeof Object.getOwnPropertyDescriptor(
                        Intl.DateTimeFormat.prototype, 'format').get,
                    fmtIsFn: typeof full.format,
                    fmtLen: full.format.length,
                };
                // 各 locale 的星期空格口径（真机 WebKit）
                out.locales = {};
                ['zh-CN', 'zh-Hans-CN', 'zh-SG', 'zh-HK', 'zh-MO', 'zh-TW',
                 'zh-Hant', 'zh-Hant-HK', 'ja-JP'].forEach(function (loc) {
                    out.locales[loc] = {
                        dateFull: mk(loc, { dateStyle: 'full' }).format(d),
                        exp: mk(loc, { year: 'numeric', month: 'long',
                                       day: 'numeric', weekday: 'long' })
                            .format(d),
                        dy: mk(loc, { day: 'numeric', weekday: 'long' })
                            .format(d),
                        narrow: mk(loc, { year: 'numeric', month: 'long',
                                          day: 'numeric', weekday: 'narrow' })
                            .format(d),
                        // 时区名位置：显式 timeZoneName 时真机把名字提到
                        // 时刻之前（港澳台繁），dateStyle 路径只有台繁提
                        tzExp: mk(loc, { hour: 'numeric', minute: '2-digit',
                                         timeZoneName: 'short' }).format(d),
                        tzStyle: mk(loc, { dateStyle: 'full',
                                           timeStyle: 'full' }).format(d),
                    };
                });
                // 日文：带分钟时星期前置，12 小时制里 12 时写作「0 时」
                out.ja = {
                    reorder: mk('ja-JP', { weekday: 'long', hour: 'numeric',
                                           minute: '2-digit' }).format(d),
                    zero: mk('ja-JP', { hour: 'numeric', hour12: true })
                        .format(new Date(Date.UTC(2026, 9, 20, 19, 5, 9))),
                };
                // 简-SG（新马）默认 12 小时制，且时区名后无空格
                out.sg = {
                    full: mk('zh-SG', { dateStyle: 'full', timeStyle: 'full' })
                        .format(d),
                    week: mk('zh-SG', { weekday: 'long', hour: 'numeric',
                                        minute: '2-digit' }).format(d),
                };
                // 逐时 dayPeriod（本地小时 → 真机分区值）
                out.dpHours = {};
                var f24 = new Intl.DateTimeFormat('en-US', {
                    timeZone: tz, hour: '2-digit', hourCycle: 'h23' });
                ['zh-CN', 'zh-TW', 'zh-HK'].forEach(function (loc) {
                    var f = mk(loc, { hour: 'numeric', minute: '2-digit',
                                      hour12: true });
                    var rows = [];
                    for (var u = 0; u < 48; u++) {
                        var dd = new Date(Date.UTC(2026, 9, 19, 0, u * 30, 9));
                        var lh = parseInt(f24.format(dd), 10);
                        if (rows[lh]) { continue; }
                        rows[lh] = f.format(dd).replace(/[0-9:]/g, '');
                    }
                    out.dpHours[loc] = rows;
                });
                // formatRange 两端各自取 dayPeriod（不含时区名：区间模板
                // 的分隔符与时区名位置是另一套 CLDR 数据，不属于本项）
                var a = new Date(Date.UTC(2026, 9, 20, 6, 5, 9));
                var b = new Date(Date.UTC(2026, 9, 20, 18, 5, 9));
                var rf = mk('zh-CN', { hour: 'numeric', minute: '2-digit',
                                       hour12: true });
                out.range = rf.formatRange(a, b);
                out.rangeJoin = out.range === rf.formatRangeToParts(a, b)
                    .map(function (p) { return p.value; }).join('');
                // 非 zh/ja 不受影响（CLDR 数据差异不属于本项）
                out.en = mk('en-US', { dateStyle: 'full', timeStyle: 'full' })
                    .format(d);
                return out;
            } catch (e) { return 'ERR:' + e; }
        })(),
        // ---- F-48：visualViewport 与 innerHeight 的关系 ----
        innerH: window.innerHeight,
        vvH: (window.visualViewport ? window.visualViewport.height : null),
        // ---- F-46 / F-47：控件 UA 样式表计算值 ----
        uaFormStyles: (function () {
            try {
                var host = document.createElement('div');
                host.style.cssText = 'position:fixed;left:-9999px;top:0';
                document.body.appendChild(host);
                var out = {};
                ['textarea', 'input', 'select', 'button'].forEach(function (t) {
                    var el = document.createElement(t);
                    if (t === 'input') { el.type = 'text'; }
                    host.appendChild(el);
                    var cs = getComputedStyle(el);
                    out[t] = {
                        fontFamily: cs.fontFamily,
                        fontSize: cs.fontSize,
                        overflow: cs.overflow,
                        userSelect: cs.getPropertyValue('-webkit-user-select')
                            || cs.userSelect,
                        height: cs.height,
                    };
                });
                out.bodyUserSelect = getComputedStyle(document.body)
                    .getPropertyValue('-webkit-user-select')
                    || getComputedStyle(document.body).userSelect;
                return out;
            } catch (e) { return null; }
        })(),
        evSearch: 'onsearch' in window,
        evContextLost: 'oncontextlost' in window,
        evContextRestored: 'oncontextrestored' in window,
        evBeforeInstall: 'onbeforeinstallprompt' in window,
        evAppInstalled: 'onappinstalled' in window,
        docWebkitHidden: 'webkitHidden' in document,
        docWebkitVisibility: 'webkitVisibilityState' in document,
        screenIsExtended: 'isExtended' in screen,
        pdfViewerValue: nav.pdfViewerEnabled,
        connTypeIn: !!(nav.connection && ('type' in nav.connection)),
        connType: (nav.connection && ('type' in nav.connection)) ? nav.connection.type : null,
        // ---- NetworkInformation 值域（P2-6）：downlinkMax 与 ontypechange ----
        connDownlinkMax: (nav.connection && ('downlinkMax' in nav.connection))
            ? nav.connection.downlinkMax : '<absent>',
        connDownlinkMaxType: (nav.connection && ('downlinkMax' in nav.connection))
            ? typeof nav.connection.downlinkMax : '<absent>',
        connOnTypeIn: !!(nav.connection && ('ontypechange' in nav.connection)),
        connOnTypeDefault: (nav.connection && ('ontypechange' in nav.connection))
            ? nav.connection.ontypechange : '<absent>',
        connOnTypeDesc: (function () {
            if (!nav.connection || !('ontypechange' in nav.connection)) { return null; }
            var d = Object.getOwnPropertyDescriptor(NetworkInformation.prototype, 'ontypechange');
            return d ? {get: !!d.get, set: !!d.set, enum: d.enumerable, cfg: d.configurable} : null;
        })(),
        connOnTypeRoundTrip: (function () {
            if (!nav.connection || !('ontypechange' in nav.connection)) { return null; }
            var fn = function () {};
            try {
                nav.connection.ontypechange = fn;
                var ok = nav.connection.ontypechange === fn;
                nav.connection.ontypechange = null;
                return ok && nav.connection.ontypechange === null;
            } catch (e) { return false; }
        })(),
        // ---- 媒体编解码能力表（P1-3）：真机 iOS 无 ogg/webm，HEVC 走 hvc1 ----
        canPlay: (function () {
            var a = document.createElement('audio');
            var v = document.createElement('video');
            var probe = function (t) {
                // 用 indexOf 代替正则：本字符串非 raw，反斜杠转义会触发
                // SyntaxWarning（JS 侧语义相同）
                try { return (t.indexOf('audio/') === 0 ? a : v).canPlayType(t); }
                catch (e) { return 'ERR'; }
            };
            return {
                ogg: probe('audio/ogg'),
                oggVorbis: probe('audio/ogg; codecs="vorbis"'),
                oggOpus: probe('audio/ogg; codecs="opus"'),
                webm: probe('video/webm'),
                webmVp8: probe('video/webm; codecs="vp8"'),
                webmVp9: probe('video/webm; codecs="vp09.00.10.08"'),
                videoOgg: probe('video/ogg'),
                hvc1: probe('video/mp4; codecs="hvc1"'),
                hvc1Full: probe('video/mp4; codecs="hvc1.1.6.L93.B0"'),
                hev1: probe('video/mp4; codecs="hev1"'),
                mp4Avc: probe('video/mp4; codecs="avc1.42E01E"'),
                upperCase: probe('AUDIO/OGG; CODECS="Vorbis"'),
                native: nativeStr(HTMLMediaElement.prototype.canPlayType),
                length: HTMLMediaElement.prototype.canPlayType.length,
                illegal: (function () {
                    try { HTMLMediaElement.prototype.canPlayType.call({}, 'audio/mp4'); return 'no-throw'; }
                    catch (e) { return e.name; }
                })(),
            };
        })(),
        // ---- F-130 / F-134：ManagedMediaSource 静态 isTypeSupported ----
        mms: (function () {
            var C = window.ManagedMediaSource;
            if (typeof C !== 'function') { return {absent: true}; }
            var d = Object.getOwnPropertyDescriptor(C, 'isTypeSupported');
            var out = {
                absent: false,
                type: typeof C.isTypeSupported,
                own: !!d,
                desc: d ? [d.enumerable, d.configurable, d.writable, typeof d.value] : null,
                length: (typeof C.isTypeSupported === 'function') ? C.isTypeSupported.length : null,
                native: (typeof C.isTypeSupported === 'function') ? nativeStr(C.isTypeSupported) : null,
                inheritsMS: Object.getPrototypeOf(C) === window.MediaSource,
            };
            if (typeof C.isTypeSupported !== 'function') { return out; }
            var probe = function (t) {
                try { return C.isTypeSupported(t); } catch (e) { return 'ERR:' + e.name; }
            };
            out.cases = {
                mp4: probe('video/mp4'),
                mp4Codecs: probe('video/mp4; codecs="avc1.42E01E"'),
                audioMp4: probe('audio/mp4'),
                mp2t: probe('video/mp2t'),
                webm: probe('video/webm'),
                webmVp9: probe('video/webm; codecs="vp9"'),
                audioWebm: probe('audio/webm'),
                ogg: probe('audio/ogg'),
                mkv: probe('video/x-matroska'),
                upper: probe('VIDEO/MP4'),
                spaces: probe('  video/mp4  '),
                // F-142 验收串（清单 §46）：MediaSource 与 ManagedMediaSource
                // 同表，逐条固定 ledger 里点名的取值。
                webmVp8: probe('video/webm;codecs=vp8'),
                webmVp9NoQ: probe('video/webm;codecs=vp9'),
                webmVp9Full: probe('video/webm;codecs="vp09.00.10.08"'),
                webmOpus: probe('audio/webm;codecs="opus"'),
                mp4Dovi: probe('video/mp4;codecs="dvh1.05.06"'),
                mp4Opus: probe('audio/mp4;codecs="opus"'),
                mp4Flac: probe('audio/mp4;codecs="flac"'),
                mp4Av1: probe('video/mp4;codecs="av01.0.05M.08"'),
            };
            try { C.isTypeSupported(); out.noArg = 'no-throw'; }
            catch (e) { out.noArg = e.name + ':' + e.message; }
            out.undef = probe(undefined);
            out.nul = probe(null);
            // 与 MediaSource 同源：同一串两面必须一致
            out.msMp4 = MediaSource.isTypeSupported('video/mp4');
            out.msWebm = MediaSource.isTypeSupported('video/webm');
            // F-142：MediaSource 是**另一个**自有静态（真机实测两者
            // 各自 length 1、函数不校验 this），必须单独包装、单独验收。
            out.msCases = {
                webmVp9: (function () {
                    try { return MediaSource.isTypeSupported('video/webm;codecs=vp9'); }
                    catch (e) { return 'ERR:' + e.name; }
                })(),
                webmVp8: MediaSource.isTypeSupported('video/webm;codecs=vp8'),
                webmOpus: MediaSource.isTypeSupported('audio/webm;codecs="opus"'),
                ogg: MediaSource.isTypeSupported('audio/ogg'),
                // F-123：MSE 的 av01 与 canPlayType 面同步跟随机型能力位
                mp4Av1: MediaSource.isTypeSupported('video/mp4;codecs="av01.0.05M.08"'),
                noArg: (function () {
                    try { MediaSource.isTypeSupported(); return 'no-throw'; }
                    catch (e) { return e.name + ':' + e.message; }
                })(),
            };
            out.msLen = MediaSource.isTypeSupported.length;
            out.msNative = nativeStr(MediaSource.isTypeSupported);
            return out;
        })(),
        // ---- WebGL 参数上限与扩展集（P2-1）：桌面专属扩展必须缺席 ----
        webglSurface: (function () {
            var o = {extCount: null, exts: null, limits: {}, getExtS3tc: null,
                     getExtCaseInsensitive: null, unknownEnum: '<n/a>',
                     getParamLength: null, badReceiver: null};
            try {
                var g = document.createElement('canvas').getContext('webgl');
                if (!g) { return o; }
                var exts = g.getSupportedExtensions() || [];
                o.extCount = exts.length;
                o.exts = exts;
                o.getExtS3tc = g.getExtension('WEBGL_compressed_texture_s3tc');
                o.getExtCaseInsensitive = g.getExtension('webgl_compressed_texture_S3TC');
                o.getExtS3tcBool = !!o.getExtS3tc;
                o.getExtCaseInsensitiveBool = !!o.getExtCaseInsensitive;
                o.getExtAstc = !!g.getExtension('WEBGL_compressed_texture_astc');
                o.getExtPvrtc = !!g.getExtension('WEBKIT_WEBGL_compressed_texture_pvrtc');
                try {
                    var pv = g.getExtension('WEBKIT_WEBGL_compressed_texture_pvrtc');
                    o.pvrtcConst = pv ? pv.COMPRESSED_RGBA_PVRTC_4BPPV1_IMG : null;
                } catch (e) { o.pvrtcConst = 'ERR'; }
                o.hasTimerQuery = exts.some(function (x) { return /disjoint_timer_query/i.test(x); });
                o.glVersion = g.getParameter(g.VERSION);
                o.glSL = g.getParameter(g.SHADING_LANGUAGE_VERSION);
                o.ctxAttrs = Object.keys(g.getContextAttributes()).sort();
                var P = function (n) {
                    try { var v = g.getParameter(g[n]); return (v && v.length !== undefined) ? Array.from(v) : v; }
                    catch (e) { return 'ERR'; }
                };
                ['MAX_TEXTURE_SIZE', 'MAX_RENDERBUFFER_SIZE', 'MAX_VIEWPORT_DIMS',
                 'MAX_VERTEX_ATTRIBS', 'ALIASED_POINT_SIZE_RANGE'].forEach(function (n) {
                    o.limits[n] = P(n);
                });
                o.unknownEnum = g.getParameter(0x1234);
                o.getParamLength = WebGLRenderingContext.prototype.getParameter.length;
                o.getExtListLength = WebGLRenderingContext.prototype.getSupportedExtensions.length;
                o.badReceiver = (function () {
                    try { WebGLRenderingContext.prototype.getParameter.call({}, 3379); return 'no-throw'; }
                    catch (e) { return e.name; }
                })();
                var g2 = document.createElement('canvas').getContext('webgl2');
                if (g2) {
                    var e2 = g2.getSupportedExtensions() || [];
                    o.gl2ExtCount = e2.length;
                    o.gl2HasS3tc = e2.some(function (x) { return /s3tc/i.test(x); });
                    o.gl2HasTimer = e2.some(function (x) { return /disjoint_timer_query/i.test(x); });
                    o.gl2Version = g2.getParameter(g2.VERSION);
                    o.gl2SL = g2.getParameter(g2.SHADING_LANGUAGE_VERSION);
                }
            } catch (e) { o.error = String(e); }
            return o;
        })(),
        // ---- iOS WebKit 专有手势事件家族（Chromium 全平台无）----
        hasGestureEvent: typeof window.GestureEvent === 'function',
        hasGestureHandler: 'ongesturestart' in window,
        gestureProtoOk: (() => {
            try {
                return typeof window.GestureEvent === 'function'
                    && Object.getPrototypeOf(window.GestureEvent.prototype) === UIEvent.prototype;
            } catch (e) { return false; }
        })(),
        // ---- 引擎面「一行判假」信号（iOS 批次：MDN BCD / WebKit 源码口径）----
        engine: {
            // 元素级全屏（iPhone 应整块缺席：标准版 + webkit 前缀版 + 事件处理属性）
            fsElementOwn: Object.getOwnPropertyNames(Element.prototype)
                .filter((n) => /fullscreen/i.test(n)),
            fsDocumentOwn: Object.getOwnPropertyNames(Document.prototype)
                .filter((n) => /fullscreen/i.test(n)),
            fsShadowOwn: (typeof ShadowRoot === 'undefined') ? null
                : Object.getOwnPropertyNames(ShadowRoot.prototype)
                    .filter((n) => /fullscreen/i.test(n)),
            fsRequestType: typeof document.documentElement.requestFullscreen,
            fsEnabledType: typeof document.fullscreenEnabled,
            fsExitType: typeof document.exitFullscreen,
            fsElementInDoc: 'fullscreenElement' in document,
            fsHandlerInDoc: 'onfullscreenchange' in document,
            // <video> 的 webkit 全屏家族（iOS 全设备有：真机唯一元素级全屏通道）
            videoEnterType: typeof document.createElement('video').webkitEnterFullscreen,
            videoEnterCapType: typeof document.createElement('video').webkitEnterFullScreen,
            videoExitType: typeof document.createElement('video').webkitExitFullscreen,
            videoSupportsType: (() => {
                const v = document.createElement('video');
                return ('webkitSupportsFullscreen' in v)
                    ? typeof v.webkitSupportsFullscreen : 'ABSENT';
            })(),
            videoSupportsNoSrc: (() => {
                const v = document.createElement('video');
                return ('webkitSupportsFullscreen' in v) ? v.webkitSupportsFullscreen : null;
            })(),
            videoSupportsWithSrc: (() => {
                const v = document.createElement('video');
                if (!('webkitSupportsFullscreen' in v)) return null;
                v.src = 'data:video/mp4;base64,AAAA';
                return v.webkitSupportsFullscreen;
            })(),
            videoDisplayingAfterEnter: (() => {
                const v = document.createElement('video');
                if (typeof v.webkitEnterFullscreen !== 'function') return null;
                v.webkitEnterFullscreen();
                return v.webkitDisplayingFullscreen;
            })(),
            videoDisplayingAfterExit: (() => {
                const v = document.createElement('video');
                if (typeof v.webkitEnterFullscreen !== 'function') return null;
                v.webkitEnterFullscreen();
                v.webkitExitFullscreen();
                return v.webkitDisplayingFullscreen;
            })(),
            // WebKit 专属 CSS 面：-webkit-touch-callout / -webkit-overflow-scrolling
            cssTouchCalloutTwoArg: CSS.supports('-webkit-touch-callout', 'none'),
            cssTouchCalloutBadValue: CSS.supports('-webkit-touch-callout', 'bogus'),
            cssTouchCalloutOneArg: CSS.supports('-webkit-touch-callout: none'),
            cssTouchCalloutParen: CSS.supports('(-webkit-touch-callout: none)'),
            cssTouchCalloutCompound: CSS.supports('(-webkit-touch-callout: none) and (display: grid)'),
            cssTouchCalloutNegInvalid: CSS.supports('not (-webkit-touch-callout: bogus)'),
            cssOscTwoArg: CSS.supports('-webkit-overflow-scrolling', 'touch'),
            cssOscOneArg: CSS.supports('-webkit-overflow-scrolling: touch'),
            cssNativeTrue: CSS.supports('display', 'grid'),
            cssNativeFalse: CSS.supports('display', 'bogusvalue'),
            // ---- F-07 / F-24：双向差集（deny 桌面独有 / allow WebKit 独有）----
            cssDenyInterpolate: CSS.supports('interpolate-size', 'allow-keywords'),
            cssDenyOverflowClip: CSS.supports('overflow-clip-margin', '4px'),
            cssDenyCornerShape: CSS.supports('corner-shape', 'superellipse(1.2)'),
            cssDenyReadingFlow: CSS.supports('reading-flow', 'grid-rows'),
            cssDenyHostContext: CSS.supports('selector(:host-context(a))'),
            cssAllowMarginTrim: CSS.supports('margin-trim', 'block'),
            cssAllowTextAutospace: CSS.supports('text-autospace', 'ideograph-alpha'),
            cssAllowBackdrop: CSS.supports('-webkit-backdrop-filter', 'blur(4px)'),
            cssAllowSticky: CSS.supports('position', '-webkit-sticky'),
            cssAllowHyphens: CSS.supports('-webkit-hyphens', 'auto'),
            cssAllowApplePay: CSS.supports('-apple-pay-button-style', 'black'),
            cssAllowMarginTrimCond: CSS.supports('(margin-trim: block)'),
            cssDenyInterpolateCond: CSS.supports('(interpolate-size: allow-keywords)'),
            // F-88：WebKit 专有属性必须校验值合法性（真机 notavalue 一律 false）
            cssBadMarginTrim: CSS.supports('margin-trim', 'notavalue'),
            cssBadTextAutospace: CSS.supports('text-autospace', 'notavalue'),
            cssBadBackdrop: CSS.supports('-webkit-backdrop-filter', 'notavalue'),
            cssBadHyphens: CSS.supports('-webkit-hyphens', 'notavalue'),
            cssBadApplePay: CSS.supports('-apple-pay-button-style', 'notavalue'),
            cssBadMarginTrimCond: CSS.supports('(margin-trim: notavalue)'),
            cssBadBackdropCond: CSS.supports('(-webkit-backdrop-filter: notavalue)'),
            // 合法值域的边界：inline 不在 margin-trim 值域、blur 必须带单位
            cssMarginTrimInline: CSS.supports('margin-trim', 'inline'),
            cssBackdropUnitless: CSS.supports('-webkit-backdrop-filter', 'blur(4)'),
            cssBackdropMulti: CSS.supports('-webkit-backdrop-filter',
                                           'blur(4px) brightness(50%)'),
            cssMarginTrimCombo: CSS.supports('margin-trim',
                                             'block-end block-start'),
            cssTextAutospaceCombo: CSS.supports('text-autospace',
                                                'ideograph-alpha ideograph-numeric'),
            // F-101：复合条件里的非前缀 allow 项也要走改写（此前只有
            // -webkit- 前缀键进 __wkPropNames，margin-trim 会落回原生 Blink）
            cssComboMarginTrim: CSS.supports(
                '(color: red) and (margin-trim: block)'),
            cssComboMarginTrimBad: CSS.supports(
                '(color: red) and (margin-trim: notavalue)'),
            cssComboTextAutospace: CSS.supports(
                '(text-autospace: ideograph-alpha) or (color: red)'),
            cssComboHyphens: CSS.supports(
                '(color: red) and (-webkit-hyphens: auto)'),
            // F-98：WebKit 专有属性在 inline 声明里的读写 / 枚举 / computed
            // 回读。真机口径（Playwright WebKit 27.2 移动上下文实测）：
            // -webkit-hyphens 的规范名是 hyphens；-apple-pay-button-style 有
            // 驼峰访问器（其余专有属性在 27.2 上已无驼峰名）；
            // position: -webkit-sticky 的 inline 保留原文、computed 归一为
            // sticky。
            cssF98: (() => {
                const el = document.createElement('div');
                el.style.setProperty('color', 'red');
                el.style.setProperty('-webkit-backdrop-filter', 'blur(4px)');
                el.style.setProperty('margin-trim', 'block');
                el.style.setProperty('-webkit-hyphens', 'auto');
                el.style.setProperty('-apple-pay-button-style', 'black');
                el.style.setProperty('margin-trim', 'notavalue');
                document.body.appendChild(el);
                const st = el.style;
                const cs = getComputedStyle(el);
                const items = [];
                for (let i = 0; i < st.length; i++) { items.push(st[i]); }
                const stick = document.createElement('div');
                stick.style.setProperty('position', '-webkit-sticky');
                document.body.appendChild(stick);
                const out = {
                    len: st.length,
                    items: items,
                    itemAt: st.item(1),
                    inlineMT: st.getPropertyValue('margin-trim'),
                    inlinePay: st.applePayButtonStyle,
                    inlineHyphens: st.getPropertyValue('-webkit-hyphens'),
                    compMT: cs.getPropertyValue('margin-trim'),
                    compPay: cs.applePayButtonStyle,
                    compHyphens: cs.getPropertyValue('-webkit-hyphens'),
                    stickyInline: stick.style.getPropertyValue('position'),
                    stickyComp: getComputedStyle(stick)
                        .getPropertyValue('position'),
                    removed: st.removeProperty('-webkit-backdrop-filter'),
                    lenAfter: st.length,
                    emptyIn: (0 in document.createElement('div').style),
                };
                el.remove();
                stick.remove();
                return out;
            })(),
            // F-99 / F-100：computed 值的序列化口径与 UA 默认值。真机口径
            // （Playwright WebKit 27.2 移动上下文实测，含 rtl / vertical-rl /
            // display:none 分支）：布局长度走 1/64px LayoutUnit（物理右侧外边距
            // 例外，走 float32）；其余长度是 float32 按 %.6f 打印后去尾零；
            // outline/border 宽度整数截断；-webkit-locale 是未加引号的 ident；
            // 表单控件的 computed line-height 是 normal、cursor 文本类 auto /
            // 按钮类 default。
            cssF99: (() => {
                const mk = (css, tag) => {
                    const e = document.createElement(tag || 'div');
                    if (css) e.setAttribute('style', css);
                    document.body.appendChild(e);
                    return e;
                };
                const d = mk('width:21.44px;height:33.3px;margin:21.44px;'
                    + 'padding:21.44px;line-height:1.7;font-size:16px;'
                    + 'border:1.5px solid;outline:2.5px solid;'
                    + 'outline-offset:1.5px');
                const c = getComputedStyle(d);
                const rtl = mk('direction:rtl;margin-inline-start:21.44px;'
                    + 'margin-inline-end:21.44px;margin-block-start:21.44px');
                const cr = getComputedStyle(rtl);
                const vert = mk('writing-mode:vertical-rl;margin:21.44px');
                const cv = getComputedStyle(vert);
                const hidden = mk('display:none;width:33.3px;margin:21.44px');
                const ch = getComputedStyle(hidden);
                const ell = mk('border-radius:10px/20px');
                const inlineEl = mk('width:33.3px;line-height:1.7;'
                    + 'margin-right:21.44px');
                const out = {
                    width: c.width,
                    height: c.height,
                    margin: c.margin,
                    marginRight: c.marginRight,
                    padding: c.padding,
                    lineHeight: c.lineHeight,
                    fontSize: c.fontSize,
                    borderWidth: c.borderTopWidth,
                    outlineWidth: c.outlineWidth,
                    outlineOffset: c.outlineOffset,
                    rtlInlineStart: cr.marginInlineStart,
                    rtlInlineEnd: cr.marginInlineEnd,
                    rtlBlockStart: cr.marginBlockStart,
                    vertBlockStart: cv.marginBlockStart,
                    vertBlockEnd: cv.marginBlockEnd,
                    hiddenWidth: ch.width,
                    hiddenMargin: ch.margin,
                    radiusSlash: getComputedStyle(ell).borderRadius,
                    inlineWidth: inlineEl.style.width,
                    inlineLineHeight: inlineEl.style.lineHeight,
                    compWidth: getComputedStyle(inlineEl).width,
                    compLineHeight: getComputedStyle(inlineEl).lineHeight,
                    locale: c.getPropertyValue('-webkit-locale'),
                    localeCamel: c.webkitLocale,
                    marginVP: c.getPropertyValue('margin-top'),
                    marginCamel: c.marginTop,
                };
                for (const t of ['checkbox', 'radio', 'range', 'color',
                                 'file', 'date']) {
                    const i = document.createElement('input');
                    i.type = t;
                    document.body.appendChild(i);
                    out['cur_' + t] = getComputedStyle(i).cursor;
                    out['lh_' + t] = getComputedStyle(i).lineHeight;
                }
                const bt = mk('', 'button');
                out.btnLH = getComputedStyle(bt).lineHeight;
                out.btnCursor = getComputedStyle(bt).cursor;
                const ta = mk('', 'textarea');
                out.taLH = getComputedStyle(ta).lineHeight;
                out.taCursor = getComputedStyle(ta).cursor;
                const sel = mk('', 'select');
                out.selLH = getComputedStyle(sel).lineHeight;
                out.selCursor = getComputedStyle(sel).cursor;
                return out;
            })(),
            tcInStyle: 'webkitTouchCallout' in document.body.style,
            oscInStyle: 'webkitOverflowScrolling' in document.body.style,
            tcDefault: document.body.style.webkitTouchCallout,
            tcRoundtrip: (() => {
                const s = document.body.style;
                const before = s.webkitTouchCallout;
                s.webkitTouchCallout = 'none';
                const mid = s.webkitTouchCallout;
                s.removeProperty('-webkit-touch-callout');
                const after = s.webkitTouchCallout;
                s.setProperty('-webkit-overflow-scrolling', 'touch');
                const osc = s.getPropertyValue('-webkit-overflow-scrolling');
                s.removeProperty('-webkit-overflow-scrolling');
                return [before, mid, after, osc];
            })(),
            tcSetterInvalidIgnored: (() => {
                const s = document.body.style;
                s.webkitTouchCallout = 'none';
                s.webkitTouchCallout = 'bogus';
                const v = s.webkitTouchCallout;
                s.removeProperty('-webkit-touch-callout');
                return v;
            })(),
            // 传感器构造器（WebKit 不可构造）与 iOS 专有罗盘属性
            dmeCtorThrows: (() => {
                try { new DeviceMotionEvent('devicemotion'); return 'ok'; }
                catch (e) { return e.name; }
            })(),
            doeCtorThrows: (() => {
                try { new DeviceOrientationEvent('deviceorientation'); return 'ok'; }
                catch (e) { return e.name; }
            })(),
            dmeProtoCtorSelf: DeviceMotionEvent.prototype.constructor === DeviceMotionEvent,
            dmeNativeStr: /\\[native code\\]/.test(String(DeviceMotionEvent)),
            compassHeadingIn: 'webkitCompassHeading' in DeviceOrientationEvent.prototype,
            compassAccuracyIn: 'webkitCompassAccuracy' in DeviceOrientationEvent.prototype,
            compassHeadingValue: (() => {
                const d = descOf(DeviceOrientationEvent.prototype, 'webkitCompassHeading');
                if (!d || !d.get) return 'ABSENT';
                try { return d.get.call(new Event('fp')); } catch (e) { return 'throw'; }
            })(),
            absoluteInProto: 'absolute' in DeviceOrientationEvent.prototype,
            initDOELen: (typeof DeviceOrientationEvent.prototype.initDeviceOrientationEvent === 'function')
                ? DeviceOrientationEvent.prototype.initDeviceOrientationEvent.length : null,
            initDMELen: (typeof DeviceMotionEvent.prototype.initDeviceMotionEvent === 'function')
                ? DeviceMotionEvent.prototype.initDeviceMotionEvent.length : null,
            // 真机闭环：createEvent → init → 回读（WebKit 支持这两条链，Chromium 不支持）
            createEventDOE: (() => {
                try {
                    const e = document.createEvent('DeviceOrientationEvent');
                    return 'ok:' + typeof e.initDeviceOrientationEvent + ':' + e.type;
                } catch (err) { return 'throw:' + err.name; }
            })(),
            createEventDME: (() => {
                try {
                    const e = document.createEvent('DeviceMotionEvent');
                    return 'ok:' + typeof e.initDeviceMotionEvent + ':' + e.type;
                } catch (err) { return 'throw:' + err.name; }
            })(),
            legacyDOEChain: (() => {
                try {
                    const e = document.createEvent('DeviceOrientationEvent');
                    if (typeof e.initDeviceOrientationEvent !== 'function') return null;
                    e.initDeviceOrientationEvent('deviceorientation', false, false, 10, 20, 30, 45, 5);
                    return [e.type, e.alpha, e.beta, e.gamma,
                            e.webkitCompassHeading, e.webkitCompassAccuracy];
                } catch (err) { return 'throw:' + err.name; }
            })(),
            legacyDMEChain: (() => {
                try {
                    const e = document.createEvent('DeviceMotionEvent');
                    if (typeof e.initDeviceMotionEvent !== 'function') return null;
                    e.initDeviceMotionEvent('devicemotion', false, false,
                        {x: 1, y: 2, z: 3}, {x: 4, y: 5, z: 6},
                        {alpha: 7, beta: 8, gamma: 9}, 16);
                    return [e.type, e.acceleration.x, e.accelerationIncludingGravity.y,
                            e.rotationRate.gamma, e.interval];
                } catch (err) { return 'throw:' + err.name; }
            })(),
            // ---- Safari 全局面（item 13：WebKitPoint / convertPoint 换算 /
            //      webkitCancelRequestAnimationFrame / WebKitNamespace 家族）----
            wkp: (() => {
                const P = window.WebKitPoint;
                if (typeof P !== 'function') return null;
                const p = new P(1.5, 2.5);
                const p0 = new P();
                const one = new P(0.1);
                const mut = new P(1, 2);
                mut.x = 0.1;
                const pd = descOf(P, 'prototype');
                const xd = descOf(P.prototype, 'x');
                const wd = descOf(window, 'WebKitPoint');
                const q = new P(NaN, Infinity);
                return {
                    len: P.length, name: P.name, native: nativeStr(P),
                    ownNames: Object.getOwnPropertyNames(P),
                    protoNames: Object.getOwnPropertyNames(P.prototype),
                    protoWritable: pd ? pd.writable : null,
                    protoCtorSelf: P.prototype.constructor === P,
                    winDesc: wd ? [wd.writable, wd.enumerable, wd.configurable,
                                   ('value' in wd) ? 'data' : 'accessor'] : null,
                    xDesc: xd ? [xd.enumerable, xd.configurable,
                                 typeof xd.get, typeof xd.set] : null,
                    vals: [p.x, p.y], defaults: [p0.x, p0.y], one: [one.x, one.y],
                    fround: one.x === Math.fround(0.1),
                    setFround: mut.x === Math.fround(0.1),
                    nanInf: [Number.isNaN(q.x), q.y === Infinity],
                    src: String(P).slice(0, 24),
                    tag: Object.prototype.toString.call(p),
                    instProto: Object.getPrototypeOf(p) === P.prototype,
                    instOwn: Object.getOwnPropertyNames(p),
                    callNoNew: (() => { try { P(1, 2); return 'ok'; }
                        catch (e) { return e.name; } })(),
                    badRecv: (() => { try { P.prototype.x; return 'ok'; }
                        catch (e) { return e.name; } })(),
                };
            })(),
            conv: (() => {
                const f1 = window.webkitConvertPointFromNodeToPage;
                const f2 = window.webkitConvertPointFromPageToNode;
                const craf = window.webkitCancelRequestAnimationFrame;
                if (typeof f1 !== 'function' || typeof f2 !== 'function') {
                    return { present: false, t1: typeof f1, t2: typeof f2,
                             crafType: typeof craf };
                }
                const holder = document.createElement('div');
                holder.style.cssText = 'position:absolute;left:37px;top:23px;'
                    + 'width:120px;height:40px;background:#eef';
                document.body.appendChild(holder);
                const inner = document.createElement('div');
                inner.style.cssText = 'position:relative;left:5px;top:7px;'
                    + 'width:20px;height:10px;background:#fdd';
                holder.appendChild(inner);
                const hidden = document.createElement('div');
                hidden.style.cssText = 'display:none';
                holder.appendChild(hidden);
                const spacer = document.createElement('div');
                spacer.style.cssText = 'height:2000px;width:1px';
                document.body.appendChild(spacer);
                const sx = window.pageXOffset || 0;
                const sy = window.pageYOffset || 0;
                const hr = holder.getBoundingClientRect();
                const ir = inner.getBoundingClientRect();
                const p0 = f1(holder, new WebKitPoint(0, 0));
                const p5 = f1(holder, new WebKitPoint(5, 40));
                const back = f2(holder, p5);
                const innerPt = f1(inner, new WebKitPoint(0, 0));
                const hiddenPt = f1(hidden, new WebKitPoint(0, 0));
                const detPt = f1(document.createElement('div'), new WebKitPoint(3, 4));
                const docPt = f1(document, new WebKitPoint(0, 0));
                window.scrollTo(0, 60);
                const sp = f1(holder, new WebKitPoint(0, 0));
                window.scrollTo(0, 0);
                const out = {
                    present: true, len1: f1.length, len2: f2.length, name1: f1.name,
                    native1: nativeStr(f1),
                    desc1: (() => { const d = descOf(window,
                        'webkitConvertPointFromNodeToPage');
                        return d ? [d.writable, d.enumerable, d.configurable] : null; })(),
                    zeroOk: Math.abs(p0.x - (hr.left + sx)) < 1e-6
                        && Math.abs(p0.y - (hr.top + sy)) < 1e-6,
                    fiveOk: Math.abs(p5.x - (p0.x + 5)) < 1e-2
                        && Math.abs(p5.y - (p0.y + 40)) < 1e-2,
                    roundtripOk: Math.abs(back.x - 5) < 1e-2
                        && Math.abs(back.y - 40) < 1e-2,
                    innerOk: Math.abs(innerPt.x - (ir.left + sx)) < 1e-6
                        && Math.abs(innerPt.y - (ir.top + sy)) < 1e-6,
                    hiddenOk: Math.abs(hiddenPt.x - p0.x) < 1e-6
                        && Math.abs(hiddenPt.y - p0.y) < 1e-6,
                    detachedOk: Math.abs(detPt.x - 3) < 1e-6
                        && Math.abs(detPt.y - 4) < 1e-6,
                    docOk: docPt !== null && Math.abs(docPt.x) <= 1
                        && Math.abs(docPt.y) <= 1,
                    scrolledStable: Math.abs(p0.y - sp.y) < 1e-2,
                    nullArgs: [f1(null, new WebKitPoint(0, 0)) === null,
                               f1(null, null) === null,
                               f1(undefined, undefined) === null],
                    undefNode: f1(undefined, new WebKitPoint(1, 1)) === null,
                    retType: (window.WebKitPoint && p0 instanceof window.WebKitPoint)
                        ? 'WebKitPoint' : 'other',
                    errNode: (() => { try { f1(5, new WebKitPoint(0, 0)); return 'ok'; }
                        catch (e) { return e.name; } })(),
                    errPoint: (() => { try { f1(holder, {}); return 'ok'; }
                        catch (e) { return e.name; } })(),
                    errOneArg: (() => { try { f1(new WebKitPoint(0, 0)); return 'ok'; }
                        catch (e) { return e.name; } })(),
                    craf: typeof craf === 'function'
                        ? [craf.length, craf.name, nativeStr(craf)] : null,
                };
                holder.parentNode.removeChild(holder);
                spacer.parentNode.removeChild(spacer);
                return out;
            })(),
            ns: (() => {
                const shape = (C) => {
                    if (typeof C !== 'function') return null;
                    const pd = descOf(C, 'prototype');
                    return {
                        len: C.length, name: C.name, native: nativeStr(C),
                        ownNames: Object.getOwnPropertyNames(C),
                        protoNames: Object.getOwnPropertyNames(C.prototype),
                        protoWritable: pd ? pd.writable : null,
                        protoCtorSelf: C.prototype.constructor === C,
                        callThrows: (() => { try { C(); return 'ok'; }
                            catch (e) { return e.name + ':' + e.message; } })(),
                        newThrows: (() => { try { new C(); return 'ok'; }
                            catch (e) { return e.name + ':' + e.message; } })(),
                    };
                };
                const wk = window.webkit;
                const h = (wk && wk.messageHandlers)
                    ? wk.messageHandlers.crwebinvoke : null;
                const wd = descOf(window, 'webkit');
                return {
                    WN: shape(window.WebKitNamespace),
                    UH: shape(window.UserMessageHandlersNamespace),
                    UM: shape(window.UserMessageHandler),
                    wkType: typeof wk,
                    wkDesc: wd ? [wd.writable, wd.enumerable, wd.configurable] : null,
                    wkProtoOk: (wk && window.WebKitNamespace)
                        ? Object.getPrototypeOf(wk) === window.WebKitNamespace.prototype
                        : null,
                    wkOwn: wk ? Object.getOwnPropertyNames(wk) : null,
                    mhProtoOk: (wk && wk.messageHandlers
                                && window.UserMessageHandlersNamespace)
                        ? Object.getPrototypeOf(wk.messageHandlers)
                            === window.UserMessageHandlersNamespace.prototype : null,
                    mhKeys: (wk && wk.messageHandlers)
                        ? Object.keys(wk.messageHandlers) : null,
                    crweb: h ? {
                        protoOk: window.UserMessageHandler
                            ? Object.getPrototypeOf(h)
                                === window.UserMessageHandler.prototype : null,
                        pmLen: h.postMessage ? h.postMessage.length : null,
                        pmNative: h.postMessage ? nativeStr(h.postMessage) : null,
                        pmRet: (() => { const p = h.postMessage(1);
                            return (p && typeof p.then === 'function')
                                ? 'promise' : typeof p; })(),
                    } : null,
                    buffersType: wk ? typeof wk.buffers : null,
                    buffersKeys: (wk && wk.buffers) ? Object.keys(wk.buffers) : null,
                    evalScriptProto: window.WebKitNamespace
                        ? ('evaluateScript' in window.WebKitNamespace.prototype) : null,
                    jsHandleProto: window.WebKitNamespace
                        ? ('createJSHandle' in window.WebKitNamespace.prototype) : null,
                    pmLegacyProto: window.UserMessageHandler
                        ? ('postLegacySynchronousMessage'
                           in window.UserMessageHandler.prototype) : null,
                    jsHandleGlobal: typeof window.WebKitJSHandle,
                    nodeSnapshotGlobal: typeof window.WebKitNodeSnapshot,
                };
            })(),
            // ---- 平台 API 差集（桌面内核会暴露、所属平台真机没有的 API）----
            // 读数 = [typeof, 名字是否仍 in 对象]；断言在下方按平台裁定。
            plat: (() => {
                const t = (obj, n) => { try { return typeof obj[n]; } catch (e) { return 'ERR'; } };
                const has = (obj, n) => { try { return n in obj; } catch (e) { return 'ERR'; } };
                const read = (obj, names) => {
                    const o = {};
                    for (const n of names) { o[n] = [t(obj, n), has(obj, n)]; }
                    return o;
                };
                return {
                    // iOS 差集清单（同时用于 Android 的「必须保留」断言）
                    win: read(window, ['Sensor', 'SensorErrorEvent', 'Accelerometer',
                        'Gyroscope', 'LinearAccelerationSensor', 'GravitySensor',
                        'AbsoluteOrientationSensor', 'RelativeOrientationSensor',
                        'EyeDropper', 'IdleDetector', 'WebSocketStream', 'ImageDecoder',
                        'FileSystemObserver', 'queryLocalFonts',
                        'documentPictureInPicture', 'launchQueue', 'Scheduler',
                        'scheduler', 'OTPCredential', 'Ink', 'MediaStreamTrackGenerator',
                        'sharedStorage', 'SharedStorage', 'fence', 'Fence',
                        'HTMLFencedFrameElement', 'showSaveFilePicker',
                        'showDirectoryPicker',
                        // F-92：入口删了但类名仍在的漏项（BCD safari_ios=false
                        // + Playwright WebKit 27.2 缺席 + 内核暴露）
                        'PresentationAvailability', 'StorageBucket',
                        // F-92：Gamepad 震动面。WebKit 的 GamepadVibrationActuatorEnabled
                        // 默认值在 iOS 恒 false（仅 PLATFORM(MAC) 有
                        // HAVE_WIDE_GAMECONTROLLER_SUPPORT），类名与
                        // Gamepad.prototype.vibrationActuator 应双双缺席。
                        'GamepadHapticActuator']),
                    doc: read(document, ['browsingTopics']),
                    nav: read(navigator, ['xr', 'virtualKeyboard', 'ink',
                        'runAdAuction', 'joinAdInterestGroup', 'leaveAdInterestGroup',
                        'deprecatedReplaceInURN', 'updateAdInterestGroups',
                        // F-83：真名是 deprecatedRunAdAuction…（此前清单里
                        // 少了一个 Ad，删除落空，18.6 上该 boolean 暴露）
                        'deprecatedRunAdAuctionEnforcesKAnonymity',
                        'protectedAudience']),
                    // 双平台真机都存在、必须保持的面（防误删）
                    kept: read(window, ['VideoDecoder', 'VideoFrame', 'AudioDecoder',
                        'EncodedAudioChunk', 'URLPattern', 'Highlight', 'BarcodeDetector',
                        'ImageCapture', 'MediaStreamTrackProcessor', 'navigation', 'GPU',
                        'FileSystemHandle', 'FileSystemFileHandle',
                        'FileSystemDirectoryHandle', 'FileSystemWritableFileStream']),
                    keptDoc: read(document, ['startViewTransition', 'hasStorageAccess',
                        'requestStorageAccess', 'fragmentDirective']),
                    keptNav: read(navigator, ['share', 'canShare', 'wakeLock', 'gpu']),
                };
            })(),
            // ---- JavaScriptCore 引擎面（iOS 目标；检测站四条独立判据）----
            // 取证：Playwright WebKit 27.2 实测 + WebKit main
            // Source/JavaScriptCore/runtime/{ErrorConstructor,ErrorPrototype}.cpp。
            // 注意 Error.captureStackTrace 不是 V8 专有（ErrorConstructor.cpp:52
            // 注册同名静态方法），检测站该文案已过时，与 SharedWorker 同类。
            jsc: (() => {
                const e = new Error('jsc');
                const dLine = descOf(e, 'line');
                const dCol = descOf(e, 'column');
                const dErrMsg = descOf(Error.prototype, 'message');
                const nativeEval = String(eval);
                const nativeAlert = String(alert);
                const myFn = function myFn() {};
                const errStack = (() => {
                    try { null.x(); return ''; } catch (x) { return String(x.stack || ''); }
                })();
                const thrownDesc = (() => {
                    try { null.x(); } catch (x) {
                        return descOf(x, 'line') ? 'own' : 'absent';
                    }
                })();
                return {
                    line: e.line, column: e.column,
                    lineDesc: dLine ? [dLine.writable, dLine.enumerable, dLine.configurable] : null,
                    colDesc: dCol ? [dCol.writable, dCol.enumerable, dCol.configurable] : null,
                    ownNames: Object.getOwnPropertyNames(e),
                    stackHead: String(e.stack || '').split('\\n')[0],
                    // JSC 栈帧 `fn@url:line:col`；V8 为 `    at fn (...)`。
                    hasV8StackTrace: /^\\s*at\\s+/m.test(String(e.stack || '')),
                    stackEngine: /@/.test(String(e.stack || '')) ? 'jsc' : 'v8',
                    // 原生函数 toString：JSC 多行缩进，V8 单行。
                    nativeEvalMultiLine: nativeEval.indexOf('\\n') !== -1,
                    nativeAlertMultiLine: nativeAlert.indexOf('\\n') !== -1,
                    nativeEval,
                    // 非原生函数不受改写影响
                    userFn: String(myFn),
                    // V8 专有：Error.prepareStackTrace 钩子（JSC 无）
                    hasPrepareStackTrace: typeof Error.prepareStackTrace !== 'undefined',
                    // JSC 专有：Error.prototype.message 是数据属性（V8 是访问器）
                    errProtoMessageIsData: !!(dErrMsg && !dErrMsg.get),
                    errProtoMessageVal: dErrMsg ? dErrMsg.value : 'ABSENT',
                    errProtoNameIsData: (() => {
                        const d = descOf(Error.prototype, 'name');
                        return !!(d && !d.get);
                    })(),
                    // 引擎内部抛出的 TypeError：V8 在 C++ 侧创建，不过 Error 陷阱
                    thrownLineDesc: thrownDesc,
                    thrownErrStack: errStack.split('\\n').slice(0, 2).join('|'),
                    // Proxy 透明性
                    instanceofOk: (e instanceof Error) && (e instanceof TypeError === false),
                    subclassOk: (() => {
                        try {
                            class Sub extends Error {}
                            const s = new Sub('x');
                            return [s instanceof Sub, s instanceof Error, s.name];
                        } catch (x) { return 'throw:' + x.name; }
                    })(),
                    errName: Error.name, errLen: Error.length,
                    errNameDesc: (() => {
                        const d = descOf(Error, 'name');
                        return d ? [d.writable, d.enumerable, d.configurable] : null;
                    })(),
                    errLenDesc: (() => {
                        const d = descOf(Error, 'length');
                        return d ? [d.writable, d.enumerable, d.configurable] : null;
                    })(),
                    statics: Object.getOwnPropertyNames(Error).filter((n) =>
                        ['captureStackTrace', 'stackTraceLimit', 'isError'].includes(n)),
                    captureStackTraceType: typeof Error.captureStackTrace,
                    // V8 专有全局/成员（WebKit 从未实现）
                    v8BI: (() => { try { return 'v8BreakIterator' in Intl; }
                        catch (x) { return 'ERR'; } })(),
                    wkTemp: (() => { try { return 'webkitTemporaryStorage' in navigator; }
                        catch (x) { return 'ERR'; } })(),
                    wkPersist: (() => { try { return 'webkitPersistentStorage' in navigator; }
                        catch (x) { return 'ERR'; } })(),
                };
            })(),
            // ---- WebKit 媒体面（P2-13）：AirPlay / EME 前缀面 ----
            // 桌面内核全缺席（实测），真机 Safari/CriOS 全存在。检测站按
            // 「声明 iOS 但无这些成员」判「现代 Blink」。取证见
            // app/mobile_cdp_injector.py 的「WebKit 媒体面」注释块。
            media: (() => {
                const P = HTMLMediaElement.prototype;
                const V = HTMLVideoElement.prototype;
                const v = document.createElement('video');
                const ctor = window.WebKitPlaybackTargetAvailabilityEvent;
                const mk = window.WebKitMediaKeys;
                const sessionC = window.WebKitMediaKeySession;
                const errC = window.WebKitMediaKeyError;
                const desc = (o, n) => {
                    const x = descOf(o, n);
                    return x ? [x.get ? 'accessor' : 'data', x.writable,
                                x.enumerable, x.configurable] : null;
                };
                return {
                    picker: [typeof P.webkitShowPlaybackTargetPicker,
                             'webkitShowPlaybackTargetPicker' in P,
                             desc(P, 'webkitShowPlaybackTargetPicker')],
                    pickerLen: P.webkitShowPlaybackTargetPicker
                        ? P.webkitShowPlaybackTargetPicker.length : null,
                    setKeys: [typeof P.webkitSetMediaKeys,
                              'webkitSetMediaKeys' in P,
                              desc(P, 'webkitSetMediaKeys')],
                    setKeysLen: P.webkitSetMediaKeys ? P.webkitSetMediaKeys.length : null,
                    wireless: [typeof v.webkitCurrentPlaybackTargetIsWireless,
                               desc(P, 'webkitCurrentPlaybackTargetIsWireless')],
                    wirelessVal: v.webkitCurrentPlaybackTargetIsWireless,
                    keys: [typeof v.webkitKeys, desc(P, 'webkitKeys')],
                    keysVal: v.webkitKeys,
                    presMode: desc(V, 'webkitPresentationMode'),
                    presVal: v.webkitPresentationMode,
                    // 真机：webkitPresentationMode 只在 HTMLVideoElement，不在
                    // HTMLMediaElement（容易挂错层级）
                    presOnMedia: desc(P, 'webkitPresentationMode'),
                    badRecv: (() => { try { P.webkitKeys; return 'ok'; }
                        catch (x) { return 'throw:' + x.name; } })(),
                    availCtor: ctor ? {
                        len: ctor.length, name: ctor.name,
                        own: Object.getOwnPropertyNames(ctor),
                        protoOwn: Object.getOwnPropertyNames(ctor.prototype),
                        availDesc: desc(ctor.prototype, 'availability'),
                        noArg: (() => { try { return new ctor('x').availability; }
                            catch (x) { return 'throw:' + x.name; } })(),
                        withArg: (() => { try {
                            return new ctor('x', {availability: 'available'}).availability;
                        } catch (x) { return 'throw:' + x.name; } })(),
                        tag: (() => { try {
                            return Object.prototype.toString.call(new ctor('x'));
                        } catch (x) { return 'throw:' + x.name; } })(),
                        instOf: (() => { try {
                            const e = new ctor('x');
                            return [e instanceof ctor, e instanceof Event];
                        } catch (x) { return 'throw:' + x.name; } })(),
                        noNew: (() => { try { ctor('x'); return 'ok'; }
                            catch (x) { return 'throw:' + x.name; } })(),
                        native: nativeStr(ctor),
                    } : null,
                    mk: mk ? {
                        len: mk.length, name: mk.name,
                        own: Object.getOwnPropertyNames(mk),
                        protoOwn: Object.getOwnPropertyNames(mk.prototype),
                        isTypeSupported: typeof mk.isTypeSupported,
                        supported: (() => { try { return mk.isTypeSupported('x', 'y'); }
                            catch (x) { return 'throw:' + x.name; } })(),
                        noNew: (() => { try { new mk('x'); return 'ok'; }
                            catch (x) { return 'throw:' + x.name; } })(),
                    } : null,
                    sessionProto: sessionC
                        ? Object.getOwnPropertyNames(sessionC.prototype) : null,
                    msgProto: window.WebKitMediaKeyMessageEvent
                        ? Object.getOwnPropertyNames(WebKitMediaKeyMessageEvent.prototype) : null,
                    neededProto: window.WebKitMediaKeyNeededEvent
                        ? Object.getOwnPropertyNames(WebKitMediaKeyNeededEvent.prototype) : null,
                    errCodes: errC ? ['MEDIA_KEYERR_UNKNOWN', 'MEDIA_KEYERR_CLIENT',
                        'MEDIA_KEYERR_SERVICE', 'MEDIA_KEYERR_OUTPUT',
                        'MEDIA_KEYERR_HARDWARECHANGE', 'MEDIA_KEYERR_DOMAIN']
                        .map((k) => [k, errC[k]]) : null,
                    errDesc: errC ? desc(errC, 'MEDIA_KEYERR_UNKNOWN') : null,
                    errProtoCodes: errC
                        ? Object.getOwnPropertyNames(errC.prototype).filter((n) =>
                            n.indexOf('MEDIA_KEYERR_') === 0) : null,
                };
            })(),
            // ---- Notification 面（WebKit Notification.idl 口径）----
            // 真机与桌面内核在静态成员/原型成员/选项缺省值上全线不同，
            // 取证见 app/mobile_cdp_injector.py 的「Notification 面」注释块。
            notif: (() => {
                const N = window.Notification;
                const out = { type: typeof N };
                if (typeof N !== 'function') { return out; }
                const P = N.prototype;
                const dd = (o, n) => {
                    const x = descOf(o, n);
                    return x ? [x.get ? 'accessor' : 'data', x.writable,
                                x.enumerable, x.configurable] : null;
                };
                out.len = N.length;
                out.own = Object.getOwnPropertyNames(N);
                out.protoOwn = Object.getOwnPropertyNames(P);
                out.permission = (() => { try { return N.permission; }
                    catch (e) { return 'throw:' + e.name; } })();
                out.permissionDesc = dd(N, 'permission');
                out.maxActionsIn = (() => { try { return 'maxActions' in N; }
                    catch (e) { return 'ERR'; } })();
                out.maxActions = (() => { try { return N.maxActions; }
                    catch (e) { return 'throw:' + e.name; } })();
                out.reqPermDesc = (() => {
                    const x = descOf(N, 'requestPermission');
                    return x ? [typeof x.value, x.writable, x.enumerable,
                                x.configurable, x.value.length] : null;
                })();
                out.chain = (() => {
                    const a = [];
                    let x = P;
                    for (let i = 0; i < 4 && x; i++) {
                        a.push(x === EventTarget.prototype
                            ? 'EventTarget.prototype'
                            : ((x.constructor && x.constructor.name) || 'obj'));
                        x = Object.getPrototypeOf(x);
                    }
                    return a;
                })();
                out.protoDesc = (() => {
                    const x = descOf(N, 'prototype');
                    return x ? [x.writable, x.enumerable, x.configurable] : null;
                })();
                out.tag = Object.prototype.toString.call(P);
                out.inst = (() => {
                    try {
                        const n = new N('t', {body: 'b', tag: 'tg',
                            icon: 'https://example.com/i.png', silent: true,
                            dir: 'rtl', lang: 'en', data: {a: 1}});
                        let hits = 0;
                        n.addEventListener('click', () => { hits++; });
                        n.dispatchEvent(new Event('click'));
                        return {
                            title: n.title, body: n.body, tag: n.tag,
                            icon: n.icon, silent: n.silent, dir: n.dir,
                            lang: n.lang, data: n.data, navigate: n.navigate,
                            own: Object.getOwnPropertyNames(n),
                            isET: n instanceof EventTarget,
                            isN: n instanceof N, hits,
                            show: String(n.show()),
                            close: String(n.close()),
                        };
                    } catch (e) { return 'throw:' + e.name; }
                })();
                out.defaults = (() => {
                    try {
                        const n = new N('t');
                        return {icon: n.icon, silent: n.silent, dir: n.dir,
                                lang: n.lang, body: n.body, tag: n.tag,
                                data: n.data, navigate: n.navigate};
                    } catch (e) { return 'throw:' + e.name; }
                })();
                out.silentFalse = (() => {
                    try { return new N('t', {silent: false}).silent; }
                    catch (e) { return 'throw:' + e.name; }
                })();
                out.noNew = (() => { try { N('t'); return 'ok'; }
                    catch (e) { return 'throw:' + e.name; } })();
                out.badRecv = (() => { try { P.title; return 'ok'; }
                    catch (e) { return 'throw:' + e.name; } })();
                out.badRecvNav = (() => { try { P.navigate; return 'ok'; }
                    catch (e) { return 'throw:' + e.name; } })();
                out.handlerLoop = (() => {
                    try {
                        const n = new N('t');
                        const f = () => {};
                        n.ondisplay = f;
                        const same = n.ondisplay === f;
                        n.ondisplay = null;
                        return [same, n.ondisplay];
                    } catch (e) { return 'throw:' + e.name; }
                })();
                out.showBadRecv = (() => { try { P.show(); return 'ok'; }
                    catch (e) { return 'throw:' + e.name; } })();
                out.native = nativeStr(N);
                return out;
            })(),
            // ---- WebKit 原生触控工厂：document.createTouch / createTouchList ----
            // 检测站按「声明 iOS 但无 createTouch」直接判「现代 Blink 内核」。
            // 真机形态（Playwright WebKit 27.2 实测，见 injector 第 12 块注释）：
            // 扩展四参全部生效、Touch 表面只有 webkit* 前缀成员、构造器不可构造。
            touchFactory: (() => {
                const D = Document.prototype;
                const out = {
                    ct: typeof document.createTouch,
                    ctl: typeof document.createTouchList,
                };
                // 原型面与工厂是否存在于无关：Android 上用来断言「没有被 iOS
                // 补丁改成 WebKit 口径」。
                out.touchProto = typeof Touch === 'function'
                    ? Object.getOwnPropertyNames(Touch.prototype) : null;
                out.touchListProto = typeof TouchList === 'function'
                    ? Object.getOwnPropertyNames(TouchList.prototype) : null;
                out.touchEventProto = typeof TouchEvent === 'function'
                    ? Object.getOwnPropertyNames(TouchEvent.prototype) : null;
                if (out.ct !== 'function' || out.ctl !== 'function') {
                    return out;
                }
                const dd = (o, n) => {
                    const x = descOf(o, n);
                    return x ? [x.get ? 'accessor' : 'data', x.writable,
                                x.enumerable, x.configurable] : null;
                };
                out.ctDesc = dd(D, 'createTouch');
                out.ctlDesc = dd(D, 'createTouchList');
                out.ctLen = document.createTouch.length;
                out.ctlLen = document.createTouchList.length;
                out.ctName = document.createTouch.name;
                out.native = nativeStr(document.createTouch);
                out.own = Object.getOwnPropertyNames(document.createTouch);
                // 构造函数：真机两者都不可构造（Illegal constructor）
                out.newTouch = (() => { try { new Touch({}); return 'ok'; }
                    catch (e) { return 'throw:' + e.name; } })();
                out.newTouchList = (() => { try { new TouchList(); return 'ok'; }
                    catch (e) { return 'throw:' + e.name; } })();
                out.touchLen = Touch.length;
                out.touchDesc = (() => {
                    const x = descOf(window, 'Touch');
                    return x ? [x.writable, x.enumerable, x.configurable] : null;
                })();
                // 全参调用：九项逐个回读
                out.full = (() => {
                    try {
                        const t = document.createTouch(
                            window, document.body, 7, 100, 200, 300, 400, 5, 6, 45, 0.75);
                        return {
                            identifier: t.identifier,
                            target: t.target === document.body,
                            pageX: t.pageX, pageY: t.pageY,
                            clientX: t.clientX, clientY: t.clientY,
                            screenX: t.screenX, screenY: t.screenY,
                            wkRX: t.webkitRadiusX, wkRY: t.webkitRadiusY,
                            wkRot: t.webkitRotationAngle, wkForce: t.webkitForce,
                            // 真机没有无前缀成员：`in` 为 false、读取为 undefined
                            hasForce: 'force' in t, hasRadiusX: 'radiusX' in t,
                            force: String(t.force), radiusX: String(t.radiusX),
                            rotationAngle: String(t.rotationAngle),
                            own: Object.getOwnPropertyNames(t),
                            tag: Object.prototype.toString.call(t),
                            instOf: t instanceof Touch,
                            protoOk: Object.getPrototypeOf(t) === Touch.prototype,
                        };
                    } catch (e) { return 'throw:' + e.name; }
                })();
                // 无参：target 为 null、数值项为 0
                out.minimal = (() => {
                    try {
                        const t = document.createTouch(window);
                        return {target: String(t.target), identifier: t.identifier,
                                pageX: t.pageX, pageY: t.pageY,
                                wkForce: t.webkitForce, own: Object.getOwnPropertyNames(t)};
                    } catch (e) { return 'throw:' + e.name; }
                })();
                // 字符串参数按 WebKit 语义强制转换
                out.coerce = (() => {
                    try {
                        const t = document.createTouch(
                            window, document.body, '7', '100', '200', 0, 0, '5', '6', 0, 0.5);
                        return [t.identifier, t.pageX, t.webkitRadiusX,
                                t.webkitRotationAngle, t.webkitForce];
                    } catch (e) { return 'throw:' + e.name; }
                })();
                // TouchList：索引与 item() 同源、越界为 null、空列表长度 0
                out.list = (() => {
                    try {
                        const t = document.createTouch(window, document.body, 1, 0, 0, 0, 0, 0, 0, 0, 0);
                        const t2 = document.createTouch(window, document.body, 2, 0, 0, 0, 0, 0, 0, 0, 0);
                        const tl = document.createTouchList(t, t2);
                        const tl0 = document.createTouchList();
                        return {
                            len: tl.length, isTL: tl instanceof TouchList,
                            same0: tl[0] === t, same1: tl[1] === t2,
                            item0: tl.item(0) === t,
                            itemNeg: String(tl.item(-1)), itemOOR: String(tl.item(5)),
                            own: Object.getOwnPropertyNames(tl),
                            protoOk: Object.getPrototypeOf(tl) === TouchList.prototype,
                            len0: tl0.length, isTL0: tl0 instanceof TouchList,
                        };
                    } catch (e) { return 'throw:' + e.name; }
                })();
                // 坏 receiver：两个工厂方法都走原生 receiver 校验
                out.badRecv = (() => { try { D.createTouch.call({}, window); return 'ok'; }
                    catch (e) { return 'throw:' + e.name; } })();
                out.badRecvList = (() => { try { D.createTouchList.call({}); return 'ok'; }
                    catch (e) { return 'throw:' + e.name; } })();
                return out;
            })(),
        },
        // ---- F-06 / F-17 / F-18 / F-31 / F-32 / F-33 / F-34：读数与成员面 ----
        poTypes: (typeof PerformanceObserver === 'function'
            && PerformanceObserver.supportedEntryTypes)
            ? Array.from(PerformanceObserver.supportedEntryTypes) : null,
        poFrozen: (typeof PerformanceObserver === 'function'
            && PerformanceObserver.supportedEntryTypes)
            ? Object.isFrozen(PerformanceObserver.supportedEntryTypes) : null,
        poDesc: (() => {
            try {
                const d = descOf(PerformanceObserver, 'supportedEntryTypes');
                if (!d) { return null; }
                return {get: typeof d.get, writable: d.writable,
                        enumerable: d.enumerable, configurable: d.configurable};
            } catch (e) { return 'ERR:' + e.name; }
        })(),
        poWriteBlocked: (() => {
            try {
                PerformanceObserver.supportedEntryTypes = ['x'];
                return JSON.stringify(PerformanceObserver.supportedEntryTypes);
            } catch (e) { return 'throw:' + e.name; }
        })(),
        audioState: (() => {
            try {
                const Ctor = window.AudioContext || window.webkitAudioContext;
                const c = new Ctor();
                const o = {state: c.state, baseState: c.baseLatency !== undefined};
                try { c.close(); } catch (e) {}
                return o;
            } catch (e) { return 'throw:' + e.name; }
        })(),
        audioMembers: (() => {
            const proto = window.AudioContext ? window.AudioContext.prototype : null;
            if (!proto) { return null; }
            return {
                sinkId: 'sinkId' in proto,
                setSinkId: 'setSinkId' in proto,
                onsinkchange: 'onsinkchange' in proto,
                playbackStats: 'playbackStats' in proto,
                basePlaybackStats: window.BaseAudioContext
                    ? ('playbackStats' in window.BaseAudioContext.prototype) : null,
            };
        })(),
        orientAngle: screen.orientation ? screen.orientation.angle : null,
        orientType: screen.orientation ? screen.orientation.type : null,
        // F-90：iOS 普通浏览上下文里 Notification 整体缺席（Android 侧存在，
        // 故保留 permission 读值供 Android 断言）。
        notifPerm: typeof Notification === 'function' ? Notification.permission : null,
        notifAbsent: (() => {
            const out = {
                type: typeof Notification,
                inWin: 'Notification' in window,
                eventType: typeof NotificationEvent,
                pushManager: typeof PushManager,
                pushSub: typeof PushSubscription,
                pushOpts: typeof PushSubscriptionOptions,
                pushEvent: typeof PushEvent,
                pushMsg: typeof PushMessageData,
                winPushManager: (() => {
                    try { return typeof window.pushManager; } catch (e) { return 'ERR'; }
                })(),
                swrPush: (() => {
                    try {
                        const C = window.ServiceWorkerRegistration;
                        if (typeof C !== 'function') { return 'absent'; }
                        return 'pushManager' in C.prototype;
                    } catch (e) { return 'ERR'; }
                })(),
                // F-102：其余 Blink 专有 / pref 门控成员
                swrDrops: (() => {
                    try {
                        const C = window.ServiceWorkerRegistration;
                        if (typeof C !== 'function') { return 'absent'; }
                        const P = C.prototype;
                        const out = {};
                        ['sync', 'periodicSync', 'backgroundFetch',
                         'paymentManager', 'cookies', 'pushManager']
                            .forEach((n) => { out[n] = n in P; });
                        // 反向保留：navigationPreload 默认开，真机存在
                        out.navigationPreload = 'navigationPreload' in P;
                        return out;
                    } catch (e) { return 'ERR'; }
                })(),
            };
            return out;
        })(),
        consoleHasContext: 'context' in console,
        consoleHasCreateTask: 'createTask' in console,
        consoleHasMemory: 'memory' in console,
        consoleNames: Object.getOwnPropertyNames(console).sort(),
        // ---- F-26 / F-27：Chromium 专属原型成员应缺席 ----
        protoDrop: (() => {
            const pick = (ctorName, members) => {
                const C = window[ctorName];
                if (typeof C !== 'function') { return {ctor: 'absent'}; }
                const r = {};
                members.forEach((m) => { r[m] = m in C.prototype; });
                return r;
            };
            return {
                rtc: pick('RTCPeerConnection',
                    ['addStream', 'removeStream', 'createDTMFSender',
                     'getLocalStreams', 'getRemoteStreams',
                     'onaddstream', 'onremovestream']),
                fontFace: pick('FontFace', ['ascentOverride', 'descentOverride',
                    'lineGapOverride', 'variationSettings', 'variant']),
                inputIncremental: pick('HTMLInputElement', ['incremental']),
                pointerEvent: pick('PointerEvent', ['persistentDeviceId']),
                intersector: pick('IntersectionObserver', ['delay', 'trackVisibility']),
                xhr: pick('XMLHttpRequest',
                    ['setAttributionReporting', 'setPrivateToken']),
                element: pick('Element',
                    ['setHTML', 'moveBefore', 'elementTiming', 'fakeShadowRoot',
                     'activeViewTransition']),
                document: pick('Document',
                    ['exitPointerLock', 'requestStorageAccessFor', 'featurePolicy',
                     'hasPrivateToken', 'hasRedemptionRecord',
                     'hasUnpartitionedCookieAccess', 'wasDiscarded', 'prerendering']),
                screen: pick('Screen', ['isExtended', 'onchange']),
                eventTarget: pick('EventTarget', ['when']),
                anchor: pick('HTMLAnchorElement',
                    ['attributionSrc', 'interestForElement', 'browsingTopics',
                     'sharedStorageWritable']),
                file: pick('File', ['lastModifiedDate']),
                webgl: pick('WebGLRenderingContext',
                    ['makeXRCompatible', 'drawingBufferStorage', 'drawingBufferFormat',
                     'unpackColorSpace']),
                // F-106：四项的真实宿主是 WebGL2RenderingContext
                webgl2: pick('WebGL2RenderingContext',
                    ['makeXRCompatible', 'drawingBufferStorage', 'drawingBufferFormat',
                     'unpackColorSpace']),
                // F-103 / F-104 / F-105 / F-108 / F-109 / F-111 / F-112 / F-113
                mediaDevices: pick('MediaDevices', ['setCaptureHandleConfig']),
                mediaTrack: pick('MediaStreamTrack',
                    ['getCaptureHandle', 'oncapturehandlechange']),
                canvas2d: pick('CanvasRenderingContext2D',
                    ['fontKerning', 'fontStretch', 'fontVariantCaps', 'isContextLost',
                     'lang', 'textRendering']),
                // 对照：filter 自 iOS 18 起真机有，必须保留
                canvas2dKeep: pick('CanvasRenderingContext2D', ['filter']),
                mediaMetadata: pick('MediaMetadata', ['chapterInfo']),
                mediaRecorder: pick('MediaRecorder', ['audioBitrateMode']),
                mediaKeys: pick('MediaKeys', ['getStatusForPolicy']),
                request: pick('Request', ['isHistoryNavigation', 'targetAddressSpace']),
                htmlElement: pick('HTMLElement',
                    ['editContext', 'virtualKeyboardPolicy', 'onpointerrawupdate',
                     'onbeforexrselect', 'oncontextlost', 'oncontextrestored',
                     'onscrollsnapchange', 'onscrollsnapchanging']),
                mediaElement: pick('HTMLMediaElement',
                    ['captureStream', 'controlsList', 'loading',
                     'setSinkId', 'sinkId', 'audioTracks', 'videoTracks',
                     'onsinkchange']),
                iframe: pick('HTMLIFrameElement',
                    ['adAuctionHeaders', 'allowPaymentRequest', 'browsingTopics',
                     'credentialless', 'csp', 'featurePolicy', 'privateToken',
                     'sharedStorageWritable']),
                area: pick('HTMLAreaElement', ['attributionSrc', 'interestForElement']),
                image: pick('HTMLImageElement',
                    ['attributionSrc', 'interestForElement', 'browsingTopics',
                     'sharedStorageWritable']),
                elementExtra: pick('Element',
                    ['startViewTransition', 'onbeforecopy', 'onbeforecut',
                     'onbeforepaste', 'onsearch']),
                // F-115 / F-117：WebKit 落点在 Element/HTMLElement，应保留
                handlerKeep: {
                    focusin: 'onfocusin' in document.body,
                    beforecopyBody: 'onbeforecopy' in document.body,
                    beforecopyRoot: 'onbeforecopy' in document.documentElement,
                    beforecopyDoc: 'onbeforecopy' in document,
                    ariaNotify: 'ariaNotify' in Element.prototype,
                    docSVT: 'startViewTransition' in Document.prototype,
                },
                // F-118 / F-119：WebKit 独有处理器。F-118 的 5 名在 Element 上
                // （[NotEnumerable]，root/body 按继承命中）；F-119 的 pageswap /
                // pagereveal 只转发到 body/frameset，root 必须仍为 false。
                webkitHandlers: (() => {
                    const names = ['onbeforeload', 'onwebkitneedkey',
                        'onwebkitpresentationmodechanged',
                        'onwebkitcurrentplaybacktargetiswirelesschanged',
                        'onwebkitplaybacktargetavailabilitychanged'];
                    const out = {root: {}, body: {}, own: {}, enum: {}};
                    for (const n of names) {
                        out.root[n] = n in document.documentElement;
                        out.body[n] = n in document.body;
                        const d = Object.getOwnPropertyDescriptor(
                            Element.prototype, n);
                        out.own[n] = !!d;
                        out.enum[n] = d ? d.enumerable : null;
                    }
                    out.pageswapBody = 'onpageswap' in document.body;
                    out.pagerevealBody = 'onpagereveal' in document.body;
                    out.pageswapRoot = 'onpageswap' in document.documentElement;
                    out.pagerevealRoot = 'onpagereveal' in document.documentElement;
                    out.pageswapWindow = 'onpageswap' in window;
                    const bp = Object.getOwnPropertyDescriptor(
                        HTMLBodyElement.prototype, 'onpageswap');
                    const fp2 = Object.getOwnPropertyDescriptor(
                        HTMLFrameSetElement.prototype, 'onpagereveal');
                    out.bodyOwn = !!bp;
                    out.frameOwn = !!fp2;
                    out.bodyEnum = bp ? bp.enumerable : null;
                    out.bodyNull = document.body.onpageswap;
                    return out;
                })(),
                webglConst: (() => {
                    const P = window.WebGLRenderingContext;
                    if (!P) { return null; }
                    return {RGB8: 'RGB8' in P, RGBA8: 'RGBA8' in P};
                })(),
                imageData: pick('ImageData', ['pixelFormat']),
                viewport: pick('VisualViewport', ['onscrollend']),
            };
        })(),
        // ---- F-31：storage.estimate 口径（异步，见下方 out.storageEstimate）----
        // ---- F-19：container-only 查询应为 maybe（WebKit MIMETypeCache 语义）----
        canPlayIos: (() => {
            const v = document.createElement('video');
            const probe = (t) => { try { return v.canPlayType(t); } catch (e) { return 'ERR'; } };
            return {
                mpeg: probe('audio/mpeg'),
                mp3Codec: probe('audio/mpeg; codecs="mp3"'),
                aac: probe('audio/aac'),
                mp3: probe('audio/mp3'),
                flac: probe('audio/flac'),
                flacCodec: probe('audio/flac; codecs="flac"'),
                flacMp4: probe('audio/mp4; codecs="flac"'),
                quicktime: probe('video/quicktime'),
                dvh1: probe('video/mp4; codecs="dvh1.05.06"'),
                av01: probe('video/mp4; codecs="av01.0.05M.08"'),
                mp4a405: probe('audio/mp4; codecs="mp4a.40.5"'),
                avc1Codec: probe('video/mp4; codecs="avc1.42E01E"'),
                hls: probe('application/vnd.apple.mpegurl'),
                unknownH264: probe('video/h264'),
                emptyStr: probe(''),
                // ---- F-143：白名单语义验收串（清单 §49 修复顺序 2）----
                // 这些串旧实现全部透传宿主 Blink：opus/mp3-in-mp4 → probably、
                // x-matroska → maybe、hev1 版本化 → probably。
                opusMp4: probe('audio/mp4; codecs="opus"'),
                mp3Mp4: probe('audio/mp4; codecs="mp3"'),
                mkv: probe('video/x-matroska'),
                mkvCodec: probe('video/x-matroska; codecs="avc1"'),
                hev1Full: probe('video/mp4; codecs="hev1.1.6.L93.B0"'),
                avi: probe('video/avi'),
                // 白名单容器族（F-125）：容器命中且无 codecs → maybe
                gpp2: probe('video/3gpp2'),
                aiff: probe('audio/aiff'),
                basic: probe('audio/basic'),
                xm4a: probe('audio/x-m4a'),
                mp2t: probe('video/mp2t'),
                caf: probe('audio/x-caf'),
                aacp: probe('audio/aacp'),
                wavePcm: probe('audio/wave; codecs="1"'),
                // 白名单外 / 未知 codec 一律 ''
                oggContainer: probe('audio/ogg'),
                webmCodec: probe('video/webm; codecs="vp8"'),
                noArg: (function () {
                    try { return 'no-throw:' + v.canPlayType(); }
                    catch (e) { return e.name + ':' + e.message; }
                })(),
            };
        })(),
        // ---- F-25 / F-36：真机有、Chromium 缺的 WebKit 面 ----
        addMembers: (() => {
            const has = (proto, names) => {
                const r = {};
                names.forEach((n) => {
                    let v;
                    try { v = proto[n]; } catch (e) { v = 'THROW'; }
                    r[n] = {inn: n in proto, type: typeof v};
                });
                return r;
            };
            const P = (n) => window[n] && window[n].prototype;
            const iface = {};
            ['AudioSession', 'MediaController', 'XMLHttpRequestProgressEvent',
             'SVGViewSpec', 'CSSValue', 'CSSPrimitiveValue', 'CSSValueList',
             'RGBColor', 'Rect', 'Counter', 'SVGPathSeg', 'MathMLMathElement',
             'DataCue', 'VTTRegion', 'ManagedMediaSource', 'ManagedSourceBuffer',
             'BufferedChangeEvent', 'VideoTrack', 'AudioTrack', 'VideoTrackList',
             'AudioTrackList', 'AudioTrackConfiguration', 'MediaRecorderErrorEvent',
             'OverconstrainedErrorEvent', 'FileSystemEntry', 'FileSystemDirectoryEntry',
             'FileSystemFileEntry', 'FileSystemDirectoryReader', 'Report',
             'FontFaceSet', 'StylePropertyMapReadOnly', 'CSSMathValue',
             'CSSNumericValue', 'CSSUnitValue', 'CSSKeywordValue', 'CSSImageValue',
             'CSSTransformComponent', 'CSSStyleValue', 'SpeechRecognitionResultList',
             'SpeechRecognitionResult', 'SpeechRecognitionAlternative',
             'ApplePayError', 'FileSystem',
             'TextTrack', 'TextTrackCue'].forEach((n) => {
                const C = window[n];
                if (typeof C !== 'function') { iface[n] = 'absent'; return; }
                const d = Object.getOwnPropertyDescriptor(window, n);
                const msgOf = (f) => {
                    try { f(); return 'ok'; }
                    catch (e) { return 'throw:' + e.name + ':'
                        + String(e.message).slice(0, 24); }
                };
                iface[n] = {len: C.length, ctor: msgOf(() => new C()),
                            noNew: msgOf(() => C()),
                            en: d && d.enumerable,
                            wr: d && d.writable,
                            tag: Object.prototype.toString.call(C.prototype),
                            proto: Object.getOwnPropertyNames(C.prototype)};
            });
            const c2d = document.createElement('canvas').getContext('2d');
            const m = new MouseEvent('click');
            const w = new WheelEvent('wheel');
            const k = new KeyboardEvent('keydown');
            const v = document.createElement('video');
            const rng = new Range();
            const ffn = new FontFace('x', 'url(x)');
            return {
                c2d: has(P('CanvasRenderingContext2D'),
                    ['setLineWidth', 'setStrokeColor', 'setFillColor', 'setLineCap',
                     'setLineJoin', 'setMiterLimit', 'setShadow', 'clearShadow',
                     'webkitLineDash', 'webkitLineDashOffset']),
                c2dLive: {dash: c2d.webkitLineDash, dashOffset: c2d.webkitLineDashOffset,
                          // 这些是 iOS 专有面；Android 上不存在，探针不能炸。
                          fillAfterSet: (() => {
                              if (typeof c2d.setFillColor !== 'function') { return 'absent'; }
                              c2d.setFillColor('#123456');
                              return c2d.fillStyle; })(),
                          shadowAfterSet: (() => {
                              if (typeof c2d.setShadow !== 'function') { return 'absent'; }
                              c2d.setShadow(3, 4, 5, 'red');
                              return [c2d.shadowOffsetX,
                                      c2d.shadowOffsetY,
                                      c2d.shadowBlur,
                                      c2d.shadowColor]; })(),
                          clearShadow: (() => {
                              if (typeof c2d.clearShadow !== 'function') { return 'absent'; }
                              c2d.clearShadow();
                              return [c2d.shadowOffsetX,
                                      c2d.shadowBlur]; })()},
                mouse: has(MouseEvent.prototype, ['webkitForce']),
                mouseLive: m.webkitForce,
                mouseConst: {own: MouseEvent.WEBKIT_FORCE_AT_MOUSE_DOWN,
                             force: MouseEvent.WEBKIT_FORCE_AT_FORCE_MOUSE_DOWN,
                             proto: MouseEvent.prototype.WEBKIT_FORCE_AT_MOUSE_DOWN},
                wheel: {inn: 'webkitDirectionInvertedFromDevice'
                            in WheelEvent.prototype,
                        val: w.webkitDirectionInvertedFromDevice},
                kbd: {inn: 'keyIdentifier' in KeyboardEvent.prototype,
                      val: k.keyIdentifier},
                media: (() => {
                    // 整个媒体面是 iOS 专有补丁；Android 上多数成员不存在，
                    // 探针逐项兜底，避免 TypeError 中断整个 state 采集。
                    const safe = (f) => { try { return f(); } catch (e) { return 'absent'; } };
                    return {
                        fastSeek: 'fastSeek' in HTMLMediaElement.prototype,
                        getStartDate: safe(() => String(v.getStartDate())),
                        vpqKeys: safe(() => Object.keys(v.getVideoPlaybackQuality()).sort()),
                        audioTracks: safe(() => String(v.audioTracks)),
                        videoTracks: safe(() => String(v.videoTracks)),
                        controller: safe(() => String(v.controller)),
                        mediaGroup: safe(() => v.mediaGroup),
                        wirelessDisabled: safe(() => v.webkitWirelessVideoPlaybackDisabled),
                        autoPip: safe(() => v.autoPictureInPicture),
                        showPicker: safe(() => typeof v.webkitShowPlaybackTargetPicker),
                        curWireless: safe(() => v.webkitCurrentPlaybackTargetIsWireless),
                        presMode: safe(() => v.webkitSupportsPresentationMode('inline')),
                        setPres: safe(() => typeof v.webkitSetPresentationMode),
                    };
                })(),
                srcBuf: has(P('SourceBuffer'),
                    ['audioTracks', 'textTracks', 'videoTracks']),
                range: {inn: 'compareNode' in Range.prototype,
                        consts: [Range.NODE_BEFORE, Range.NODE_AFTER,
                                 Range.NODE_BEFORE_AND_AFTER, Range.NODE_INSIDE],
                        protoConst: Range.prototype.NODE_BEFORE},
                metrics: (() => { const mm = c2d.measureText('Hg');
                    return {asc: mm.emHeightAscent, desc: mm.emHeightDescent}; })(),
                fontFace: {inn: 'width' in FontFace.prototype, val: ffn.width},
                cssDecl: has(P('CSSStyleDeclaration'),
                    ['cssRules', 'getPropertyCSSValue']),
                track: {inn: 'onconfigurationchange'
                            in MediaStreamTrack.prototype},
                workerErr: {inn: 'onmessageerror' in Worker.prototype},
                handlers: (() => {
                    // F-77 D4：真机 27.2 实测，12 个事件属性在 Window /
                    // Document / HTMLElement / SVGElement 四处都有；可枚举性
                    // 只有 copy/cut/paste 为 true，Force Touch 与 touch 家族
                    // 九个为 false（/tmp/f77p/hosts.py）。
                    const names = ['oncopy', 'oncut', 'onpaste',
                        'onwebkitmouseforcechanged', 'onwebkitmouseforcedown',
                        'onwebkitmouseforceup', 'onwebkitmouseforcewillbegin',
                        'ontouchforcechange', 'ontouchstart', 'ontouchmove',
                        'ontouchend', 'ontouchcancel'];
                    const hosts = {win: window, doc: Document.prototype,
                                   el: HTMLElement.prototype,
                                   svg: SVGElement.prototype};
                    const out = {};
                    Object.keys(hosts).forEach((h) => {
                        const r = {};
                        names.forEach((n) => {
                            const d = Object.getOwnPropertyDescriptor(hosts[h], n);
                            r[n] = d ? (d.enumerable ? 'e' : 'ne') : 'absent';
                        });
                        out[h] = r;
                    });
                    const before = window.oncopy;
                    const fn = () => {};
                    window.oncopy = fn;
                    const after = window.oncopy;
                    window.oncopy = null;
                    return {hosts: out, before: String(before),
                            assign: after === fn, reset: window.oncopy === null};
                })(),
                globals: (() => {
                    // F-77 D3 三个全局项的真机形态：webkitIndexedDB 是只读
                    // getter（同一对象）、openDatabase 是 get/set 访问器且
                    // getter 返回 JSC 内部函数（typeof 为 "undefined"）、
                    // safari 是普通数据属性。
                    const d = (n) => {
                        const x = Object.getOwnPropertyDescriptor(window, n);
                        return x ? [x.enumerable, x.configurable,
                                    typeof x.get, typeof x.set,
                                    typeof x.value] : null;
                    };
                    const odb = window.openDatabase;
                    const safari = window.safari;
                    const gpc = Object.getOwnPropertyDescriptor(
                        Navigator.prototype, 'globalPrivacyControl');
                    const out = {
                        webkitIndexedDB: typeof window.webkitIndexedDB,
                        openDatabase: typeof window.openDatabase,
                        gDesc: {webkitIndexedDB: d('webkitIndexedDB'),
                                openDatabase: d('openDatabase'),
                                safari: d('safari')},
                        gpcDesc: gpc ? [gpc.enumerable, gpc.configurable,
                                        typeof gpc.get, typeof gpc.set] : null,
                        gpcVal: navigator.globalPrivacyControl,
                        gpcNative: gpc && gpc.get ? nativeStr(gpc.get) : null,
                        // F-84：AudioSession 是 Singleton 双暴露面。
                        audio: (() => {
                            const d = Object.getOwnPropertyDescriptor(
                                Navigator.prototype, 'audioSession');
                            const a = navigator.audioSession;
                            return {
                                desc: d ? [d.enumerable, d.configurable,
                                           typeof d.get, typeof d.set,
                                           d.get && d.get.length] : null,
                                tag: a === undefined ? null
                                    : Object.prototype.toString.call(a),
                                same: navigator.audioSession === a,
                                own: a ? Object.getOwnPropertyNames(a) : null,
                                type: a ? a.type : null,
                                inst: a ? (a instanceof window.AudioSession) : null,
                            };
                        })(),
                        sameIDB: window.webkitIndexedDB === window.indexedDB,
                        odbNull: odb == null,
                        odbUndef: odb === undefined,
                        odbLen: odb && typeof odb.length === 'number' ? odb.length : null,
                        odbOwn: odb ? Object.getOwnPropertyNames(odb) : null,
                        odbCall: (() => {
                            try { odb(); return 'ok'; }
                            catch (e) { return e.name + ':' + e.message; }
                        })(),
                        safariTag: safari && safari.pushNotification
                            ? String(safari.pushNotification) : null,
                    };
                    // F-29 / F-30：表单控件默认样式（真机 system-ui + 18/21px）
                    const mk = (tag) => {
                        const el = document.createElement(tag);
                        if (tag === 'button') { el.textContent = '确定'; }
                        document.body.appendChild(el);
                        return el;
                    };
                    const snap = (el) => {
                        const cs = getComputedStyle(el);
                        return {ff: cs.fontFamily, fs: cs.fontSize,
                                h: el.getBoundingClientRect().height};
                    };
                    const btn = mk('button');
                    const inp = mk('input');
                    const sel = mk('select');
                    const ta = mk('textarea');
                    out.form = {button: snap(btn), input: snap(inp),
                                select: snap(sel), textarea: snap(ta)};
                    out.uaStyle = (() => {
                        const st = document.getElementById('__wk_uaform');
                        return st ? {present: true, len: st.textContent.length,
                                     sheets: document.styleSheets.length}
                                  : {present: false};
                    })();
                    [btn, inp, sel, ta].forEach((e) => e.remove());
                    return out;
                })(),
                iface: iface,
                staticAdd: {dvbpe: 'BYTES_PER_ELEMENT' in DataView,
                            regMultiline: 'multiline' in RegExp,
                            regDollar1: '$1' in RegExp,
                            symDispose: 'dispose' in Symbol,
                            symAsyncDispose: 'asyncDispose' in Symbol},
            };
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
    // ---- F-20：permissions.query 分州读数 ----
    out.perms = {};
    for (const name of ['geolocation', 'notifications', 'camera', 'microphone',
                        'accelerometer', 'gyroscope', 'magnetometer', 'midi',
                        'background-fetch', 'display-capture', 'background-sync',
                        'clipboard-read', 'clipboard-write', 'persistent-storage',
                        'local-fonts', 'window-management', 'payment-handler',
                        'screen-wake-lock', 'storage-access', 'push',
                        'bogus-permission-name']) {
        try {
            const st = await nav.permissions.query({name: name});
            out.perms[name] = st.state;
        } catch (e) { out.perms[name] = 'throw:' + e.name; }
    }
    // ---- F-31：storage.estimate 配额与字段集 ----
    out.storageEstimate = null;
    try {
        if (nav.storage && nav.storage.estimate) {
            const r = await nav.storage.estimate();
            out.storageEstimate = {
                quota: r.quota, usage: r.usage,
                keys: Object.keys(r).sort(),
                usageDetails: 'usageDetails' in r,
            };
        }
    } catch (e) { out.storageEstimate = 'throw:' + e.name; }
    // ---- F-32：enumerateDevices 应无 audiooutput ----
    out.deviceKinds = (() => {
        try {
            if (!nav.mediaDevices || !nav.mediaDevices.enumerateDevices) { return null; }
            return nav.mediaDevices.enumerateDevices();
        } catch (e) { return 'throw:' + e.name; }
    })();
    out.deviceKinds = out.deviceKinds && typeof out.deviceKinds.then === 'function'
        ? await out.deviceKinds.then((ds) => ds.map((d) => d.kind),
                                   (e) => 'throw:' + e.name)
        : out.deviceKinds;
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
    // ---- 媒体查询面（全平台）：CDP setDeviceMetricsOverride(mobile=True) 的
    // 触控/悬停语义。仿真通道断开（内核还原全部 Emulation 覆盖）时立即回落
    // 宿主桌面值 pointer:fine / hover:hover —— 检测报告 12 项的共同根因。----
    out.mediaQueries = (function () {
        var q = function (s) { try { return matchMedia(s).matches; } catch (e) { return null; } };
        return {
            pointerCoarse: q('(pointer: coarse)'),
            pointerFine: q('(pointer: fine)'),
            hoverNone: q('(hover: none)'),
            hoverHover: q('(hover: hover)'),
            anyPointerCoarse: q('(any-pointer: coarse)'),
            anyHoverNone: q('(any-hover: none)'),
            // 色域：iPhone 7 及以后全系 Display P3 屏，(color-gamut: p3) 恒为
            // true；宿主桌面显示器多为 sRGB。由 CDP setEmulatedMedia 原生注入
            // （app/mobile_cdp_injector.py 页面路径与 OOPIF 路径各自下发），
            // 不跨页面继承——新 page 目标需重新下发，故此处按当前页读。
            p3: q('(color-gamut: p3)'),
            srgb: q('(color-gamut: srgb)'),
            // HDR 屏（F-12）：iPhone X/XS/11 Pro 及 12 以后全系为 true，
            // SE 系列 false。CDP 不支持仿真该特性，由 matchMedia 补丁按机型给值。
            dynamicRangeHigh: q('(dynamic-range: high)'),
            dynamicRangeStandard: q('(dynamic-range: standard)'),
            dynamicRangeMedia: (function () {
                try { return matchMedia('(dynamic-range: high)').media; }
                catch (e) { return null; }
            })(),
        };
    })();
    // ---- 音频采样率：真机 OfflineAudioContext 采样率恒为精确整数；内核
    // audio 伪装会按种子微扰（实测宿主 44100 → 伪装 44100.01171875），
    // 移动端已强制 --disable-spoofing=audio 回到真机口径。----
    out.audioSampleRate = (function () {
        var r = {};
        try { r.s44100 = new OfflineAudioContext(1, 64, 44100).sampleRate; }
        catch (e) { r.s44100 = null; }
        try { r.s48000 = new OfflineAudioContext(1, 64, 48000).sampleRate; }
        catch (e) { r.s48000 = null; }
        return r;
    })();
    // ---- F-132：主世界 WebCodecs 静态 isConfigSupported（原生 Blink 类
    // 只包静态，原型/构造器保持原生，VideoFrame 品牌校验不受影响）----
    out.wcIcs = await (async function () {
        try {
            var C = window.VideoDecoder;
            if (typeof C !== 'function') { return { absent: true }; }
            var d = Object.getOwnPropertyDescriptor(C, 'isConfigSupported');
            var msg = function (cfg) {
                try { C.isConfigSupported(cfg); return 'no-throw'; }
                catch (e) { return e.name + ':' + e.message; }
            };
            var A = window.AudioEncoder;
            var amsg = function (cfg) {
                try { A.isConfigSupported(cfg); return 'no-throw'; }
                catch (e) { return e.name + ':' + e.message; }
            };
            var q = function (c) {
                return C.isConfigSupported({ codec: c })
                    .then(function (r) { return !!r.supported; },
                          function () { return 'ERR'; });
            };
            var qe = function (c) {
                return window.VideoEncoder.isConfigSupported(
                    { codec: c, width: 640, height: 480 })
                    .then(function (r) { return !!r.supported; },
                          function () { return 'ERR'; });
            };
            var qa = function (c) {
                return window.AudioDecoder.isConfigSupported(
                    { codec: c, numberOfChannels: 2, sampleRate: 48000 })
                    .then(function (r) { return !!r.supported; },
                          function () { return 'ERR'; });
            };
            var m = await Promise.all([
                q('vp8'), q('avc1.42E01E'), q('hvc1.1.6.L93.B0'),
                q('hev1.1.6.L93.B0'), q('vp09.00.10.08'), q('vp09.02.10.10'),
                q('av01.0.05M.08'), q('h264'), q('vp9'),
                qe('avc1.42E01E'), qe('hvc1.1.6.L93.B0'), qe('av01.0.05M.08'),
                qa('opus'), qa('pcm-s16'), qa('pcm-s20'),
            ]);
            return {
                type: typeof C.isConfigSupported,
                own: !!d,
                desc: d ? [d.enumerable, d.configurable, d.writable] : null,
                length: C.isConfigSupported.length,
                native: nativeStr(C.isConfigSupported),
                noArg: msg(),
                emptyCfg: msg({}),
                aeNoRate: amsg({ codec: 'opus', numberOfChannels: 2 }),
                vp8: m[0], avc1: m[1], hvc1: m[2], hev1: m[3], vp09: m[4],
                vp09p2: m[5], av01: m[6], h264: m[7], vp9: m[8],
                eAvc1: m[9], eHevc: m[10], eAv01: m[11],
                adOpus: m[12], adPcm: m[13], adPcmBad: m[14],
                // 品牌校验未被破坏：VideoFrame 仍是原生类
                vfNative: (function () {
                    try {
                        return typeof window.VideoFrame === 'function'
                            && /\\[native code\\]/.test(String(window.VideoFrame));
                    } catch (e) { return false; }
                })(),
            };
        } catch (e) { return 'ERR:' + e.name; }
    })();
    // ---- F-135：mediaCapabilities 收口（原型两方法，构造器/实例保持原生）----
    // 期望值锚定 Playwright WebKit 27.2 实测（见 mcDec/mcEnc 逐项注释）；
    // 三态编码 S/m/p = supported/smooth/powerEfficient。
    out.mcCap = (function () {
        var C = window.MediaCapabilities;
        var P = C && C.prototype;
        var d = P ? descOf(P, 'decodingInfo') : null;
        var e = P ? descOf(P, 'encodingInfo') : null;
        return {
            ctor: typeof C,
            inst: !!(navigator.mediaCapabilities instanceof C),
            decDesc: d ? [d.writable, d.enumerable, d.configurable] : null,
            encDesc: e ? [e.writable, e.enumerable, e.configurable] : null,
            decLen: P && P.decodingInfo ? P.decodingInfo.length : null,
            encLen: P && P.encodingInfo ? P.encodingInfo.length : null,
            ctorNative: nativeStr(C),
        };
    })();
    out.mcDec = await (async function () {
        var mc = navigator.mediaCapabilities;
        if (!mc) { return { absent: true }; }
        var t = function (cfg) {
            return mc.decodingInfo(cfg).then(function (r) {
                return (r.supported ? 'S' : '-') + (r.smooth ? 'm' : '-')
                    + (r.powerEfficient ? 'p' : '-');
            }, function (err) { return 'REJ:' + err.name + ':' + err.message; });
        };
        var V = function (ct) {
            return {type: 'file', video: {contentType: ct, width: 1280,
                    height: 720, bitrate: 2000000, framerate: 30}};
        };
        var A = function (ct) {
            return {type: 'file', audio: {contentType: ct, channels: 2,
                    samplerate: 48000, bitrate: 128000}};
        };
        var W = function (ct) {
            return {type: 'webrtc', video: {contentType: ct, width: 1280,
                    height: 720, bitrate: 2000000, framerate: 30}};
        };
        var o = {};
        o.avc1 = await t(V('video/mp4; codecs="avc1.42E01E"'));
        o.hvc1 = await t(V('video/mp4; codecs="hvc1.1.6.L93.B0"'));
        o.hev1 = await t(V('video/mp4; codecs="hev1.1.6.L93.B0"'));
        o.vp9mp4 = await t(V('video/mp4; codecs="vp09.00.10.08"'));
        o.vp9webm = await t(V('video/webm; codecs="vp9"'));
        o.av01 = await t(V('video/mp4; codecs="av01.0.05M.08"'));
        o.mp4v = await t(V('video/mp4; codecs="mp4v.20.9"'));
        o.mp4Bare = await t(V('video/mp4'));
        o.mp4a = await t(A('audio/mp4; codecs="mp4a.40.2"'));
        o.aacBare = await t(A('audio/mp4; codecs="aac"'));
        o.flac = await t(A('audio/mp4; codecs="flac"'));
        o.opusMp4 = await t(A('audio/mp4; codecs="opus"'));
        o.oggOpus = await t(A('audio/ogg; codecs="opus"'));
        o.alac = await t(A('audio/mp4; codecs="alac"'));
        o.unqAvc1 = await t(V('video/mp4;codecs=avc1.42E01E'));
        o.unqTrail = await t(V('video/mp4;codecs=avc1.42E01E;x=1'));
        o.noArg = await mc.decodingInfo().then(function () { return 'no-throw'; },
            function (err) { return 'REJ:' + err.name + ':' + err.message; });
        o.badRecv = await window.MediaCapabilities.prototype.decodingInfo.call(
            {}, V('video/mp4')).then(
            function () { return 'no-throw'; },
            function (err) { return 'REJ:' + err.name + ':' + err.message; });
        // WebRTC 解码走 LibWebRTCProvider 的接收能力表（与 file 路径不同源）
        o.rtcVp8 = await t(W('video/VP8'));
        o.rtcVp9 = await t(W('video/VP9'));
        o.rtcAv1 = await t(W('video/AV1'));
        o.rtcH264 = await t(W('video/H264'));
        o.rtcH265 = await t(W('video/H265'));
        o.rtcFlexfec = await t(W('video/flexfec-03'));
        return o;
    })();
    out.mcEnc = await (async function () {
        var mc = navigator.mediaCapabilities;
        if (!mc) { return { absent: true }; }
        var t = function (cfg) {
            return mc.encodingInfo(cfg).then(function (r) {
                return (r.supported ? 'S' : '-') + (r.smooth ? 'm' : '-')
                    + (r.powerEfficient ? 'p' : '-');
            }, function (err) { return 'REJ:' + err.name + ':' + err.message; });
        };
        var V = function (ct) {
            return {type: 'webrtc', video: {contentType: ct, width: 1280,
                    height: 720, bitrate: 2000000, framerate: 30}};
        };
        var o = {};
        o.h264 = await t(V('video/H264'));
        o.h265 = await t(V('video/H265'));
        o.vp8 = await t(V('video/VP8'));
        o.vp9 = await t(V('video/VP9'));
        o.av1 = await t(V('video/AV1'));
        o.rtx = await t(V('video/rtx'));
        o.red = await t(V('video/red'));
        o.ulpfec = await t(V('video/ulpfec'));
        // FEC 只收不发：接收能力表有 flexfec-03，发送能力表没有
        o.flexfec = await t(V('video/flexfec-03'));
        o.opus = await t({type: 'webrtc', audio: {contentType: 'audio/opus',
                channels: 2, samplerate: 48000, bitrate: 128000}});
        o.record = await t({type: 'record', video: {
                contentType: 'video/mp4; codecs="avc1.42E01E"', width: 1280,
                height: 720, bitrate: 2000000, framerate: 30}});
        o.noArg = await mc.encodingInfo().then(function () { return 'no-throw'; },
            function (err) { return 'REJ:' + err.name + ':' + err.message; });
        return o;
    })();
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
    + ' uaP: navigator.userAgentData ? navigator.userAgentData.platform : null'
    + ' , connDlMax: (navigator.connection && ("downlinkMax" in navigator.connection)) ? navigator.connection.downlinkMax : "<absent>"'
    + ' , connOnType: (navigator.connection && ("ontypechange" in navigator.connection)) ? (navigator.connection.ontypechange === null ? "null" : typeof navigator.connection.ontypechange) : "<absent>"'
    + ' , glRenderer: (function () { try { var g = new OffscreenCanvas(8, 8).getContext("webgl");'
    + ' var d = g && g.getExtension("WEBGL_debug_renderer_info");'
    + ' return d ? [g.getParameter(d.UNMASKED_VENDOR_WEBGL), g.getParameter(d.UNMASKED_RENDERER_WEBGL)] : null;'
    + ' } catch (e) { return "ERR"; } })()'
    + ' , glExt: (function () { try { var g = new OffscreenCanvas(8, 8).getContext("webgl");'
    + ' if (!g) { return null; } var e = g.getSupportedExtensions() || [];'
    + ' return {count: e.length, s3tc: e.some(function (x) { return /s3tc/i.test(x); }),'
    + ' bptc: e.some(function (x) { return /bptc/i.test(x); }),'
    + ' pvrtc: e.some(function (x) { return /pvrtc/i.test(x); }),'
    + ' polygon: e.some(function (x) { return /polygon_mode/i.test(x); }),'
    + ' timer: e.some(function (x) { return /disjoint_timer_query/i.test(x); }),'
    + ' maxTex: g.getParameter(g.MAX_TEXTURE_SIZE)}; } catch (e) { return "ERR"; } })()'
    + ' , platW: (function () { var ns = ["IdleDetector","WebSocketStream","ImageDecoder","FileSystemObserver","Scheduler","scheduler","VideoDecoder","BarcodeDetector","URLPattern","WebGL2RenderingContext","Notification","PushManager","PushSubscription","PushSubscriptionOptions"];'
    + ' var o = {}; for (var i = 0; i < ns.length; i++) { try { o[ns[i]] = typeof self[ns[i]]; } catch (e) { o[ns[i]] = "ERR"; } } return o; })()'
    + ' , intlV8: (function () { try { return "v8BreakIterator" in Intl; } catch (e) { return "ERR"; } })()'
    + ' , intlOwn: (function () { try { return Object.getOwnPropertyNames(Intl).indexOf("v8BreakIterator") !== -1; } catch (e) { return "ERR"; } })()'
    // Worker 作用域补建面（F-37 / F-38）：真机 WebKit Worker 有、Chromium
    // Worker 没有的 43 个接口 + 真机 WorkerNavigator 的成员面 + 反方向删除。
    + ' , wkAdd: (function () { var ns = ["FontFaceSet","CSSUnitValue","CSSMathSum","CSSTransformValue","CSSStyleValue","AudioDecoder","VideoDecoder","AudioEncoder","VideoEncoder","ManagedMediaSource","MediaStreamTrack","NavigationPreloadManager","Report","StylePropertyMapReadOnly","BufferedChangeEvent","VideoTrackGenerator","CSSSkewX","CSSTransformComponent","ServiceWorker","ServiceWorkerContainer","ServiceWorkerRegistration"];'
    + ' var o = {}; for (var i = 0; i < ns.length; i++) { try { o[ns[i]] = typeof self[ns[i]]; } catch (e) { o[ns[i]] = "ERR"; } } return o; })()'
    + ' , wkNav: (function () { try { return Object.getOwnPropertyNames(WorkerNavigator.prototype); } catch (e) { return "ERR"; } })()'
    // F-77 D3：真机 Worker 的 navigator.serviceWorker 是 WorkerNavigator
    // 原型上的只读 getter，每次读同一个 ServiceWorkerContainer 实例；
    // GPC 在 Worker 侧不存在（页面侧才有，见 pageGpc）。
    + ' , wkSw: (function () { try {'
    + ' var s = navigator.serviceWorker;'
    + ' var d = Object.getOwnPropertyDescriptor(WorkerNavigator.prototype, "serviceWorker");'
    + ' return {tag: Object.prototype.toString.call(s),'
    + ' own: Object.getOwnPropertyNames(s),'
    + ' same: s === navigator.serviceWorker,'
    + ' ctrl: s.controller,'
    + ' d: d ? [d.enumerable, d.configurable, typeof d.get, typeof d.set,'
    + ' d.get && d.get.length] : null};'
    + ' } catch (e) { return "ERR:" + e.name; } })()'
    + ' , wkGpc: (function () { try { return "globalPrivacyControl" in WorkerNavigator.prototype; } catch (e) { return "ERR"; } })()'
    + ' , wkCss: (function () { try { var v = new self.CSSUnitValue(5, "px");'
    + ' var s = String(self.CSSStyleValue.prototype.toString); var L = String.fromCharCode(10);'
    + ' return [String(v), v.type().length, String(new self.CSSMathSum(v, new self.CSSUnitValue(2, "px"))),'
    + ' s === "function toString() {" + L + "    [native code]" + L + "}",'
    + ' self.CSSNumericArray.prototype[Symbol.iterator] === self.CSSNumericArray.prototype.values];'
    + ' } catch (e) { return "ERR:" + e.name; } })()'
    + ' , wkBrand: (function () { try { self.CSSMathSum.prototype.values.get.call({}); return "no-throw"; }'
    + ' catch (e) { return e.name + ":" + e.message; } })()'
    + ' , wkLeak: (function () { return [typeof self.__wkStage, typeof self.__wkErr]; })()'
    // F-130 / F-134：Worker 侧 ManagedMediaSource.isTypeSupported 与页面同口径
    + ' , wkMms: (function () { try { var C = self.ManagedMediaSource;'
    + ' if (typeof C !== "function") { return {absent: true}; }'
    + ' var p = function (t) { try { return C.isTypeSupported(t); } catch (e) { return "ERR:" + e.name; } };'
    + ' var d = Object.getOwnPropertyDescriptor(C, "isTypeSupported");'
    + ' return {type: typeof C.isTypeSupported, own: !!d,'
    + ' desc: d ? [d.enumerable, d.configurable, d.writable] : null,'
    + ' length: typeof C.isTypeSupported === "function" ? C.isTypeSupported.length : null,'
    + ' cases: {mp4: p("video/mp4"), mp4Codecs: p("video/mp4; codecs=avc1.42E01E"),'
    + ' webm: p("video/webm"), webmVp9: p("video/webm; codecs=vp9"),'
    + ' audioWebm: p("audio/webm"), ogg: p("audio/ogg"), upper: p("VIDEO/MP4")},'
    + ' msMp4: self.MediaSource.isTypeSupported("video/mp4"),'
    + ' msWebm: self.MediaSource.isTypeSupported("video/webm"),'
    // F-142：Worker 侧 MediaSource.isTypeSupported 必须单独包装（它不是
    // 补建出来的接口，走不到 __wkAddIfaceW），否则 Worker 仍是 Blink 口径。
    + ' msOwn: !!Object.getOwnPropertyDescriptor(self.MediaSource, "isTypeSupported"),'
    + ' msLen: self.MediaSource.isTypeSupported.length,'
    + ' msNoArg: (function () { try { self.MediaSource.isTypeSupported(); return "no-throw"; }'
    + ' catch (e) { return e.name + ":" + e.message; } })(),'
    // F-133：Worker 的 WebCodecs 是 Blink 原生全局，补建链路会整条跳过，
    // 静态 isConfigSupported 必须单独包成 WebKit 口径（异常文案 + 表）。
    + ' msVp9: self.MediaSource.isTypeSupported("video/webm; codecs=vp9")};'
    + ' } catch (e) { return "ERR:" + e.name; } })()'
    + ' , wcIcs: (function () { try {'
    + ' var C = self.VideoDecoder; if (typeof C !== "function") { return {absent: true}; }'
    + ' var d = Object.getOwnPropertyDescriptor(C, "isConfigSupported");'
    + ' var msg = function (cfg) { try { C.isConfigSupported(cfg); return "no-throw"; }'
    + ' catch (e) { return e.name + ":" + e.message; } };'
    + ' var A = self.AudioEncoder;'
    + ' var amsg = function (cfg) { try { A.isConfigSupported(cfg); return "no-throw"; }'
    + ' catch (e) { return e.name + ":" + e.message; } };'
    + ' return {type: typeof C.isConfigSupported, own: !!d,'
    + ' desc: d ? [d.enumerable, d.configurable, d.writable] : null,'
    + ' length: C.isConfigSupported.length,'
    + ' noArg: msg(), emptyCfg: msg({}),'
    + ' aeNoRate: amsg({codec: "opus", numberOfChannels: 2}),'
    + ' wcP: ((self.__wcP = (function () {'
    + ' var E = self.VideoEncoder, A = self.AudioDecoder;'
    + ' var q = function (c) { return C.isConfigSupported({codec: c})'
    + ' .then(function (r) { return !!r.supported; }, function () { return "ERR"; }); };'
    + ' var qe = function (c) { return E.isConfigSupported('
    + ' {codec: c, width: 640, height: 480})'
    + ' .then(function (r) { return !!r.supported; }, function () { return "ERR"; }); };'
    + ' var qa = function (c) { return A.isConfigSupported('
    + ' {codec: c, numberOfChannels: 2, sampleRate: 48000})'
    + ' .then(function (r) { return !!r.supported; }, function () { return "ERR"; }); };'
    + ' return Promise.all([q("vp8"), q("avc1.42E01E"), q("hvc1.1.6.L93.B0"),'
    + ' q("hev1.1.6.L93.B0"), q("vp09.00.10.08"), q("vp09.02.10.10"),'
    + ' q("av01.0.05M.08"), q("h264"), q("vp9"),'
    + ' qe("avc1.42E01E"), qe("hvc1.1.6.L93.B0"), qe("av01.0.05M.08"),'
    + ' qa("opus"), qa("pcm-s16"), qa("pcm-s20")]).then(function (v) {'
    + ' return {vp8: v[0], avc1: v[1], hvc1: v[2], hev1: v[3], vp09: v[4],'
    + ' vp09p2: v[5], av01: v[6], h264: v[7], vp9: v[8],'
    + ' eAvc1: v[9], eHevc: v[10], eAv01: v[11],'
    + ' adOpus: v[12], adPcm: v[13], adPcmBad: v[14]}; }); })()) && 0)};'
    + ' } catch (e) { return "ERR:" + e.name; } })()'
    + ' , mediaW: (function () { var ns = ["WebKitPlaybackTargetAvailabilityEvent","WebKitMediaKeys","WebKitMediaKeySession","WebKitMediaKeyMessageEvent","WebKitMediaKeyNeededEvent","WebKitMediaKeyError","HTMLMediaElement","HTMLVideoElement"];'
    + ' var o = {}; for (var i = 0; i < ns.length; i++) { try { o[ns[i]] = typeof self[ns[i]]; } catch (e) { o[ns[i]] = "ERR"; } } return o; })()'
    + '}); Promise.all([self.__wcP]).then(function (v) {'
    + ' postMessage({wcCodecs: v[0]}); }, function () {'
    + ' postMessage({wcCodecs: "ERR"}); }); }, 300);';
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
    + ' uaP: navigator.userAgentData ? navigator.userAgentData.platform : null'
    + ' , connDlMax: (navigator.connection && ("downlinkMax" in navigator.connection)) ? navigator.connection.downlinkMax : "<absent>"'
    + ' , connOnType: (navigator.connection && ("ontypechange" in navigator.connection)) ? (navigator.connection.ontypechange === null ? "null" : typeof navigator.connection.ontypechange) : "<absent>"'
    + ' , glRenderer: (function () { try { var g = new OffscreenCanvas(8, 8).getContext("webgl");'
    + ' var d = g && g.getExtension("WEBGL_debug_renderer_info");'
    + ' return d ? [g.getParameter(d.UNMASKED_VENDOR_WEBGL), g.getParameter(d.UNMASKED_RENDERER_WEBGL)] : null;'
    + ' } catch (e) { return "ERR"; } })()'
    + ' , glExt: (function () { try { var g = new OffscreenCanvas(8, 8).getContext("webgl");'
    + ' if (!g) { return null; } var e = g.getSupportedExtensions() || [];'
    + ' return {count: e.length, s3tc: e.some(function (x) { return /s3tc/i.test(x); }),'
    + ' bptc: e.some(function (x) { return /bptc/i.test(x); }),'
    + ' pvrtc: e.some(function (x) { return /pvrtc/i.test(x); }),'
    + ' polygon: e.some(function (x) { return /polygon_mode/i.test(x); }),'
    + ' timer: e.some(function (x) { return /disjoint_timer_query/i.test(x); }),'
    + ' maxTex: g.getParameter(g.MAX_TEXTURE_SIZE)}; } catch (e) { return "ERR"; } })()'
    + ' , platW: (function () { var ns = ["IdleDetector","WebSocketStream","ImageDecoder","FileSystemObserver","Scheduler","scheduler","VideoDecoder","BarcodeDetector","URLPattern","WebGL2RenderingContext","Notification","PushManager","PushSubscription","PushSubscriptionOptions"];'
    + ' var o = {}; for (var i = 0; i < ns.length; i++) { try { o[ns[i]] = typeof self[ns[i]]; } catch (e) { o[ns[i]] = "ERR"; } } return o; })()'
    + ' , intlV8: (function () { try { return "v8BreakIterator" in Intl; } catch (e) { return "ERR"; } })()'
    + ' , intlOwn: (function () { try { return Object.getOwnPropertyNames(Intl).indexOf("v8BreakIterator") !== -1; } catch (e) { return "ERR"; } })()'
    // Worker 作用域补建面（F-37 / F-38）：真机 WebKit Worker 有、Chromium
    // Worker 没有的 43 个接口 + 真机 WorkerNavigator 的成员面 + 反方向删除。
    + ' , wkAdd: (function () { var ns = ["FontFaceSet","CSSUnitValue","CSSMathSum","CSSTransformValue","CSSStyleValue","AudioDecoder","VideoDecoder","AudioEncoder","VideoEncoder","ManagedMediaSource","MediaStreamTrack","NavigationPreloadManager","Report","StylePropertyMapReadOnly","BufferedChangeEvent","VideoTrackGenerator","CSSSkewX","CSSTransformComponent","ServiceWorker","ServiceWorkerContainer","ServiceWorkerRegistration"];'
    + ' var o = {}; for (var i = 0; i < ns.length; i++) { try { o[ns[i]] = typeof self[ns[i]]; } catch (e) { o[ns[i]] = "ERR"; } } return o; })()'
    + ' , wkNav: (function () { try { return Object.getOwnPropertyNames(WorkerNavigator.prototype); } catch (e) { return "ERR"; } })()'
    // F-77 D3：真机 Worker 的 navigator.serviceWorker 是 WorkerNavigator
    // 原型上的只读 getter，每次读同一个 ServiceWorkerContainer 实例；
    // GPC 在 Worker 侧不存在（页面侧才有，见 pageGpc）。
    + ' , wkSw: (function () { try {'
    + ' var s = navigator.serviceWorker;'
    + ' var d = Object.getOwnPropertyDescriptor(WorkerNavigator.prototype, "serviceWorker");'
    + ' return {tag: Object.prototype.toString.call(s),'
    + ' own: Object.getOwnPropertyNames(s),'
    + ' same: s === navigator.serviceWorker,'
    + ' ctrl: s.controller,'
    + ' d: d ? [d.enumerable, d.configurable, typeof d.get, typeof d.set,'
    + ' d.get && d.get.length] : null};'
    + ' } catch (e) { return "ERR:" + e.name; } })()'
    + ' , wkGpc: (function () { try { return "globalPrivacyControl" in WorkerNavigator.prototype; } catch (e) { return "ERR"; } })()'
    + ' , wkCss: (function () { try { var v = new self.CSSUnitValue(5, "px");'
    + ' var s = String(self.CSSStyleValue.prototype.toString); var L = String.fromCharCode(10);'
    + ' return [String(v), v.type().length, String(new self.CSSMathSum(v, new self.CSSUnitValue(2, "px"))),'
    + ' s === "function toString() {" + L + "    [native code]" + L + "}",'
    + ' self.CSSNumericArray.prototype[Symbol.iterator] === self.CSSNumericArray.prototype.values];'
    + ' } catch (e) { return "ERR:" + e.name; } })()'
    + ' , wkBrand: (function () { try { self.CSSMathSum.prototype.values.get.call({}); return "no-throw"; }'
    + ' catch (e) { return e.name + ":" + e.message; } })()'
    + ' , wkLeak: (function () { return [typeof self.__wkStage, typeof self.__wkErr]; })()'
    // F-130 / F-134：Worker 侧 ManagedMediaSource.isTypeSupported 与页面同口径
    + ' , wkMms: (function () { try { var C = self.ManagedMediaSource;'
    + ' if (typeof C !== "function") { return {absent: true}; }'
    + ' var p = function (t) { try { return C.isTypeSupported(t); } catch (e) { return "ERR:" + e.name; } };'
    + ' var d = Object.getOwnPropertyDescriptor(C, "isTypeSupported");'
    + ' return {type: typeof C.isTypeSupported, own: !!d,'
    + ' desc: d ? [d.enumerable, d.configurable, d.writable] : null,'
    + ' length: typeof C.isTypeSupported === "function" ? C.isTypeSupported.length : null,'
    + ' cases: {mp4: p("video/mp4"), mp4Codecs: p("video/mp4; codecs=avc1.42E01E"),'
    + ' webm: p("video/webm"), webmVp9: p("video/webm; codecs=vp9"),'
    + ' audioWebm: p("audio/webm"), ogg: p("audio/ogg"), upper: p("VIDEO/MP4")},'
    + ' msMp4: self.MediaSource.isTypeSupported("video/mp4"),'
    + ' msWebm: self.MediaSource.isTypeSupported("video/webm"),'
    // F-142：Worker 侧 MediaSource.isTypeSupported 必须单独包装（它不是
    // 补建出来的接口，走不到 __wkAddIfaceW），否则 Worker 仍是 Blink 口径。
    + ' msOwn: !!Object.getOwnPropertyDescriptor(self.MediaSource, "isTypeSupported"),'
    + ' msLen: self.MediaSource.isTypeSupported.length,'
    + ' msNoArg: (function () { try { self.MediaSource.isTypeSupported(); return "no-throw"; }'
    + ' catch (e) { return e.name + ":" + e.message; } })(),'
    // F-133：Worker 的 WebCodecs 是 Blink 原生全局，补建链路会整条跳过，
    // 静态 isConfigSupported 必须单独包成 WebKit 口径（异常文案 + 表）。
    + ' msVp9: self.MediaSource.isTypeSupported("video/webm; codecs=vp9")};'
    + ' } catch (e) { return "ERR:" + e.name; } })()'
    + ' , wcIcs: (function () { try {'
    + ' var C = self.VideoDecoder; if (typeof C !== "function") { return {absent: true}; }'
    + ' var d = Object.getOwnPropertyDescriptor(C, "isConfigSupported");'
    + ' var msg = function (cfg) { try { C.isConfigSupported(cfg); return "no-throw"; }'
    + ' catch (e) { return e.name + ":" + e.message; } };'
    + ' var A = self.AudioEncoder;'
    + ' var amsg = function (cfg) { try { A.isConfigSupported(cfg); return "no-throw"; }'
    + ' catch (e) { return e.name + ":" + e.message; } };'
    + ' return {type: typeof C.isConfigSupported, own: !!d,'
    + ' desc: d ? [d.enumerable, d.configurable, d.writable] : null,'
    + ' length: C.isConfigSupported.length,'
    + ' noArg: msg(), emptyCfg: msg({}),'
    + ' aeNoRate: amsg({codec: "opus", numberOfChannels: 2}),'
    + ' wcP: ((self.__wcP = (function () {'
    + ' var E = self.VideoEncoder, A = self.AudioDecoder;'
    + ' var q = function (c) { return C.isConfigSupported({codec: c})'
    + ' .then(function (r) { return !!r.supported; }, function () { return "ERR"; }); };'
    + ' var qe = function (c) { return E.isConfigSupported('
    + ' {codec: c, width: 640, height: 480})'
    + ' .then(function (r) { return !!r.supported; }, function () { return "ERR"; }); };'
    + ' var qa = function (c) { return A.isConfigSupported('
    + ' {codec: c, numberOfChannels: 2, sampleRate: 48000})'
    + ' .then(function (r) { return !!r.supported; }, function () { return "ERR"; }); };'
    + ' return Promise.all([q("vp8"), q("avc1.42E01E"), q("hvc1.1.6.L93.B0"),'
    + ' q("hev1.1.6.L93.B0"), q("vp09.00.10.08"), q("vp09.02.10.10"),'
    + ' q("av01.0.05M.08"), q("h264"), q("vp9"),'
    + ' qe("avc1.42E01E"), qe("hvc1.1.6.L93.B0"), qe("av01.0.05M.08"),'
    + ' qa("opus"), qa("pcm-s16"), qa("pcm-s20")]).then(function (v) {'
    + ' return {vp8: v[0], avc1: v[1], hvc1: v[2], hev1: v[3], vp09: v[4],'
    + ' vp09p2: v[5], av01: v[6], h264: v[7], vp9: v[8],'
    + ' eAvc1: v[9], eHevc: v[10], eAv01: v[11],'
    + ' adOpus: v[12], adPcm: v[13], adPcmBad: v[14]}; }); })()) && 0)};'
    + ' } catch (e) { return "ERR:" + e.name; } })()'
    + ' , mediaW: (function () { var ns = ["WebKitPlaybackTargetAvailabilityEvent","WebKitMediaKeys","WebKitMediaKeySession","WebKitMediaKeyMessageEvent","WebKitMediaKeyNeededEvent","WebKitMediaKeyError","HTMLMediaElement","HTMLVideoElement"];'
    + ' var o = {}; for (var i = 0; i < ns.length; i++) { try { o[ns[i]] = typeof self[ns[i]]; } catch (e) { o[ns[i]] = "ERR"; } } return o; })()'
    + '}); Promise.all([self.__wcP]).then(function (v) {'
    + ' postMessage({wcCodecs: v[0]}); }, function () {'
    + ' postMessage({wcCodecs: "ERR"}); }); }, 300);';
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

# iOS Chrome POST 重放面（B1-7）：真机 CriOS 的 POST 导航由宿主重放，
# 最终文档的 window 上带 __crPostRequestWorkaround 命名空间 + 两个回执通道；
# GET 文档上这三者都不存在。表单提交是唯一能产生真实 POST 导航的途径。
POST_FORM_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<form id="pf" method="POST" action="/post-target">
<input name="a" value="1"><button type="submit">go</button></form>
</body></html>"""
POST_TARGET_HTML = """<!doctype html><html><body>posted</body></html>"""
CRIOS_POST_JS = """() => {
  const mh = window.webkit && window.webkit.messageHandlers;
  const ns = window.__crPostRequestWorkaround;
  const fn = ns && ns.runPostRequest;
  return {
    path: location.pathname,
    present: typeof ns !== 'undefined',
    type: typeof ns,
    keys: ns ? Object.keys(ns) : null,
    runLen: typeof fn === 'function' ? fn.length : null,
    runNative: typeof fn === 'function' ? /\\[native code\\]/.test(String(fn)) : null,
    chanSuccess: mh ? ('POSTSuccessHandler' in mh) : null,
    chanError: mh ? ('POSTErrorHandler' in mh) : null,
    chanInvoke: mh ? ('crwebinvoke' in mh) : null,
    chanOwn: mh ? Object.getOwnPropertyNames(mh) : null,
    mhProtoOwn: mh ? Object.getOwnPropertyNames(Object.getPrototypeOf(mh)) : null,
    flagLeak: Object.prototype.hasOwnProperty.call(window, '__fpwbPostNav'),
    runSrc: typeof fn === 'function' ? String(fn) : null,
    cookie: document.cookie,
  };
}"""

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
    3) 触摸拖拽剖面（P2-4）：真机拖拽的 touchmove 带速度/压力曲线，松手后
       内核按松手速度发起原生 fling。这里用 app/mobile_input.py 的真实路径
       拖一次，读 event.timeStamp 间隔、force 序列与松手后的 scrollY 增量。
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
        out["drag"] = await _drag_profile_probe(page)
    except Exception as exc:  # noqa: BLE001
        out["drag_error"] = f"{type(exc).__name__}: {exc}"[:160]
    try:
        await asyncio.wait_for(_mouse_click(page), 5)
        out["mouse_ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["mouse_error"] = type(exc).__name__
    await asyncio.sleep(0.25)
    out["mouse"] = await page.evaluate("() => (window.__ev || []).splice(0)")
    # 滚轮：触屏没有 wheel，输入代理把滚量换算成一次单指拖拽。
    # 需要可滚动页面（拖拽探针收尾时已移除自己的撑高块，这里自建）。
    try:
        await page.evaluate(
            "() => { const d = document.createElement('div');"
            " d.id = '__wheel_spacer'; d.style.cssText = 'height:4000px;width:1px';"
            " document.body.appendChild(d); window.scrollTo(0, 0); }")
        await asyncio.sleep(0.05)
        before = await page.evaluate("() => window.scrollY")
        await asyncio.wait_for(page.mouse.wheel(0, 400), 8)
        await asyncio.sleep(0.4)
        after = await page.evaluate("() => window.scrollY")
        out["wheel_delta"] = after - before
        out["wheel"] = await page.evaluate("() => (window.__ev || []).splice(0)")
    except Exception as exc:  # noqa: BLE001
        out["wheel_error"] = f"{type(exc).__name__}: {exc}"[:160]
    await page.evaluate(
        "() => { const el = document.getElementById('__mobile_input_probe');"
        " if (el) el.remove();"
        " const sp = document.getElementById('__wheel_spacer'); if (sp) sp.remove();"
        " window.scrollTo(0, 0); }")
    return out


DRAG_CAPTURE_JS = r"""() => {
  // 探针页本身只有一行文字；拖拽要读 scrollY，先撑出可滚动高度。
  // html/body 的默认 margin 会改变可拖拽范围，这里统一抹平。
  document.documentElement.style.cssText = 'margin:0;padding:0;overflow:auto';
  document.body.style.cssText = 'margin:0;padding:0';
  const spacer = document.createElement('div');
  spacer.id = '__drag_spacer';
  spacer.style.cssText = 'height:6000px;width:1px';
  document.body.appendChild(spacer);
  window.scrollTo(0, 0);
  window.__drag = [];
  document.addEventListener('touchstart', (e) => {
    window.__dragStart = e.touches[0].clientY;
  }, true);
  document.addEventListener('touchmove', (e) => {
    const t = e.touches[0];
    // 压力/接触面积的成员名随平台口径不同：Chromium 是 force/radiusX，
    // WebKit（iOS 目标）只有 webkitForce/webkitRadiusX。
    window.__drag.push({ts: e.timeStamp, y: t.clientY,
                        f: t.force !== undefined ? t.force : t.webkitForce,
                        r: t.radiusX !== undefined ? t.radiusX : t.webkitRadiusX});
  }, true);
  return {h: innerHeight, sh: document.documentElement.scrollHeight};
}"""


async def _drag_profile_probe(page) -> dict:
    """触摸拖拽剖面：时间戳间隔 / 位移 / force / 松手后惯性位移。"""
    from app.mobile_input import touch_input

    out: dict = {}
    await page.evaluate(DRAG_CAPTURE_JS)
    await asyncio.sleep(0.1)
    # 起手点取视口中部、行程 600px、20 帧：每帧约 30px。帧数再少松手
    # 速度过高（fling 溢出断言量级），再多每帧位移过小、加速段被内核
    # 的 touchmove 合并吃掉。
    await touch_input(page).scroll(160, 900, 600, steps=20)
    await asyncio.sleep(0.05)
    at_release = await page.evaluate("() => window.scrollY")
    await asyncio.sleep(0.8)
    after = await page.evaluate("() => window.scrollY")
    ev = await page.evaluate("() => window.__drag || []")
    start_y = await page.evaluate("() => window.__dragStart ?? null")
    out["moves"] = len(ev)
    out["atRelease"] = at_release
    out["after"] = after
    out["fling"] = after - at_release
    out["startY"] = start_y
    out["endY"] = ev[-1]["y"] if ev else None
    if len(ev) >= 3:
        out["tsDeltas"] = [round(ev[i + 1]["ts"] - ev[i]["ts"], 1)
                           for i in range(len(ev) - 1)]
        out["dy"] = [round(ev[i + 1]["y"] - ev[i]["y"], 1)
                     for i in range(len(ev) - 1)]
        out["force"] = [round(float(e["f"]), 3) if e["f"] is not None else None
                        for e in ev]
        out["radius"] = [round(float(e["r"]), 2) if e["r"] is not None else None
                         for e in ev]
    await page.evaluate("() => { const el = document.getElementById('__drag_spacer');"
                        " if (el) el.remove(); window.scrollTo(0, 0); }")
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
    orientationGaps: null,
  };
  const oTimes = [];
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
    oTimes.push(e.timeStamp);
    if (num(e.alpha) || num(e.beta) || num(e.gamma)) S.counts.orientationNumeric++;
    S.lastOrientation = orient(e);
  }, true);
  window.addEventListener('deviceorientationabsolute', (e) => {
    S.counts.orientationAbsolute++;
    if (num(e.alpha) || num(e.beta) || num(e.gamma)) S.counts.orientationAbsoluteNumeric++;
    S.lastOrientationAbsolute = orient(e);
  }, true);
  await new Promise((r) => setTimeout(r, 1500));
  // 派发节奏：相邻事件的 timeStamp 间隔（真机传感器采样抖动的直接读数）
  const gaps = [];
  for (let i = 1; i < oTimes.length; i++) gaps.push(+(oTimes[i] - oTimes[i - 1]).toFixed(1));
  if (gaps.length) {
    const sorted = [...gaps].sort((a, b) => a - b);
    const at = (q) => sorted[Math.min(sorted.length - 1, Math.floor(q * sorted.length))];
    S.orientationGaps = { n: gaps.length, min: sorted[0], median: at(0.5),
                          p90: at(0.9), max: sorted[sorted.length - 1],
                          distinct: new Set(gaps).size };
  }
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


# 自动播放策略（F-28）：无用户激活时未静音媒体 play() 必拒，静音放行。
AUTOPLAY_JS = """async () => {
  // 1x1 静音 wav
  const WAV = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwA'
            + 'AIhYAQACABAAZGF0YQAAAAA=';
  const attempt = async (opts) => {
    const a = document.createElement('audio');
    a.src = WAV;
    if (opts.muted) { a.muted = true; }
    if (opts.playsInline) { a.playsInline = true; }
    if (opts.volume !== undefined) { a.volume = opts.volume; }
    document.body.appendChild(a);
    let r;
    try {
      r = await Promise.race([
        a.play().then(() => 'resolved',
                      (e) => 'rejected:' + e.name + ':' + e.message),
        new Promise((res) => setTimeout(() => res('pending'), 600)),
      ]);
    } catch (e) {
      r = 'sync-throw:' + e.name;
    } finally { try { a.remove(); } catch (e) {} }
    return r;
  };
  const act = navigator.userActivation;
  return {
    active: !!(act && act.isActive),
    plain: await attempt({}),
    muted: await attempt({muted: true, playsInline: true}),
    volumeZero: await attempt({volume: 0}),
  };
}"""


async def autoplay_probe(ws_endpoint: str) -> dict:
    """自动播放策略（F-28）：在全新标签页上以原始 CDP 测量。

    必须绕开 Playwright：其 evaluate 固定带 userGesture=true，页面永远
    处于「有激活」状态，量不到无手势那一侧。
    """
    import itertools

    import websockets

    probe: dict = {}
    async with websockets.connect(ws_endpoint, max_size=64 * 1024 * 1024,
                                  ping_interval=None, proxy=None) as ws:
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
                       sid: str | None = None, timeout: float = 15.0) -> dict:
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
            res = await send("Target.createTarget", {"url": "about:blank"})
            target_id = (res.get("result") or {}).get("targetId")
            if not target_id:
                return {"__stage__": "createTarget", "__res__": res}
            at = await send("Target.attachToTarget",
                            {"targetId": target_id, "flatten": True})
            sid = (at.get("result") or {}).get("sessionId")
            if not sid:
                return {"__stage__": "attach", "__res__": at}
            await send("Runtime.enable", {}, sid=sid)
            # 等 mobile session 的 targetCreated 处理器把补丁挂到该 target 上
            # （与 race_test 同理，轮询一个已知补丁标记）。
            for _ in range(40):
                chk = await send("Runtime.evaluate", {
                    "expression": "typeof window.__mobilePatch === 'number'",
                    "returnByValue": True,
                }, sid=sid, timeout=3.0)
                if ((chk.get("result") or {}).get("result") or {}).get("value"):
                    break
                await asyncio.sleep(0.15)
            r = await send("Runtime.evaluate", {
                # AUTOPLAY_JS 是函数表达式；原始 CDP 不会自动调用（Playwright
                # 的 evaluate 才会），这里显式执行。
                "expression": "(" + AUTOPLAY_JS + ")()",
                "awaitPromise": True,
                "returnByValue": True, "userGesture": False,
            }, sid=sid, timeout=20.0)
            val = (((r.get("result") or {}).get("result")) or {}).get("value")
            if isinstance(val, dict):
                probe = val
            else:
                probe = {"__raw__": r}
            await send("Target.closeTarget", {"targetId": target_id})
        finally:
            rt.cancel()
    return probe


# 安全区读数：自定义属性接住 env()，未定义的变量读到空串、0 读 "0px"
SAFE_AREA_JS = """() => {
  const el = document.createElement('div');
  el.style.setProperty('--t', 'env(safe-area-inset-top)');
  el.style.setProperty('--b', 'env(safe-area-inset-bottom)');
  el.style.setProperty('--tm', 'env(safe-area-max-inset-top)');
  document.documentElement.appendChild(el);
  const cs = getComputedStyle(el);
  const out = {top: cs.getPropertyValue('--t').trim(),
               bottom: cs.getPropertyValue('--b').trim(),
               topMax: cs.getPropertyValue('--tm').trim(),
               viewportFitCover: !!document.querySelector('meta[name="viewport" i]')
                 && /viewport-fit=cover/i.test(
                      document.querySelector('meta[name="viewport" i]').content || '')};
  el.remove();
  return out;
}"""


async def safe_area_probe(page, port: int) -> dict:
    """安全区：cover 页面按机型取真机口径，普通页面为 0，跨导航不残留。"""
    out: dict = {}
    for name, path in (("cover", "/safe-area-cover"),
                       ("plain", "/safe-area-plain"),
                       ("coverAgain", "/safe-area-cover")):
        try:
            await page.goto(f"http://127.0.0.1:{port}{path}",
                            wait_until="domcontentloaded", timeout=15000)
            # 判定在 DOMContentLoaded 之后的 CDP 往返里完成，留一个短窗口
            await asyncio.sleep(0.25)
            out[name] = await page.evaluate(SAFE_AREA_JS)
        except Exception as exc:  # noqa: BLE001
            out[name] = {"error": f"{type(exc).__name__}: {exc}"[:120]}
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


# F-43：各 locale 的星期空格口径（真机 WebKit 实测，2026-10-21）
# 表里是「显式组合（year+month+day+weekday）是否在『日』后留空格」；
# dateStyle full 一律留空格（引擎模板自带）
_INTL_WEEK_SPACE = {
    "zh-CN": True, "zh-Hans-CN": True, "zh-SG": True, "zh-MO": True,
    "zh-HK": True, "zh-Hant-HK": True, "ja-JP": True,
    "zh-TW": False, "zh-Hant": False,
}
# 真机里窄星期（narrow）也有空格的 locale
_INTL_NARROW_SPACE = {
    "zh-CN", "zh-Hans-CN", "zh-SG", "zh-MO", "zh-HK", "zh-Hant-HK",
}
# F-43：真机时区名位置口径（显式 timeZoneName / dateStyle full 两条路径）
# 2026-10-20T06:05:09Z @ America/Los_Angeles → 本地 2026-10-19 23:05:09
_INTL_TZ_EXP = {
    "zh-CN": "GMT-7 23:05",
    "zh-SG": "GMT-7晚上11:05",
    "zh-HK": "下午11:05 [PDT]",
    "zh-MO": "晚上11:05 [PDT]",
    "zh-TW": "PDT 晚上11:05",
    "zh-Hant": "PDT 晚上11:05",
    "zh-Hant-HK": "下午11:05 [PDT]",
    "ja-JP": "23:05 GMT-7",
}
_INTL_TZ_STYLE = {
    "zh-CN": "2026年10月19日 星期一 北美太平洋夏令时间23:05:09",
    "zh-SG": "2026年10月19日 星期一 北美太平洋夏令时间晚上11:05:09",
    "zh-HK": "2026年10月19日 星期一 下午11:05:09 [北美太平洋夏令時間]",
    "zh-MO": "2026年10月19日 星期一 晚上11:05:09 [北美太平洋夏令時間]",
    "zh-TW": "2026年10月19日 星期一 太平洋夏令時間 晚上11:05:09",
    "zh-Hant": "2026年10月19日 星期一 太平洋夏令時間 晚上11:05:09",
    "zh-Hant-HK": "2026年10月19日 星期一 北美太平洋夏令時間 下午11:05:09",
    "ja-JP": "2026年10月19日 月曜日 23時05分09秒 米国太平洋夏時間",
}


def _intl_tz_ok(locales: dict) -> bool:
    """逐 locale 核对时区名位置（显式 timeZoneName / dateStyle 两条路径）。"""
    if not locales:
        return False
    for loc, want in _INTL_TZ_EXP.items():
        row = locales.get(loc)
        if not isinstance(row, dict) or row.get("tzExp") != want:
            return False
    for loc, want in _INTL_TZ_STYLE.items():
        row = locales.get(loc)
        if not isinstance(row, dict) or row.get("tzStyle") != want:
            return False
    return True


# F-43：真机 12 小时制 dayPeriod（下标 = 本地小时）
_INTL_DP_HOURS = {
    "zh-CN": ["凌晨"] * 5 + ["早上"] * 3 + ["上午"] * 4 + ["中午"]
             + ["下午"] * 6 + ["晚上"] * 5,
    "zh-TW": ["凌晨"] * 5 + ["清晨"] * 3 + ["上午"] * 4 + ["中午"]
             + ["下午"] * 6 + ["晚上"] * 5,
    "zh-HK": ["上午"] * 12 + ["下午"] * 12,
}


def _intl_locale_ok(locales: dict) -> bool:
    """逐 locale 核对「日 星期」空格：full/显式/mdy 一致，narrow 按表。"""
    if not locales:
        return False
    for loc, want in _INTL_WEEK_SPACE.items():
        row = locales.get(loc)
        if not isinstance(row, dict):
            return False
        full = row.get("dateFull", "")
        exp = row.get("exp", "")
        dy = row.get("dy", "")
        narrow = row.get("narrow", "")
        if want:
            # 真机：dateStyle 与显式组合都在「日」后留空格
            if "日 " not in full or "日 " not in exp:
                return False
            # 只有「日」而无月份时简繁把「日」换成空格（19 星期一）；
            # 日文的「19日」是日期后缀，不在此列
            if loc != "ja-JP" and "日" in dy and "日 " not in dy:
                return False
        else:
            # zh-TW/zh-Hant：dateStyle 有空格（引擎自带），显式组合没有
            if "日 " in exp or "日 " in dy:
                return False
        # 窄星期：只有表里的 locale 插空格
        if ("日 " in narrow) != (loc in _INTL_NARROW_SPACE):
            return False
    return True


def _intl_dp_ok(rows_by_loc: dict) -> bool:
    if not rows_by_loc:
        return False
    for loc, want in _INTL_DP_HOURS.items():
        got = rows_by_loc.get(loc)
        if not isinstance(got, list) or len(got) != 24:
            return False
        if list(got) != want:
            return False
    return True


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
                                      ping_interval=None, proxy=None) as ws:
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


HEAL_PROBE_JS = (
    "(() => ({ touch: navigator.maxTouchPoints,"
    # iPad 的 UA 既不含 Android 也不含 iPhone——漏掉会让 iPad 机型误判
    " uaMobile: /Android|iPhone|iPad/.test(navigator.userAgent),"
    " coarse: matchMedia('(pointer: coarse)').matches,"
    " plat: (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform }))()"
)


async def heal_test(manager, profile_id: str, ws_endpoint: str) -> dict:
    """断线自愈回归：仿真通道静默断开 → 看门狗自动重连 → 全页面重放。

    复现事故形态（检测站 12 项失真的共同根因）：CDP 连接断开后内核会还原
    全部 Emulation 覆盖、文档起点补丁脚本随之失效，此后打开的标签页完全
    裸奔（宿主 GPU / maxTouchPoints=0 / pointer:fine）；已存在文档只剩 JS
    层补丁，媒体查询回落宿主值。看门狗必须在秒级自动重连，并把仿真与补丁
    重新覆盖到所有已存在页面（含断线期间新开的标签页）。
    """
    import itertools

    import websockets

    out: dict = {"state_before": manager.emulation_state(profile_id)}
    inst = manager.get_instance(profile_id)
    sess = (getattr(inst, "extra", None) or {}).get("mobile_session") if inst else None
    if sess is None or not hasattr(sess, "_watchdog"):
        out["error"] = "未找到带看门狗的移动仿真会话"
        return out

    # 断开：模拟通道静默死亡。读循环立即标记不健康（看门狗 1s 内进入重连）
    out["verified_before"] = getattr(sess, "emulation_verified", None)
    await sess._ws.close()
    await asyncio.sleep(0.1)
    out["state_down"] = manager.emulation_state(profile_id)

    async with websockets.connect(ws_endpoint, max_size=64 * 1024 * 1024,
                                  ping_interval=None, proxy=None) as ws:
        ids = itertools.count(1)
        pending: dict = {}

        async def reader():
            async for raw in ws:
                msg = json.loads(raw)
                mid = msg.get("id")
                fut = pending.pop(mid, None) if mid is not None else None
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

        async def read_page(target_id: str) -> dict:
            at = await send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            sid = (at.get("result") or {}).get("sessionId")
            if not sid:
                return {}
            try:
                # 其他客户端（Playwright / 会话）可能把新目标门控在暂停态
                await send("Runtime.runIfWaitingForDebugger", {}, sid=sid, timeout=2.0)
                r = await send("Runtime.evaluate",
                               {"expression": HEAL_PROBE_JS, "returnByValue": True},
                               sid=sid, timeout=3.0)
                return (((r.get("result") or {}).get("result")) or {}).get("value") or {}
            finally:
                await send("Target.detachFromTarget", {"sessionId": sid}, timeout=2.0)

        try:
            # 断线期间新开标签页：此刻无门控、无补丁 → 裸奔（事故形态）
            res = await send("Target.createTarget", {"url": "about:blank"})
            outage_tid = (res.get("result") or {}).get("targetId") or ""
            out["outage_bare"] = await read_page(outage_tid) if outage_tid else {}

            # 等看门狗重连（探活间隔 10s 上限 + 连接耗时）
            for _ in range(40):
                await asyncio.sleep(0.5)
                if sess.healthy:
                    break
            out["healthy"] = bool(sess.healthy)
            out["reconnects"] = int(getattr(sess, "reconnect_count", 0))
            out["ua_guard_count"] = int(getattr(sess, "ua_guard_count", 0))
            out["state_after"] = manager.emulation_state(profile_id)
            await asyncio.sleep(0.5)
            out["outage_healed"] = await read_page(outage_tid) if outage_tid else {}
            tl = (await send("Target.getTargets")).get("result", {}).get("targetInfos", [])
            healed_main: dict = {}
            for t in tl:
                if t.get("type") == "page" and t.get("targetId") != outage_tid:
                    healed_main = await read_page(t["targetId"])
                    break
            out["main_healed"] = healed_main
            if outage_tid:
                await send("Target.closeTarget", {"targetId": outage_tid}, timeout=2.0)
        finally:
            rt.cancel()
    return out


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
    # 竞态/自愈测的是仿真会话本身，走内核直连端点（绕过输入代理）
    direct = res.get("ws_endpoint_direct") or res["ws_endpoint"]
    probe = await race_test(direct)
    print(json.dumps(probe, ensure_ascii=False))

    frame_state: dict = {}
    worker_state: dict = {}
    accept_ch_state: dict = {}
    webgpu_state: dict = {}
    media_state: dict = {}
    event_state: dict = {}
    sensor_state: dict = {}
    autoplay_state: dict = {}
    safe_area_state: dict = {}
    post_state: dict = {}
    heal_state: dict = {}

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
        print(json.dumps({k: v for k, v in event_state.items() if k != "drag"},
                         ensure_ascii=False))
        print("\n== 触摸拖拽剖面（速度 / 压力 / 松手惯性）==")
        print(json.dumps(event_state.get("drag") or {}, ensure_ascii=False))

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
                elif self.path.startswith("/safe-area-cover"):
                    body = ('<!doctype html><html><head><meta name="viewport" '
                            'content="width=device-width,initial-scale=1,'
                            'viewport-fit=cover"></head><body>cover</body></html>')
                elif self.path.startswith("/safe-area-plain"):
                    body = ('<!doctype html><html><head><meta name="viewport" '
                            'content="width=device-width,initial-scale=1">'
                            '</head><body>plain</body></html>')
                elif self.path.startswith("/post-form"):
                    body = POST_FORM_HTML
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

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                # 标记 cookie 是浏览器侧种的，请求阶段的实收头里不该出现
                oopif_state.setdefault("post_cookies", []).append(
                    self.headers.get("Cookie") or "")
                data = (POST_TARGET_HTML if self.path.startswith("/post-target")
                        else "not found").encode("utf-8")
                self.send_response(200 if self.path.startswith("/post-target") else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
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
                if bundle.get("msgs") and "late" in wk and "wcCodecs" in wk:
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

            # ---- 自动播放策略（F-28）：全新页面，无用户激活 ----
            autoplay_state = await autoplay_probe(direct)
            print("\n== 自动播放策略（无手势）==")
            print(json.dumps(autoplay_state, ensure_ascii=False))

            # ---- 安全区（P2-5 后半）：env(safe-area-inset-*) 计算值 ----
            safe_area_state = await safe_area_probe(page, oopif_state["port"])
            print("\n== 安全区（cover / 普通页面 / 二次 cover）==")
            print(json.dumps(safe_area_state, ensure_ascii=False))

            # ---- 表单 POST 导航（B1-7）：对比 GET / POST 文档的 CriOS 重放面 ----
            post_state: dict = {}
            post_url = f"http://127.0.0.1:{oopif_state['port']}/post-form"
            await page.goto(post_url, wait_until="domcontentloaded")
            post_state["get_before"] = await page.evaluate(CRIOS_POST_JS)
            async with page.expect_navigation(wait_until="domcontentloaded"):
                await page.evaluate("() => document.getElementById('pf').submit()")
            post_state["post"] = await page.evaluate(CRIOS_POST_JS)
            await page.goto(post_url, wait_until="domcontentloaded")
            post_state["get_after"] = await page.evaluate(CRIOS_POST_JS)
            post_state["cookie_leak"] = any(
                "__fpwb_pn_" in c for c in (oopif_state.get("post_cookies") or []))
            print("\n== 表单 POST 导航（CriOS 重放面，GET 对照）==")
            print(json.dumps(post_state, ensure_ascii=False))

            # ---- 断线自愈回归：仿真通道静默断开 → 看门狗自动重连 → 全页面重放 ----
            print("\n== 断线自愈回归（看门狗：断开 → 自动重连 → 重放）==")
            heal_state = await heal_test(manager, profile["id"],
                                    res.get("ws_endpoint_direct") or res["ws_endpoint"])
            print(json.dumps(heal_state, ensure_ascii=False))
        finally:
            oopif_server.shutdown()
    finally:
        await pw.stop()
        await manager.stop(profile["id"])

    gl = state.get("webgl") or {}
    is_android = target_os == "android"
    ev_touch = [str(e) for e in (event_state.get("touch") or [])]
    ev_mouse = [str(e) for e in (event_state.get("mouse") or [])]
    ev_wheel = [str(e) for e in (event_state.get("wheel") or [])]
    drag = event_state.get("drag") or {}
    drag_ts = drag.get("tsDeltas") or []
    drag_dy = drag.get("dy") or []
    drag_force = drag.get("force") or []

    def _ua_guard_count() -> int:
        """F-86：读仿真会话的 UA 竞态兜底次数（环境已停止 → 取 0）。"""
        sess = (heal_state or {}).get("ua_guard_count")
        return int(sess or 0)

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
        "事件层：鼠标点击应答正常（代理回包，未挂起）": (
            event_state.get("mouse_ok") is True
            and any(s.split(":", 1)[0] == "mousedown" for s in ev_mouse)),
        # ---- 输入代理（P2-3）：下游鼠标命令 → 触摸序列 ----
        "输入代理：鼠标点击 → touchstart/pointerdown:touch": (
            "touchstart" in ev_mouse and "pointerdown:touch" in ev_mouse),
        "输入代理：鼠标点击不产生 pointerdown:mouse": (
            "pointerdown:mouse" not in ev_mouse),
        "输入代理：滚轮 → 触摸拖拽（滚动量 ≥ 100px）": (
            (event_state.get("wheel_delta") or 0) >= 100
            and "touchstart" in ev_wheel and "touchmove" in ev_wheel),
        "输入代理：滚轮不产生 wheel 事件": "wheel" not in ev_wheel,
        # ---- 触摸拖拽剖面（P2-4）：速度曲线 / 压力曲线 / 原生 fling ----
        # 位移剖面：手指上滑时 clientY 递减，dy 为负；按绝对值比较。
        # 首帧位移必须明显小于匀速段（真机拖拽的起步加速段），且全程
        # 不是同一个值（等距拖拽是自动化痕迹）。视口高会改变可拖拽距离，
        # 所以阈值按「相对匀速段的比例」判定而不是绝对像素。
        "拖拽：起步加速段存在（速度剖面）": (
            len(drag_dy) >= 6
            and abs(drag_dy[0]) >= 4
            and abs(drag_dy[0]) <= 0.75 * max(abs(d) for d in drag_dy)
            and len(set(drag_dy)) >= 4),
        "拖拽：位移覆盖完整行程（首帧到末帧连续）": (
            drag.get("startY") is not None and drag.get("endY") is not None
            and abs((drag["endY"] - drag["startY"])) > 100),
        # 内核从第二次拖拽起会把 touchmove 合并到每两帧一条（33ms），
        # 首次是 60fps 一条（16.7ms）；两种都是真机采样档，只要不出现
        # 同帧连发（<8ms）即可。
        "拖拽：timeStamp 间隔落在真机采样档（8~40ms）": (
            len(drag_ts) >= 3 and all(8 <= d <= 40 for d in drag_ts)),
        "拖拽：force 随按压起伏（非恒定 1）": (
            len(set(drag_force)) >= 5 and 0 < min(drag_force) < max(drag_force) <= 1),
        "拖拽：松手后内核原生 fling（scrollY 继续增长）": (
            drag.get("fling", 0) >= 20),
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
        # 内核 --lang/acceptLanguage 只改 navigator.language，改不动 ICU 默认
        # locale；Intl 必须由 Emulation.setLocaleOverride 对齐，否则宿主中文机器
        # 上恒为 zh-CN，与欧美出口时区并存即「属地与语言不符」的跨域特征。
        "Intl locale 覆盖生效（launch.locale）": state.get("intlLocale") == "zh-CN",
        # ---- 伪造接收者防护：Object.create(实例) 上调用补丁 getter 必须抛 ----
        "伪造接收者：Navigator getter 抛 Illegal invocation": (
            bool(state.get("brandGuard"))
            and all(v == "TypeError:Illegal invocation"
                    for v in state["brandGuard"].values())),
        "伪造接收者：覆盖面齐全（含 Screen/Window）": (
            len(state.get("brandGuard") or {}) >= 12),
        # 反向保真：原生 enumerateDevices 不校验接收者，补丁不得画蛇添足
        "伪造接收者：enumerateDevices 保持原生宽松": (
            (state.get("brandLenient") or {}).get("enumerateDevices")
            in ("lenient", "absent")),
        # ---- 竞态：新标签页首个文档（含内联脚本）必须已是补丁值 ----
        "竞态：首文档 UA 为移动端": probe.get("ua") == nav["userAgent"],
        "竞态：首文档色深 24": probe.get("colorDepth") == 24,
        "竞态：首文档触控已启用": int(probe.get("touch") or 0) >= 5,
        "竞态：首文档时区已对齐": probe.get("tz") == "America/Los_Angeles",
        "竞态：首文档 GPU 已替换": (probe.get("webgl") or "") == vc.get("renderer"),
        # ---- F-86：UA 竞态兜底 ----
        # 首文档 UA 一旦落成内核默认 iOS 形态（iPad 机型表现为 UA 与
        # platform 自相矛盾），页面脚本读到的那一刻就已经错了——兜底只能在
        # 提交后补发覆盖，因此这里断言的是「兜底通道在工作且没有反复触发」：
        # 计数 ≤ 1 说明最多只遇到一次竞态，且补发后没有再次漂移。
        "F-86：UA 竞态兜底未反复触发（≤1 次）": (
            _ua_guard_count() <= 1),
        "F-86：UA 与 platform 自洽（iPad 不得出现 iPhone 形态）": (
            not ("iPhone" in (state.get("userAgent") or "")
                 and state.get("platform") == "iPad")),
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
        # ---- BrowserScan 服务端 OS 判定输入：语音列表口径 ----
        # iOS：真机 Safari 无手势时为空列表；Android：真机 Chrome Android 预装
        # Google TTS，返回数十条（F-45）——空列表本身就是 Android 身份的判据。
        "speechSynthesis 语音列表符合平台口径（iOS 空 / Android 数十条）": (
            state.get("voices") == 0 if target_os == "ios"
            else 20 <= (state.get("voices") or 0) <= 80),
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
    # ---- 表单 POST 导航（B1-7）：GET / POST 文档的 CriOS 重放面对照 ----
    post = post_state.get("post") if isinstance(post_state, dict) else None
    post = post if isinstance(post, dict) else {}
    wk_main = worker_state if isinstance(worker_state, dict) else {}
    state["wkIcs"] = wk_main.get("wcIcs")
    state["wcCodecs"] = wk_main.get("wcCodecs")
    state["wkSw"] = wk_main.get("wkSw")
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
    # ---- 安全区（P2-5 后半）：cover 页面取机型口径，普通页面为 0 ----
    sa = safe_area_state if isinstance(safe_area_state, dict) else {}
    sa_cover = sa.get("cover") or {}
    sa_plain = sa.get("plain") or {}
    sa_again = sa.get("coverAgain") or {}
    sa_spec = derive_spec(fpd, target_os).get("safeArea") or {}
    # ---- 引擎面「一行判假」信号（iOS 批次）----
    eng = state.get("engine") or {}
    ns = eng.get("ns") or {}
    plat = eng.get("plat") or {}
    jsc = eng.get("jsc") or {}
    med = eng.get("media") or {}
    ntf = eng.get("notif") or {}
    tf = eng.get("touchFactory") or {}
    f98 = eng.get("cssF98") if isinstance(eng.get("cssF98"), dict) else {}
    f99 = eng.get("cssF99") if isinstance(eng.get("cssF99"), dict) else {}
    mms = state.get("mms") if isinstance(state.get("mms"), dict) else {}
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
        eng_tf = eng.get("touchFactory") or {}
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
            # ---- F-40 / F-49 / F-51：Android 侧同口径（BCD chrome_android）----
            "navigator.mediaDevices.getDisplayMedia 不存在（BCD chrome_android=false）": (
                state["mdGdm"] == "undefined"),
            "CSS.paintWorklet 存在（Chrome Android 65+）": (
                state["paintWorklet"] == "object"),
            "select/input showPicker 均存在（Chrome Android 137/122+）": (
                state["selectShowPicker"] == "function"
                and state["inputShowPicker"] == "function"),
            # ---- F-52 / F-55 / F-56：Android 侧保留 Blink 口径 ----
            "canvas webp 编码可用（Chrome Android 32+）": (
                str(state.get("canvasWebp") or "").startswith("data:image/webp")),
            # ---- F-104 / F-108 / F-109 / F-111 / F-112 / F-113：Android 保留面 ----
            "原型成员：Android 保留 Canvas 2D / HTMLMediaElement / iframe Blink 成员": (
                (state.get("protoDrop") or {}).get("canvas2d", {})
                    .get("fontKerning") is True
                and (state.get("protoDrop") or {}).get("mediaElement", {})
                    .get("controlsList") is True
                and (state.get("protoDrop") or {}).get("iframe", {})
                    .get("credentialless") is True
                and (state.get("protoDrop") or {}).get("image", {})
                    .get("sharedStorageWritable") is True),
            # ---- F-120 / F-121：Android 媒体面（BCD chrome_android）----
            # F-120 不是缺口：BCD 把 audioTracks/videoTracks 记在
            # #enable-experimental-web-platform-features 之后，真机 Chrome
            # Android 默认不暴露，仿真侧缺席才对。
            "原型成员：Android audioTracks/videoTracks 缺席（实验特性门控，非缺口）": (
                (state.get("protoDrop") or {}).get("mediaElement", {})
                    .get("audioTracks") is False
                and (state.get("protoDrop") or {}).get("mediaElement", {})
                    .get("videoTracks") is False),
            "原型成员：Android setSinkId/sinkId 缺席（桌面 Chrome 专有面）": (
                (state.get("protoDrop") or {}).get("mediaElement", {})
                    .get("setSinkId") is False
                and (state.get("protoDrop") or {}).get("mediaElement", {})
                    .get("sinkId") is False
                and (state.get("protoDrop") or {}).get("mediaElement", {})
                    .get("onsinkchange") is False),
            # ---- F-103：Capture Handle 是桌面 Chrome 专有面，Android 也缺席 ----
            "原型成员：Android Capture Handle 缺席（chrome_android=false）": (
                (state.get("protoDrop") or {}).get("mediaDevices", {})
                    .get("setCaptureHandleConfig") is False
                and (state.get("protoDrop") or {}).get("mediaTrack", {})
                    .get("getCaptureHandle") is False),
            "MediaRecorder webm 系可用（Blink 口径）": (
                state["mrWebm"] is True and state["mrWebmH264"] is True),
            "2D getContextAttributes 保留 Blink 键集（含 alpha）": (
                "alpha" in (state.get("ctx2dAttrs") or [])),
            # ---- F-43：Android 侧保持 Blink ICU 口径（真机 Chrome Android）----
            "Intl zh-CN full 无「日 星期」空格（Blink ICU）": (
                isinstance(state.get("intlZh"), dict)
                and "日星期" in state["intlZh"]["dateFull"]
                and state["intlZh"]["timeFull"] ==
                    "北美太平洋夏令时间 23:05:09"
                and state["intlZh"]["h12"].endswith("下午11:05:09")),
            # ---- F-58 / F-59：Android 侧与真机 Chrome Android 同引擎口径 ----
            "UIEvent.prototype 无 pageX/sourceCapabilities 在 Blink 位置": (
                "sourceCapabilities" in (state.get("uiEventProto") or [])
                and state.get("uiPageX") is False),
            "AnimationEvent/TransitionEvent 无 legacy animation（Blink 已移除）": (
                state.get("animHasAnimation") is False
                and state.get("transHasAnimation") is False),
            # ---- F-48：Android 的 visualViewport 与 innerHeight 相等（正确形态）----
            "visualViewport.height == innerHeight（Chrome Android 行为）": (
                isinstance(state.get("vvH"), (int, float))
                and state.get("vvH") == state.get("innerH")),
            # ---- F-46：Android 控件字体为 Roboto（非宿主 Arial）----
            "控件字体族为 Roboto（真机 Chrome Android 口径）": (
                "Roboto" in (state.get("uaFormStyles") or {})
                    .get("select", {}).get("fontFamily", "")),
            # ---- F-54：无手势 AudioContext 为 suspended ----
            "AudioContext 无手势时为 suspended（Chrome Android 自动播放策略）": (
                state.get("acStateNoGesture") == "suspended"),
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
            # ---- P2-6：downlinkMax 值域 + ontypechange 空实现 ----
            "connection.downlinkMax == 1300（LTE 档，非 Infinity）": (
                state.get("connDownlinkMax") == 1300
                and state.get("connDownlinkMaxType") == "number"),
            "connection.ontypechange 存在且默认 null": (
                state.get("connOnTypeIn") is True and state.get("connOnTypeDefault") is None),
            "ontypechange 为 get/set 访问器（与 onchange 同形）": (
                isinstance(state.get("connOnTypeDesc"), dict)
                and state["connOnTypeDesc"].get("get") is True
                and state["connOnTypeDesc"].get("set") is True
                and state["connOnTypeDesc"].get("enum") is True
                and state["connOnTypeDesc"].get("cfg") is True),
            "ontypechange 赋值回环 + 置 null": state.get("connOnTypeRoundTrip") is True,
            "Worker（页面）：downlinkMax == 1300": wk_main.get("connDlMax") == 1300,
            "Worker（页面）：ontypechange 存在且为 null": wk_main.get("connOnType") == "null",
            # ---- P1-3：Android 只校准 HEVC（hvc1 有、hev1 无），ogg/webm 保持内核原生 ----
            "canPlayType：hvc1 == 'probably'": (state.get("canPlay") or {}).get("hvc1") == "probably",
            "canPlayType：hev1 == ''": (state.get("canPlay") or {}).get("hev1") == "",
            "canPlayType：原生外观 + 坏 receiver 抛 TypeError": (
                (state.get("canPlay") or {}).get("native") is True
                and (state.get("canPlay") or {}).get("length") == 1
                and (state.get("canPlay") or {}).get("illegal") == "TypeError"),
            # ---- P2-1：WebGL 扩展集剔除桌面专属项（Android 保留 disjoint_timer）----
            "WebGL：S3TC/BPTC/polygon_mode 缺席": (
                (state.get("webglSurface") or {}).get("getExtS3tc") is None
                and not any(re.search(r"s3tc|bptc|polygon_mode", x, re.I)
                            for x in ((state.get("webglSurface") or {}).get("exts") or []))),
            "WebGL：getExtension 大小写不敏感同样返回 null": (
                (state.get("webglSurface") or {}).get("getExtCaseInsensitive") is None),
            "WebGL：移动纹理扩展保留（ASTC/ETC）": (
                (state.get("webglSurface") or {}).get("getExtAstc") is True),
            "WebGL：Android 保留 EXT_disjoint_timer_query": any(
                re.search(r"disjoint_timer_query", x, re.I)
                for x in ((state.get("webglSurface") or {}).get("exts") or [])),
            "WebGL：扩展数 ≤ 30（桌面宿主 39）": (
                ((state.get("webglSurface") or {}).get("extCount") or 99) <= 30),
            "WebGL：MAX_* 为移动档且未知枚举仍为 null": (
                (state.get("webglSurface") or {}).get("limits", {}).get("MAX_TEXTURE_SIZE") == 16384
                and (state.get("webglSurface") or {}).get("limits", {}).get("MAX_VIEWPORT_DIMS") == [16384, 16384]
                and (state.get("webglSurface") or {}).get("unknownEnum") is None),
            "WebGL：getParameter 原生形态 + 坏 receiver 抛 TypeError": (
                (state.get("webglSurface") or {}).get("getParamLength") == 1
                and (state.get("webglSurface") or {}).get("getExtListLength") == 0
                and (state.get("webglSurface") or {}).get("badReceiver") == "TypeError"),
            "WebGL2：同样剔除 S3TC": (
                (state.get("webglSurface") or {}).get("gl2HasS3tc") is False),
            "Worker（页面）：WebGL 渲染器 == 主文档（OffscreenCanvas 同口径）": (
                isinstance(wk_main.get("glRenderer"), list)
                and wk_main["glRenderer"] == [gl.get("vendor"), gl.get("renderer")]),
            "Worker（跨域 iframe）：WebGL 渲染器 == 主文档": (
                isinstance(fr_worker.get("glRenderer"), list)
                and fr_worker["glRenderer"] == [gl.get("vendor"), gl.get("renderer")]),
            "Worker（页面）：WebGL 扩展集与主文档同口径（无 S3TC/BPTC）": (
                isinstance(wk_main.get("glExt"), dict)
                and wk_main["glExt"].get("s3tc") is False
                and wk_main["glExt"].get("bptc") is False
                and wk_main["glExt"].get("count") == (state.get("webglSurface") or {}).get("extCount")),
            "Worker（跨域 iframe）：WebGL 扩展集同口径": (
                isinstance(fr_worker.get("glExt"), dict)
                and fr_worker["glExt"].get("s3tc") is False
                and fr_worker["glExt"].get("bptc") is False),
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
            # P2-10：读数刷新率 60Hz。deviceorientation 按「读数变化」派发，
            # 1.5s 观测窗内 20Hz 约 30 条、60Hz 约 90 条；但事件循环偶发停顿
            # 与内核按帧合并会成片丢事件（实测同一实现 58~68 条），所以计数
            # 只做「明显高于 20Hz」的下限，采样周期由下一条断言按中位数锁。
            "传感器：读数刷新率明显高于 20Hz（orientation 1.5s ≥ 45 条）": (
                sensor_counts.get("orientationNumeric", 0) >= 45),
            # 事件条数会被内核按帧合并、也会被偶发的长停顿拉低，单看计数
            # 分辨不出「40Hz 稳定」和「60Hz 带停顿」。中位数直接量采样周期：
            # 20Hz 档 ≈ 50ms，60Hz 档 ≈ 16.7ms（抖动 ±20% 后 13~20ms）。
            "传感器：读数间隔中位数落在 60Hz 档（≤ 20ms）": (
                0 < (sensor_state.get("orientationGaps") or {}).get("median", 999) <= 20),
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
            # ---- 引擎面锁定：Android 保持 Chromium 口径（防误删/防误补）----
            "引擎面：标准全屏 API 保留（Chrome Android 口径）": (
                eng.get("fsRequestType") == "function"
                and eng.get("fsEnabledType") == "boolean"
                and eng.get("fsElementInDoc") is True
                and eng.get("fsHandlerInDoc") is True),
            "引擎面：无 webkit 视频全屏家族（Chromium 口径）": (
                eng.get("videoEnterType") == "undefined"
                and eng.get("videoSupportsType") == "ABSENT"),
            "引擎面：无 WebKit 专属 CSS 面（touch-callout/overflow-scrolling 均 false）": (
                eng.get("cssTouchCalloutTwoArg") is False
                and eng.get("cssOscTwoArg") is False
                and eng.get("tcInStyle") is False
                and eng.get("oscInStyle") is False),
            "引擎面：传感器构造器可构造（Chromium 口径）": (
                eng.get("dmeCtorThrows") == "ok" and eng.get("doeCtorThrows") == "ok"),
            "引擎面：absolute 保留 + 无 webkitCompassHeading（Chromium 口径）": (
                eng.get("absoluteInProto") is True and eng.get("compassHeadingIn") is False),
            "引擎面：无 WebKit legacy 传感器链（createEvent → init 不可用）": (
                eng.get("legacyDOEChain") is None
                or str(eng.get("legacyDOEChain")).startswith("throw:")),
            # ---- Safari 全局面（item 13）在 Android 必须缺席（Blink 口径）----
            "引擎面：无 WebKitPoint / convertPoint / webkitCancelRequestAnimationFrame":
                (eng.get("wkp") is None
                 and eng.get("conv", {}).get("present") is False
                 and eng.get("conv", {}).get("crafType") == "undefined"),
            "引擎面：无 WebKitNamespace 家族接口对象": (
                ns.get("WN") is None and ns.get("UH") is None and ns.get("UM") is None
                and ns.get("wkType") == "undefined"),
            # ---- 平台 API 差集（桌面独有面收敛；BCD chrome_android=false）----
            "平台API：桌面独有面缺席（EyeDropper/queryLocalFonts/documentPiP/launchQueue/FileSystemObserver）": (
                all((plat.get("win") or {}).get(n) == ["undefined", False]
                    for n in ("EyeDropper", "FileSystemObserver", "queryLocalFonts",
                              "documentPictureInPicture", "launchQueue"))),
            "平台API：Android 真机存在面保持（传感器家族）": (
                all((plat.get("win") or {}).get(n) == ["function", True]
                    for n in ("Sensor", "SensorErrorEvent", "Accelerometer", "Gyroscope",
                              "LinearAccelerationSensor", "GravitySensor",
                              "AbsoluteOrientationSensor", "RelativeOrientationSensor"))),
            "平台API：Android 真机存在面保持（Chromium 专有族与 FSA 选择器）": (
                all((plat.get("win") or {}).get(n) == ["function", True]
                    for n in ("IdleDetector", "WebSocketStream", "ImageDecoder",
                              "OTPCredential", "Ink", "MediaStreamTrackGenerator",
                              "Fence", "HTMLFencedFrameElement",
                              "showSaveFilePicker", "showDirectoryPicker"))
                and (plat.get("win") or {}).get("sharedStorage") == ["object", True]
                and (plat.get("win") or {}).get("Scheduler") == ["function", True]
                and (plat.get("win") or {}).get("scheduler") == ["object", True]
                and (plat.get("doc") or {}).get("browsingTopics") == ["function", True]
                and all((plat.get("nav") or {}).get(n) == ["function", True]
                        for n in ("runAdAuction", "joinAdInterestGroup",
                                  "leaveAdInterestGroup", "deprecatedReplaceInURN",
                                  "updateAdInterestGroups"))
                and all((plat.get("nav") or {}).get(n) == ["object", True]
                        for n in ("xr", "virtualKeyboard", "ink"))
                and (plat.get("nav") or {}).get("protectedAudience") == ["object", True]),
            "平台API：Worker 侧同名差集（FileSystemObserver 缺席、其余保持）": (
                (wk_main.get("platW") or {}).get("FileSystemObserver") == "undefined"
                and all((wk_main.get("platW") or {}).get(n) == "function"
                        for n in ("IdleDetector", "WebSocketStream", "ImageDecoder",
                                  "Scheduler", "VideoDecoder", "BarcodeDetector",
                                  "URLPattern", "WebGL2RenderingContext"))
                and (wk_main.get("platW") or {}).get("scheduler") == "object"),
            "平台API：跨域 iframe Worker 同口径": (
                (fr_worker.get("platW") or {}).get("FileSystemObserver") == "undefined"
                and (fr_worker.get("platW") or {}).get("IdleDetector") == "function"),
            # WebKit 媒体面（AirPlay/EME 前缀面）是 Safari 专有，Android Chrome
            # 全无——不得因为 iOS 目标的补丁而误挂到 Android 上。
            "媒体面：WebKit 媒体面在 Android 上全缺席（未误伤）": (
                (med.get("picker") or [None, None, None])[0] == "undefined"
                and (med.get("picker") or [None, None, None])[2] is None
                and (med.get("setKeys") or [None, None, None])[0] == "undefined"
                and (med.get("wireless") or [None, None])[0] == "undefined"
                and (med.get("keys") or [None, None])[0] == "undefined"
                and med.get("presMode") is None
                and med.get("availCtor") is None and med.get("mk") is None
                and med.get("sessionProto") is None and med.get("errCodes") is None
                and all(v == "undefined"
                        for v in (wk_main.get("mediaW") or {}).values())),
            # document.createTouch 是 WebKit 专有工厂，Android Chrome 全无——
            # 且不得因为 iOS 目标的补丁把 Touch 表面改成 WebKit 口径。
            "触控工厂：Android 无 createTouch / createTouchList（未误伤）": (
                eng_tf.get("ct") == "undefined"
                and eng_tf.get("ctl") == "undefined"
                and "force" in (eng_tf.get("touchProto") or [])
                and "webkitForce" not in (eng_tf.get("touchProto") or [])),
            # Android Chrome 从不暴露安全区：显示开孔不换算成 env()，
            # cover 页面同样是 0——「声明 Android 却带安全区」反而穿帮
            "安全区：Android 恒为 0（cover 页面亦然，与真机 Chrome 一致）": (
                sa_cover.get("viewportFitCover") is True
                and sa_cover.get("top") == "0px"
                and sa_cover.get("bottom") == "0px"
                and sa_plain.get("top") == "0px"),
            # Worker 补建表只服务 iOS（WebKit Worker 面）；Android Chrome 的
            # Worker 里这些接口本就不存在，误挂会反向穿帮。WebCodecs 四件套 /
            # NavigationPreloadManager / CSSSkewX 是 Chromium Worker 原生就有的，
            # 不在断言范围内。
            "Worker：iOS 补建面在 Android 上全缺席（未误伤）": (
                all((wk_main.get("wkAdd") or {}).get(n) == "undefined"
                    for n in ("FontFaceSet", "CSSUnitValue", "CSSMathSum",
                              "CSSTransformValue", "CSSStyleValue",
                              "ManagedMediaSource", "MediaStreamTrack", "Report",
                              "StylePropertyMapReadOnly", "BufferedChangeEvent",
                              "VideoTrackGenerator", "CSSTransformComponent",
                              "ServiceWorker", "ServiceWorkerContainer"))
                and len(wk_main.get("wkAdd") or {}) == 21),
            "Worker：WorkerNavigator 未被裁剪（Chromium 面保留）": (
                isinstance(wk_main.get("wkNav"), list)
                and "connection" in wk_main["wkNav"]
                and "deviceMemory" in wk_main["wkNav"]
                and "globalPrivacyControl" not in wk_main["wkNav"]),
        })
    else:
        is_ios_chrome = extra.get("browser") == "chrome_ios"
        # F-73：版本门控后的保留面随 profile 的 iOS 版本变化——断言必须
        # 用同一份门控表推导期望，不能写死「真机存在」。
        _ios_ver = _parse_ios_version(extra.get("osVersion")) or (0,)
        _ios_at_least = lambda major, minor=0: _ios_ver >= (major, minor)
        _is_ipad = (nav.get("platform") == "iPad"
                    or "iPad" in (nav.get("userAgent") or ""))
        _av1 = _ios_av1_capable(str((extra or {}).get("model") or ""),
                                str((extra or {}).get("marketName") or ""))
        checks.update({
            # ---- Worker 作用域补建面（F-37 / F-38）----
            # Worker 不继承页面 init 脚本：检测方在 Worker 里一行
            # `'FontFaceSet' in self` 即可判假，所以补建必须发生在 Worker 全局。
            # F-85：AudioDecoder / AudioEncoder 是 Safari 26 才有的 WebCodecs
            # 音频侧，Worker 补建表同样按版本门控（低版本 profile 上必须缺席）。
            "Worker：补建接口在页面 Worker 里全部存在": (
                all(v == "function" for k, v in (wk_main.get("wkAdd") or {}).items()
                    if k not in ("AudioDecoder", "AudioEncoder"))
                and all((wk_main.get("wkAdd") or {}).get(n)
                        == ("function" if _ios_at_least(26) else "undefined")
                        for n in ("AudioDecoder", "AudioEncoder"))
                and len(wk_main.get("wkAdd") or {}) == 21),
            "Worker：跨域 iframe Worker 同口径补建": (
                all(v == "function" for k, v in (fr_worker.get("wkAdd") or {}).items()
                    if k not in ("AudioDecoder", "AudioEncoder"))
                and all((fr_worker.get("wkAdd") or {}).get(n)
                        == ("function" if _ios_at_least(26) else "undefined")
                        for n in ("AudioDecoder", "AudioEncoder"))
                and len(fr_worker.get("wkAdd") or {}) == 21),
            "Worker：WorkerNavigator 只留真机 16 个成员（serviceWorker 在、GPC 不在）": (
                isinstance(wk_main.get("wkNav"), list)
                and wk_main["wkNav"][:1] == ["constructor"]
                and wk_main["wkNav"][1:] == _IOS_WORKER_NAV_PROTO
                and wk_main.get("wkGpc") is False),
            # F-77 D3：真机 Worker 的 navigator.serviceWorker 是原型上的只读
            # getter（enumerable true、无 setter、get.length 0），每次读同一个
            # 实例；实例零自有属性、controller 为 null。
            "Worker：navigator.serviceWorker 为 SameObject 的 ServiceWorkerContainer": (
                (wk_main.get("wkSw") or {}).get("tag")
                    == "[object ServiceWorkerContainer]"
                and (wk_main.get("wkSw") or {}).get("own") == []
                and (wk_main.get("wkSw") or {}).get("same") is True
                and (wk_main.get("wkSw") or {}).get("ctrl") is None
                and (wk_main.get("wkSw") or {}).get("d") == [True, True, "function", "undefined", 0]),
            "Worker：CSS Typed OM 字符串化/iterator/原生外观与真机一致": (
                wk_main.get("wkCss") == ["5px", 1, "calc(5px + 2px)", True, True]),
            "Worker：品牌校验为 WebKit 文案": (
                wk_main.get("wkBrand")
                == "TypeError:The CSSMathSum.values getter can only be used on"
                   " instances of CSSMathSum"),
            "Worker：无调试残留（__wkStage/__wkErr 不挂在全局）": (
                wk_main.get("wkLeak") == ["undefined", "undefined"]),
            # ---- F-130 / F-134 / F-142：ManagedMediaSource 静态 isTypeSupported ----
            # 真机（Playwright WebKit 27.2）：静态方法自有的数据属性
            # {writable:true, enumerable:true, configurable:true}、length 1，
            # 无参抛 'Not enough arguments'；判定走 F-142 的 MSE 独立真值表
            # （WebM 解析器优先、否则 AVFoundation），与 canPlayType 不同源：
            # webm 容器 true、其中 vp9 false（iOS 无 SW VP9）、vp8 true；
            # ogg 容器 false（iOS 无 Ogg 解析器）；essence 大小写敏感。
            # F-123：av01 与 WebM 的 av1HardwareDecoderAvailable() 同受
            # VideoToolbox 硬解门控 → 跟随机型能力位，非恒 false。
            "ManagedMediaSource.isTypeSupported：存在且为真机形态": (
                isinstance(mms, dict) and mms.get("absent") is False
                and mms.get("type") == "function"
                and mms.get("own") is True
                and mms.get("desc") == [True, True, True, "function"]
                and mms.get("length") == 1
                and mms.get("native") is True
                and mms.get("inheritsMS") is True),
            "ManagedMediaSource.isTypeSupported：mp4/webm 容器 true、vp9/ogg/mkv false": (
                isinstance(mms, dict)
                and mms.get("cases") == {
                    "mp4": True, "mp4Codecs": True, "audioMp4": True,
                    "mp2t": True, "webm": True, "webmVp9": False,
                    "audioWebm": True, "ogg": False, "mkv": False,
                    "upper": False, "spaces": True,
                    "webmVp8": True, "webmVp9NoQ": False, "webmVp9Full": False,
                    "webmOpus": True, "mp4Dovi": True, "mp4Opus": False,
                    "mp4Flac": False, "mp4Av1": _av1}),
            # F-142 验收串（清单 §46 点名的取值，两条 MSE 面同表）：
            # vp9 false / vp8 true / opus-in-webm true / dvh1 true /
            # mp4 内 opus false（AVFoundation 不认小写 opus，只认 'Opus'）/
            # flac-in-mp4 false（MSE 与 canPlayType 不同源的关键反例）。
            "ManagedMediaSource.isTypeSupported：ledger 验收串逐条命中（F-142）": (
                isinstance(mms, dict)
                and (mms.get("cases") or {}).get("webmVp8") is True
                and (mms.get("cases") or {}).get("webmVp9NoQ") is False
                and (mms.get("cases") or {}).get("webmVp9Full") is False
                and (mms.get("cases") or {}).get("webmOpus") is True
                and (mms.get("cases") or {}).get("mp4Dovi") is True
                and (mms.get("cases") or {}).get("mp4Opus") is False
                and (mms.get("cases") or {}).get("mp4Flac") is False),
            "MediaSource.isTypeSupported：自有静态、与 ManagedMediaSource 同表（F-142）": (
                (mms.get("msLen") == 1)
                and (mms.get("msNative") is True)
                and (mms.get("msCases") == {
                    "webmVp9": False, "webmVp8": True, "webmOpus": True,
                    "ogg": False, "mp4Av1": _av1,
                    "noArg": "TypeError:Not enough arguments"})),
            # 不与原生 MediaSource.isTypeSupported 对拍：fp-chromium 内核
            # 未编入 MSE 解码器，原生面一律 false（video/mp4 也 false），
            # 在本环境里当不了真机口径的参照物。msMp4/msWebm 仅作读数保留。
            "ManagedMediaSource.isTypeSupported：无参抛 WebKit 文案、非字符串参数不抛": (
                isinstance(mms, dict)
                and mms.get("noArg") == "TypeError:Not enough arguments"
                and mms.get("undef") is False
                and mms.get("nul") is False),
            "Worker：ManagedMediaSource.isTypeSupported 与页面同口径（F-134/F-142）": (
                isinstance(wk_main.get("wkMms"), dict)
                and wk_main["wkMms"].get("type") == "function"
                and wk_main["wkMms"].get("length") == 1
                and wk_main["wkMms"].get("desc") == [True, True, True]
                and wk_main["wkMms"].get("cases") == {
                    "mp4": True, "mp4Codecs": True, "webm": True,
                    "webmVp9": False, "audioWebm": True, "ogg": False,
                    "upper": False}),
            # F-142：Worker 的 MediaSource 是原生 Blink 全局，不经过补建路径，
            # 必须单独包装；否则 Worker 面 webm 家族 true、无参文案为 Blink 版。
            "Worker：MediaSource.isTypeSupported 走同一张 MSE 真值表（F-142）": (
                isinstance(wk_main.get("wkMms"), dict)
                and wk_main["wkMms"].get("msOwn") is True
                and wk_main["wkMms"].get("msLen") == 1
                and wk_main["wkMms"].get("msNoArg")
                    == "TypeError:Not enough arguments"
                and wk_main["wkMms"].get("msMp4") is True
                and wk_main["wkMms"].get("msVp9") is False),
            # F-133：Worker 的 WebCodecs 静态面。fp-chromium 的 Worker 原生
            # 就带这四个类，补建链路（existing && !repair）整条跳过，因此
            # isConfigSupported 必须单独包一层——否则异常文案与 codec 表都是
            # Blink 口径。av01 由 WebCodecsAV1Enabled（COCOA 默认 false，
            # status: preview）门控，与机型有无 AV1 硬解无关。
            "Worker：VideoDecoder.isConfigSupported 走 WebKit 口径（F-133）": (
                isinstance(wk_main.get("wcIcs"), dict)
                and wk_main["wcIcs"].get("type") == "function"
                and wk_main["wcIcs"].get("own") is True
                and wk_main["wcIcs"].get("desc") == [True, True, True]
                and wk_main["wcIcs"].get("length") == 1
                and wk_main["wcIcs"].get("noArg")
                    == "TypeError:Not enough arguments"
                and wk_main["wcIcs"].get("emptyCfg")
                    == "TypeError:Member WebCodecsVideoDecoderConfig.codec is"
                       " required and must be an instance of DOMString"),
            "Worker：AudioEncoder.isConfigSupported 校验顺序 codec→ch→rate（F-133）": (
                isinstance(wk_main.get("wcIcs"), dict)
                and wk_main["wcIcs"].get("aeNoRate")
                    == "TypeError:Member WebCodecsAudioEncoderConfig.sampleRate is"
                       " required and must be an instance of unsigned long"),
            # F-133 表：WebKit main 的 isSupported*Codec 谓词。av01 由
            # WebCodecsAV1Enabled（COCOA 默认 false）门控——与机型 AV1 硬解
            # 无关，所以这里不按 _ios_av1_capable 分叉。
            "Worker：VideoDecoder/Encoder 的 codec 矩阵 == WebKit 谓词（F-133）": (
                isinstance(wk_main.get("wcCodecs"), dict)
                and wk_main["wcCodecs"] == {
                    "vp8": True, "avc1": True, "hvc1": True, "hev1": True,
                    "vp09": True, "vp09p2": False, "av01": False,
                    "h264": False, "vp9": False,
                    "eAvc1": True, "eHevc": True, "eAv01": False,
                    "adOpus": True, "adPcm": True, "adPcmBad": False}),
            "Worker：AudioDecoder 认 opus / pcm-s16，不认 pcm-s20（F-133）": (
                isinstance(wk_main.get("wcCodecs"), dict)
                and wk_main["wcCodecs"].get("adOpus") is True
                and wk_main["wcCodecs"].get("adPcm") is True
                and wk_main["wcCodecs"].get("adPcmBad") is False),
            "UA-CH 不存在（Safari/CriOS 均无该 API）": state["uaData"] == "ABSENT",
            "getBattery 不存在（WKWebView 无该 API）": state["hasGetBattery"] is False,
            "deviceMemory 不暴露（WKWebView 无该 API）": state["deviceMemory"] is None,
            "connection 不存在（Safari/CriOS 均无该 API）": state["hasConnection"] is False,
            "Worker（页面）：无 connection.type（WKWebView 行为）": wk_main.get("connType") == "<none>",
            "Worker（页面）：无 downlinkMax/ontypechange（整块缺席）": (
                wk_main.get("connDlMax") == "<absent>"
                and wk_main.get("connOnType") == "<absent>"),
            # ---- P2-5 后半：env(safe-area-inset-*) 计算值 ----
            "安全区：cover 页面 env() == 机型口径（top/bottom）": (
                sa_cover.get("viewportFitCover") is True
                and sa_cover.get("top") == f"{int(sa_spec.get('top', 0))}px"
                and sa_cover.get("bottom") == f"{int(sa_spec.get('bottom', 0))}px"),
            "安全区：普通页面 env() == 0（WebKit 只在 cover 下避让）": (
                sa_plain.get("viewportFitCover") is False
                and sa_plain.get("top") == "0px"
                and sa_plain.get("bottom") == "0px"),
            "安全区：跨导航不残留（二次 cover 与首次一致）": (
                sa_again.get("top") == sa_cover.get("top")
                and sa_again.get("bottom") == sa_cover.get("bottom")),
            "安全区：max-inset 同步下发（与 inset 同值）": (
                sa_cover.get("topMax") == sa_cover.get("top")),
            # ---- P1-3：iOS 真机口径（ogg/webm 整族不可播、HEVC 仅 hvc1）----
            "canPlayType：ogg/webm 全族 == ''（AVFoundation 容器表无此项）": all(
                (state.get("canPlay") or {}).get(k) == ""
                for k in ("ogg", "oggVorbis", "oggOpus", "webm", "webmVp8",
                          "webmVp9", "videoOgg")),
            "canPlayType：hvc1 两档 == 'probably'，hev1 == ''": (
                (state.get("canPlay") or {}).get("hvc1") == "probably"
                and (state.get("canPlay") or {}).get("hvc1Full") == "probably"
                and (state.get("canPlay") or {}).get("hev1") == ""),
            "canPlayType：MP4/AVC 仍可播（未误伤原生面）": (
                (state.get("canPlay") or {}).get("mp4Avc") == "probably"),
            "canPlayType：大小写不敏感命中同一表项": (
                (state.get("canPlay") or {}).get("upperCase") == ""),
            "canPlayType：原生外观 + 坏 receiver 抛 TypeError": (
                (state.get("canPlay") or {}).get("native") is True
                and (state.get("canPlay") or {}).get("length") == 1
                and (state.get("canPlay") or {}).get("illegal") == "TypeError"),
            # ---- F-132：主世界 WebCodecs 静态面（原生 Blink 类只包静态）----
            "WebCodecs：VideoDecoder.isConfigSupported 走 WebKit 文案（F-132）": (
                isinstance(state.get("wcIcs"), dict)
                and state["wcIcs"].get("type") == "function"
                and state["wcIcs"].get("own") is True
                and state["wcIcs"].get("desc") == [True, True, True]
                and state["wcIcs"].get("length") == 1
                and state["wcIcs"].get("native") is True
                and state["wcIcs"].get("noArg")
                    == "TypeError:Not enough arguments"
                and state["wcIcs"].get("emptyCfg")
                    == "TypeError:Member WebCodecsVideoDecoderConfig.codec is"
                       " required and must be an instance of DOMString"),
            "WebCodecs：AudioEncoder 校验顺序 codec→ch→rate（F-132）": (
                isinstance(state.get("wcIcs"), dict)
                and state["wcIcs"].get("aeNoRate")
                    == "TypeError:Member WebCodecsAudioEncoderConfig.sampleRate is"
                       " required and must be an instance of unsigned long"),
            "WebCodecs：codec 矩阵 == WebKit 谓词（F-132）": (
                isinstance(state.get("wcIcs"), dict)
                and {k: v for k, v in state["wcIcs"].items()
                     if k in ("vp8", "avc1", "hvc1", "hev1", "vp09", "vp09p2",
                              "av01", "h264", "vp9", "eAvc1", "eHevc",
                              "eAv01", "adOpus", "adPcm", "adPcmBad")} == {
                    "vp8": True, "avc1": True, "hvc1": True, "hev1": True,
                    "vp09": True, "vp09p2": False, "av01": False,
                    "h264": False, "vp9": False,
                    "eAvc1": True, "eHevc": True, "eAv01": False,
                    "adOpus": True, "adPcm": True, "adPcmBad": False}),
            "WebCodecs：VideoFrame 构造器保持原生（品牌校验未破坏）": (
                isinstance(state.get("wcIcs"), dict)
                and state["wcIcs"].get("vfNative") is True),
            # ---- F-135：mediaCapabilities 收口（只包原型两方法，构造器/实例留原生）----
            # 期望值锚定 Playwright WebKit 27.2 实测；AV1 两行按机型硬解门控分叉。
            "mediaCapabilities：构造器/实例原生，两方法可写可枚举可配置": (
                isinstance(state.get("mcCap"), dict)
                and state["mcCap"].get("ctor") == "function"
                and state["mcCap"].get("inst") is True
                and state["mcCap"].get("decDesc") == [True, True, True]
                and state["mcCap"].get("encDesc") == [True, True, True]
                and state["mcCap"].get("decLen") == 1
                and state["mcCap"].get("encLen") == 1
                and state["mcCap"].get("ctorNative") is True),
            "mediaCapabilities：decodingInfo 文件面 == WebKit 27.2": (
                isinstance(state.get("mcDec"), dict)
                and {k: v for k, v in state["mcDec"].items()
                     if k in ("avc1", "hvc1", "hev1", "vp9mp4", "vp9webm",
                              "mp4v", "mp4a", "aacBare", "flac", "opusMp4",
                              "oggOpus", "alac", "unqAvc1", "unqTrail")} == {
                    "avc1": "Smp", "hvc1": "Smp", "hev1": "---",
                    "vp9mp4": "Smp", "vp9webm": "---",
                    "mp4v": "Sm-", "mp4a": "S--", "aacBare": "---",
                    "flac": "S--", "opusMp4": "---", "oggOpus": "S--",
                    "alac": "S--", "unqAvc1": "Smp", "unqTrail": "Smp"}),
            "mediaCapabilities：AV1 解码按机型硬解门控（F-135）": (
                isinstance(state.get("mcDec"), dict)
                and state["mcDec"].get("av01") == ("Smp" if _av1 else "---")
                and state["mcDec"].get("rtcAv1")
                    == ("Smp" if _av1 else "S--")),
            # iOS 的 WebRTC VP9 硬件解码位恒 false（LibWebRTCProvider 默认实现），
            # 与 macOS 的 Smp 分叉，走软件解码表；flexfec-03 在接收表里。
            "mediaCapabilities：decodingInfo WebRTC 面（VP9 走 iOS 软件口径）": (
                isinstance(state.get("mcDec"), dict)
                and {k: v for k, v in state["mcDec"].items()
                     if k in ("rtcVp8", "rtcVp9", "rtcH264", "rtcH265",
                              "rtcFlexfec")} == {
                    "rtcVp8": "Sm-", "rtcVp9": "Sm-", "rtcH264": "Smp",
                    "rtcH265": "Smp", "rtcFlexfec": "S--"}),
            "mediaCapabilities：异常文案与坏 receiver 校验 == WebKit": (
                isinstance(state.get("mcDec"), dict)
                and state["mcDec"].get("mp4Bare")
                    == "REJ:TypeError:Type error"
                and state["mcDec"].get("noArg")
                    == "REJ:TypeError:Not enough arguments"
                and state["mcDec"].get("badRecv")
                    == "REJ:TypeError:Can only call MediaCapabilities.decodingInfo"
                       " on instances of MediaCapabilities"),
            # 发送能力表取自 GetRtpSenderCapabilities：没有 flexfec-03（只收不发）
            "mediaCapabilities：encodingInfo 全表 == WebKit 27.2（flexfec 只收不发）": (
                isinstance(state.get("mcEnc"), dict)
                and {k: v for k, v in state["mcEnc"].items()
                     if k in ("h264", "h265", "vp8", "vp9", "av1", "rtx",
                              "red", "ulpfec", "flexfec", "opus",
                              "record")} == {
                    "h264": "Smp", "h265": "Smp", "vp8": "S--", "vp9": "S--",
                    "av1": "S--", "rtx": "S--", "red": "S--", "ulpfec": "S--",
                    "flexfec": "---", "opus": "S--", "record": "---"}
                and state["mcEnc"].get("noArg")
                    == "REJ:TypeError:Not enough arguments"),
            # ---- F-23：iOS 走 ANGLE-Metal，扩展集与 macOS WebKit 同源 ----
            # 真机**有** S3TC/BPTC/polygon_mode（此前误按「移动 GPU 不导出」
            # 剔除，属反向判据），只有 Spectre 侧信道计时扩展被 Apple 封禁。
            "WebGL：S3TC/BPTC 等在位（iOS ANGLE-Metal 真机口径）": (
                bool((state.get("webglSurface") or {}).get("getExtS3tcBool")) is True
                and ((state.get("webglSurface") or {}).get("gl2HasS3tc") is True)),
            "WebGL：EXT_disjoint_timer_query 缺席（iOS）": (
                (state.get("webglSurface") or {}).get("hasTimerQuery") is False
                and (state.get("webglSurface") or {}).get("gl2HasTimer") is False),
            "WebGL：WEBKIT_WEBGL_compressed_texture_pvrtc 补入（iOS 家族特征）": (
                (state.get("webglSurface") or {}).get("getExtPvrtc") is True
                and (state.get("webglSurface") or {}).get("pvrtcConst") == 0x8C02
                and any("pvrtc" in x.lower()
                        for x in ((state.get("webglSurface") or {}).get("exts") or []))),
            "WebGL：移动纹理扩展保留（ASTC/ETC）": (
                (state.get("webglSurface") or {}).get("getExtAstc") is True),
            # F-16 / F-91：版本串按枚举映射为 WebKit 真值。WebGL1 的
            # SHADING_LANGUAGE_VERSION 自带 `(1.0)` 版本后缀，不是 Chromium
            # 后缀，不能被「剥尾部括号」的通用正则吃掉。
            "WebGL：VERSION / SHADING_LANGUAGE_VERSION 为 WebKit 口径": (
                (state.get("webglSurface") or {}).get("glVersion") == "WebGL 1.0"
                and (state.get("webglSurface") or {}).get("glSL")
                == "WebGL GLSL ES 1.0 (1.0)"
                and (state.get("webglSurface") or {}).get("gl2Version") == "WebGL 2.0"
                and (state.get("webglSurface") or {}).get("gl2SL") == "WebGL GLSL ES 3.00"),
            # F-22：真机 WebGLContextAttributes 只有 8 个字段
            "WebGL：getContextAttributes 仅 8 字段（无 desynchronized/xrCompatible）": (
                (state.get("webglSurface") or {}).get("ctxAttrs") == [
                    "alpha", "antialias", "depth", "failIfMajorPerformanceCaveat",
                    "powerPreference", "premultipliedAlpha", "preserveDrawingBuffer",
                    "stencil"]),
            "WebGL：MAX_* 为移动档且未知枚举仍为 null": (
                (state.get("webglSurface") or {}).get("limits", {}).get("MAX_TEXTURE_SIZE") == 16384
                and (state.get("webglSurface") or {}).get("limits", {}).get("MAX_VIEWPORT_DIMS") == [16384, 16384]
                and (state.get("webglSurface") or {}).get("unknownEnum") is None),
            "WebGL：getParameter 原生形态 + 坏 receiver 抛 TypeError": (
                (state.get("webglSurface") or {}).get("getParamLength") == 1
                and (state.get("webglSurface") or {}).get("getExtListLength") == 0
                and (state.get("webglSurface") or {}).get("badReceiver") == "TypeError"),
            "WebGL2：S3TC 同样在位（ANGLE-Metal 同源）": (
                (state.get("webglSurface") or {}).get("gl2HasS3tc") is True),
            "Worker（页面）：WebGL 渲染器 == 主文档（OffscreenCanvas 同口径）": (
                isinstance(wk_main.get("glRenderer"), list)
                and wk_main["glRenderer"] == [gl.get("vendor"), gl.get("renderer")]),
            "Worker（跨域 iframe）：WebGL 渲染器 == 主文档": (
                isinstance(fr_worker.get("glRenderer"), list)
                and fr_worker["glRenderer"] == [gl.get("vendor"), gl.get("renderer")]),
            "Worker（页面）：WebGL 扩展集与主文档同口径（S3TC 在位 / 计时缺席）": (
                isinstance(wk_main.get("glExt"), dict)
                and wk_main["glExt"].get("s3tc") is True
                and wk_main["glExt"].get("pvrtc") is True
                and wk_main["glExt"].get("timer") is False
                and wk_main["glExt"].get("count") == (state.get("webglSurface") or {}).get("extCount")),
            "Worker（跨域 iframe）：WebGL 扩展集同口径": (
                isinstance(fr_worker.get("glExt"), dict)
                and fr_worker["glExt"].get("s3tc") is True
                and fr_worker["glExt"].get("timer") is False),
            # ---- F-06：PerformanceObserver.supportedEntryTypes 静态面 ----
            "supportedEntryTypes == WebKit 8 项定序": (
                state.get("poTypes") == [
                    "event", "first-input", "largest-contentful-paint",
                    "mark", "measure", "navigation", "paint", "resource"]),
            "supportedEntryTypes：FrozenArray 语义（冻结 + 数据属性 + 写入被拒）": (
                state.get("poFrozen") is True
                and (state.get("poDesc") or {}).get("get") == "undefined"
                and (state.get("poDesc") or {}).get("writable") is False
                and (state.get("poDesc") or {}).get("enumerable") is True
                and (state.get("poDesc") or {}).get("configurable") is True
                and (isinstance(state.get("poWriteBlocked"), str)
                     and state["poWriteBlocked"].startswith('["event"'))),
            # ---- F-17：AudioContext 状态机与成员面 ----
            "AudioContext.state：无手势时为 suspended": (
                (state.get("audioState") or {}).get("state") == "suspended"),
            "AudioContext：playbackStats/sinkId/onsinkchange 缺席（Safari 未实现）": (
                (state.get("audioMembers") or {}).get("sinkId") is False
                and (state.get("audioMembers") or {}).get("setSinkId") is False
                and (state.get("audioMembers") or {}).get("onsinkchange") is False
                and (state.get("audioMembers") or {}).get("playbackStats") is False
                and (state.get("audioMembers") or {}).get("basePlaybackStats") is False),
            # ---- F-44（修正 F-18）：iPhone 自然方向是竖屏 ----
            # WebKit naturalScreenOrientationType()：iPad → landscape-primary，
            # 其余 iOS → portrait-primary；angle 按该表查，所以 iPhone 竖屏
            # 恒 0、iPad 竖持恒 90。旧口径写死 90 会造出
            # 「portrait-primary × angle=90」这种真机不存在的组合（自洽性即
            # 一行判据）；反过来对 iPad 断言 0 也会误报——自然方向是横屏。
            "screen.orientation.angle 与设备自然方向一致（iPhone 0 / iPad 90）": (
                state.get("orientAngle")
                == (90 if extra.get("deviceType") == "tablet" else 0)
                and (state.get("orientType") or "").startswith("portrait")),
            # ---- F-19：container-only 查询为 maybe，codecs 查询走扩展判定 ----
            "canPlayType：容器名查询 == 'maybe'（无 codecs 不判死）": all(
                (state.get("canPlayIos") or {}).get(k) == "maybe"
                for k in ("mpeg", "aac", "mp3", "flac", "quicktime", "hls")),
            "canPlayType：codecs 查询 == 'probably'（AVFoundation 可解码）": all(
                (state.get("canPlayIos") or {}).get(k) == "probably"
                for k in ("mp3Codec", "flacMp4", "mp4a405", "dvh1", "avc1Codec")),
            "canPlayType：audio/flac 带 codecs 反而不可播（WebKit 实测 ''）": (
                (state.get("canPlayIos") or {}).get("flacCodec") == ""),
            "canPlayType：AV1 按机型能力位 / video-h264 == ''": (
                (state.get("canPlayIos") or {}).get("av01") == (
                    "probably" if _ios_av1_capable(
                        str((extra or {}).get("model") or ""),
                        str((extra or {}).get("marketName") or "")) else "")
                and (state.get("canPlayIos") or {}).get("unknownH264") == ""
                and (state.get("canPlayIos") or {}).get("emptyStr") == ""),
            # ---- F-143：兜底改白名单语义（表外不再透传宿主 Blink）----
            # 旧实现这几条全部继承 Blink：opus/mp3-in-mp4 → probably、
            # x-matroska → maybe、hev1 版本化串 → probably（F-124）。
            # 真机口径来自 WebKit MIMETypeCache::canDecodeType：
            # opus-in-mp4 走 canDecodeExtendedType 的免责分支；mp3 只被
            # audio/mpeg 的 shouldOverrideExtendedType 特例放行；x-matroska
            # 不在 30 项容器白名单内；hev1 版本化串不被 AVFoundation 接受。
            "canPlayType（F-143）：表外串一律 ''（opus/mp3-in-mp4、mkv、hev1 版本化）": all(
                (state.get("canPlayIos") or {}).get(k) == ""
                for k in ("opusMp4", "mp3Mp4", "mkv", "mkvCodec", "hev1Full", "avi")),
            "canPlayType（F-143）：白名单容器族裸查询 == 'maybe'（F-125 整族）": all(
                (state.get("canPlayIos") or {}).get(k) == "maybe"
                for k in ("gpp2", "aiff", "basic", "xm4a", "mp2t", "caf", "aacp")),
            "canPlayType（F-143）：白名单容器 + codecs 走扩展判定": (
                (state.get("canPlayIos") or {}).get("wavePcm") == "probably"
                # audio/ogg 不在 30 项白名单内（F-125 排除表同族），裸查询为 ''
                and (state.get("canPlayIos") or {}).get("oggContainer") == ""
                and (state.get("canPlayIos") or {}).get("webmCodec") == ""),
            "canPlayType（F-143）：无参抛 Not enough arguments（WebKit 文案）": (
                (state.get("canPlayIos") or {}).get("noArg")
                == "TypeError:Not enough arguments"),
            # ---- F-20：Permissions.query 分州口径 ----
            "Permissions：加速度计/陀螺仪/midi/display-capture 抛 NotSupportedError": all(
                (state.get("perms") or {}).get(k) == "throw:NotSupportedError"
                for k in ("accelerometer", "gyroscope", "midi", "display-capture")),
            "Permissions：background-sync/clipboard/persistent-storage 抛 TypeError": all(
                (state.get("perms") or {}).get(k) == "throw:TypeError"
                for k in ("background-sync", "clipboard-read", "clipboard-write",
                          "persistent-storage", "local-fonts")),
            "Permissions：screen-wake-lock/storage-access/push == prompt（反向项）": all(
                (state.get("perms") or {}).get(k) == "prompt"
                for k in ("screen-wake-lock", "storage-access", "push")),
            # ---- F-26 / F-27：Chromium 专属原型成员缺席 ----
            "原型成员：RTCPeerConnection addStream 家族缺席": (
                (state.get("protoDrop") or {}).get("rtc", {}).get("addStream") is False
                and (state.get("protoDrop") or {}).get("rtc", {}).get("getLocalStreams") is False
                and (state.get("protoDrop") or {}).get("rtc", {}).get("createDTMFSender") is False
                and (state.get("protoDrop") or {}).get("rtc", {}).get("onaddstream") is False),
            "原型成员：FontFace 度量覆写 / HTMLInputElement.incremental 缺席": (
                (state.get("protoDrop") or {}).get("fontFace", {}).get("ascentOverride") is False
                and (state.get("protoDrop") or {}).get("fontFace", {}).get("variant") is False
                and (state.get("protoDrop") or {}).get("inputIncremental", {}).get("incremental") is False),
            "原型成员：Element.moveBefore/setHTML、Document.wasDiscarded 缺席": (
                (state.get("protoDrop") or {}).get("element", {}).get("moveBefore") is False
                and (state.get("protoDrop") or {}).get("element", {}).get("setHTML") is False
                and (state.get("protoDrop") or {}).get("document", {}).get("wasDiscarded") is False),
            "原型成员：EventTarget.when、File.lastModifiedDate、ImageData.pixelFormat 缺席": (
                (state.get("protoDrop") or {}).get("eventTarget", {}).get("when") is False
                and (state.get("protoDrop") or {}).get("file", {}).get("lastModifiedDate") is False
                and (state.get("protoDrop") or {}).get("imageData", {}).get("pixelFormat") is False),
            "原型成员：WebGL makeXRCompatible / Screen.isExtended 缺席": (
                (state.get("protoDrop") or {}).get("webgl", {}).get("makeXRCompatible") is False
                and (state.get("protoDrop") or {}).get("screen", {}).get("isExtended") is False),
            # ---- F-106：删除表挂在 WebGL2（真实宿主），WebGL1 保持缺席 ----
            "原型成员：WebGL2RenderingContext 四项缺席（F-106 落点）": (
                all((state.get("protoDrop") or {}).get("webgl2", {}).get(m) is False
                    for m in ("makeXRCompatible", "drawingBufferStorage",
                              "drawingBufferFormat", "unpackColorSpace"))),
            # ---- F-103：Capture Handle 双端缺席（Android 也是 false） ----
            "原型成员：Capture Handle（setCaptureHandleConfig / getCaptureHandle）缺席": (
                (state.get("protoDrop") or {}).get("mediaDevices", {})
                    .get("setCaptureHandleConfig") is False
                and (state.get("protoDrop") or {}).get("mediaTrack", {})
                    .get("getCaptureHandle") is False
                and (state.get("protoDrop") or {}).get("mediaTrack", {})
                    .get("oncapturehandlechange") is False),
            # ---- F-104：Canvas 2D 六项缺席，filter 保留（iOS 18+） ----
            "原型成员：CanvasRenderingContext2D 六项 Blink 专有属性缺席": (
                all((state.get("protoDrop") or {}).get("canvas2d", {}).get(m) is False
                    for m in ("fontKerning", "fontStretch", "fontVariantCaps",
                              "isContextLost", "lang", "textRendering"))
                and (state.get("protoDrop") or {}).get("canvas2dKeep", {})
                    .get("filter") is True),
            # ---- F-105 / F-108 / F-111 / F-112 / F-113 ----
            "原型成员：零散 Blink 成员（MediaMetadata/Recorder/Keys/Request）缺席": (
                (state.get("protoDrop") or {}).get("mediaMetadata", {}).get("chapterInfo") is False
                and (state.get("protoDrop") or {}).get("mediaRecorder", {})
                    .get("audioBitrateMode") is False
                and (state.get("protoDrop") or {}).get("mediaKeys", {})
                    .get("getStatusForPolicy") is False
                and (state.get("protoDrop") or {}).get("request", {})
                    .get("isHistoryNavigation") is False
                and (state.get("protoDrop") or {}).get("request", {})
                    .get("targetAddressSpace") is False),
            "原型成员：HTMLElement 六项事件处理器与 editContext 缺席": (
                all((state.get("protoDrop") or {}).get("htmlElement", {}).get(m) is False
                    for m in ("onbeforexrselect", "oncontextlost", "oncontextrestored",
                              "onpointerrawupdate", "onscrollsnapchange",
                              "onscrollsnapchanging", "editContext",
                              "virtualKeyboardPolicy"))),
            "原型成员：HTMLMediaElement captureStream/controlsList/loading 缺席": (
                all((state.get("protoDrop") or {}).get("mediaElement", {}).get(m) is False
                    for m in ("captureStream", "controlsList", "loading"))),
            # ---- F-122：setSinkId/sinkId 按 safari_ios=18.4 门控 ----
            "原型成员：HTMLMediaElement setSinkId/sinkId 按 18.4 门控": (
                (lambda v, m: (v >= (18, 4)
                               and m.get("setSinkId") is True
                               and m.get("sinkId") is True
                               and m.get("onsinkchange") is False)
                 or (v < (18, 4)
                     and m.get("setSinkId") is False
                     and m.get("sinkId") is False))(
                    _ios_ver, (state.get("protoDrop") or {}).get("mediaElement") or {})),
            "原型成员：HTMLIFrameElement 八项 Blink/Privacy Sandbox 属性缺席": (
                all((state.get("protoDrop") or {}).get("iframe", {}).get(m) is False
                    for m in ("adAuctionHeaders", "allowPaymentRequest", "browsingTopics",
                              "credentialless", "csp", "featurePolicy", "privateToken",
                              "sharedStorageWritable"))),
            "原型成员：HTMLAreaElement / HTMLImageElement 归因族缺席": (
                (state.get("protoDrop") or {}).get("area", {})
                    .get("interestForElement") is False
                and (state.get("protoDrop") or {}).get("area", {})
                    .get("attributionSrc") is False
                and (state.get("protoDrop") or {}).get("image", {})
                    .get("sharedStorageWritable") is False),
            "原型成员：Element.startViewTransition / onbefore* / onsearch 缺席（Document 级保留）": (
                all((state.get("protoDrop") or {}).get("elementExtra", {}).get(m) is False
                    for m in ("startViewTransition", "onbeforecopy", "onbeforecut",
                              "onbeforepaste", "onsearch"))
                and ((state.get("protoDrop") or {}).get("handlerKeep") or {})
                    .get("docSVT") is True
                and ((state.get("protoDrop") or {}).get("handlerKeep") or {})
                    .get("ariaNotify") is True),
            "事件处理器：onfocusin/onbeforecopy 落点在 Element/HTMLElement": (
                # WebKit 26.5/27.2（has_touch+is_mobile 上下文）实测：五名都在
                # Element/HTMLElement 上，document.documentElement（HTMLHtmlElement
                # → HTMLElement）与 body 一样命中，Document 侧 onbefore* 也保留。
                ((state.get("protoDrop") or {}).get("handlerKeep") or {})
                    .get("focusin") is True
                and ((state.get("protoDrop") or {}).get("handlerKeep") or {})
                    .get("beforecopyBody") is True
                and ((state.get("protoDrop") or {}).get("handlerKeep") or {})
                    .get("beforecopyRoot") is True
                and ((state.get("protoDrop") or {}).get("handlerKeep") or {})
                    .get("beforecopyDoc") is True),
            # ---- F-118 / F-119：WebKit 独有事件处理器 ----
            "事件处理器：Element 上的 5 名 WebKit 专有处理器（不可枚举，root/body 继承命中）": (
                (lambda h: bool(h)
                    and all(h.get("root", {}).get(n) is True
                            and h.get("body", {}).get(n) is True
                            and h.get("own", {}).get(n) is True
                            and h.get("enum", {}).get(n) is False
                            for n in ("onbeforeload", "onwebkitneedkey",
                                      "onwebkitpresentationmodechanged",
                                      "onwebkitcurrentplaybacktargetiswirelesschanged",
                                      "onwebkitplaybacktargetavailabilitychanged")))(
                    (state.get("protoDrop") or {}).get("webkitHandlers"))),
            "事件处理器：onpageswap/onpagereveal 只落 body/frameset（root 仍 false，可枚举）": (
                (lambda h: bool(h)
                    and h.get("pageswapBody") is True
                    and h.get("pagerevealBody") is True
                    and h.get("pageswapRoot") is False
                    and h.get("pagerevealRoot") is False
                    and h.get("pageswapWindow") is True
                    and h.get("bodyOwn") is True
                    and h.get("frameOwn") is True
                    and h.get("bodyEnum") is True
                    and h.get("bodyNull") is None)(
                    (state.get("protoDrop") or {}).get("webkitHandlers"))),
            # ---- F-31：storage.estimate 为 1 GiB 且无 usageDetails ----
            "storage.estimate：quota == 1048576000 且仅 {quota, usage}": (
                isinstance(state.get("storageEstimate"), dict)
                and state["storageEstimate"].get("quota") == 1048576000
                and state["storageEstimate"].get("keys") == ["quota", "usage"]
                and state["storageEstimate"].get("usageDetails") is False),
            # ---- F-32：enumerateDevices 无 audiooutput ----
            "enumerateDevices：无 audiooutput 项（iOS 不暴露输出设备）": (
                isinstance(state.get("deviceKinds"), list)
                and "audiooutput" not in state["deviceKinds"]),
            # ---- F-90：iOS 普通浏览上下文 Notification 整族缺席 ----
            # NotificationsEnabled 的 defaultValue 在 PLATFORM(IOS_FAMILY)
            # 为 false，Notification.idl / PushManager.idl / SWR+PushAPI.idl
            # 全部 EnabledBySetting 门控——普通页连名字都不存在，F-33 的
            # permission 读值断言随之作废（仅在 standalone PWA 里才有意义）。
            "Notification 整族缺席（真机普通页 typeof === 'undefined'）": (
                (state.get("notifAbsent") or {}).get("type") == "undefined"
                and (state.get("notifAbsent") or {}).get("inWin") is False
                and (state.get("notifAbsent") or {}).get("eventType") == "undefined"),
            "Push 族缺席（PushManager/PushSubscription/PushSubscriptionOptions/PushEvent/PushMessageData）": (
                all((state.get("notifAbsent") or {}).get(k) == "undefined"
                    for k in ("pushManager", "pushSub", "pushOpts",
                              "pushEvent", "pushMsg"))),
            "window.pushManager 缺席（BCD 18.4，同 PushAPIEnabled 门控）": (
                (state.get("notifAbsent") or {}).get("winPushManager") == "undefined"),
            "ServiceWorkerRegistration.prototype.pushManager 缺席": (
                (state.get("notifAbsent") or {}).get("swrPush") is False),
            # F-102：Blink 专有 / pref 门控的五项成员同删；
            # navigationPreload 默认开，真机存在，必须保留。
            "ServiceWorkerRegistration：sync/periodicSync/backgroundFetch/paymentManager/cookies 缺席，navigationPreload 保留": (
                (state.get("notifAbsent") or {}).get("swrDrops") not in (None, "absent", "ERR")
                and not any((state.get("notifAbsent") or {}).get("swrDrops", {}).get(n)
                            for n in ("sync", "periodicSync", "backgroundFetch",
                                      "paymentManager", "cookies", "pushManager"))
                and (state.get("notifAbsent") or {}).get("swrDrops", {})
                .get("navigationPreload") is True),
            # ---- F-28：无用户手势时未静音媒体 play() 必拒（iOS 口径）----
            "自动播放：无手势未静音 play() reject NotAllowedError，静音放行": (
                autoplay_state.get("active") is False
                and (autoplay_state.get("plain") or "").startswith(
                    "rejected:NotAllowedError:")
                and autoplay_state.get("muted") == "resolved"
                and autoplay_state.get("volumeZero") == "resolved"),
            # ---- F-34：console 侧 Chromium 专有方法缺席 ----
            "console：context/createTask/memory 缺席": (
                state.get("consoleHasContext") is False
                and state.get("consoleHasCreateTask") is False
                and state.get("consoleHasMemory") is False),
            # ---- F-25：真机有、Chromium 缺的 WebKit 面（补成员机制）----
            "补成员：Canvas 遗留方法族在位且改画布状态": (
                all((state.get("addMembers") or {}).get("c2d", {}).get(k, {}).get("inn") is True
                    for k in ("setLineWidth", "setStrokeColor", "setFillColor",
                              "setLineCap", "setLineJoin", "setMiterLimit",
                              "setShadow", "clearShadow", "webkitLineDash",
                              "webkitLineDashOffset"))
                and (state.get("addMembers") or {}).get("c2dLive", {}).get("fillAfterSet")
                    == "#123456"
                and (state.get("addMembers") or {}).get("c2dLive", {}).get("dash") == []
                and (state.get("addMembers") or {}).get("c2dLive", {}).get("dashOffset") == 0
                and (state.get("addMembers") or {}).get("c2dLive", {}).get("clearShadow")
                    == [0, 0]),
            "补成员：Force Touch 常量与 webkitForce / Wheel 方向位在位": (
                (state.get("addMembers") or {}).get("mouseConst") == {
                    "own": 1, "force": 2, "proto": 1}
                and (state.get("addMembers") or {}).get("mouseLive") == 0
                and (state.get("addMembers") or {}).get("wheel", {}).get("inn") is True
                and (state.get("addMembers") or {}).get("wheel", {}).get("val") is False),
            # F-77 D4：12 个事件属性 × 4 个宿主；可枚举性只有 copy/cut/paste
            # 为 true（真机 /tmp/f77p/hosts.py）。
            "补成员：12 个事件属性在 Window/Document/HTMLElement/SVGElement 全在位": (
                all(
                    isinstance((state.get("addMembers") or {}).get("handlers", {})
                               .get("hosts", {}).get(h), dict)
                    and all(v in ("e", "ne")
                            for v in (state.get("addMembers") or {}).get("handlers", {})
                                    .get("hosts", {}).get(h, {}).values())
                    and len((state.get("addMembers") or {}).get("handlers", {})
                            .get("hosts", {}).get(h, {})) == 12
                    for h in ("win", "doc", "el", "svg"))
                and (state.get("addMembers") or {}).get("handlers", {}).get("before") == "null"
                and (state.get("addMembers") or {}).get("handlers", {}).get("assign") is True
                and (state.get("addMembers") or {}).get("handlers", {}).get("reset") is True),
            "补成员：事件属性可枚举性（copy/cut/paste 真，其余九个假）": (
                all(
                    (state.get("addMembers") or {}).get("handlers", {})
                        .get("hosts", {}).get(h, {}).get(n)
                    == ("e" if n in ("oncopy", "oncut", "onpaste") else "ne")
                    for h in ("win", "doc", "el", "svg")
                    for n in ("oncopy", "oncut", "onpaste",
                              "onwebkitmouseforcechanged", "onwebkitmouseforcedown",
                              "onwebkitmouseforceup", "onwebkitmouseforcewillbegin",
                              "ontouchforcechange", "ontouchstart", "ontouchmove",
                              "ontouchend", "ontouchcancel"))),
            "补成员：媒体面 fastSeek / audioTracks / getStartDate / 播放质量": (
                (state.get("addMembers") or {}).get("media", {}).get("fastSeek") is True
                and (state.get("addMembers") or {}).get("media", {}).get("audioTracks")
                    == "[object AudioTrackList]"
                and (state.get("addMembers") or {}).get("media", {}).get("videoTracks")
                    == "[object VideoTrackList]"
                and (state.get("addMembers") or {}).get("media", {}).get("getStartDate")
                    == "Invalid Date"
                and (state.get("addMembers") or {}).get("media", {}).get("vpqKeys") == []
                and (state.get("addMembers") or {}).get("media", {}).get("mediaGroup") == ""
                and (state.get("addMembers") or {}).get("media", {}).get("controller")
                    == "null"
                and (state.get("addMembers") or {}).get("media", {}).get("wirelessDisabled")
                    is True
                and (state.get("addMembers") or {}).get("media", {}).get("presMode") is True),
            "补成员：SourceBuffer 轨道面 / Range.compareNode 与常量": (
                all((state.get("addMembers") or {}).get("srcBuf", {}).get(k, {}).get("inn")
                    is True for k in ("audioTracks", "textTracks", "videoTracks"))
                and (state.get("addMembers") or {}).get("range", {}).get("inn") is True
                and (state.get("addMembers") or {}).get("range", {}).get("consts")
                    == [0, 1, 2, 3]
                and (state.get("addMembers") or {}).get("range", {}).get("protoConst") == 0),
            "补成员：TextMetrics em* / FontFace.width / CSSStyleDeclaration legacy": (
                (state.get("addMembers") or {}).get("metrics", {}).get("asc") is not None
                and (state.get("addMembers") or {}).get("fontFace") == {
                    "inn": True, "val": "normal"}
                and all((state.get("addMembers") or {}).get("cssDecl", {}).get(k, {}).get("inn")
                        is True for k in ("cssRules", "getPropertyCSSValue"))),
            # F-77 D3：三个全局项按真机形态补——webkitIndexedDB 是只读 getter
            # （同一对象）；openDatabase 是 get/set 访问器、未赋值读作
            # "undefined"（真机是 JSC 内部函数对象，typeof 相同，V8 造不出，
            # 退化为真 undefined）；safari 是数据属性。
            "补成员：webkitIndexedDB / openDatabase / safari / onmessageerror 等在位": (
                (state.get("addMembers") or {}).get("globals", {}).get("webkitIndexedDB")
                    == "object"
                and (state.get("addMembers") or {}).get("globals", {}).get("sameIDB") is True
                and (state.get("addMembers") or {}).get("globals", {}).get("gDesc", {})
                    .get("webkitIndexedDB") == [True, True, "function", "undefined", "undefined"]
                and (state.get("addMembers") or {}).get("globals", {}).get("openDatabase")
                    == "undefined"
                and (state.get("addMembers") or {}).get("globals", {}).get("gDesc", {})
                    .get("openDatabase") == [False, True, "function", "function", "undefined"]
                and (state.get("addMembers") or {}).get("globals", {}).get("odbNull") is True
                and (state.get("addMembers") or {}).get("globals", {}).get("odbUndef") is True
                and (state.get("addMembers") or {}).get("globals", {}).get("gDesc", {})
                    .get("safari") == [True, True, "undefined", "undefined", "object"]
                and (state.get("addMembers") or {}).get("globals", {}).get("safariTag")
                    == "[object SafariRemoteNotification]"
                and (state.get("addMembers") or {}).get("workerErr", {}).get("inn") is True),
            # F-77 / F-14：页面侧 GPC 由补丁装在 Navigator 原型上（真机是只读
            # getter、可枚举），Worker 侧不装（见 Worker 断言）。
            "补成员：navigator.globalPrivacyControl 为原生外观的只读 false": (
                (state.get("addMembers") or {}).get("globals", {}).get("gpcVal") is False
                and (state.get("addMembers") or {}).get("globals", {}).get("gpcDesc")
                    == [True, True, "function", "undefined"]
                and (state.get("addMembers") or {}).get("globals", {}).get("gpcNative")
                    is True),
            # F-84：navigator.audioSession 与 window.AudioSession 同实例。
            "补成员：navigator.audioSession 为 AudioSession 单例（与接口同源）": (
                (state.get("addMembers") or {}).get("globals", {}).get("audio", {})
                    .get("desc") == [True, True, "function", "undefined", 0]
                and (state.get("addMembers") or {}).get("globals", {}).get("audio", {})
                    .get("tag") == "[object AudioSession]"
                and (state.get("addMembers") or {}).get("globals", {}).get("audio", {})
                    .get("same") is True
                and (state.get("addMembers") or {}).get("globals", {}).get("audio", {})
                    .get("own") == []
                and (state.get("addMembers") or {}).get("globals", {}).get("audio", {})
                    .get("type") == "auto"
                and (state.get("addMembers") or {}).get("globals", {}).get("audio", {})
                    .get("inst") is True),
            # ---- F-29 / F-30：表单控件默认字体与高度（真机 WebKit 口径）----
            "补成员：表单控件默认 system-ui 字体 + 真机控件高度": (
                all(
                    (state.get("addMembers") or {}).get("globals", {})
                        .get("form", {}).get(k, {}).get("ff") == "system-ui"
                    for k in ("button", "input", "select", "textarea"))
                and (state.get("addMembers") or {}).get("globals", {})
                    .get("form", {}).get("button", {}).get("h") == 18
                and (state.get("addMembers") or {}).get("globals", {})
                    .get("form", {}).get("input", {}).get("h") == 21
                and (state.get("addMembers") or {}).get("globals", {})
                    .get("form", {}).get("textarea", {}).get("h") == 32),
            "补成员：接口对象族齐全（Media/FileSystem/CSS Typed OM/Text 轨道）": (
                all(isinstance((state.get("addMembers") or {}).get("iface", {}).get(k), dict)
                    for k in ("AudioSession", "MediaController", "CSSValue",
                              "CSSPrimitiveValue", "RGBColor", "Rect", "Counter",
                              "SVGPathSeg", "SVGViewSpec", "VTTRegion",
                              "ManagedMediaSource", "ManagedSourceBuffer",
                              "BufferedChangeEvent", "VideoTrack", "AudioTrack",
                              "VideoTrackList", "AudioTrackList",
                              "AudioTrackConfiguration", "FileSystemEntry",
                              "FileSystemDirectoryEntry", "FileSystemFileEntry",
                              "FileSystemDirectoryReader", "Report", "FontFaceSet",
                              "StylePropertyMapReadOnly", "CSSMathValue",
                              "CSSNumericValue", "CSSUnitValue", "CSSKeywordValue",
                              "CSSImageValue", "CSSTransformComponent",
                              "CSSStyleValue", "SpeechRecognitionResultList",
                              "SpeechRecognitionResult", "SpeechRecognitionAlternative",
                              "ApplePayError", "FileSystem",
                              "TextTrack", "TextTrackCue",
                              "XMLHttpRequestProgressEvent", "MathMLMathElement",
                              "DataCue", "MediaRecorderErrorEvent",
                              "OverconstrainedErrorEvent"))),
            "补成员：接口对象形态（不可枚举 / 可写 / toStringTag / 构造语义）": (
                (state.get("addMembers") or {}).get("iface", {}).get("AudioSession")
                    == {"len": 0, "ctor": "throw:TypeError:Illegal constructor",
                        "noNew": "throw:TypeError:Illegal constructor",
                        "en": False, "wr": True,
                        "tag": "[object AudioSession]",
                        "proto": ["constructor", "type"]}
                and (state.get("addMembers") or {}).get("iface", {}).get("MediaController", {})
                    .get("ctor") == "ok"
                and (state.get("addMembers") or {}).get("iface", {}).get("MediaController", {})
                    .get("noNew") == "throw:TypeError:Constructor requires 'ne"
                and (state.get("addMembers") or {}).get("iface", {}).get("FontFaceSet", {})
                    .get("len") == 1
                # 混合体：不带 new 走 args 文案，带 new 是裸 Illegal constructor
                and (state.get("addMembers") or {}).get("iface", {}).get("FontFaceSet", {})
                    .get("noNew") == "throw:TypeError:Constructor requires 'ne"
                and (state.get("addMembers") or {}).get("iface", {}).get("FontFaceSet", {})
                    .get("ctor") == "throw:TypeError:Illegal constructor"
                and (state.get("addMembers") or {}).get("iface", {}).get("CSSUnitValue", {})
                    .get("ctor") == "throw:TypeError:Not enough arguments"
                and (state.get("addMembers") or {}).get("iface", {}).get("CSSUnitValue", {})
                    .get("noNew") == "throw:TypeError:Constructor requires 'ne"
                and (state.get("addMembers") or {}).get("iface", {}).get("DataCue", {})
                    .get("ctor") == "throw:TypeError:Not enough arguments"),
            # ---- F-36：静态量（DataView.BYTES_PER_ELEMENT / RegExp legacy）----
            "静态量：DataView.BYTES_PER_ELEMENT 与 RegExp legacy 属性在位": (
                (state.get("addMembers") or {}).get("staticAdd", {}).get("dvbpe") is True
                and (state.get("addMembers") or {}).get("staticAdd", {}).get("regMultiline")
                    is True
                and (state.get("addMembers") or {}).get("staticAdd", {}).get("regDollar1")
                    is True),
            "pdfViewerEnabled == true（Safari 16.4+ 真实能力位，与插件为空不矛盾）": (
                state["hasPdfViewer"] is True and state["pdfViewerValue"] is True),
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
            # ---- F-04：navigator 残留的 Chromium 专有成员 ----
            "navigator：doNotTrack/scheduling/storageBuckets/windowControlsOverlay 均不存在": (
                state["hasDoNotTrack"] is False and state["hasScheduling"] is False
                and state["hasStorageBuckets"] is False and state["hasWco"] is False),
            # F-90：徽标 API 与 Notification/Push 同属 iOS 普通上下文缺席面
            # （AppBadgeEnabled 默认 false，BCD 的 16.4 是主屏 web app 口径）。
            # F-139：devicePosture 同属「偏好默认关」面（WebKit
            # DevicePostureAPIEnabled status: testable / defaultValue: false），
            # 出货版 Safari 全版本缺席——不是 27 门控。
            "navigator：setAppBadge / clearAppBadge / devicePosture 缺席": (
                state["hasSetAppBadge"] is False
                and state["hasClearAppBadge"] is False
                and state["hasDevicePosture"] is False),
            # F-87：真机 WebKit 无手柄返回空数组；Blink 固定 4 槽位 null。
            "navigator.getGamepads() 无手柄返回空数组（真机 WebKit 口径）": (
                state["gamepads"] == [True, 0, None, 0]),
            # ---- F-05 / F-09 / F-21：窗口侧入口接口对象与 Blink 遗留别名 ----
            "窗口：External/external/crashReport/viewport/credentialless 不存在": (
                state["gExternal"] == "undefined" and state["gCrashReport"] == "undefined"
                and state["gViewport"] == "undefined"
                and state["gCredentialless"] == "undefined"),
            "窗口：InputDeviceCapabilities / screen.isExtended 不存在": (
                state["gInputDeviceCaps"] == "undefined"
                and state["screenIsExtended"] is False),
            "窗口：自相矛盾接口对象（NavigatorUAData/NetworkInformation）不存在": (
                state["gUAData"] == "undefined" and state["gNetworkInfo"] == "undefined"),
            "窗口：BackgroundFetch/SyncManager/BeforeInstallPrompt/WCO 不存在": (
                state["gBackgroundFetch"] == "undefined"
                and state["gSyncManager"] == "undefined"
                and state["gBeforeInstall"] == "undefined"
                and state["gWco"] == "undefined"),
            "窗口：requestIdleCallback / EditContext / Sanitizer / LaunchQueue 不存在": (
                state["gIdleCb"] == "undefined" and state["gEditContext"] == "undefined"
                and state["gSanitizer"] == "undefined"
                and state["gLaunchQueue"] == "undefined"),
            "窗口：SuppressedError / DisposableStack / WebSocketError 不存在": (
                state["gSuppressedError"] == "undefined"
                and state["gDisposableStack"] == "undefined"
                and state["gWebSocketError"] == "undefined"),
            "窗口：webkit* 遗留别名族不存在（比裸名更刺眼）": (
                state["gWebkitMediaStream"] == "undefined"
                and state["gWebkitRTCPeer"] == "undefined"
                and state["gWebkitSpeechGrammar"] == "undefined"),
            "窗口：BarcodeDetector/PaymentRequest/SpeechRecognition 等按 iOS 版本门控": (
                state["gBarcodeDetector"]
                == ("function" if _ios_at_least(17) else "undefined")
                and state["gPaymentRequest"]
                == ("function" if _ios_at_least(11, 1) else "undefined")
                and state["gSpeechRecognition"]
                == ("function" if _ios_at_least(14, 1) else "undefined")
                and state["gTrackProcessor"]
                == ("function" if _ios_at_least(18) else "undefined")
                and state["gDigitalCredential"]
                == ("function" if _ios_at_least(26) else "undefined")
                # F-139：DevicePosture 由 DevicePostureAPIEnabled 偏好门控
                # （testable / 默认 false）→ 全版本缺席
                and state["gDevicePostureCtor"] == "undefined"),
            # F-82：WebGPU 整族（40 名窗口接口 + navigator.gpu）BCD safari_ios=26。
            # 低版本必须整族缺席，26+ 必须整族在册（含 navigator.gpu）。
            "窗口：WebGPU 整族按 iOS 版本门控（41 名，26+ 才有）": (
                (state["gpuFamilyCount"] == (40 if _ios_at_least(26) else 0))
                and state["hasNavigatorGpu"] is _ios_at_least(26)
                and state["gGPU"]
                == ("function" if _ios_at_least(26) else "undefined")
                and state["gGPUAdapter"]
                == ("function" if _ios_at_least(26) else "undefined")
                and state["gGPUCanvasContext"]
                == ("function" if _ios_at_least(26) else "undefined")
                and state["gGPUBufferUsage"]
                == ("object" if _ios_at_least(26) else "undefined")),
            # F-85：余量 37 名（WebTransport / Trusted Types / Navigation /
            # WebCodecs 音频侧 / CSS Position-Try / Streams BYOB / 单丁）。
            # 逐个按 BCD 精确版本判定，不按 26 一刀切——Origin 是 26.5、
            # Navigation 族是 26.2、WebTransport/BYOB 是 26.4。
            "窗口：F-85 余量族按 iOS 精确版本门控（37 名）": (
                all(
                    state["gated26"].get(n) == (want if _ios_at_least(*ver) else "undefined")
                    for n, ver, want in (
                        ("WebTransport", (26, 4), "function"),
                        ("WebTransportBidirectionalStream", (26, 4), "function"),
                        ("WebTransportDatagramDuplexStream", (26, 4), "function"),
                        ("WebTransportError", (26, 4), "function"),
                        ("ReadableByteStreamController", (26, 4), "function"),
                        ("ReadableStreamBYOBReader", (26, 4), "function"),
                        ("ReadableStreamBYOBRequest", (26, 4), "function"),
                        ("Navigation", (26, 2), "function"),
                        ("NavigateEvent", (26, 2), "function"),
                        ("NavigationActivation", (26, 2), "function"),
                        ("NavigationCurrentEntryChangeEvent", (26, 2), "function"),
                        ("NavigationDestination", (26, 2), "function"),
                        ("NavigationHistoryEntry", (26, 2), "function"),
                        ("NavigationTransition", (26, 2), "function"),
                        ("navigation", (26, 2), "object"),
                        ("CaretPosition", (26, 2), "function"),
                        ("CommandEvent", (26, 2), "function"),
                        ("EventCounts", (26, 2), "function"),
                        ("LargestContentfulPaint", (26, 2), "function"),
                        ("PerformanceEventTiming", (26, 2), "function"),
                        ("Origin", (26, 5), "function"),
                        ("TrustedHTML", (26, 0), "function"),
                        ("TrustedScript", (26, 0), "function"),
                        ("TrustedScriptURL", (26, 0), "function"),
                        ("TrustedTypePolicy", (26, 0), "function"),
                        ("TrustedTypePolicyFactory", (26, 0), "function"),
                        ("trustedTypes", (26, 0), "object"),
                        ("AudioData", (26, 0), "function"),
                        ("AudioDecoder", (26, 0), "function"),
                        ("AudioEncoder", (26, 0), "function"),
                        ("EncodedAudioChunk", (26, 0), "function"),
                        ("CSSPositionTryDescriptors", (26, 0), "function"),
                        ("CSSPositionTryRule", (26, 0), "function"),
                        ("FileSystemWritableFileStream", (26, 0), "function"),
                        ("ScrollTimeline", (26, 0), "function"),
                        ("ViewTimeline", (26, 0), "function"),
                        ("URLPattern", (26, 0), "function"),
                    ))
                and len(state["gated26"]) == 37),
            # ---- F-40 / F-49 / F-51：对象子属性与原型方法缺口 ----
            "navigator.mediaDevices.getDisplayMedia 按 iOS 版本门控（27+ 才有）": (
                state["mdGdm"] == ("function" if _ios_at_least(27) else "undefined")),
            "CSS.paintWorklet 不存在（Safari 从未实现 Paint API）": (
                state["paintWorklet"] == "undefined"),
            "select.showPicker 不存在 / input.showPicker 保留": (
                state["selectShowPicker"] == "undefined"
                and state["inputShowPicker"] == "function"),
            # ---- F-52 / F-55 / F-56：编码与上下文属性面 ----
            "canvas webp 编码回退 PNG（真机 iOS 无 webp 编码器）": (
                str(state.get("canvasWebp") or "").startswith("data:image/png")),
            # 清单 §46：webm 容器 true、vp8 / vp9,opus true、
            # x-matroska;codecs=avc1 false（容器不在白名单）。h264-in-webm
            # 同样 false（WebM 分支只认 vp8/vp9/opus/vorbis）。
            "MediaRecorder 矩阵为 WebKit 口径（webm true / h264-in-webm false）": (
                state["mrWebm"] is True and state["mrWebmH264"] is False
                and state["mrVp8"] is True and state["mrVp9Opus"] is True
                and state["mrMp4"] is True),
            "2D getContextAttributes 为真机 3 键": (
                state.get("ctx2dAttrs") == ["colorSpace", "desynchronized",
                                            "willReadFrequently"]),
            # ---- F-58 / F-59：事件原型 legacy 面 ----
            "UIEvent.prototype 含 pageX/pageY/layerX/layerY、无 sourceCapabilities": (
                all(k in (state.get("uiEventProto") or [])
                    for k in ("pageX", "pageY", "layerX", "layerY"))
                and "sourceCapabilities" not in (state.get("uiEventProto") or [])
                and state.get("uiPageX") is True),
            "AnimationEvent/TransitionEvent 带 legacy animation 属性": (
                state.get("animHasAnimation") is True
                and state.get("transHasAnimation") is True),
            # ---- F-43：zh/ja 排版口径（WebKit libicucore）----
            "Intl zh-CN full 日期段为「日 星期X」、24 小时制时区名后无空格": (
                isinstance(state.get("intlZh"), dict)
                and state["intlZh"]["dateFull"] == "2026年10月19日 星期一"
                and state["intlZh"]["full"] ==
                    "2026年10月19日 星期一 北美太平洋夏令时间23:05:09"
                and state["intlZh"]["timeFull"] == "北美太平洋夏令时间23:05:09"),
            "Intl 各 locale 星期空格口径与真机一致": (
                isinstance(state.get("intlZh"), dict)
                and _intl_locale_ok(state["intlZh"].get("locales") or {})),
            "Intl 各 locale 时区名位置与真机一致（显式/dateStyle 两路）": (
                isinstance(state.get("intlZh"), dict)
                and _intl_tz_ok(state["intlZh"].get("locales") or {})),
            "Intl ja 带分钟时星期前置、12 时制 12 时写作 0 时": (
                isinstance(state.get("intlZh"), dict)
                and (state["intlZh"].get("ja") or {}).get("reorder")
                    == "月曜日 23:05"
                and (state["intlZh"].get("ja") or {}).get("zero")
                    == "午後0時"),
            "Intl zh-SG 默认 12 小时制、时区名后无空格": (
                isinstance(state.get("intlZh"), dict)
                and (state["intlZh"].get("sg") or {}).get("full")
                    == "2026年10月19日 星期一 "
                       "北美太平洋夏令时间晚上11:05:09"
                and (state["intlZh"].get("sg") or {}).get("week")
                    == "星期一 晚上11:05"),
            "Intl 12 小时制 dayPeriod 按本地小时分区（逐时表）": (
                isinstance(state.get("intlZh"), dict)
                and _intl_dp_ok(state["intlZh"].get("dpHours") or {})),
            "Intl formatRange 两端各自取 dayPeriod 且与 parts 自洽": (
                isinstance(state.get("intlZh"), dict)
                and state["intlZh"]["range"] ==
                    "2026/10/19 晚上11:05 – 2026/10/20 上午11:05"
                and state["intlZh"]["rangeJoin"] is True),
            "Date#toLocaleString 与 format 同口径": (
                isinstance(state.get("intlZh"), dict)
                and state["intlZh"]["tls"] == state["intlZh"]["full"]),
            "Intl 非 zh/ja 不受影响、非法组合仍抛 TypeError": (
                isinstance(state.get("intlZh"), dict)
                and state["intlZh"]["en"] ==
                    "Monday, October 19, 2026 at 11:05:09 PM Pacific "
                    "Daylight Time"
                and state["intlZh"]["tldErr"] is True),
            "Intl formatToParts 与 format 自洽": (
                isinstance(state.get("intlZh"), dict)
                and state["intlZh"]["join"] is True),
            "Intl format 仍是原型取值器返回的函数（形态不变）": (
                isinstance(state.get("intlZh"), dict)
                and state["intlZh"]["fmtGetter"] == "function"
                and state["intlZh"]["fmtIsFn"] == "function"
                and state["intlZh"]["fmtLen"] == 1),
            # ---- F-60 / F-61：命令面手势与平台口径 ----
            "无手势无选区 execCommand('copy') 返回 false": (
                state.get("execCopyNoSel") is False),
            "queryCommandSupported('paste') 为 true（WebKit 口径）": (
                state.get("qcsPaste") is True),
            # ---- F-48：真机 iOS visualViewport 与 innerHeight 不等 ----
            "visualViewport.height < innerHeight（真机 iOS 工具栏 inset）": (
                isinstance(state.get("vvH"), (int, float))
                and isinstance(state.get("innerH"), (int, float))
                and state["innerH"] - state["vvH"] >= 40),
            # ---- F-46 / F-47：控件 UA 样式表计算值 ----
            "控件 UA 样式表：textarea/select 为 system-ui、input overflow=visible": (
                "monospace" not in (state.get("uaFormStyles") or {})
                    .get("textarea", {}).get("fontFamily", "")
                and (state.get("uaFormStyles") or {}).get("input", {}).get("overflow")
                == "visible"
                and "Arial" not in (state.get("uaFormStyles") or {})
                    .get("select", {}).get("fontFamily", "")),
            "正文 -webkit-user-select 为 none（真机 iOS 正文不可选）": (
                (state.get("uaFormStyles") or {}).get("bodyUserSelect") == "none"),
            # ---- F-08：Blink 专有事件处理器属性 ----
            "窗口：onsearch/oncontextlost/oncontextrestored/onbeforeinstallprompt/onappinstalled 不存在": (
                state["evSearch"] is False and state["evContextLost"] is False
                and state["evContextRestored"] is False
                and state["evBeforeInstall"] is False
                and state["evAppInstalled"] is False),
            # ---- F-10：Blink 前缀可见性别名 ----
            "document：webkitHidden / webkitVisibilityState 不存在": (
                state["docWebkitHidden"] is False
                and state["docWebkitVisibility"] is False),
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
            # ---- 平台 API 差集（Chromium 专有面收敛；BCD safari_ios=false）----
            # 通用传感器 API：WebKit 从未实现（真机一行判假面）
            "平台API：传感器家族整块缺席（WebKit 无通用传感器 API）": (
                all((plat.get("win") or {}).get(n) == ["undefined", False]
                    for n in ("Sensor", "SensorErrorEvent", "Accelerometer", "Gyroscope",
                              "LinearAccelerationSensor", "GravitySensor",
                              "AbsoluteOrientationSensor", "RelativeOrientationSensor"))),
            "平台API：Chromium 专有族缺席（EyeDropper/IdleDetector/WebSocketStream/ImageDecoder/…）": (
                all((plat.get("win") or {}).get(n) == ["undefined", False]
                    for n in ("EyeDropper", "IdleDetector", "WebSocketStream", "ImageDecoder",
                              "FileSystemObserver", "queryLocalFonts",
                              "documentPictureInPicture", "launchQueue", "Scheduler",
                              "scheduler", "OTPCredential", "Ink",
                              "MediaStreamTrackGenerator"))),
            "平台API：隐私沙盒与 FSA 选择器缺席（sharedStorage/fence/Topics/PA 面）": (
                all((plat.get("win") or {}).get(n) == ["undefined", False]
                    for n in ("sharedStorage", "SharedStorage", "fence", "Fence",
                              "HTMLFencedFrameElement", "showSaveFilePicker",
                              "showDirectoryPicker"))),
            "平台API：F-92 存储分桶 / 呈现可用性类名缺席（入口已删，类名同删）": (
                all((plat.get("win") or {}).get(n) == ["undefined", False]
                    for n in ("PresentationAvailability", "StorageBucket"))),
            "平台API：F-92 Gamepad 震动面缺席（iOS 开关默认 false，类名 + 原型成员同删）": (
                (plat.get("win") or {}).get("GamepadHapticActuator") == ["undefined", False]
                and state.get("gamepadVibActuator") is False),
            "平台API：document.browsingTopics 与 navigator 广告/XR/VirtualKeyboard 面缺席": (
                (plat.get("doc") or {}).get("browsingTopics") == ["undefined", False]
                and all((plat.get("nav") or {}).get(n) == ["undefined", False]
                        for n in ("xr", "virtualKeyboard", "ink", "runAdAuction",
                                  "joinAdInterestGroup", "leaveAdInterestGroup",
                                  "deprecatedReplaceInURN", "updateAdInterestGroups",
                                  "deprecatedRunAdAuctionEnforcesKAnonymity",
                                  "protectedAudience"))),
            "平台API：真机存在面保持（WebCodecs/URLPattern/BarcodeDetector/全屏外引擎面）": (
                all((plat.get("kept") or {}).get(n) == ["function", True]
                    for n in ("VideoDecoder", "VideoFrame", "Highlight",
                              "BarcodeDetector", "ImageCapture",
                              "FileSystemHandle", "FileSystemFileHandle",
                              "FileSystemDirectoryHandle"))
                # F-85：这批按 BCD 接口自身版本门控，低版本 profile 必须缺席
                and all((plat.get("kept") or {}).get(n)
                        == (["function", True] if _ios_at_least(26) else ["undefined", False])
                        for n in ("AudioDecoder", "EncodedAudioChunk", "URLPattern",
                                  "GPU", "FileSystemWritableFileStream"))
                # MediaStreamTrackProcessor 是 iOS 18+，低版本 profile 必须缺席
                and (plat.get("kept") or {}).get("MediaStreamTrackProcessor")
                == (["function", True] if _ios_at_least(18) else ["undefined", False])
                # F-85：Navigation API 是 26.2，低版本 profile 必须缺席
                and (plat.get("kept") or {}).get("navigation")
                == (["object", True] if _ios_at_least(26, 2) else ["undefined", False])
                and all((plat.get("keptDoc") or {}).get(n) == ["function", True]
                        for n in ("startViewTransition", "hasStorageAccess",
                                  "requestStorageAccess"))
                and all((plat.get("keptNav") or {}).get(n) == ["function", True]
                        for n in ("share", "canShare"))
                and (plat.get("keptNav") or {}).get("wakeLock") == ["object", True]
                and (plat.get("keptNav") or {}).get("gpu")
                == (["object", True] if _ios_at_least(26) else ["undefined", False])),
            "平台API：Worker 侧差集（IdleDetector/WebSocketStream/ImageDecoder/Scheduler 缺席）": (
                all((wk_main.get("platW") or {}).get(n) == "undefined"
                    for n in ("IdleDetector", "WebSocketStream", "ImageDecoder",
                              "FileSystemObserver", "Scheduler", "scheduler"))
                and all((wk_main.get("platW") or {}).get(n) == "function"
                        for n in ("VideoDecoder", "BarcodeDetector",
                                  "WebGL2RenderingContext"))
                # F-85：URLPattern 在 Worker 全局同样按版本门控
                and (wk_main.get("platW") or {}).get("URLPattern")
                == ("function" if _ios_at_least(26) else "undefined")),
            # F-90：Notification / Push 族在 iOS Worker 全局同样缺席
            # （页面侧由 notifAbsent 断言，Worker 是独立全局，须单独收口）。
            "平台API：Worker 侧 Notification / Push 族缺席": (
                all((wk_main.get("platW") or {}).get(n) == "undefined"
                    for n in ("Notification", "PushManager", "PushSubscription",
                              "PushSubscriptionOptions"))),
            "平台API：跨域 iframe Worker 同口径": (
                (fr_worker.get("platW") or {}).get("IdleDetector") == "undefined"
                and (fr_worker.get("platW") or {}).get("Scheduler") == "undefined"
                and (fr_worker.get("platW") or {}).get("VideoDecoder") == "function"
                and (fr_worker.get("platW") or {}).get("Notification") == "undefined"),
            # ---- JavaScriptCore 引擎面（iOS 目标）----
            # 声明 iOS 而引擎是 V8 有四条独立判据，逐条对齐 WebKit；取证见
            # app/mobile_cdp_injector.py 的「JavaScriptCore 引擎面」注释块。
            "引擎面：Error 栈为 JSC 格式（`fn@url:line:col`，非 V8 的 `at fn`）": (
                jsc.get("stackEngine") == "jsc"
                and jsc.get("hasV8StackTrace") is False),
            "引擎面：原生函数 toString 为 JSC 多行缩进形态": (
                jsc.get("nativeEvalMultiLine") is True
                and jsc.get("nativeAlertMultiLine") is True),
            "引擎面：用户函数 toString 不被改写（无补丁痕迹）": (
                isinstance(jsc.get("userFn"), str)
                and "myFn" in jsc["userFn"]
                and "[native code]" not in jsc["userFn"]),
            "引擎面：Error.prepareStackTrace 缺席（V8 专有钩子，JSC 无）": (
                jsc.get("hasPrepareStackTrace") is False),
            "引擎面：Error.prototype.message/name 为数据属性（JSC 口径）": (
                jsc.get("errProtoMessageIsData") is True
                and jsc.get("errProtoNameIsData") is True),
            "引擎面：显式构造的 Error 具备 own line/column 数据属性": (
                jsc.get("ownNames") == ["stack", "message", "line", "column"]
                and jsc.get("lineDesc") == [True, False, True]
                and jsc.get("colDesc") == [True, False, True]
                and isinstance(jsc.get("line"), int)
                and isinstance(jsc.get("column"), int)),
            "引擎面：栈首帧为 `fn@loc` 形态（无 Proxy 陷阱合成帧）": (
                isinstance(jsc.get("stackHead"), str)
                and "@" in jsc["stackHead"]
                and "Object.construct@" not in jsc["stackHead"]
                and "Object.apply@" not in jsc["stackHead"]),
            "引擎面：Proxy 透明（instanceof / 子类化 / name / length 不变）": (
                jsc.get("instanceofOk") is True
                and jsc.get("subclassOk") == [True, True, "Error"]
                and jsc.get("errName") == "Error"
                and jsc.get("errLen") == 1
                # WebKit 实测：Error.name / Error.length 均为
                # [writable:false, enumerable:false, configurable:true]
                and jsc.get("errNameDesc") == [False, False, True]
                and jsc.get("errLenDesc") == [False, False, True]),
            # Error.captureStackTrace 是 WebKit/JSC 自有成员（ErrorConstructor.cpp:52），
            # 检测站「V8 专有」文案已过时——必须保留，不得抹除。
            "引擎面：Error.captureStackTrace 保留（JSC 自有，非 V8 专有）": (
                jsc.get("captureStackTraceType") == "function"
                and "captureStackTrace" in (jsc.get("statics") or [])),
            "引擎面：Intl.v8BreakIterator 缺席（V8 专有，WebKit Intl 无此成员）": (
                jsc.get("v8BI") is False),
            "引擎面：navigator.webkitTemporaryStorage/PersistentStorage 缺席": (
                jsc.get("wkTemp") is False and jsc.get("wkPersist") is False),
            # Intl.v8BreakIterator 在 Worker 里与页面同源存在（实测），不补删
            # 即构成「页面 false、Worker true」的跨上下文矛盾。
            "引擎面：Worker 内 Intl.v8BreakIterator 同样缺席（页面/Worker 同口径）": (
                wk_main.get("intlV8") is False
                and wk_main.get("intlOwn") is False
                and fr_worker.get("intlV8") is False),
            # ---- WebKit 媒体面（AirPlay / EME 前缀面）----
            # 桌面内核六项全缺席；真机 Safari/CriOS 全存在（Playwright WebKit
            # 27.2 实测）。取证见 app/mobile_cdp_injector.py 同名注释块。
            "媒体面：HTMLMediaElement 四项 WebKit 成员（函数为 data、属性为 accessor）": (
                (med.get("picker") or [None, None, None])[0] == "function"
                and (med.get("picker") or [None, None, None])[2]
                == ["data", True, True, True]
                and med.get("pickerLen") == 0
                and (med.get("setKeys") or [None, None, None])[0] == "function"
                and (med.get("setKeys") or [None, None, None])[2]
                == ["data", True, True, True]
                and med.get("setKeysLen") == 1
                and (med.get("wireless") or [None, None])[1]
                == ["accessor", None, True, True]
                and med.get("wirelessVal") is False
                and (med.get("keys") or [None, None])[1]
                == ["accessor", None, True, True]
                and med.get("keysVal") is None),
            "媒体面：webkitPresentationMode 只挂 HTMLVideoElement（默认 'inline'）": (
                med.get("presMode") == ["accessor", None, True, True]
                and med.get("presVal") == "inline"
                # 挂错层级（挂到 HTMLMediaElement）是常见实现错误
                and med.get("presOnMedia") is None),
            "媒体面：webkitKeys 取用者为非媒体元素时抛 TypeError": (
                med.get("badRecv") == "throw:TypeError"),
            "媒体面：WebKitPlaybackTargetAvailabilityEvent 可构造且语义正确": (
                (med.get("availCtor") or {}).get("len") == 1
                and (med.get("availCtor") or {}).get("name")
                == "WebKitPlaybackTargetAvailabilityEvent"
                and (med.get("availCtor") or {}).get("own")
                == ["length", "name", "prototype"]
                and (med.get("availCtor") or {}).get("protoOwn")
                == ["constructor", "availability"]
                and (med.get("availCtor") or {}).get("availDesc")
                == ["accessor", None, True, True]
                and (med.get("availCtor") or {}).get("noArg") == ""
                and (med.get("availCtor") or {}).get("withArg") == "available"
                and (med.get("availCtor") or {}).get("tag")
                == "[object WebKitPlaybackTargetAvailabilityEvent]"
                and (med.get("availCtor") or {}).get("instOf") == [True, True]
                and (med.get("availCtor") or {}).get("noNew") == "throw:TypeError"
                and (med.get("availCtor") or {}).get("native") is True),
            "媒体面：WebKitMediaKeys 静态面/原型面/构造语义": (
                (med.get("mk") or {}).get("len") == 1
                and (med.get("mk") or {}).get("name") == "WebKitMediaKeys"
                and (med.get("mk") or {}).get("own")
                == ["length", "name", "prototype", "isTypeSupported"]
                and (med.get("mk") or {}).get("protoOwn")
                == ["constructor", "keySystem", "createSession"]
                and (med.get("mk") or {}).get("isTypeSupported") == "function"
                and (med.get("mk") or {}).get("supported") is False
                # 真机 new WebKitMediaKeys() 抛 NotSupportedError（无 CDM 实现）
                and (med.get("mk") or {}).get("noNew") == "throw:NotSupportedError"),
            "媒体面：WebKitMediaKeySession/MessageEvent/NeededEvent 原型成员齐全": (
                med.get("sessionProto") == ["constructor", "error", "keySystem",
                                            "sessionId", "onwebkitkeyadded",
                                            "onwebkitkeyerror", "onwebkitkeymessage",
                                            "update", "close"]
                and med.get("msgProto") == ["constructor", "message", "destinationURL"]
                and med.get("neededProto") == ["constructor", "initData"]),
            "媒体面：WebKitMediaKeyError 六个常量（静态 + 原型，值 1..6，不可写不可配置）": (
                med.get("errCodes") == [
                    ["MEDIA_KEYERR_UNKNOWN", 1], ["MEDIA_KEYERR_CLIENT", 2],
                    ["MEDIA_KEYERR_SERVICE", 3], ["MEDIA_KEYERR_OUTPUT", 4],
                    ["MEDIA_KEYERR_HARDWARECHANGE", 5], ["MEDIA_KEYERR_DOMAIN", 6]]
                and med.get("errDesc") == ["data", False, True, False]
                and sorted(med.get("errProtoCodes") or [])
                == sorted(k for k, _ in (med.get("errCodes") or []))),
            # 真机 WebKit Worker 里媒体面全部缺席（实测），内核不得漏出，
            # 否则「页面有、Worker 无」的跨上下文矛盾会变成反方向的破绽。
            "媒体面：Worker 内媒体面全缺席（与 WebKit Worker 同口径）": (
                all(v == "undefined" for v in (wk_main.get("mediaW") or {}).values())
                and all(v == "undefined"
                        for v in (fr_worker.get("mediaW") or {}).values())
                and bool(wk_main.get("mediaW")) and bool(fr_worker.get("mediaW"))),
            # ---- Notification 面 ----
            # F-90：iOS 普通浏览上下文里 NotificationsEnabled 默认为 false，
            # Notification 连同 Push 族整体缺席——这里只断言「没有」，
            # 原来的静态成员 / 原型成员 / 缺省值九项断言随假面一起作废
            # （WebKit Notification.idl 口径的仿真只在 Android 侧有意义）。
            "Notification：iOS 页面全局缺席（typeof undefined 且不在 window 上）": (
                ntf.get("type") == "undefined"),
            # ---- WebKit 原生触控工厂（document.createTouch / createTouchList）----
            # 取证：Playwright WebKit 27.2 实测 + WebKit main Source/WebCore/
            # dom/Document.idl（createTouch / createTouchList）与 Touch.idl。
            "触控工厂：createTouch / createTouchList 存在且为 Document.prototype 数据属性": (
                tf.get("ct") == "function" and tf.get("ctl") == "function"
                and tf.get("ctDesc") == ["data", True, True, True]
                and tf.get("ctlDesc") == ["data", True, True, True]
                and tf.get("ctLen") == 0 and tf.get("ctlLen") == 0
                and tf.get("ctName") == "createTouch"
                and tf.get("native") is True),
            "触控工厂：Touch / TouchList 均不可构造（Illegal constructor）": (
                tf.get("newTouch") == "throw:TypeError"
                and tf.get("newTouchList") == "throw:TypeError"),
            # 真机 Touch 原型只有 webkit* 前缀成员；无前缀 radiusX/force/
            # rotationAngle 整个不存在（不是 null）——这是「Chromium 内核装 iOS」
            # 的独立判据，与有没有 createTouch 无关。
            "触控工厂：Touch 原型为 WebKit 口径（webkit* 前缀、无无前缀成员）": (
                tf.get("touchProto") == [
                    "constructor", "clientX", "clientY", "screenX", "screenY",
                    "pageX", "pageY", "target", "identifier",
                    "webkitRadiusX", "webkitRadiusY", "webkitRotationAngle",
                    "webkitForce"]
                and tf.get("touchListProto") == ["constructor", "length", "item"]
                and tf.get("touchEventProto") == [
                    "constructor", "touches", "targetTouches", "changedTouches",
                    "ctrlKey", "shiftKey", "altKey", "metaKey"]
                # Touch.length 在 V8 上是 [writable:false, configurable:false]，
                # 改不动（WebKit 为 0）——该项为已知残余，见方案 §2.14
                and tf.get("touchDesc") == [True, False, True]),
            "触控工厂：createTouch 全参回读（九项取值 + 无前缀成员缺席）": (
                (tf.get("full") or {}).get("identifier") == 7
                and (tf.get("full") or {}).get("target") is True
                and (tf.get("full") or {}).get("pageX") == 100
                and (tf.get("full") or {}).get("pageY") == 200
                # clientX/clientY 镜像 pageX/pageY
                and (tf.get("full") or {}).get("clientX") == 100
                and (tf.get("full") or {}).get("clientY") == 200
                and (tf.get("full") or {}).get("screenX") == 300
                and (tf.get("full") or {}).get("screenY") == 400
                # 扩展四参全部生效（不是 null/0）
                and (tf.get("full") or {}).get("wkRX") == 5
                and (tf.get("full") or {}).get("wkRY") == 6
                and (tf.get("full") or {}).get("wkRot") == 45
                and (tf.get("full") or {}).get("wkForce") == 0.75
                and (tf.get("full") or {}).get("hasForce") is False
                and (tf.get("full") or {}).get("hasRadiusX") is False
                and (tf.get("full") or {}).get("force") == "undefined"
                and (tf.get("full") or {}).get("radiusX") == "undefined"
                and (tf.get("full") or {}).get("rotationAngle") == "undefined"),
            "触控工厂：返回真 Touch 实例（无 own 属性、instanceof、[object Touch]）": (
                (tf.get("full") or {}).get("own") == []
                and (tf.get("full") or {}).get("instOf") is True
                and (tf.get("full") or {}).get("protoOk") is True
                and (tf.get("full") or {}).get("tag") == "[object Touch]"),
            "触控工厂：无参调用 target 为 null、数值项为 0": (
                (tf.get("minimal") or {}).get("target") == "null"
                and (tf.get("minimal") or {}).get("identifier") == 0
                and (tf.get("minimal") or {}).get("pageX") == 0
                and (tf.get("minimal") or {}).get("pageY") == 0
                and (tf.get("minimal") or {}).get("wkForce") == 0),
            "触控工厂：字符串参数按 WebKit 语义强制转换": (
                tf.get("coerce") == [7, 100, 5, 0, 0.5]),
            "触控工厂：createTouchList 返回真 TouchList（索引与 item() 同源、越界为 null）": (
                (tf.get("list") or {}).get("len") == 2
                and (tf.get("list") or {}).get("isTL") is True
                and (tf.get("list") or {}).get("same0") is True
                and (tf.get("list") or {}).get("same1") is True
                and (tf.get("list") or {}).get("item0") is True
                and (tf.get("list") or {}).get("itemNeg") == "null"
                and (tf.get("list") or {}).get("itemOOR") == "null"
                and (tf.get("list") or {}).get("own") == ["0", "1"]
                and (tf.get("list") or {}).get("protoOk") is True
                and (tf.get("list") or {}).get("len0") == 0
                and (tf.get("list") or {}).get("isTL0") is True),
            "触控工厂：坏 receiver 抛 TypeError（两个工厂方法）": (
                tf.get("badRecv") == "throw:TypeError"
                and tf.get("badRecvList") == "throw:TypeError"),
            # ---- 色域：Display P3（iPhone 7 及以后全系硬件判据）----
            "色域：(color-gamut: p3) 为真且 srgb 亦为真（iPhone 屏口径）": (
                (state.get("mediaQueries") or {}).get("p3") is True
                and (state.get("mediaQueries") or {}).get("srgb") is True),
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
            # 桌面内核在注册监听时会派发一次全 null 的 devicemotion /
            # deviceorientation / deviceorientationabsolute 哨兵事件；真机 iOS
            # 未授权时事件总数为 0（且 WebKit 根本没有 deviceorientationabsolute）。
            "传感器：未授权时事件总数为 0（三类哨兵事件全抑制）": (
                sensor_counts.get("motion", 0) == 0
                and sensor_counts.get("orientation", 0) == 0
                and sensor_counts.get("orientationAbsolute", 0) == 0),
            # ---- 引擎面「一行判假」信号（iOS 批次；BCD / WebKit 源码口径）----
            # 元素级全屏按设备分口径：iPhone 整块缺席；iPad Safari 16.4+ 有
            # （模板按 platform/UA 的 iPad 判断保留原生实现）。
            "全屏：元素级全屏成员按设备口径（iPhone 清空 / iPad 保留）": (
                ("requestFullscreen" in (eng.get("fsElementOwn") or [])
                 and "exitFullscreen" in (eng.get("fsDocumentOwn") or []))
                if _is_ipad else
                (eng.get("fsElementOwn") == [] and eng.get("fsDocumentOwn") == []
                 and eng.get("fsShadowOwn") in ([], None))),
            "全屏：requestFullscreen/fullscreenEnabled/exitFullscreen 按设备口径": (
                (eng.get("fsRequestType") == "function"
                 and eng.get("fsEnabledType") == "boolean"
                 and eng.get("fsExitType") == "function"
                 and eng.get("fsElementInDoc") is True)
                if _is_ipad else
                (eng.get("fsRequestType") == "undefined"
                 and eng.get("fsEnabledType") == "undefined"
                 and eng.get("fsExitType") == "undefined"
                 and eng.get("fsElementInDoc") is False
                 and eng.get("fsHandlerInDoc") is False)),
            "视频：webkit 全屏家族齐全（含 FullScreen 大写拼写别名）": (
                eng.get("videoEnterType") == "function"
                and eng.get("videoEnterCapType") == "function"
                and eng.get("videoExitType") == "function"),
            "视频：webkitSupportsFullscreen 无源 false / 有源 true": (
                eng.get("videoSupportsType") == "boolean"
                and eng.get("videoSupportsNoSrc") is False
                and eng.get("videoSupportsWithSrc") is True),
            "视频：webkitDisplayingFullscreen 随 enter/exit 切换": (
                eng.get("videoDisplayingAfterEnter") is True
                and eng.get("videoDisplayingAfterExit") is False),
            "CSS：CSS.supports 白名单（两参/单参/条件式/复合式）": (
                eng.get("cssTouchCalloutTwoArg") is True
                and eng.get("cssTouchCalloutOneArg") is True
                and eng.get("cssTouchCalloutParen") is True
                and eng.get("cssTouchCalloutCompound") is True
                and eng.get("cssOscTwoArg") is True
                and eng.get("cssOscOneArg") is True),
            "CSS：值合法性（非法值 false、否定式语义正确）": (
                eng.get("cssTouchCalloutBadValue") is False
                and eng.get("cssTouchCalloutNegInvalid") is True),
            # F-88：allow 表里 5 个「任意值」项改为真值域校验
            "CSS：allow 面非法值一律 false（F-88）": (
                eng.get("cssBadMarginTrim") is False
                and eng.get("cssBadTextAutospace") is False
                and eng.get("cssBadBackdrop") is False
                and eng.get("cssBadHyphens") is False
                and eng.get("cssBadApplePay") is False
                and eng.get("cssBadMarginTrimCond") is False
                and eng.get("cssBadBackdropCond") is False),
            "CSS：值域边界（margin-trim: inline / blur 无单位 → false；多函数与 || 组合 → true）": (
                eng.get("cssMarginTrimInline") is False
                and eng.get("cssBackdropUnitless") is False
                and eng.get("cssBackdropMulti") is True
                and eng.get("cssMarginTrimCombo") is True
                and eng.get("cssTextAutospaceCombo") is True),
            "CSS：原生判定不受影响（display: grid 真 / 非法值假）": (
                eng.get("cssNativeTrue") is True and eng.get("cssNativeFalse") is False),
            # F-101：复合条件（and/or/not）里的 allow 项同样走改写
            "CSS：复合条件里的非前缀 allow 项（margin-trim/text-autospace）": (
                eng.get("cssComboMarginTrim") is True
                and eng.get("cssComboMarginTrimBad") is False
                and eng.get("cssComboTextAutospace") is True
                and eng.get("cssComboHyphens") is True),
            # F-98：inline 存储 + 枚举 + computed 回读
            "CSS：WebKit 专有属性的 inline 读写 / 索引枚举 / computed 回读": (
                f98.get("len") == 5
                and f98.get("items") == ["color", "-webkit-backdrop-filter",
                                         "margin-trim", "hyphens",
                                         "-apple-pay-button-style"]
                and f98.get("itemAt") == "-webkit-backdrop-filter"
                and f98.get("inlineMT") == "block"
                and f98.get("inlinePay") == "black"
                and f98.get("inlineHyphens") == "auto"
                and f98.get("compMT") == "block"
                and f98.get("compPay") == "black"
                and f98.get("compHyphens") == "auto"
                and f98.get("stickyInline") == "-webkit-sticky"
                and f98.get("stickyComp") == "sticky"
                and f98.get("removed") == "blur(4px)"
                and f98.get("lenAfter") == 4
                and f98.get("emptyIn") is False),
            # F-99：computed 数值序列化（LayoutUnit 1/64 / float32 / 整数档）
            "CSS：computed 长度按 WebKit 三档口径序列化": (
                f99.get("width") == "21.4375px"
                and f99.get("height") == "33.296875px"
                and f99.get("margin") == "21.4375px 21.440001px 21.4375px 21.4375px"
                and f99.get("marginRight") == "21.440001px"
                and f99.get("padding") == "21.440001px"
                and f99.get("lineHeight") == "27.200001px"
                and f99.get("fontSize") == "16px"
                and f99.get("borderWidth") == "1px"
                and f99.get("outlineWidth") == "2px"
                and f99.get("outlineOffset") == "1px"),
            "CSS：物理右侧外边距是量化例外（rtl / vertical-rl 同样）": (
                f99.get("rtlInlineStart") == "21.440001px"
                and f99.get("rtlInlineEnd") == "21.4375px"
                and f99.get("rtlBlockStart") == "21.4375px"
                and f99.get("vertBlockStart") == "21.440001px"
                and f99.get("vertBlockEnd") == "21.4375px"),
            "CSS：未参与布局的元素不做量化（display:none）": (
                f99.get("hiddenWidth") == "33.299999px"
                and f99.get("hiddenMargin") == "21.440001px"),
            "CSS：简写折叠与 -webkit-locale 去引号": (
                f99.get("radiusSlash") == "10px / 20px"
                and f99.get("locale") == "en"
                and f99.get("localeCamel") == "en"),
            "CSS：inline 声明保留作者原文，仅 computed 归一": (
                f99.get("inlineWidth") == "33.3px"
                and f99.get("inlineLineHeight") == "1.7"
                and f99.get("compWidth") == "33.296875px"
                and f99.get("compLineHeight") == "27.200001px"),
            "CSS：camel / getPropertyValue 两条旁路口径一致": (
                f99.get("marginVP") == "21.4375px"
                and f99.get("marginCamel") == "21.4375px"),
            # F-100：表单控件 UA 默认值
            "CSS：表单控件 computed line-height 为 normal": (
                f99.get("btnLH") == "normal"
                and f99.get("taLH") == "normal"
                and f99.get("selLH") == "normal"
                and all(f99.get("lh_" + t) == "normal" for t in (
                    "checkbox", "radio", "range", "color", "file", "date"))),
            "CSS：表单控件 computed cursor（文本类 auto / 按钮类 default）": (
                f99.get("btnCursor") == "default"
                and f99.get("selCursor") == "default"
                and f99.get("taCursor") == "auto"
                and all(f99.get("cur_" + t) == "auto" for t in (
                    "checkbox", "radio", "range", "color", "file", "date"))),
            # F-07 / F-24：deny（桌面独有）与 allow（WebKit 独有）双向差集
            "CSS：deny 面 5 项为 false（interpolate-size/overflow-clip-margin/corner-shape/reading-flow/:host-context）": (
                eng.get("cssDenyInterpolate") is False
                and eng.get("cssDenyOverflowClip") is False
                and eng.get("cssDenyCornerShape") is False
                and eng.get("cssDenyReadingFlow") is False
                and eng.get("cssDenyHostContext") is False),
            "CSS：allow 面 6 项为 true（margin-trim/text-autospace/-webkit-backdrop-filter/-webkit-sticky/-webkit-hyphens/-apple-pay-button-style）": (
                eng.get("cssAllowMarginTrim") is True
                and eng.get("cssAllowTextAutospace") is True
                and eng.get("cssAllowBackdrop") is True
                and eng.get("cssAllowSticky") is True
                and eng.get("cssAllowHyphens") is True
                and eng.get("cssAllowApplePay") is True),
            "CSS：单参条件文本同样走双向差集": (
                eng.get("cssAllowMarginTrimCond") is True
                and eng.get("cssDenyInterpolateCond") is False),
            "CSS：webkitTouchCallout/webkitOverflowScrolling 属性面 + 读写回环": (
                eng.get("tcInStyle") is True and eng.get("oscInStyle") is True
                and eng.get("tcDefault") == ""
                and eng.get("tcRoundtrip") == ["", "none", "", "touch"]),
            "CSS：非法值静默忽略（不覆盖旧值）": (
                eng.get("tcSetterInvalidIgnored") == "none"),
            "传感器：new DeviceMotionEvent/DeviceOrientationEvent 抛 TypeError": (
                eng.get("dmeCtorThrows") == "TypeError"
                and eng.get("doeCtorThrows") == "TypeError"),
            "传感器：构造器保持原生形态（prototype.constructor 指回 + native 外观）": (
                eng.get("dmeProtoCtorSelf") is True and eng.get("dmeNativeStr") is True),
            "传感器：webkitCompassHeading/Accuracy 存在，无数据为 null": (
                eng.get("compassHeadingIn") is True
                and eng.get("compassAccuracyIn") is True
                and eng.get("compassHeadingValue") is None),
            "传感器：legacy 初始化器齐全（initDeviceMotionEvent/…OrientationEvent，length 0）": (
                eng.get("initDOELen") == 0 and eng.get("initDMELen") == 0),
            "传感器：createEvent 两条链可用（WebKit 口径）": (
                str(eng.get("createEventDOE", "")).startswith("ok:function:")
                and str(eng.get("createEventDME", "")).startswith("ok:function:")),
            "传感器：createEvent → init → 回读一致（orientation 链，含罗盘值）": (
                eng.get("legacyDOEChain") == ["deviceorientation", 10, 20, 30, 45, 5]),
            "传感器：createEvent → init → 回读一致（motion 链，含字典与 interval）": (
                eng.get("legacyDMEChain") == ["devicemotion", 1, 5, 9, 16]),
            # ---- Safari 全局面（item 13：WebKit main / iOS 口径）----
            "引擎面：WebKitPoint 形状（原型访问器 / 不可无 new / 原生外观）": (
                eng.get("wkp") is not None
                and eng["wkp"].get("len") == 0
                and eng["wkp"].get("name") == "WebKitPoint"
                and eng["wkp"].get("native") is True
                and eng["wkp"].get("ownNames") == ["length", "name", "prototype"]
                and eng["wkp"].get("protoNames") == ["constructor", "x", "y"]
                and eng["wkp"].get("protoWritable") is False
                and eng["wkp"].get("protoCtorSelf") is True
                and eng["wkp"].get("winDesc") == [True, False, True, "data"]
                and eng["wkp"].get("xDesc") == [True, True, "function", "function"]),
            "引擎面：WebKitPoint 取值（float32 / 默认值 / NaN+Inf / toStringTag）": (
                eng.get("wkp", {}).get("vals") == [1.5, 2.5]
                and eng["wkp"].get("defaults") == [0, 0]
                and eng["wkp"].get("one") == [0.10000000149011612, 0]
                and eng["wkp"].get("fround") is True
                and eng["wkp"].get("setFround") is True
                and eng["wkp"].get("nanInf") == [True, True]
                and eng["wkp"].get("tag") == "[object WebKitPoint]"
                and eng["wkp"].get("instProto") is True
                and eng["wkp"].get("instOwn") == []
                and eng["wkp"].get("callNoNew") == "TypeError"
                and eng["wkp"].get("badRecv") == "TypeError"),
            "引擎面：webkitConvertPoint 形状 + 坐标语义（原点/往返/嵌套/滚动不变）": (
                eng.get("conv", {}).get("present") is True
                and eng["conv"].get("len1") == 0 and eng["conv"].get("len2") == 0
                and eng["conv"].get("name1") == "webkitConvertPointFromNodeToPage"
                and eng["conv"].get("native1") is True
                and eng["conv"].get("desc1") == [True, True, True]
                and eng["conv"].get("zeroOk") is True
                and eng["conv"].get("fiveOk") is True
                and eng["conv"].get("roundtripOk") is True
                and eng["conv"].get("innerOk") is True
                and eng["conv"].get("hiddenOk") is True
                and eng["conv"].get("detachedOk") is True
                and eng["conv"].get("docOk") is True
                and eng["conv"].get("scrolledStable") is True
                and eng["conv"].get("retType") == "WebKitPoint"),
            "引擎面：webkitConvertPoint 边界（null → null；非 Node/非 WebKitPoint 抛 TypeError）": (
                eng["conv"].get("nullArgs") == [True, True, True]
                and eng["conv"].get("undefNode") is True
                and eng["conv"].get("errNode") == "TypeError"
                and eng["conv"].get("errPoint") == "TypeError"
                and eng["conv"].get("errOneArg") == "TypeError"),
            "引擎面：webkitCancelRequestAnimationFrame 别名（length 1 / 原生外观）": (
                eng.get("conv", {}).get("craf")
                == [1, "webkitCancelRequestAnimationFrame", True]),
            "引擎面：WebKitNamespace 家族接口对象（不可构造 / 页面世界原型成员）": (
                ns.get("WN") is not None and ns.get("UH") is not None
                and ns.get("UM") is not None
                and ns["WN"].get("ownNames") == ["length", "name", "prototype"]
                and ns["WN"].get("protoNames")
                    == ["constructor", "messageHandlers", "buffers"]
                and ns["WN"].get("protoWritable") is False
                and ns["WN"].get("protoCtorSelf") is True
                and ns["WN"].get("native") is True
                and ns["WN"].get("callThrows") == "TypeError:Illegal constructor"
                and ns["WN"].get("newThrows") == "TypeError:Illegal constructor"
                and ns["UH"].get("protoNames") == ["constructor"]
                and ns["UM"].get("protoNames") == ["constructor", "postMessage"]
                and ns.get("evalScriptProto") is False
                and ns.get("jsHandleProto") is False
                and ns.get("pmLegacyProto") is False
                and ns.get("jsHandleGlobal") == "undefined"
                and ns.get("nodeSnapshotGlobal") == "undefined"),
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
                "CriOS：messageHandlers 通道可读但不可枚举（只具名 getter 语义）": (
                    state["wkHandlerIn"] is True
                    and state["wkHandlers"] == []
                    and state["wkHandlerOwn"] == []
                    and state["wkHandlerUnknown"] == "undefined"
                    and state["wkHandlerDesc"] == [True, True, True]),
                # ---- window.webkit 按 WebKit 机制装配（item 13）----
                "CriOS：window.webkit 为 WebKitNamespace 实例（ReadOnly+DontDelete / 无自有属性）": (
                    ns.get("wkType") == "object"
                    and ns.get("wkDesc") == [False, True, False]
                    and ns.get("wkProtoOk") is True
                    and ns.get("wkOwn") == []),
                "CriOS：messageHandlers 为 UserMessageHandlersNamespace 实例": (
                    ns.get("mhProtoOk") is True
                    and ns.get("mhKeys") == []),
                "CriOS：crwebinvoke.postMessage 原生外观且返回 pending Promise": (
                    (ns.get("crweb") or {}).get("protoOk") is True
                    and (ns.get("crweb") or {}).get("pmLen") == 1
                    and (ns.get("crweb") or {}).get("pmNative") is True
                    and (ns.get("crweb") or {}).get("pmRet") == "promise"),
                "CriOS：buffers 为空 WebKitBufferNamespace（LegacyNoInterfaceObject）": (
                    ns.get("buffersType") == "object"
                    and ns.get("buffersKeys") == []
                    and state["wkBuffersTag"] == "[object WebKitBufferNamespace]"
                    and state["wkBuffersCtorName"] == "WebKitBufferNamespace"
                    and state["wkBuffersGlobal"] == "undefined"),
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
                # ---- POST 重放面（B1-7）：只在 POST 文档出现 ----
                # 真机源码：post_request.js 把 __crPostRequestWorkaround 定义为
                # 命名空间对象（不是函数），两个回执通道由路由在重放期间临时注册。
                "CriOS POST：__crPostRequestWorkaround 为命名空间对象": (
                    post.get("present") is True
                    and post.get("type") == "object"
                    and post.get("keys") == ["runPostRequest"]),
                "CriOS POST：runPostRequest 四参且非 native": (
                    post.get("runLen") == 4 and post.get("runNative") is False),
                "CriOS POST：函数源文本为 Chrome 61 原文（同步 XHR + document.write）": (
                    isinstance(post.get("runSrc"), str)
                    and "function(\n    url, headers, body, contentType) {"
                    in post["runSrc"]
                    and "new XMLHttpRequest()" in post["runSrc"]
                    and "document.open();" in post["runSrc"]
                    and "window.webkit.messageHandlers['POSTSuccessHandler']"
                    in post["runSrc"]),
                "CriOS POST：两个回执通道存在且不可枚举": (
                    post.get("chanSuccess") is True
                    and post.get("chanError") is True
                    and post.get("chanInvoke") is True
                    and post.get("chanOwn") == []
                    and post.get("mhProtoOwn") == ["constructor"]),
                "CriOS POST：标记 cookie / 交接标记均已清": (
                    post.get("cookie") == ""
                    and post.get("flagLeak") is False
                    and post_state.get("cookie_leak") is False),
                "反证：GET 文档无重放面（导航前/后）": (
                    (post_state.get("get_before") or {}).get("present") is False
                    and (post_state.get("get_after") or {}).get("present") is False
                    and (post_state.get("get_after") or {}).get("chanSuccess")
                    is False),
            })
        else:
            checks.update({
                "Safari：UA 不含 CriOS": state["isCriOS"] is False,
                "Safari：无 __gCrWeb（真机 Safari 无该对象）": state["hasGCrWeb"] is False,
                "Safari：无 window.webkit.messageHandlers": state["hasWK"] is False,
                "Safari：无 window.webkit（页面世界未注册消息处理器，JSDOMWindowCustom 门控）": (
                    ns.get("wkType") == "undefined"),
                "Safari：history/print/scrollTo 保持原生外观": (
                    state["historyPatched"] is False
                    and state["printPatched"] is False
                    and state["scrollPatched"] is False),
                # POST 重放面是 CriOS 宿主行为，Safari 的 POST 文档上不存在
                "Safari POST：无重放面": (
                    post.get("present") is False
                    and not post.get("chanSuccess")),
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

    # ---- 媒体查询面（全平台）：CDP 触控仿真的语义断言 ----
    # 声明同步：app/mobile_cdp_injector.py::_apply_device_emulation 的
    # setDeviceMetricsOverride(mobile=True)；仿真通道断开时这些查询回落宿主
    # 桌面值（pointer:fine / hover:hover）——检测报告 12 项的共同根因。
    mq = state.get("mediaQueries") or {}
    asr = state.get("audioSampleRate") or {}
    hs = heal_state or {}
    h_new = hs.get("outage_healed") or {}
    h_main = hs.get("main_healed") or {}
    checks.update({
        "媒体查询：pointer:coarse 且无 fine（触控设备语义）": (
            mq.get("pointerCoarse") is True and mq.get("pointerFine") is False),
        "媒体查询：hover:none 且无 hover（无悬停设备语义）": (
            mq.get("hoverNone") is True and mq.get("hoverHover") is False),
        "媒体查询：any-pointer:coarse / any-hover:none 一致": (
            mq.get("anyPointerCoarse") is True and mq.get("anyHoverNone") is True),
        # F-12：HDR 屏机型 (dynamic-range: high) 为 true（CDP 无法仿真，
        # 由 matchMedia 补丁按机型给值）；SE 系列为 false。Android 侧不补
        # 该特性（真机 Chrome Android 的取值随设备屏而变，未做机型表），
        # 只断言查询串原样透出、不被补丁改写。
        "媒体查询：dynamic-range 查询串原样（Android 未启用该补丁）": (
            mq.get("dynamicRangeMedia") == "(dynamic-range: high)"),
        # 真机口径：内核 --disable-spoofing=audio 已关闭「按种子微扰采样率」的
        # 伪装（实测宿主 44100 → 伪装 44100.01171875），采样率必须精确整数。
        "音频：OfflineAudioContext 采样率为精确整数（44100/48000）": (
            isinstance(asr.get("s44100"), (int, float))
            and abs(asr["s44100"] - 44100) < 1e-6
            and isinstance(asr.get("s48000"), (int, float))
            and abs(asr["s48000"] - 48000) < 1e-6),
        # ---- 断线自愈（看门狗）：事故形态复现 + 自动恢复 ----
        "自愈：通道断开即标记重连中（面板状态可见）": (
            hs.get("state_down") == "reconnecting"),
        # F-01：连接成功 ≠ 仿真生效——注入后主动读一次真实读数
        "注入后读数校验：maxTouchPoints/pointer 已生效（state=ok）": (
            hs.get("verified_before") is True
            and hs.get("state_before") == "ok"),
        "自愈：看门狗自动重连（计数递增，状态回 ok）": (
            hs.get("healthy") is True
            and hs.get("state_after") == "ok"
            and int(hs.get("reconnects") or 0) >= 1),
        "自愈：断线期间新标签页已重放（触控/媒体查询/移动 UA）": (
            int(h_new.get("touch") or 0) >= 5
            and h_new.get("coarse") is True
            and h_new.get("uaMobile") is True),
        "自愈：断线前已存在页面已重放（触控/媒体查询）": (
            int(h_main.get("touch") or 0) >= 5 and h_main.get("coarse") is True),
    })

    # F-12：iOS 的 HDR 屏口径（CDP 无法仿真，由 matchMedia 补丁按机型给值）。
    if target_os == "ios":
        checks["媒体查询：dynamic-range 与机型口径一致（HDR 屏 true / SE false）"] = (
            mq.get("dynamicRangeHigh") is (
                "SE" not in (extra.get("marketName") or ""))
            and mq.get("dynamicRangeStandard") is True)

    # ---- F-15：孤儿实例回收 ----
    # 探针自己拉起的浏览器在 finally 里已被关掉；此刻 profile 目录下不应
    # 再有「父进程已消失」的浏览器进程。回收器先跑一遍，再把结果与
    # 「跑完后仍剩多少」对上——只断言不残留，不断言回收了谁（并发跑别的
    # 用例时可能有别的实例在场，断言数量会误报）。
    try:
        from app.config import PROFILE_ROOT as _profile_root
        from app.kernels import fp_chromium_kernel as _fpk
        _fpk.reclaim_orphans(_profile_root)
        _root_text = str(_profile_root)
        _left = [p for p in _fpk._iter_browser_processes()
                 if p[2].startswith(_root_text + os.sep)]
        _live_left = []
        for _pid, _ppid, _ in _left:
            if _ppid != 1 and _fpk._alive(_ppid):
                continue
            _live_left.append(_pid)
        checks["F-15：profile 目录下无孤儿浏览器残留（父进程已消失）"] = (
            not _live_left)
    except Exception as e:  # noqa: BLE001
        checks["F-15：profile 目录下无孤儿浏览器残留（父进程已消失）"] = False
        print("      F-15 检查异常:", e)

    print("\n== 检查项 ==")
    ok_all = True
    for name, passed in checks.items():
        print(("  ✅ " if passed else "  ❌ ") + name)
        if not passed and os.environ.get("VERIFY_DEBUG"):
            keys = (("wcCodecs",) if "codec 矩阵" in name
                    else ("wkIcs",) if "F-133" in name
                    else ("wkSw",) if "serviceWorker" in name else ("intlZh",))
            print("      state:", json.dumps(
                {k: v for k, v in state.items() if k in keys}, ensure_ascii=False))
        ok_all = ok_all and passed
    print("\n" + (f"✅ 全部通过（{len(checks)} 项）" if ok_all
                  else f"❌ 存在失败项（{len(checks)} 项）"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        import shutil
        shutil.rmtree(os.environ.get("FPWB_HOME", ""), ignore_errors=True)
    sys.exit(code)
