"""真机对拍共用探针载荷（仿真侧与真机侧加载同一份 JS）。

为什么要共用：对拍的前提是「两侧跑的是同一段读数代码」。把读数 JS 复制到
采集页面里、改一边忘一边，diff 出来的差异就分不清是环境差异还是代码漂移。

载荷是一个纯函数 `fpwbProbe(opts)`：
  opts.part      只跑单面（main / engine / platform / sensor），仿真侧分次调用
  opts.sensorMs  传感器观测窗口（默认 900ms）
  opts.hooks     装真机页面交互面板（授权按钮 / 复制 / 上传），仿真侧不装

读数结构对应 docs/移动端大厂检测对抗优化方案.md 的读数矩阵；
归一化与分类规则见 scripts/mobile_probe_compare.py。

生成方式：由 scripts/mobile_bigtech_probe.py 的原始 JS 机械抽取（AST +
字符串改写），抽取前后逐字一致。改动读数逻辑请改这里，不要再往探针里加。
"""

PROBE_VERSION = 1

# 函数声明（不是 IIFE）：仿真侧用 `eval_expr()` 包成 IIFE 交给 evaluate，
# 不落全局；真机页面用 `new Function(PROBE_FN_JS + "\nreturn fpwbProbe;")`。
PROBE_FN_JS = r"""
async function fpwbProbe(opts) {
  opts = opts || {};
  var sensorMs = (typeof opts.sensorMs === 'number') ? opts.sensorMs : 900;
  var part = opts.part || null;
  var PROBE_VERSION = 1;

  // ---- 采集编排 ----
  // part 只跑单面（仿真侧按原顺序分次调用）；不传 = 全量（真机页面）。
  async function collect(only) {
    var parts = {};
    if (!only || only === 'main') parts.main = await readMain();
    if (!only || only === 'engine') parts.engine = readEngine();
    if (!only || only === 'platform') parts.platform = await readPlatform();
    if (!only || only === 'sensor') parts.sensor = await readSensor(sensorMs);
    if (!only || only === 'crios') parts.crios = readCrios();
    return { probe_version: PROBE_VERSION, collected_at: new Date().toISOString(),
             parts: parts };
  }

  if (opts.hooks) { installHooks(collect); return { hooks_installed: true }; }
  return await collect(part);

  // ---- 真机页面钩子 ----
  // iOS 13+ 的传感器授权必须发生在用户手势里，且一次手势只够授权一个
  // 构造器；两个都授权要再点一次。状态行如实显示授权结果。
  async function askSensorPermission() {
    var res = { motion: 'n/a', orientation: 'n/a' };
    var DM = window.DeviceMotionEvent;
    var DO = window.DeviceOrientationEvent;
    try {
      if (DM && typeof DM.requestPermission === 'function') res.motion = await DM.requestPermission();
      else if (DM) res.motion = 'not-required';
    } catch (e) { res.motion = 'throw:' + ((e && e.name) || e); }
    try {
      if (DO && typeof DO.requestPermission === 'function') res.orientation = await DO.requestPermission();
      else if (DO) res.orientation = 'not-required';
    } catch (e) { res.orientation = 'throw:' + ((e && e.name) || e); }
    return res;
  }

  function installHooks(run) {
    var box = document.createElement('div');
    box.id = 'fpwb-probe';
    box.style.cssText = 'position:fixed;left:0;right:0;bottom:0;z-index:2147483647;'
      + 'background:#111;color:#eee;font:13px/1.5 -apple-system,system-ui,sans-serif;'
      + 'padding:12px 14px calc(12px + env(safe-area-inset-bottom));max-height:72vh;overflow:auto;'
      + '-webkit-text-size-adjust:100%';
    var title = document.createElement('div');
    title.textContent = 'fpwb 真机读数采集';
    title.style.cssText = 'font-weight:600;font-size:15px;margin-bottom:4px';
    var note = document.createElement('div');
    note.style.cssText = 'margin-bottom:10px;' + (isSecureContext ? 'opacity:.75' : 'color:#ff8a80');
    note.textContent = isSecureContext
      ? '安全上下文：是 —— 传感器可派发'
      : '安全上下文：否 —— 传感器不会派发，读数无效（改用 https 或 localhost）';
    var btn = document.createElement('button');
    btn.textContent = '授权传感器并开始采集';
    btn.style.cssText = 'font:inherit;padding:12px 16px;border-radius:8px;border:0;'
      + 'background:#2d7ff9;color:#fff;width:100%;margin-bottom:8px';
    var status = document.createElement('div');
    status.style.cssText = 'opacity:.85;min-height:20px;margin-bottom:8px;word-break:break-all';
    var out = document.createElement('textarea');
    out.readOnly = true;
    out.spellcheck = false;
    out.style.cssText = 'width:100%;height:150px;font:11px/1.4 ui-monospace,Menlo,monospace;'
      + 'background:#000;color:#9f9;border:1px solid #333;border-radius:6px;padding:8px;'
      + 'box-sizing:border-box';
    var row = document.createElement('div');
    row.style.cssText = 'display:flex;gap:8px;margin-top:8px';
    var copyBtn = document.createElement('button');
    copyBtn.textContent = '复制 JSON';
    var sendBtn = document.createElement('button');
    sendBtn.textContent = '上传到电脑';
    for (var b = 0; b < 2; b++) {
      var el = b === 0 ? copyBtn : sendBtn;
      el.style.cssText = 'flex:1;font:inherit;padding:10px;border-radius:8px;border:1px solid #444;'
        + 'background:#222;color:#eee';
      row.appendChild(el);
    }
    box.appendChild(title);
    box.appendChild(note);
    box.appendChild(btn);
    box.appendChild(status);
    box.appendChild(out);
    box.appendChild(row);
    document.body.appendChild(box);

    var payload = null;
    btn.onclick = async function () {
      btn.disabled = true;
      status.textContent = '请求传感器授权…';
      var perm = await askSensorPermission();
      status.textContent = '采集中（约 3 秒）… 授权结果 ' + JSON.stringify(perm);
      try {
        payload = await run();
        payload.sensor_permission = perm;
        out.value = JSON.stringify(payload, null, 1);
        document.title = 'fpwb-probe-done';
        status.textContent = '完成。授权 ' + JSON.stringify(perm)
          + (perm.motion === 'granted' && perm.orientation === 'granted'
             ? '' : '（未全部授权时再次点击可补授权）');
      } catch (e) {
        status.textContent = '采集失败：' + ((e && e.message) || e);
      }
      btn.disabled = false;
    };
    copyBtn.onclick = function () {
      if (!payload) { status.textContent = '还没有采集结果'; return; }
      var text = JSON.stringify(payload, null, 1);
      var done = function () { status.textContent = '已复制到剪贴板'; };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { out.select(); });
      } else { out.select(); }
    };
    sendBtn.onclick = function () {
      if (!payload) { status.textContent = '还没有采集结果'; return; }
      status.textContent = '上传中…';
      fetch('/submit', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                         body: JSON.stringify(payload) })
        .then(function (r) { return r.text(); })
        .then(function (t) { status.textContent = '已上传：' + t; })
        .catch(function (e) { status.textContent = '上传失败（可改用复制）：' + e.message; });
    };
  }

async function readMain() {
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
}

function readEngine() {
  // 引擎面「一行判假」读数：全屏面 / <video> webkit 全屏家族 / WebKit 专属
  // CSS 面 / 传感器构造器与 legacy 链。真机 iOS 与 Chrome Android 在这些
  // 读数上互斥（见 docs/移动端大厂检测对抗优化方案.md）。
  const fsNames = (obj) => Object.getOwnPropertyNames(obj).filter((n) => /fullscreen/i.test(n));
  const chain = (kind) => {
    try {
      const e = document.createEvent(kind);
      const init = (kind === 'DeviceOrientationEvent')
        ? e.initDeviceOrientationEvent : e.initDeviceMotionEvent;
      if (typeof init !== 'function') return 'no-init';
      if (kind === 'DeviceOrientationEvent') {
        init.call(e, 'deviceorientation', false, false, 10, 20, 30, 45, 5);
        return [e.type, e.alpha, e.beta, e.gamma, e.webkitCompassHeading, e.webkitCompassAccuracy];
      }
      init.call(e, 'devicemotion', false, false, {x: 1, y: 2, z: 3},
                {x: 4, y: 5, z: 6}, {alpha: 7, beta: 8, gamma: 9}, 16);
      return [e.type, e.acceleration.x, e.accelerationIncludingGravity.y,
              e.rotationRate.gamma, e.interval];
    } catch (err) { return 'throw:' + err.name; }
  };
  const ctor = (name) => {
    try { new window[name]('x'); return 'ok'; } catch (e) { return e.name; }
  };
  const v = document.createElement('video');
  const s = document.body.style;
  // 先取「属性存在性」再跑写入回环：写未知驼峰属性会在 Chromium 上留下 JS
  // expando，污染 `in` 读数（读真机时同一顺序同样关键）。
  const tcInStyle = 'webkitTouchCallout' in s;
  const oscInStyle = 'webkitOverflowScrolling' in s;
  const styleRoundtrip = (() => {
    const before = s.webkitTouchCallout;
    s.webkitTouchCallout = 'none';
    const mid = s.webkitTouchCallout;
    s.removeProperty('-webkit-touch-callout');
    return [before, mid, s.webkitTouchCallout];
  })();
  return {
    fullscreen: {
      elementOwn: fsNames(Element.prototype),
      documentOwn: fsNames(Document.prototype),
      requestType: typeof document.documentElement.requestFullscreen,
      enabledType: typeof document.fullscreenEnabled,
      exitType: typeof document.exitFullscreen,
      elementInDoc: 'fullscreenElement' in document,
    },
    video: {
      enterType: typeof v.webkitEnterFullscreen,
      supportsType: ('webkitSupportsFullscreen' in v)
        ? typeof v.webkitSupportsFullscreen : 'ABSENT',
    },
    css: {
      touchCallout: CSS.supports('-webkit-touch-callout', 'none'),
      touchCalloutOneArg: CSS.supports('-webkit-touch-callout: none'),
      overflowScrolling: CSS.supports('-webkit-overflow-scrolling', 'touch'),
      touchCalloutInStyle: tcInStyle,
      overflowScrollingInStyle: oscInStyle,
      styleRoundtrip: styleRoundtrip,
    },
    sensors: {
      dmeCtor: ctor('DeviceMotionEvent'),
      doeCtor: ctor('DeviceOrientationEvent'),
      compassHeadingIn: 'webkitCompassHeading' in DeviceOrientationEvent.prototype,
      absoluteInProto: 'absolute' in DeviceOrientationEvent.prototype,
      initDOE: typeof DeviceOrientationEvent.prototype.initDeviceOrientationEvent,
      initDME: typeof DeviceMotionEvent.prototype.initDeviceMotionEvent,
      doeChain: chain('DeviceOrientationEvent'),
      dmeChain: chain('DeviceMotionEvent'),
    },
    // Safari 全局面（item 13：WebKitPoint 家族 / WebKitNamespace 家族 /
    // webkitCancelRequestAnimationFrame）。真机 iOS 有、桌面内核无。
    safari: {
      webKitPoint: typeof window.WebKitPoint,
      wkpProto: window.WebKitPoint
        ? Object.getOwnPropertyNames(window.WebKitPoint.prototype) : null,
      wkpVals: (() => {
        try {
          if (typeof window.WebKitPoint !== 'function') return null;
          const p = new WebKitPoint(1.5, 2.5); return [p.x, p.y];
        }
        catch (e) { return 'throw:' + e.name; }
      })(),
      convertNodeToPage: typeof window.webkitConvertPointFromNodeToPage,
      convertPageToNode: typeof window.webkitConvertPointFromPageToNode,
      convertLen: window.webkitConvertPointFromNodeToPage
        ? window.webkitConvertPointFromNodeToPage.length : null,
      convertRoundtrip: (() => {
        try {
          const f1 = window.webkitConvertPointFromNodeToPage;
          const f2 = window.webkitConvertPointFromPageToNode;
          if (typeof f1 !== 'function' || typeof f2 !== 'function') return null;
          const p = f1(document.body, new WebKitPoint(5, 40));
          const b = f2(document.body, p);
          return [Math.round(b.x), Math.round(b.y)];
        } catch (e) { return 'throw:' + e.name; }
      })(),
      craf: typeof window.webkitCancelRequestAnimationFrame,
      webKitNamespace: typeof window.WebKitNamespace,
      nsProto: window.WebKitNamespace
        ? Object.getOwnPropertyNames(window.WebKitNamespace.prototype) : null,
      userMsgHandlersNs: typeof window.UserMessageHandlersNamespace,
      userMsgHandler: typeof window.UserMessageHandler,
      webKitJSHandle: typeof window.WebKitJSHandle,
      webKitNodeSnapshot: typeof window.WebKitNodeSnapshot,
      windowWebkit: typeof window.webkit,
      wkHandlers: (window.webkit && window.webkit.messageHandlers)
        ? Object.keys(window.webkit.messageHandlers) : null,
      wkHandlersOwn: (window.webkit && window.webkit.messageHandlers)
        ? Object.getOwnPropertyNames(window.webkit.messageHandlers) : null,
      wkHandlerIn: (window.webkit && window.webkit.messageHandlers)
        ? ('crwebinvoke' in window.webkit.messageHandlers) : null,
      wkHandlerUnknown: (window.webkit && window.webkit.messageHandlers)
        ? typeof window.webkit.messageHandlers.__no_such_handler__ : null,
      wkHandlerDesc: (() => {
        const mh = window.webkit && window.webkit.messageHandlers;
        if (!mh) return null;
        const d = Object.getOwnPropertyDescriptor(mh, 'crwebinvoke');
        return d ? [d.writable, d.enumerable, d.configurable] : null;
      })(),
      buffersTag: (window.webkit && window.webkit.buffers)
        ? Object.prototype.toString.call(window.webkit.buffers) : null,
      buffersCtor: (() => {
        const b = window.webkit && window.webkit.buffers;
        if (!b) return null;
        const p = Object.getPrototypeOf(b);
        return (p && p.constructor) ? p.constructor.name : null;
      })(),
      crwebInvoke: !!(window.webkit && window.webkit.messageHandlers
        && window.webkit.messageHandlers.crwebinvoke),
      crwebPmRet: (() => {
        try {
          const h = window.webkit && window.webkit.messageHandlers
            && window.webkit.messageHandlers.crwebinvoke;
          if (!h) return null;
          const p = h.postMessage(1);
          return (p && typeof p.then === 'function') ? 'promise' : typeof p;
        } catch (e) { return 'throw:' + e.name; }
      })(),
    },
  };
}

async function readSensor(ms) {
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
  await new Promise((r) => setTimeout(r, (typeof ms === 'number') ? ms : 900));
  return out;
}

async function readPlatform() {
  // 平台 API 差集读数（真机口径对比用）：窗口级 / document / navigator 三处
  // 的 [typeof, 名字是否仍 in 对象] + Worker 全局同名读数。
  const t = (obj, n) => { try { return typeof obj[n]; } catch (e) { return 'ERR'; } };
  const has = (obj, n) => { try { return n in obj; } catch (e) { return 'ERR'; } };
  const read = (obj, names) => {
    const o = {};
    for (const n of names) { o[n] = [t(obj, n), has(obj, n)]; }
    return o;
  };
  const out = {
    win: read(window, ['Sensor', 'SensorErrorEvent', 'Accelerometer', 'Gyroscope',
      'LinearAccelerationSensor', 'GravitySensor', 'AbsoluteOrientationSensor',
      'RelativeOrientationSensor', 'EyeDropper', 'IdleDetector', 'WebSocketStream',
      'ImageDecoder', 'FileSystemObserver', 'queryLocalFonts',
      'documentPictureInPicture', 'launchQueue', 'Scheduler', 'scheduler',
      'OTPCredential', 'Ink', 'MediaStreamTrackGenerator', 'sharedStorage',
      'SharedStorage', 'fence', 'Fence', 'HTMLFencedFrameElement',
      'showSaveFilePicker', 'showDirectoryPicker']),
    doc: read(document, ['browsingTopics']),
    nav: read(navigator, ['xr', 'virtualKeyboard', 'ink', 'runAdAuction',
      'joinAdInterestGroup', 'leaveAdInterestGroup', 'deprecatedReplaceInURN',
      'updateAdInterestGroups', 'protectedAudience']),
    kept: read(window, ['VideoDecoder', 'VideoFrame', 'AudioDecoder',
      'EncodedAudioChunk', 'URLPattern', 'Highlight', 'BarcodeDetector',
      'ImageCapture', 'MediaStreamTrackProcessor', 'navigation', 'GPU',
      'FileSystemHandle', 'FileSystemFileHandle', 'FileSystemDirectoryHandle',
      'FileSystemWritableFileStream']),
    keptDoc: read(document, ['startViewTransition', 'hasStorageAccess',
      'requestStorageAccess', 'fragmentDirective']),
    keptNav: read(navigator, ['share', 'canShare', 'wakeLock', 'gpu']),
    worker: null,
  };
  try {
    const names = ['IdleDetector', 'WebSocketStream', 'ImageDecoder',
      'FileSystemObserver', 'Scheduler', 'scheduler', 'VideoDecoder',
      'BarcodeDetector', 'URLPattern', 'WebGL2RenderingContext'];
    const src = 'self.onmessage = () => { const t = (n) => {'
      + ' try { return typeof self[n]; } catch (e) { return "ERR"; } };'
      + ' const names = ' + JSON.stringify(names) + '; const o = {};'
      + ' for (const n of names) { o[n] = t(n); } postMessage(o); };';
    const w = new Worker(URL.createObjectURL(new Blob([src])));
    out.worker = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('timeout')), 5000);
      w.onmessage = (e) => { clearTimeout(timer); resolve(e.data); };
      w.onerror = () => { clearTimeout(timer); reject(new Error('worker-error')); };
      w.postMessage(1);
    });
    w.terminate();
  } catch (e) { out.worker = 'ERR:' + e.name + ':' + e.message; }
  return out;
}

function readCrios() {
  // iOS Chrome 的「宿主壳」检测面：__gCrWeb 成员全集与调用通道、windowId 形态、
  // history/print/scrollTo 的源码文本、web_bundle 版本痕迹、standalone 口径。
  // 这些在仿真侧是静态补丁，成员全集与格式只能靠真机抓取校准
  // （见 docs/移动端真实指纹库.md §9、docs/Chrome61-IPA检测机制分析.md）。
  // Safari 上没有 __gCrWeb，本面全为空——正好作为口径判据。
  var safe = function (fn) { try { return fn(); } catch (e) { return 'ERR:' + ((e && e.name) || e); } };
  var own = function (o) { return Object.getOwnPropertyNames(o).sort(); };
  var src = function (f) {
    try { return (typeof f === 'function') ? Function.prototype.toString.call(f) : null; }
    catch (e) { return 'ERR:' + ((e && e.name) || e); }
  };
  var desc = function (o, k) {
    var d = Object.getOwnPropertyDescriptor(o, k);
    return d ? [d.writable, d.enumerable, d.configurable] : null;
  };
  var gcr = window.__gCrWeb;
  var out = {
    // 存在性 / 全局描述符
    hasGcrWeb: (typeof gcr !== 'undefined'),
    gcrDesc: safe(function () { return desc(window, '__gCrWeb'); }),
    gcrKeys: safe(function () { return gcr ? own(gcr) : null; }),
    // windowId 形态：长度 + 字符类（不含具体值，避免把会话标识写进文件）
    windowId: safe(function () {
      if (!gcr || typeof gcr.windowId !== 'string') return null;
      var v = gcr.windowId;
      return { type: typeof v, len: v.length,
               hex: /^[0-9a-f]+$/i.test(v), uuid: /^[0-9a-f-]{32,36}$/i.test(v),
               digitsOnly: /^\d+$/.test(v), sampleMask: v.replace(/[0-9a-f]/gi, 'x') };
    }),
    // 子命名空间成员（逐层）
    namespaces: safe(function () {
      var names = ['common', 'message', 'console', 'autofill', 'suggestion',
                   'contextualSearch', 'languageDetection', 'translate',
                   'findInPage', 'plugin'];
      var o = {};
      for (var i = 0; i < names.length; i++) {
        var ns = gcr && gcr[names[i]];
        o[names[i]] = ns ? own(ns) : null;
      }
      return o;
    }),
    // 通道对象的可调用性：真机 message.invokeOnHost 是宿主桥
    invokeOnHostType: safe(function () {
      return (gcr && gcr.message && gcr.message.invokeOnHost)
        ? typeof gcr.message.invokeOnHost : null;
    }),
    // 改写痕迹：源码文本能直接看出是不是 web_bundle 注入
    historySrc: safe(function () {
      var H = History.prototype;
      return { pushState: src(H.pushState), replaceState: src(H.replaceState),
               back: src(H.back), forward: src(H.forward), go: src(H.go) };
    }),
    printSrc: safe(function () { return src(window.print); }),
    scrollToSrc: safe(function () { return src(window.scrollTo); }),
    // web_bundle 版本痕迹：真机脚本会挂版本号/初始化标记
    versionHints: safe(function () {
      var keys = own(window).filter(function (k) {
        return /crweb|gcrweb|chromeios|crios/i.test(k);
      });
      var o = {};
      for (var i = 0; i < keys.length; i++) o[keys[i]] = typeof window[keys[i]];
      return o;
    }),
    // WKWebView 壳的 webkit 命名空间（CriOS 与 Safari 都有，成员集不同）
    webkitKeys: safe(function () {
      return (window.webkit) ? own(window.webkit) : null;
    }),
    webkitHandlers: safe(function () {
      var mh = window.webkit && window.webkit.messageHandlers;
      return mh ? own(mh) : null;
    }),
    // PWA / 全屏口径：iOS 上 Safari 与 CriOS 都是 standalone 才有
    standalone: {
      navStandalone: safe(function () {
        return (typeof navigator.standalone === 'boolean') ? navigator.standalone : null;
      }),
      displayStandalone: safe(function () {
        return matchMedia('(display-mode: standalone)').matches;
      }),
    },
    // iPadOS 13+ 默认桌面 UA，触控点数可区分 iPad（5）与 Mac（0）
    padDetection: safe(function () {
      return { maxTouchPoints: navigator.maxTouchPoints,
               uaIsMac: /Macintosh/.test(navigator.userAgent),
               uaIsIPad: /iPad/.test(navigator.userAgent) };
    }),
  };
  return out;
}
}

"""


def eval_expr(part: str | None = None, sensor_ms: int = 900) -> str:
    """仿真侧 evaluate 表达式：跑完即返回，不留全局变量。"""
    opts = []
    if part:
        opts.append("part: '" + part + "'")
    if sensor_ms != 900:
        opts.append("sensorMs: " + str(sensor_ms))
    return ("(() => {" + PROBE_FN_JS + "\nreturn fpwbProbe({"
            + ", ".join(opts) + "});\n})()")
