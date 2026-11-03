"""环境就绪度检测：在真实浏览器内实测各风控体系关注的环境一致性维度。

设计思路：风控系统（Cloudflare / Google reCAPTCHA / 极验 / 网易易盾）判定"可疑环境"
的核心信号不是单个指纹值，而是**矛盾**——IP 属地与时区/语言不符、WebRTC 暴露真实 IP、
UA 声称 A 设备但 Canvas/GPU 是 B、自动化标记残留等。本引擎启动（或复用）环境浏览器，
在页面内实地采样并逐项判定，输出可直接执行的整改建议。

检测项（均为运行时实测，非静态检查）:
  ip_reachable   代理/网络可达性（浏览器网络栈内访问 ip-api）
  ip_continuity  同一次体检内的出口 IP 连续性（发现轮换代理）
  ip_history     跨次体检/重启的出口 IP 连续性
  tz_match       浏览器时区 vs 出口 IP 属地时区
  locale_match   浏览器语言 vs 出口 IP 国家（CN→zh、欧美→en）
  webrtc_leak    WebRTC 候选公网 IP 是否与出口 IP 一致（或已禁用）
  webdriver      navigator.webdriver 自动化标记
  canvas_stable  同一会话内 Canvas 指纹读取稳定性
  webgl_sane     WebGL 渲染器字符串合理（非泛化/软件渲染）
  fonts_render   字体渲染度量有效
  audio_stable   AudioContext 指纹读取稳定性
  ua_chips       Client Hints（userAgentData）平台与目标 OS 一致（Chromium 系）
  screen_sane    屏幕尺寸/色深/像素比合理
  media_devices  媒体设备枚举非空（0 设备是虚拟机特征）
  webgpu         WebGPU 适配器可用性（新兴指纹信号）
  adblock_active 广告拦截探测（扩展可被站点探测且阻断验证域）
  plugins_sane   navigator.plugins 数量合理（真 Chrome 内置 5 个 PDF 插件）
  hw_sane        deviceMemory/hardwareConcurrency 数值合理（Chromium 系）
  turnstile_reachable Cloudflare 验证域可达
  ua_consistency  UA / platform / UA-CH 与存储指纹一致
  mobile_surface  移动端触控与 coarse 指针面一致
  storage_ready   Cookie / localStorage / IndexedDB 可用
  profile_continuity 环境数据目录可持续复用
  emulation_channel 移动端 CDP 仿真通道健康
  headless_mode   登录场景是否仍以无头模式启动
"""
import asyncio
import json
import logging
import re
import time
from urllib.parse import quote
from pathlib import Path
from typing import Any, Optional

import httpx
import websockets

from .config import PROFILE_ROOT
from .launcher import LaunchManager
from .models import ProxyConfig
from .proxy_test import canonical_timezone, test_proxy

log = logging.getLogger(__name__)


class _CamoufoxPage:
    """camoufox 内核：playwright 上下文里开新页做检查（不打扰用户页面）。"""

    def __init__(self, context):
        self.context = context
        self.page = None

    async def open(self) -> None:
        self.page = await self.context.new_page()

    async def goto(self, url: str) -> None:
        await self.page.goto(url, wait_until="domcontentloaded", timeout=45_000)

    async def evaluate(self, js: str) -> Any:
        return await self.page.evaluate(js)

    async def close(self) -> None:
        if self.page:
            try:
                await self.page.close()
            except Exception:
                pass


class _CdpPage:
    """chromium 系内核（fp-chromium / chromium）：新建 CDP 目标页做检查。"""

    def __init__(self, port: int):
        self.port = port
        self.ws = None
        self.target_id = None
        self._id = 0

    async def open(self) -> None:
        # 回环地址控制 CDP：忽略系统/环境代理，否则 HTTP_PROXY 会把 127.0.0.1 请求转发出去
        async with httpx.AsyncClient(trust_env=False) as c:
            r = await c.put(f"http://127.0.0.1:{self.port}/json/new?about:blank", timeout=10)
            info = r.json()
        self.target_id = info["id"]
        self.ws = await websockets.connect(
            info["webSocketDebuggerUrl"], max_size=8 << 20, proxy=None)

    async def _send(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        await self.ws.send(json.dumps({"id": self._id, "method": method, "params": params or {}}))
        while True:
            resp = json.loads(await asyncio.wait_for(self.ws.recv(), 30))
            if resp.get("id") == self._id:
                return resp

    async def goto(self, url: str) -> None:
        await self._send("Page.navigate", {"url": url})
        for _ in range(30):  # 等待加载完成
            await asyncio.sleep(0.5)
            r = await self._send("Runtime.evaluate", {"expression": "document.readyState"})
            if r.get("result", {}).get("result", {}).get("value") in ("complete", "interactive"):
                return

    async def evaluate(self, js: str) -> Any:
        r = await self._send("Runtime.evaluate",
                             {"expression": js, "awaitPromise": True, "returnByValue": True})
        if r.get("result", {}).get("exceptionDetails"):
            raise RuntimeError("页面脚本执行失败")
        return r.get("result", {}).get("result", {}).get("value")

    async def close(self) -> None:
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        if self.target_id:
            try:
                async with httpx.AsyncClient(trust_env=False) as c:
                    await c.get(f"http://127.0.0.1:{self.port}/json/close/{self.target_id}",
                                timeout=5)
            except Exception:
                pass

# 出口 IP 探测：多端点回退（http 优先，https 备用；页面在 https 下时混合内容需走 https 端点）
_IP_PROVIDERS = [
    ("http://ip-api.com/json/?fields=status,message,query,country,countryCode,city,timezone,isp,proxy,hosting,mobile",
     lambda d: {"query": d.get("query"), "country": d.get("country"), "countryCode": d.get("countryCode"),
                "city": d.get("city"), "timezone": d.get("timezone"), "isp": d.get("isp"),
                "proxy": d.get("proxy"), "hosting": d.get("hosting"), "mobile": d.get("mobile")}
     if d.get("status") == "success" else None),
    ("https://ipwho.is/?fields=ip,success,country,country_code,city,timezone,connection",
     lambda d: {"query": d.get("ip"), "country": d.get("country"), "countryCode": d.get("country_code"),
                "city": d.get("city"), "timezone": d.get("timezone"),
                "isp": (d.get("connection") or {}).get("isp"),
                "proxy": None, "hosting": None, "mobile": None}
     if d.get("success") else None),
    ("https://ipinfo.io/json",
     lambda d: {"query": d.get("ip"), "country": d.get("country"), "countryCode": d.get("country"),
                "city": d.get("city"), "timezone": d.get("timezone"), "isp": d.get("org"),
                "proxy": None, "hosting": None, "mobile": None}
     if d.get("ip") else None),
]

# 非安全上下文采样时的改道端点（https，仅为拿到安全上下文，数据不参与判定）
_SECURE_SAMPLE_URLS = ("https://example.com/", "https://ipwho.is/?fields=ip,success")

# 各检测项与风控体系的关联（用于报告展示）
VENDOR_MAP = {
    "ip_reachable": ["cloudflare", "google", "geetest", "yidun"],
    "ip_continuity": ["cloudflare", "google", "geetest", "yidun"],
    "ip_history": ["cloudflare", "google", "geetest", "yidun"],
    "proxy_path_consistency": ["cloudflare", "google", "geetest", "yidun"],
    "ip_quality": ["cloudflare", "google", "geetest", "yidun"],
    "tz_match": ["cloudflare", "google", "geetest", "yidun"],
    "locale_match": ["cloudflare", "geetest", "yidun"],
    "webrtc_leak": ["cloudflare", "google", "geetest", "yidun"],
    "webdriver": ["cloudflare", "google", "geetest", "yidun"],
    "canvas_stable": ["google", "geetest", "yidun"],
    "webgl_sane": ["cloudflare", "google", "geetest", "yidun"],
    "fonts_render": ["geetest", "yidun", "google"],
    "audio_stable": ["google", "geetest", "yidun"],
    "ua_chips": ["cloudflare", "google"],
    "screen_sane": ["cloudflare", "google", "geetest", "yidun"],
    "media_devices": ["google", "yidun"],
    "webgpu": ["cloudflare", "google"],
    "adblock_active": ["cloudflare"],
    "plugins_sane": ["cloudflare", "google"],
    "hw_sane": ["google", "yidun"],
    "turnstile_reachable": ["cloudflare"],
    "ua_consistency": ["cloudflare", "google", "geetest", "yidun"],
    "mobile_surface": ["google", "geetest", "yidun"],
    "storage_ready": ["google", "cloudflare", "geetest", "yidun"],
    "auth_surface": ["google"],
    "profile_continuity": ["google", "cloudflare", "geetest", "yidun"],
    "emulation_channel": ["google", "geetest", "yidun"],
    "headless_mode": ["google", "cloudflare", "geetest", "yidun"],
}

_EXPECT_LANG_PREFIX = {"CN": "zh", "TW": "zh", "HK": "zh", "US": "en", "GB": "en",
                       "JP": "ja", "KR": "ko", "DE": "de", "FR": "fr", "RU": "ru"}

_CHECK_JS = """(async () => {
  const out = {};
  out.tz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  // getBattery / navigator.gpu / mediaDevices 只在安全上下文暴露：IP 端点
  // 首选 http 源，落在那里采样会把它们全读成缺失，必须能区分。
  out.secure = window.isSecureContext;
  out.href = location.href;
  out.lang = navigator.language || '';
  out.langs = (navigator.languages || []).join(',');
  out.platform = navigator.platform || '';
  out.webdriver = navigator.webdriver;
  out.plugins = (navigator.plugins || []).length;
  out.ua = navigator.userAgent || '';
  // Client Hints（Chromium 独有；Firefox 为 undefined 属正常）
  out.chPlatform = navigator.userAgentData ? navigator.userAgentData.platform : null;
  // deviceMemory / 硬件并发（Chromium 独有 deviceMemory）
  out.deviceMemory = navigator.deviceMemory || null;
  out.hw = navigator.hardwareConcurrency || null;
  out.maxTouchPoints = Number(navigator.maxTouchPoints || 0);
  out.touchEvents = 'ontouchstart' in window;
  out.pointerCoarse = !!(window.matchMedia && matchMedia('(pointer: coarse)').matches);
  out.anyPointerCoarse = !!(window.matchMedia && matchMedia('(any-pointer: coarse)').matches);
  out.anyHover = !!(window.matchMedia && matchMedia('(any-hover: hover)').matches);
  out.innerWH = window.innerWidth + 'x' + window.innerHeight;
  out.visualWH = window.visualViewport
    ? (Math.round(window.visualViewport.width) + 'x' + Math.round(window.visualViewport.height))
    : '';
  out.authSurface = {
    secure: !!window.isSecureContext,
    popup: typeof window.open === 'function',
    credentials: !!(navigator.credentials && typeof navigator.credentials.get === 'function'),
    preventSilentAccess: !!(navigator.credentials &&
      typeof navigator.credentials.preventSilentAccess === 'function'),
    publicKey: typeof window.PublicKeyCredential === 'function',
    storageAccess: typeof document.hasStorageAccess === 'function',
  };
  out.cookieEnabled = navigator.cookieEnabled !== false;
  out.storage = {local: false, session: false, indexedDB: typeof indexedDB !== 'undefined'};
  try {
    const k = '__fpwb_readiness__';
    const prior = localStorage.getItem(k);
    localStorage.setItem(k, '1');
    out.storage.local = localStorage.getItem(k) === '1';
    if (prior === null) localStorage.removeItem(k); else localStorage.setItem(k, prior);
  } catch (e) {}
  try {
    const k = '__fpwb_readiness__';
    const prior = sessionStorage.getItem(k);
    sessionStorage.setItem(k, '1');
    out.storage.session = sessionStorage.getItem(k) === '1';
    if (prior === null) sessionStorage.removeItem(k); else sessionStorage.setItem(k, prior);
  } catch (e) {}
  // 高熵 UA-CH 只读一次并设置超时，避免权限提示或精简内核让体检挂起。
  out.chHigh = null;
  if (navigator.userAgentData && navigator.userAgentData.getHighEntropyValues) {
    try {
      out.chHigh = await Promise.race([
        navigator.userAgentData.getHighEntropyValues([
          'platformVersion', 'model', 'uaFullVersion', 'architecture', 'bitness', 'wow64'
        ]),
        new Promise((r) => setTimeout(() => r(null), 1500)),
      ]);
    } catch (e) { out.chHigh = null; }
  }
  // 屏幕与像素比
  out.screenWH = screen.width + 'x' + screen.height;
  out.colorDepth = screen.colorDepth;
  out.dpr = window.devicePixelRatio;
  // 媒体设备枚举（无授权时标签为空但数量可见；0 设备是虚拟机特征）
  try { out.mediaDevices = (await navigator.mediaDevices.enumerateDevices()).length; }
  catch (e) { out.mediaDevices = -1; }
  // 电池 API（Chromium 独有）
  out.hasBattery = typeof navigator.getBattery === 'function';
  // Canvas：同一会话两次读取
  const cv = document.createElement('canvas'); cv.width = 240; cv.height = 40;
  const x = cv.getContext('2d');
  x.textBaseline = 'top'; x.font = "14px 'Arial'";
  x.fillStyle = '#f60'; x.fillRect(100, 1, 62, 20);
  x.fillStyle = '#069'; x.fillText('readiness-check', 2, 15);
  const read = () => {
    const d = cv.toDataURL(); let h = 0;
    for (const ch of d) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    return d.length + ':' + h;
  };
  out.canvas1 = read(); out.canvas2 = read();
  // Audio 指纹：离线渲染压缩器输出（两次，验稳定性）
  try {
    const render = async () => {
      const ac = new OfflineAudioContext(1, 44100, 44100);
      const osc = ac.createOscillator(); osc.type = 'triangle'; osc.frequency.value = 10000;
      const comp = ac.createDynamicsCompressor();
      osc.connect(comp); comp.connect(ac.destination); osc.start(0);
      const buf = await ac.startRendering();
      let s = 0;
      for (let i = 4500; i < 5000; i++) s += Math.abs(buf.getChannelData(0)[i]);
      return s;
    };
    out.audio1 = await render(); out.audio2 = await render();
  } catch (e) { out.audio1 = out.audio2 = null; }
  // WebGL 渲染器
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const ext = gl.getExtension('WEBGL_debug_renderer_info');
    out.webglRenderer = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : '';
  } catch (e) { out.webglRenderer = ''; }
  // WebGPU 适配器（3 秒超时，防挂起）
  out.webgpu = await Promise.race([
    (async () => {
      if (!navigator.gpu) return 'unsupported';
      try {
        const a = await navigator.gpu.requestAdapter();
        if (!a) return 'no-adapter';
        return 'ok' + (a.info && a.info.architecture ? ':' + a.info.architecture : '');
      } catch (e) { return 'error'; }
    })(),
    new Promise((r) => setTimeout(() => r('timeout'), 3000)),
  ]);
  // 字体渲染
  const s = document.createElement('span');
  s.style.cssText = 'position:absolute;left:-9999px;font-size:72px;white-space:nowrap';
  s.textContent = 'mmmmmmmmmmlli';
  document.body.appendChild(s);
  s.style.fontFamily = 'Arial';
  out.fontWidth = s.offsetWidth;
  s.remove();
  // WebRTC：收集 ICE 候选（5 秒超时）
  out.srflx = await new Promise((resolve) => {
    let done = false; const ips = [];
    try {
      const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:stun.l.google.com:19302'}]});
      pc.createDataChannel('c');
      pc.onicecandidate = (e) => {
        if (!e.candidate) { if (!done) { done = true; resolve(ips); } return; }
        const m = e.candidate.candidate.match(/candidate:\\S+ \\d+ \\S+ \\d+ (\\S+)/);
        const ip = m && m[1];
        if (ip && /^[0-9a-fA-F.:]+$/.test(ip)) {
          // 仅记录公网地址（不含 192.168./10./172.16-31. 与本地 v6）
          const parts = ip.split('.');
          const isPrivate = parts.length === 4 &&
            (parts[0] === '10' || parts[0] === '192' && parts[1] === '168' ||
             parts[0] === '172' && +parts[1] >= 16 && +parts[1] <= 31 ||
             parts[0] === '127' || parts[0] === '169' && parts[1] === '254');
          const isLinkLocalV6 = ip.startsWith('fe80') || ip.startsWith('::1');
          if (!isPrivate && !isLinkLocalV6) ips.push(ip);
        }
      };
      pc.createOffer().then(o => pc.setLocalDescription(o)).catch(() => resolve([]));
      setTimeout(() => { if (!done) { done = true; resolve(ips); pc.close(); } }, 5000);
    } catch (e) { resolve([]); }
  });
  // 广告拦截探测：广告域请求被拦截 → 存在广告拦截扩展（可被站点探测）
  out.adblock = await fetch('https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js',
    { mode: 'no-cors' }).then(() => 'no').catch(() => 'yes');
  // Cloudflare Turnstile 验证域可达性（no-cors：网络层被阻断才 reject）
  out.turnstile = await fetch('https://challenges.cloudflare.com/turnstile/v0/api.js',
    { mode: 'no-cors' }).then(() => 'ok').catch(() => 'blocked');
  return out;
})()"""


def _normalize_tz(tz: Any) -> str:
    """ipwho.is 等端点的 timezone 字段是 {'id': 'Asia/Shanghai', ...} 对象，统一取字符串。"""
    return canonical_timezone(tz)


def _result(check_id: str, status: str, detail: str, advice: str = "") -> dict:
    return {
        "id": check_id, "status": status, "detail": detail, "advice": advice,
        "vendors": VENDOR_MAP.get(check_id, []),
    }


def _ua_os(ua: str) -> str:
    """从 UA 提取粗粒度平台，专门用于与 profile 的 target_os 互证。"""
    ua = str(ua or "")
    if "Android" in ua:
        return "android"
    if re.search(r"iPhone|iPad|iPod", ua):
        return "ios"
    if "Windows NT" in ua:
        return "windows"
    if "Mac OS X" in ua:
        return "macos"
    if "X11; Linux" in ua or "Linux x86_64" in ua:
        return "linux"
    return "unknown"


def _ch_major(value: Any) -> str:
    """UA-CH 版本字段的主版本（兼容字符串/缺失值）。"""
    if isinstance(value, dict):
        value = value.get("uaFullVersion") or value.get("fullVersion")
    return str(value or "").split(".", 1)[0]


def _proxy_session_advice(proxy: Any) -> str:
    """Describe an explicit short proxy-session hint without exposing credentials."""
    username = (proxy.get("username") if isinstance(proxy, dict)
                else getattr(proxy, "username", ""))
    username = str(username or "")
    match = re.search(r"(?:^|[-_])sess(?:ion)?time[-_=]?(\d+)(?:$|[-_])",
                      username, re.IGNORECASE)
    if not match:
        return ""
    minutes = match.group(1)
    return (f"当前代理会话标记为约 {minutes} 分钟；供应商可能在会话到期后轮换出口，"
            "注册期间应使用更长的固定会话")


async def run_readiness(manager: LaunchManager, profile: dict) -> dict:
    """执行就绪度检测（约 10~40 秒）。返回报告 dict。"""
    started_by_us = False
    inst = manager.get_instance(profile["id"])
    if inst is None:
        await manager.start(profile, headless=True)
        started_by_us = True
        inst = manager.get_instance(profile["id"])

    checks: list[dict] = []
    page = None
    engine_major = ""
    # 移动端 profile：屏幕、插件、Battery/Client Hints/deviceMemory 的存在性
    # 都按真机口径变化，桌面假设会整片误报（详见各检查项注释）。
    target_os = profile.get("target_os") or ""
    mobile = target_os in ("android", "ios")
    spec = (profile.get("fingerprint") or {}).get("fingerprint") or {}
    spec_extra = spec.get("mobileExtra") or {}
    spec_screen = spec.get("screen") or {}
    spec_nav = spec.get("navigator") or {}

    # 这三项不依赖网络页面，先报告出来；即使出口探测失败，用户仍能知道
    # 登录环境是否会丢失 Cookie/存储或移动仿真通道是否处于降级态。
    data_dir = PROFILE_ROOT / str(profile.get("id") or "")
    if data_dir.is_dir():
        checks.append(_result("profile_continuity", "pass",
                              f"环境数据目录可复用（{data_dir.name}）"))
    else:
        checks.append(_result("profile_continuity", "fail",
                              "环境数据目录不存在",
                              "使用同一环境启动并保留其用户数据目录；登录态不要跨环境复制"))

    configured_headless = bool((profile.get("launch") or {}).get("headless", False))
    if configured_headless:
        checks.append(_result("headless_mode", "warn",
                              "环境配置为 headless",
                              "登录前改用有头模式；体检自身为隔离无头启动，不代表正式启动方式"))
    else:
        checks.append(_result("headless_mode", "pass", "环境配置为有头模式"))

    try:
        if inst is None:
            raise RuntimeError("浏览器实例不可用（启动失败）")
        # 按内核选择检查通道：camoufox 用 playwright 上下文；chromium 系用 CDP 新目标页
        # 必须放在 try 内：实例可能是我们刚拉起的，此处报错也要在 finally 里关掉，否则浏览器泄漏
        if profile["kernel"] == "camoufox":
            if inst.context is None:
                raise RuntimeError("camoufox 实例缺少页面上下文")
            page = _CamoufoxPage(inst.context)
        else:
            port = inst.info.get("debug_port")
            if not port:
                raise RuntimeError("chromium 系实例缺少 CDP 调试端口")
            # Browser.getVersion 的 Browser 字段反映真实内核，而页面 UA
            # 可能来自旧环境档案。两者主版本不一致时，登录服务会看到
            # 「引擎 152 + UA 148」这类不可自洽组合；单看页面 UA 无法发现。
            try:
                async with httpx.AsyncClient(trust_env=False) as client:
                    vr = await client.get(
                        f"http://127.0.0.1:{port}/json/version", timeout=5)
                browser_text = str(vr.json().get("Browser") or "")
                vm = re.search(r"(?:Chrome|Chromium|CriOS)/(\d+)", browser_text)
                if vm:
                    engine_major = vm.group(1)
            except Exception:
                engine_major = ""
            page = _CdpPage(port)

        await page.open()

        # 移动仿真会话由 launcher 以后台任务建立；等它完成首屏校验后再判定，
        # 避免把「浏览器刚启动、CDP 尚未挂载」误报为通道降级。
        if mobile:
            emu_state = manager.emulation_state(profile["id"])
            for _ in range(25):
                if emu_state is not None:
                    break
                await asyncio.sleep(0.2)
                emu_state = manager.emulation_state(profile["id"])
            if emu_state == "ok":
                checks.append(_result("emulation_channel", "pass", "移动端仿真通道健康"))
            elif emu_state == "degraded":
                checks.append(_result("emulation_channel", "fail",
                                      "移动端仿真通道已连接但首屏读数未通过",
                                      "重启环境；若重复出现，检查 CDP 注入日志与内核版本"))
            elif emu_state == "reconnecting":
                checks.append(_result("emulation_channel", "fail",
                                      "移动端仿真通道正在重连",
                                      "等待通道恢复为健康后再进入登录页"))
            else:
                checks.append(_result("emulation_channel", "warn",
                                      "未读取到移动端仿真通道状态",
                                      "确认目标环境使用 fp-chromium，并在仿真状态为健康后登录"))

        # ---- 网络路径与出口 IP（浏览器网络栈内实测，天然走代理；多端点回退）
        ipinfo: dict = {}
        ip_errors: list[str] = []
        for url, extract in _IP_PROVIDERS:
            try:
                await page.goto(url)
                raw = json.loads(await page.evaluate("document.body.innerText"))
                info = extract(raw)
                if info and info.get("query"):
                    ipinfo = info
                    break
                ip_errors.append(f"{url.split('/')[2]}: 数据无效")
            except Exception as e:
                ip_errors.append(f"{url.split('/')[2]}: {str(e)[:40]}")
        if not ipinfo:
            checks.append(_result("ip_reachable", "fail",
                                  f"出口探测失败（{'; '.join(ip_errors[:2])}）",
                                  "若浏览器可正常上网，可能是个别探测端点不可用；"
                                  "仍建议确认代理连通性或更换住宅 IP"))
            return _report(profile, checks)
        exit_ip = ipinfo.get("query", "")

        # 跨重启保留最近一次出口，只记录 IP/属地元数据，不写入 Cookie 或
        # 账号信息。代理池常在新连接时换地址；服务端会把这种跳变与同一
        # 设备指纹关联，单次体检的连续性复测捕获不到跨次变化时由此项暴露。
        history_path = data_dir / ".fpwb_readiness_ip.json"
        profile_proxy = profile.get("proxy")
        proxy_session_advice = _proxy_session_advice(profile_proxy)
        previous_ip = ""
        try:
            if history_path.is_file():
                history = json.loads(history_path.read_text(encoding="utf-8"))
                previous_ip = str(history.get("ip") or "")
        except (OSError, ValueError, TypeError):
            previous_ip = ""
        if previous_ip and previous_ip != str(exit_ip):
            advice = "登录期间固定代理会话；跨重启更换出口会触发设备重新确认"
            if proxy_session_advice:
                advice += "；" + proxy_session_advice
            checks.append(_result(
                "ip_history", "warn",
                f"上次体检出口 {previous_ip}，本次为 {exit_ip}",
                advice,
            ))
        else:
            checks.append(_result(
                "ip_history", "pass",
                "首次记录出口" if not previous_ip else f"跨次体检出口一致（{exit_ip}）",
            ))
        try:
            tmp = history_path.with_suffix(history_path.suffix + ".tmp")
            tmp.write_text(json.dumps({
                "ip": str(exit_ip), "country": ipinfo.get("country"),
                "timezone": _normalize_tz(ipinfo.get("timezone")),
                "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            }, ensure_ascii=False), encoding="utf-8")
            tmp.replace(history_path)
        except OSError:
            log.debug("出口历史写入失败: %s", history_path)

        # 同一浏览器网络栈内立即复测一次出口。轮换代理可能在启动/登录
        # 的短窗口内更换地址；首个端点连通并不代表后续 OAuth 请求仍沿用
        # 同一出口。第二次探测失败时显式告警，不猜测放行。
        continuity_ip = ""
        continuity_errors: list[str] = []
        for continuity_url, continuity_extract in _IP_PROVIDERS:
            try:
                await page.goto(continuity_url)
                continuity_raw = json.loads(await page.evaluate("document.body.innerText"))
                continuity_info = continuity_extract(continuity_raw)
                if continuity_info and continuity_info.get("query"):
                    continuity_ip = str(continuity_info["query"])
                    break
                continuity_errors.append(f"{continuity_url.split('/')[2]}: 数据无效")
            except Exception as exc:
                continuity_errors.append(
                    f"{continuity_url.split('/')[2]}: {str(exc)[:40]}"
                )
        if continuity_ip:
            if continuity_ip == str(exit_ip):
                checks.append(_result("ip_continuity", "pass",
                                      f"连续两次出口一致（{exit_ip}）"))
            else:
                checks.append(_result(
                    "ip_continuity", "warn",
                    f"同一次体检出口发生变化（{exit_ip} → {continuity_ip}）",
                    "登录前固定代理会话；轮换出口会使账号会话与设备指纹出现跨 IP 跳变",
                ))
        else:
            checks.append(_result(
                "ip_continuity", "warn",
                f"出口连续性复测失败（{'; '.join(continuity_errors[:2])}）",
                "保持当前代理会话后重试体检；无法完成复测时不要把出口稳定性视为已确认",
            ))

        # Browser requests travel through the local authenticated forwarder,
        # while the independent probe opens a fresh upstream connection.  Some
        # rotating proxy services return a different ASN/IP per connection even
        # when the username/session is unchanged; an account service sees that
        # as a device moving between networks.  Compare both paths explicitly so
        # a browser-only continuity pass does not hide this login-risk signal.
        if profile_proxy:
            try:
                independent = await test_proxy(
                    ProxyConfig(**profile_proxy), timeout=min(15.0, 8.0))
                independent_ip = str(independent.get("exit_ip") or "")
                if not independent.get("ok") or not independent_ip:
                    checks.append(_result(
                        "proxy_path_consistency", "warn",
                        "独立代理连接未返回出口 IP",
                        "固定代理 session 后重试；登录期间需要浏览器路径与独立连接保持同一出口",
                    ))
                elif independent_ip != str(exit_ip):
                    checks.append(_result(
                        "proxy_path_consistency", "warn",
                        f"浏览器出口 {exit_ip} 与独立代理连接 {independent_ip} 不一致",
                        "代理按连接轮换出口；固定同一 session/出口后再进入登录页",
                    ))
                else:
                    checks.append(_result(
                        "proxy_path_consistency", "pass",
                        f"浏览器与独立代理连接出口一致（{exit_ip}）",
                    ))
            except Exception as exc:  # noqa: BLE001
                checks.append(_result(
                    "proxy_path_consistency", "warn",
                    f"独立代理连接探测异常（{type(exc).__name__}）",
                    "固定代理 session 后重试体检；登录前先确认出口一致性",
                ))
        else:
            checks.append(_result(
                "proxy_path_consistency", "pass", "直连环境，无独立代理路径"))

        # ip-api 的免费 HTTP 端点会被 Chromium 的 HSTS 规则升级到 HTTPS，
        # 而 HTTPS 版本要求付费 key；ipwho/ipinfo 又只返回 ASN/ISP，不给
        # proxy/hosting 标志。登录体检因此容易把「未判定」误报成质量告警。
        # 用同一浏览器网络栈补查 proxycheck（不把服务器出口混进来），只在
        # 主端点缺少质量字段时请求；服务不可达仍保留 warn，而不是猜测放行。
        if (exit_ip and all(ipinfo.get(k) is None
                            for k in ("proxy", "hosting", "mobile"))):
            try:
                quality_url = (
                    "https://proxycheck.io/v2/" + quote(str(exit_ip), safe=".:" )
                    + "?vpn=1&asn=1")
                await page.goto(quality_url)
                quality_raw = json.loads(await page.evaluate("document.body.innerText"))
                quality = quality_raw.get(str(exit_ip)) if isinstance(quality_raw, dict) else None
                if isinstance(quality, dict):
                    q_proxy = str(quality.get("proxy") or "").strip().lower()
                    q_type_raw = str(quality.get("type") or "").strip()
                    q_type = re.sub(r"[\s_-]+", "", q_type_raw.lower())
                    if q_proxy in ("yes", "no"):
                        ipinfo["proxy"] = q_proxy == "yes"
                    if q_type:
                        # proxycheck's ``Business`` label describes an ASN
                        # category, not a datacenter/hosting verdict.  Treating
                        # it as hosting made ordinary ISP exits (for example a
                        # DHCP consumer/business subnet) look like a proxy and
                        # downgraded login readiness despite the browser-side
                        # network check passing.  Keep only explicit hosting or
                        # datacenter labels in the high-risk bucket.
                        ipinfo["hosting"] = q_type in {
                            "hosting", "datacenter", "server", "vpn",
                        }
                        ipinfo["mobile"] = q_type == "mobile"
                    ipinfo["quality_type"] = q_type_raw or None
                    ipinfo["quality_source"] = "proxycheck"
            except Exception as exc:  # noqa: BLE001
                log.debug("出口质量补查失败: %s", exc)
        checks.append(_result("ip_reachable", "pass",
                              f"出口 {exit_ip}（{ipinfo.get('country')} {ipinfo.get('city') or ''}，ISP: {str(ipinfo.get('isp') or '')[:30]}）"))
        quality_flags = (ipinfo.get("proxy"), ipinfo.get("hosting"), ipinfo.get("mobile"))
        if ipinfo.get("proxy") or ipinfo.get("hosting"):
            checks.append(_result(
                "ip_quality", "warn",
                "出口被归类为代理/机房网络",
                "登录环境优先使用与目标属地一致的住宅或移动出口；地址质量属于网络侧信号，指纹参数无法抵消"))
        elif any(flag is not None for flag in quality_flags):
            quality_type = str(ipinfo.get("quality_type") or "").strip()
            detail = "出口未被标记为代理或机房网络"
            if quality_type:
                detail += f"（网络类型：{quality_type}）"
            checks.append(_result("ip_quality", "pass", detail))
        else:
            checks.append(_result("ip_quality", "warn", "当前探测端点未提供出口质量字段",
                                  "在代理控制台补做 IP 质量检查；仅凭连通性无法判断地址信誉"))

        # ---- 页面内全量采样
        data = await asyncio.wait_for(page.evaluate(_CHECK_JS), timeout=45)
        # 采样页停在非安全上下文时，getBattery / navigator.gpu / mediaDevices
        # 按规范根本不暴露，后续三项会整片误报——先改道 https 端点再采一次。
        if not data.get("secure"):
            for url in _SECURE_SAMPLE_URLS:
                try:
                    await page.goto(url)
                    retry = await asyncio.wait_for(page.evaluate(_CHECK_JS), timeout=45)
                except Exception:
                    continue
                if retry.get("secure"):
                    data = retry
                    break
        # 改道也没成功：这三项在非安全上下文里本就不可观测，不能当作缺失上报。
        secure = bool(data.get("secure"))

        # 时区一致性
        ip_tz = _normalize_tz(ipinfo.get("timezone"))
        if data["tz"] and ip_tz and data["tz"] != ip_tz:
            checks.append(_result("tz_match", "fail",
                                  f"浏览器时区 {data['tz']} ≠ IP 属地时区 {ip_tz}",
                                  "时区矛盾是最常见的环境穿帮：开启 geoip 或设置显式时区（国内预设=Asia/Shanghai）"))
        else:
            checks.append(_result("tz_match", "pass", f"时区一致（{data['tz']}）"))

        # 语言一致性
        cc = ipinfo.get("countryCode", "")
        expected = _EXPECT_LANG_PREFIX.get(cc)
        lang_prefix = (data["lang"] or "").split("-")[0].lower()
        lang_values = [v for v in str(data.get("langs") or "").split(",") if v]
        lang_issues: list[str] = []
        if expected and lang_prefix and lang_prefix != expected:
            lang_issues.append(f"IP 属地 {cc} 期望 {expected}-*，浏览器语言 {data['lang']}")
        # 登录页常同时读取 language 与 languages；首选项首项必须与单值
        # getter 同源。这个检查专门捕获旧版「Intl/Accept-Language 已切换，
        # navigator.languages 仍是 zh-CN」的交叉矛盾。
        if lang_values and data.get("lang") and lang_values[0] != data["lang"]:
            lang_issues.append(
                f"navigator.languages 首项 {lang_values[0]} ≠ language {data['lang']}")
        if lang_issues:
            checks.append(_result("locale_match", "warn",
                                  "；".join(lang_issues),
                                  "语言与属地不符会显著提高人机验证触发率；在启动选项设置 locale（国内预设=zh-CN）"))
        else:
            checks.append(_result("locale_match", "pass",
                                  f"语言 {data['lang'] or '(空)'} 与属地 {cc} 相符"))

        # UA / platform / UA-CH 交叉一致性。单看 userAgentData.platform 不够，
        # 登录页通常还会读取 model、platformVersion 和完整 Chrome 主版本。
        stored_nav = dict(spec_nav)
        if not stored_nav:
            stored_fp = profile.get("fingerprint") or {}
            stored_nav = ((stored_fp.get("preset") or {}).get("navigator") or
                          (stored_fp.get("fingerprint") or {}).get("navigator") or {})
        ua_issues: list[str] = []
        ua = str(data.get("ua") or "")
        page_major_match = re.search(r"(?:Chrome|Chromium|CriOS)/(\d+)", ua)
        page_major = page_major_match.group(1) if page_major_match else ""
        if engine_major and page_major and engine_major != page_major:
            ua_issues.append(f"真实内核主版本 {engine_major} ≠ UA 主版本 {page_major}")
        declared_os = _ua_os(ua)
        if target_os and declared_os != target_os:
            ua_issues.append(f"UA 平台 {declared_os} ≠ 目标 {target_os}")
        if target_os not in ("android", "ios"):
            expected_platforms = {
                "windows": {"Win32"}, "macos": {"MacIntel"},
                "linux": {"Linux x86_64", "Linux armv81", "Linux armv8l", "Linux armv7l"},
            }.get(target_os, set())
            if expected_platforms and data.get("platform") not in expected_platforms:
                ua_issues.append(f"platform {data.get('platform') or '(空)'} 与目标 {target_os} 不符")
        if target_os in ("android", "ios"):
            expected_ua = str(stored_nav.get("userAgent") or "")
            if expected_ua and ua != expected_ua:
                ua_issues.append("运行期 UA 与存储指纹不一致")
            expected_platform = str(stored_nav.get("platform") or "")
            if expected_platform and data.get("platform") != expected_platform:
                ua_issues.append(f"platform {data.get('platform') or '(空)'} ≠ 指纹 {expected_platform}")

            high = data.get("chHigh")
            expected_ch = spec_extra.get("uaCh") or {}
            if target_os == "android":
                if not isinstance(high, dict):
                    ua_issues.append("Android 高熵 UA-CH 不可读")
                else:
                    if expected_ch.get("model") and high.get("model") != expected_ch.get("model"):
                        ua_issues.append("UA-CH model 与机型不一致")
                    want_pv = str(expected_ch.get("platformVersion") or "")
                    if want_pv and str(high.get("platformVersion") or "") != want_pv:
                        ua_issues.append("UA-CH platformVersion 与档案不一致")
                    ua_major = re.search(r"(?:Chrome|CriOS)/(\d+)", ua)
                    ch_major = _ch_major(high)
                    if ua_major and ch_major and ua_major.group(1) != ch_major:
                        ua_issues.append("UA 主版本与 UA-CH 完整版本不一致")
            elif isinstance(high, dict):
                ua_issues.append("iOS 不应暴露高熵 UA-CH")
        if ua_issues:
            hard = any("真实内核主版本" in x or "UA 平台" in x or "运行期 UA" in x or "platform " in x
                       or "model" in x or "platformVersion" in x or "主版本" in x
                       for x in ua_issues)
            checks.append(_result("ua_consistency", "fail" if hard else "warn",
                                  "；".join(ua_issues),
                                  "重启同一环境，确保设备仿真先于页面脚本生效；Android 需保留完整 UA-CH"))
        else:
            checks.append(_result("ua_consistency", "pass",
                                  f"UA / platform / UA-CH 与 {target_os or '目标环境'} 一致"))

        # 移动端触控与媒体查询是登录页常见的首屏交叉信号；若仿真通道掉线，
        # screen 可能仍看似正确，但这里会立刻暴露 pointer/touch 回落宿主值。
        if mobile:
            touch_issues: list[str] = []
            want_touch = int(spec_nav.get("maxTouchPoints") or spec_extra.get("maxTouchPoints") or 0)
            got_touch = int(data.get("maxTouchPoints") or 0)
            if want_touch and got_touch != want_touch:
                touch_issues.append(f"maxTouchPoints {got_touch} ≠ 指纹 {want_touch}")
            if got_touch <= 0 or not data.get("touchEvents"):
                touch_issues.append("触摸事件面缺失")
            if not data.get("pointerCoarse") or not data.get("anyPointerCoarse"):
                touch_issues.append("pointer: coarse 媒体面缺失")
            if data.get("anyHover"):
                touch_issues.append("any-hover: hover 与移动设备不符")
            if touch_issues:
                checks.append(_result("mobile_surface", "fail", "；".join(touch_issues),
                                      "检查移动端 CDP 仿真状态；不要在同一环境中混用桌面 CDP 输入路径"))
            else:
                checks.append(_result("mobile_surface", "pass",
                                      f"触控 {got_touch} 点，pointer/coarse 一致"))
        else:
            checks.append(_result("mobile_surface", "pass", "非移动端环境，跳过触控面检查"))

        # 登录态依赖 Cookie 与站点存储；这里只做能力探测并清理临时 key，不写入
        # 持久化登录数据，避免体检改变用户环境。
        storage = data.get("storage") or {}
        storage_issues: list[str] = []
        if not data.get("cookieEnabled"):
            storage_issues.append("Cookie 被禁用")
        if not storage.get("local"):
            storage_issues.append("localStorage 不可写")
        if not storage.get("session"):
            storage_issues.append("sessionStorage 不可写")
        if not storage.get("indexedDB"):
            storage_issues.append("IndexedDB 不可用")
        if storage_issues:
            cookie_hard = any("Cookie" in item for item in storage_issues)
            checks.append(_result("storage_ready", "fail" if cookie_hard else "warn",
                                  "；".join(storage_issues),
                                  "登录页需要可用 Cookie；同时检查隐私扩展、站点权限与无痕/临时上下文"))
        else:
            checks.append(_result("storage_ready", "pass", "Cookie、localStorage、sessionStorage、IndexedDB 均可用"))

        # OAuth/登录页面常见的基础能力面：这里只验证能力存在与安全上下文，
        # 不触发弹窗、不调用凭据 API，也不读取账号信息。Passkey/Storage Access
        # 是可选增强能力，缺失只作提示，避免把浏览器版本差异误报为硬失败。
        auth = data.get("authSurface") or {}
        auth_issues: list[str] = []
        if not auth.get("secure"):
            auth_issues.append("登录页面不在安全上下文")
        if not auth.get("popup"):
            auth_issues.append("window.open 不可用")
        if auth_issues:
            checks.append(_result("auth_surface", "fail" if not auth.get("secure") else "warn",
                                  "；".join(auth_issues),
                                  "登录页需要 HTTPS 与可用 OAuth 弹窗；同时检查扩展和浏览器权限"))
        else:
            extras = []
            if auth.get("credentials"):
                extras.append("Credentials")
            if auth.get("publicKey"):
                extras.append("WebAuthn")
            if auth.get("storageAccess"):
                extras.append("Storage Access")
            checks.append(_result("auth_surface", "pass",
                                  "OAuth 基础能力可用" + (f"（{'+'.join(extras)} 可用）" if extras else "")))

        # WebRTC 泄露
        srflx = data.get("srflx") or []
        exit_ip = ipinfo.get("query", "")
        leaked = [ip for ip in srflx if ip != exit_ip]
        if leaked:
            checks.append(_result("webrtc_leak", "fail",
                                  f"WebRTC 暴露非出口公网 IP: {', '.join(leaked[:3])}",
                                  "真实 IP 泄露！开启 geoip（WebRTC IP 伪造）或勾选禁用 WebRTC"))
        elif srflx:
            checks.append(_result("webrtc_leak", "pass", f"WebRTC 公网 IP 与出口一致（{srflx[0]}）"))
        else:
            checks.append(_result("webrtc_leak", "pass", "未产生公网候选（已禁用/已防护）"))

        # webdriver
        if data["webdriver"] in (False, None, 0):
            checks.append(_result("webdriver", "pass", "navigator.webdriver 无自动化标记"))
        else:
            checks.append(_result("webdriver", "fail", "navigator.webdriver=true",
                                  "自动化标记未清除；检查内核选择（调试内核无伪装）"))

        # Canvas 稳定性
        if data["canvas1"] == data["canvas2"]:
            checks.append(_result("canvas_stable", "pass", "同会话 Canvas 读取稳定"))
        else:
            checks.append(_result("canvas_stable", "fail", "同会话两次读取不一致（噪声随机化）",
                                  "Canvas 噪声必须固定（本产品已按环境固定种子，请勿手动改动指纹数据）"))

        # WebGL 合理性
        renderer = data.get("webglRenderer") or ""
        if not renderer:
            checks.append(_result("webgl_sane", "warn", "WebGL 渲染器为空", "部分站点视无 WebGL 为可疑"))
        elif any(k in renderer for k in ("SwiftShader", "llvmpipe", "Software", "Basic Render")):
            checks.append(_result("webgl_sane", "warn", f"软件渲染: {renderer[:50]}",
                                  "无 GPU/软件渲染是虚拟机信号；有头模式+实体机可缓解"))
        elif renderer.strip() == "Mozilla":
            checks.append(_result("webgl_sane", "warn", "泛化渲染器 Mozilla", "建议重生成指纹（真实 GPU 参数）"))
        else:
            checks.append(_result("webgl_sane", "pass", renderer[:60]))

        # 字体渲染
        if int(data.get("fontWidth") or 0) > 0:
            checks.append(_result("fonts_render", "pass", f"字体度量正常（{data['fontWidth']}px）"))
        else:
            checks.append(_result("fonts_render", "fail", "字体度量无效", "字体子集异常，重生成指纹"))

        # ---- 引擎类型（Chromium 独有信号的"缺失"是否正常以此为界）
        ua = data.get("ua") or ""
        is_chromium = "Firefox" not in ua

        # Audio 稳定性
        if data.get("audio1") is None:
            checks.append(_result("audio_stable", "warn",
                                  "AudioContext 指纹不可读",
                                  "音频指纹缺失在部分风控中视为可疑；检查内核版本"))
        elif data["audio1"] == data["audio2"]:
            checks.append(_result("audio_stable", "pass", "同会话 Audio 指纹稳定"))
        else:
            checks.append(_result("audio_stable", "fail", "两次 Audio 指纹不一致（噪声未固定）",
                                  "Audio 噪声必须固定（本产品按环境固定种子，请勿手改指纹数据）"))

        # Client Hints（仅 Chromium 系需要检查；Firefox 无此 API 属正常）
        if is_chromium:
            got = data.get("chPlatform")
            if mobile:
                # iOS 走 WebKit 口径：真 Safari 没有 userAgentData，移动端
                # 指纹也按 removeNavigatorProps 把它摘掉——缺失才是对的。
                if target_os == "ios":
                    if got:
                        checks.append(_result("ua_chips", "warn",
                                              f"iOS 环境出现 Client Hints（{got}）",
                                              "真 Safari 无 userAgentData；出现即为穿帮"))
                    else:
                        checks.append(_result("ua_chips", "pass",
                                              "iOS 无 Client Hints（与真 Safari 一致）"))
                else:
                    expect_ch = "Android"
                    if got and got != expect_ch:
                        checks.append(_result("ua_chips", "fail",
                                              f"Client Hints 平台 {got} ≠ 目标 OS {expect_ch}",
                                              "userAgentData 与伪装 OS 矛盾（穿帮级）"))
                    elif got:
                        checks.append(_result("ua_chips", "pass", f"Client Hints 平台一致（{got}）"))
                    elif spec_extra.get("hasUaCh"):
                        checks.append(_result("ua_chips", "warn",
                                              "userAgentData 缺失（该机型规格应有）",
                                              "指纹声明了 hasUaCh，实际却没有；"
                                              "真 Android Chrome 均带 Client Hints"))
                    else:
                        checks.append(_result("ua_chips", "pass",
                                              "无 Client Hints（该机型规格即如此）"))
            else:
                expect_ch = {"windows": "Windows", "macos": "macOS", "linux": "Linux"}.get(
                    target_os, "")
                if got and expect_ch and got != expect_ch:
                    checks.append(_result("ua_chips", "fail",
                                          f"Client Hints 平台 {got} ≠ 目标 OS {expect_ch}",
                                          "userAgentData 与伪装 OS 矛盾（穿帮级）；"
                                          "fp-chromium 内核自带一致性，出现此问题请换内核或重生成指纹"))
                elif got:
                    checks.append(_result("ua_chips", "pass", f"Client Hints 平台一致（{got}）"))
                else:
                    checks.append(_result("ua_chips", "warn",
                                          "userAgentData 缺失（Chromium 应有）",
                                          "真 Chrome 均带 Client Hints；缺失是自动化/精简版信号"))
        else:
            checks.append(_result("ua_chips", "pass", "Firefox 内核无 Client Hints（正常）"))

        # 屏幕合理性
        try:
            w, h = (int(v) for v in str(data.get("screenWH", "0x0")).split("x"))
        except Exception:
            w = h = 0
        depth = data.get("colorDepth")
        dpr = data.get("dpr")
        depth_ok = depth in (24, 30)
        mismatch = ""
        if mobile:
            # 移动端 screen.width 报的是 CSS 逻辑像素（iPhone 13 为 390x844），
            # 物理分辨率在指纹里是 width/height；1024x720 的桌面下限会把
            # 所有手机判成异常，故只做「像手机」的宽松下限。
            screen_ok = w >= 320 and h >= 480
            dpr_ok = isinstance(dpr, (int, float)) and 1 <= dpr <= 4
            # 与存储指纹逐值对照：对不上说明 CDP 设备度量没有落到这一页，
            # 此时 innerWidth/screen.width 会退回宿主窗口值（穿帮级）。
            want_w = spec_screen.get("cssWidth")
            want_h = spec_screen.get("cssHeight")
            if want_w and want_h and (w, h) != (int(want_w), int(want_h)):
                mismatch = f"实测 {w}x{h} ≠ 指纹 {want_w}x{want_h}"
        else:
            screen_ok = w >= 1024 and h >= 720
            dpr_ok = dpr in (1, 1.25, 1.5, 1.75, 2, 2.5, 3)
        if mismatch:
            checks.append(_result(
                "screen_sane", "warn", mismatch,
                "设备度量没有落到该页（screen 仍是宿主值），同一环境下 "
                "innerWidth/innerHeight 也会退回宿主窗口，属穿帮级差异；"
                "请重启环境，若稳定复现请检查 CDP 注入日志"))
        elif screen_ok and depth_ok and dpr_ok:
            checks.append(_result("screen_sane", "pass",
                                  f"{w}x{h} @{depth}bit DPR={dpr}"))
        else:
            issues = []
            if not screen_ok:
                issues.append(f"尺寸 {w}x{h} 异常")
            if not depth_ok:
                issues.append(f"色深 {depth} 异常（真机常见 24/30）")
            if not dpr_ok:
                issues.append(f"像素比 {dpr} 异常")
            checks.append(_result("screen_sane", "warn", "；".join(issues),
                                  "屏幕参数不合理是虚拟机/自动化特征"))

        # 媒体设备
        md = data.get("mediaDevices")
        if not secure:
            checks.append(_result("media_devices", "warn",
                                  "非安全上下文，enumerateDevices 不可观测",
                                  "该项只在 https 页面上有意义；本次改道 https 未成功"
                                  "（检查代理是否放行 https），结果不代表真实设备数"))
        elif md is None or md == -1:
            checks.append(_result("media_devices", "warn",
                                  "enumerateDevices 不可用",
                                  "真机均有摄像头/麦克风/扬声器条目；0 设备是虚拟机信号"))
        elif md == 0:
            checks.append(_result("media_devices", "warn",
                                  "媒体设备数为 0（虚拟机特征）",
                                  "如非故意禁用，建议在真机运行或接受该项风险"))
        else:
            checks.append(_result("media_devices", "pass", f"枚举到 {md} 个媒体设备"))

        # WebGPU
        wg = data.get("webgpu")
        if not secure:
            checks.append(_result("webgpu", "warn",
                                  "非安全上下文，navigator.gpu 不可观测",
                                  "该项只在 https 页面上有意义；本次改道 https 未成功，"
                                  "结果不代表真实 WebGPU 支持"))
        elif str(wg).startswith("ok"):
            checks.append(_result("webgpu", "pass",
                                  f"WebGPU 适配器可用（{str(wg)[2:][:40] or '无架构信息'}）"))
        elif wg in ("unsupported", "no-adapter"):
            checks.append(_result("webgpu", "warn",
                                  f"WebGPU 不可用（{wg}）",
                                  "新一代检测站开始采集 WebGPU；主流真机 Chrome 已支持，"
                                  "不可用会被视为旧设备/虚拟机"))
        else:
            checks.append(_result("webgpu", "warn", f"WebGPU 探测异常（{wg}）"))

        # 广告拦截探测
        if data.get("adblock") == "yes":
            checks.append(_result("adblock_active", "warn",
                                  "检测到广告拦截（广告域请求被拦截）",
                                  "广告拦截扩展可被站点探测并阻断验证域；"
                                  "cloudflare 预设或启动选项勾选「禁用广告拦截」"))
        else:
            checks.append(_result("adblock_active", "pass", "未检测到广告拦截"))

        # 插件数量（真 Chrome 有 5 个内置 PDF 插件；0 是精简/自动化信号）
        plugins = int(data.get("plugins") or 0)
        if mobile and spec_extra.get("emptyPlugins"):
            # 移动端指纹按规格清空 plugins（Android Chrome 与 iOS Safari 的
            # 真机插件列表本就随机型/版本不同），空列表是该规格的正确取值。
            checks.append(_result("plugins_sane", "pass",
                                  f"插件数 {plugins}（该机型规格为空列表）"))
        elif is_chromium and plugins == 0:
            checks.append(_result("plugins_sane", "warn",
                                  "navigator.plugins 为空（真 Chrome 应有 5 个 PDF 插件）",
                                  "插件列表为空是 headless/精简版特征"))
        else:
            checks.append(_result("plugins_sane", "pass", f"插件数 {plugins}"))

        # 硬件参数合理性（deviceMemory/Battery 仅 Chromium；Firefox 无属正常）
        hw = data.get("hw")
        mem = data.get("deviceMemory")
        hw_ok = hw is None or 1 <= int(hw) <= 64
        mem_ok = mem is None or (float(mem) >= 0.25 and float(mem) <= 128)
        battery_note = ""
        if mobile:
            # iOS Safari 没有 Battery API / deviceMemory，移动端指纹也按
            # removeNavigatorProps 摘除；缺失是规格，不是缺陷。
            if (secure and target_os == "android" and spec_extra.get("hasBattery")
                    and not data.get("hasBattery")):
                battery_note = "；Battery API 缺失（该机型规格应有）"
        elif secure and is_chromium and not data.get("hasBattery"):
            battery_note = "；Battery API 缺失（真 Chrome 应有）"
        if hw_ok and mem_ok and not battery_note:
            detail = f"核心数 {hw or '未知'}"
            detail += f"，内存 {mem}GB" if mem else ""
            checks.append(_result("hw_sane", "pass", detail))
        else:
            detail = f"硬件参数异常（核心 {hw}，内存 {mem}）{battery_note}"
            checks.append(_result("hw_sane", "warn", detail,
                                  "hardwareConcurrency/deviceMemory/Battery 超出真机常见特征"))

        # Turnstile 验证域可达（uBlock 等广告拦截会阻断它）
        if data.get("turnstile") == "ok":
            checks.append(_result("turnstile_reachable", "pass",
                                  "challenges.cloudflare.com 可达（Turnstile 正常加载）"))
        elif data.get("turnstile") == "blocked":
            checks.append(_result("turnstile_reachable", "fail",
                                  "Cloudflare 验证域被阻断（疑似广告拦截扩展）",
                                  "在启动选项勾选「禁用广告拦截」或使用 cloudflare 预设，"
                                  "否则 Turnstile 会报 Can't verify the user is human"))
        else:
            checks.append(_result("turnstile_reachable", "warn",
                                  f"验证域探测异常: {str(data.get('turnstile'))[:40]}"))

        return _report(profile, checks)
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        if started_by_us:
            await manager.stop(profile["id"])


def _report(profile: dict, checks: list[dict]) -> dict:
    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    score = max(0, 100 - 25 * len(fails) - 8 * len(warns))
    verdict = "ready" if not fails and not warns else \
              "needs_work" if not fails else "danger"
    # 登录预设的报告需要能被 API/自动化直接消费，不让调用方重新按 id
    # 扫描整表；同时保留完整 checks 供管理端展示。
    login_ids = {"ip_reachable", "ip_continuity", "ip_history", "ip_quality", "tz_match", "locale_match", "webrtc_leak",
                 "proxy_path_consistency",
                 "webdriver", "ua_consistency", "mobile_surface", "storage_ready",
                 "auth_surface", "profile_continuity", "emulation_channel", "headless_mode"}
    login_checks = [c for c in checks if c["id"] in login_ids]
    login_fails = [c for c in login_checks if c["status"] == "fail"]
    login_warns = [c for c in login_checks if c["status"] == "warn"]
    login_verdict = "ready" if not login_fails and not login_warns else \
                    "needs_work" if not login_fails else "danger"
    return {
        "profile_id": profile["id"],
        "profile_name": profile["name"],
        "kernel": profile["kernel"],
        "preset": (profile.get("launch") or {}).get("preset", "standard"),
        "score": score,
        "verdict": verdict,
        "verdict_label": {"ready": "环境就绪", "needs_work": "建议优化", "danger": "存在穿帮风险"}[verdict],
        "login_readiness": {
            "score": max(0, 100 - 25 * len(login_fails) - 8 * len(login_warns)),
            "verdict": login_verdict,
            "verdict_label": {"ready": "可进入登录", "needs_work": "登录前建议优化",
                               "danger": "暂不建议登录"}[login_verdict],
            "checks": login_checks,
        },
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "checks": checks,
    }
