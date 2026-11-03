"""fingerprint-chromium 内核（第三内核）。

基于开源项目 adryfish/fingerprint-chromium（Ungoogled Chromium 打补丁），
在 Chromium 内核层伪造指纹（Canvas/WebGL/字体/ClientRects/GPU 等），
并保留标准 CDP 调试端口——Selenium/Puppeteer/Playwright 可直连。

与 chromium 调试内核的区别：真正具备指纹伪装能力，可用于生产。
安装：从 https://github.com/adryfish/fingerprint-chromium/releases 下载并解压，
通过环境变量 FPWB_FPCHROMIUM 指向 chrome 可执行文件，或放入 <项目>/tools/fp-chromium/。

指纹参数（命令行注入，全部由该内核在 C++ 层实现）：
  --fingerprint=<32位种子>            启用指纹伪造（种子驱动 canvas/webgl 等）
  --fingerprint-platform=<os>         伪装操作系统
  --fingerprint-platform-version=<v>  伪装 OS 具体版本（148+；缺省按种子稳定选取）
  --fingerprint-brand=Chrome          UA 品牌（Chrome/Edge/Opera/Vivaldi）
  --fingerprint-brand-version=<ver>   UA 品牌版本
  --fingerprint-hardware-concurrency  CPU 核心数
  --disable-spoofing=<list>           关闭单项伪装（font,audio,canvas,clientrects,gpu）
  --lang / --accept-lang / --timezone 语言与时区
"""
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from ..config import BASE_DIR
from ..models import LaunchConfig, ProxyConfig

KERNEL_NAME = "fp-chromium"

# 各 OS 的真实 platformVersion 取值域（Client Hints 语义；按种子确定性选取）
_PLATFORM_VERSIONS = {
    "windows": ["10.0.0", "13.0.0", "14.0.0", "15.0.0", "15.0.0", "10.0.0"],
    "macos": ["13.5.0", "14.4.1", "14.7.0", "14.7.0", "15.1.0", "15.3.0"],
    "linux": ["5.15.0", "6.1.0", "6.1.0", "6.5.0", "6.8.0", "6.8.0"],
    "android": ["13.0.0", "14.0.0", "13.0.0", "15.0.0", "14.0.0", "14.0.0"],
    "ios": ["16.6.0", "17.0.0", "17.0.0", "17.5.1", "18.0.0", "17.5.1"],
}

# 内核原生 --fingerprint-platform 仅支持桌面三系统；
# 移动端（android/ios）退化到对应底层平台，移动端 UA/触控/视口由 CDP 注入层补齐
# （app/mobile_cdp_injector.py），避免启动直接 KeyError。
_PLATFORM_ARG = {
    "windows": "windows",
    "macos": "macos",
    "linux": "linux",
    "android": "linux",
    "ios": "macos",
}


def _stable_platform_version(target_os: str, seed: int) -> str:
    """同一种子永远得到同一版本号（跨启动稳定，避免指纹漂移）。"""
    pool = _PLATFORM_VERSIONS.get(target_os)
    return pool[seed % len(pool)] if pool else "10.0.0"

# Chrome 大版本 → 对应 fingerprint-chromium release 存在（132~150）
_EXE_NAMES = ("chrome.exe", "chrome")


def find_executable() -> Optional[str]:
    env = os.environ.get("FPWB_FPCHROMIUM")
    if env and Path(env).exists():
        return env
    tools = BASE_DIR / "tools" / "fp-chromium"
    if tools.is_dir():
        # Windows/Linux：直接命名的可执行文件
        for name in _EXE_NAMES:
            for candidate in [tools / name, *tools.rglob(name)]:
                if candidate.exists():
                    return str(candidate)
        # macOS：Release 只有 .dmg，解压后为 Chromium.app，
        # 真实二进制在 Contents/MacOS/ 下；启动时会从该路径反推出
        # Chromium.app，并在 macOS 上交给 LaunchServices 注册应用。
        app_dirs = []
        preferred_app = tools / "Chromium.app"
        if preferred_app.is_dir():
            app_dirs.append(preferred_app)
        app_dirs.extend(p for p in tools.glob("*.app") if p not in app_dirs)
        app_dirs.extend(p for p in tools.rglob("*.app") if p not in app_dirs)
        for app_dir in app_dirs:
            macos_dir = app_dir / "Contents" / "MacOS"
            if macos_dir.is_dir():
                for binary in sorted(macos_dir.iterdir()):
                    if binary.is_file():
                        return str(binary)
    return None


def is_available() -> tuple[bool, str]:
    exe = find_executable()
    return (True, exe) if exe else (
        False,
        "未安装。从 github.com/adryfish/fingerprint-chromium/releases 下载解压后，"
        "设置环境变量 FPWB_FPCHROMIUM 指向 chrome 可执行文件，或放入 tools/fp-chromium/ 目录",
    )


_CHROME_VERSION_CACHE: str = ""


def chrome_version() -> Optional[str]:
    """探测内核实测的 Chromium 完整版本号（如 148.0.7778.215，进程内缓存）。

    移动端指纹的 UA / UA-CH 声称版本必须与引擎一致：真实浏览器不可能出现
    「UA 是 156 而引擎特性停留在 148」的组合，指纹检测会直接命中
    （BrowserScan 的「浏览器版本不同」即来源于此）。
    """
    global _CHROME_VERSION_CACHE
    if not _CHROME_VERSION_CACHE:
        version = ""
        try:
            exe = find_executable()
            if exe:
                out = subprocess.run(  # nosec B603 参数来自本地受控配置
                    [exe, "--version"], capture_output=True, text=True, timeout=10,
                ).stdout
                m = re.search(r"Chromium\s+(\d+\.\d+\.\d+\.\d+)", out)
                if m:
                    version = m.group(1)
        except Exception:
            version = ""
        _CHROME_VERSION_CACHE = version or "-"
    return _CHROME_VERSION_CACHE if _CHROME_VERSION_CACHE != "-" else None


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _MacOSAppProcess:
    """Process handle for a Chromium app launched through LaunchServices.

    macOS aborts Chromium during ``_RegisterApplication`` when the executable
    is started directly from a non-GUI parent.  LaunchServices gives the app a
    normal Application Services context, but ``open`` exits immediately and
    therefore cannot be used as the browser process handle.  This small proxy
    tracks the real PID discovered from the unique CDP port and exposes the
    subset of ``Popen`` used by the launcher.
    """

    def __init__(self, opener: subprocess.Popen, port: int) -> None:
        self._opener = opener
        self._port = port
        self._browser_pid: int | None = None
        self._browser_attached = False

    @property
    def pid(self) -> int:
        return self._browser_pid or self._opener.pid

    def attach(self, pid: int | None) -> None:
        if pid and pid != os.getpid():
            self._browser_pid = pid
            self._browser_attached = True

    def mark_running(self) -> None:
        """Mark the browser alive when process enumeration is restricted.

        The CDP endpoint is authoritative for startup.  Some managed macOS
        sessions deny pgrep/lsof, so a successful endpoint probe must not be
        mistaken for the short-lived ``open`` helper having exited.
        """
        self._browser_attached = True

    def poll(self) -> int | None:
        pid = self._browser_pid
        if pid is None:
            return None if self._browser_attached else self._opener.poll()
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return 0
        return None

    def terminate(self) -> None:
        pid = self._browser_pid
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif self._opener.poll() is None:
            self._opener.terminate()
        elif self._browser_attached:
            # Best-effort cleanup for sessions that deny process enumeration.
            subprocess.run(
                ["pkill", "-TERM", "-f", f"remote-debugging-port={self._port}"],
                capture_output=True,
            )

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired("Chromium", timeout)
            time.sleep(0.05)
        return 0


def _browser_pid_for_port(port: int) -> int | None:
    """Find the app process carrying this launch's unique CDP port."""
    # pgrep is cheap, but some managed macOS sessions deny process-list
    # inspection.  lsof can still identify the listener without exposing the
    # complete process table, so keep it as a fallback.
    queries = (
        ["pgrep", "-f", "--", f"--remote-debugging-port={port}"],
        ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
    )
    for query in queries:
        try:
            result = subprocess.run(
                query, capture_output=True, text=True, timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for raw in result.stdout.splitlines():
            try:
                pid = int(raw.strip())
            except ValueError:
                continue
            if pid != os.getpid():
                return pid
    return None


def _register_macos_app(app: Path) -> None:
    """Refresh LaunchServices for a bundle copied from a DMG.

    LaunchServices may retain a record for an older copy of Chromium with the
    same bundle identifier.  In that state ``open`` can report
    ``kLSNoExecutableErr`` even though the current bundle contains a valid
    executable.  ``lsregister -f`` is idempotent and does not require admin
    privileges; failures are intentionally ignored because the normal
    ``open``/Terminal fallbacks still provide the useful error.
    """
    if sys.platform != "darwin" or not app.is_dir():
        return
    lsregister = Path(
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister"
    )
    if not lsregister.is_file():
        return
    try:
        subprocess.run(
            [str(lsregister), "-f", str(app)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _launch(
    exe: str,
    args: list[str],
    port: int,
    *,
    headless: bool = False,
) -> subprocess.Popen | _MacOSAppProcess:
    """Launch Chromium using the appropriate macOS process boundary.

    A bundled Chromium must normally be opened by LaunchServices: starting the
    GUI binary as a child of a web server can abort in AppKit's
    ``_RegisterApplication``.  Headless Chromium is the important exception.
    It is deliberately started as a regular child process so CI, the local
    launch verifier, and API callers do not need a WindowServer/Finder session.
    ``FPWB_FPCHROMIUM_DIRECT`` is an escape hatch for desktop sessions where a
    site-specific wrapper needs to own the process directly.
    """
    if sys.platform == "darwin":
        app = Path(exe).resolve()
        if app.parent.name == "MacOS" and app.parent.parent.name == "Contents":
            app = app.parent.parent.parent
        force_direct = os.environ.get("FPWB_FPCHROMIUM_DIRECT", "").lower() in {
            "1", "true", "yes", "on",
        }
        if app.suffix == ".app" and app.is_dir() and not headless and not force_direct:
            _register_macos_app(app)
            # A service started by launchd/IDE may be in the user uid but not
            # in that user's GUI bootstrap namespace.  Retry through
            # ``launchctl asuser`` before surfacing the actionable error.
            open_args = [
                # Passing the bundle as the open target (rather than -a) is
                # more reliable for copies outside /Applications and avoids
                # LaunchServices choosing another app with the same bundle id.
                "/usr/bin/open", "-n", "-g", str(app), "--args", *args[1:],
            ]
            commands = [open_args]
            try:
                uid = os.getuid()
                if uid != 0:
                    commands.append(["/bin/launchctl", "asuser", str(uid), *open_args])
            except (AttributeError, OSError):
                pass
            errors: list[str] = []
            for command in commands:
                try:
                    opener = subprocess.Popen(
                        command,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    rc = opener.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    opener.kill()
                    opener.wait(timeout=2)
                    errors.append("open 超时")
                    continue
                if rc == 0:
                    return _MacOSAppProcess(opener, port)
                detail = (opener.stderr.read() if opener.stderr else "").strip()
                if detail:
                    errors.append(detail[:240])

            # When the management service is launched from a background/IDE
            # process, LaunchServices can reject even a valid bundle with
            # kLSNoExecutableErr.  A Terminal.app tab already belongs to the
            # logged-in Aqua session and can be used as a GUI process boundary
            # without changing the Chromium bundle.  This is opt-out because
            # a server/CI host may not have Terminal.app available.
            terminal_enabled = os.environ.get("FPWB_LAUNCH_TERMINAL", "1").lower() not in {
                "0", "false", "no", "off",
            }
            if terminal_enabled and Path("/usr/bin/osascript").exists():
                shell_command = "exec " + shlex.join(args)
                apple_script = """on run argv
  set shellCommand to item 1 of argv
  tell application \"Terminal\"
    activate
    do script shellCommand
  end tell
end run
"""
                try:
                    opener = subprocess.Popen(
                        ["/usr/bin/osascript", "-", shell_command],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    if opener.stdin is not None:
                        opener.stdin.write(apple_script)
                        opener.stdin.close()
                    rc = opener.wait(timeout=10)
                    if rc == 0:
                        return _MacOSAppProcess(opener, port)
                    detail = (opener.stderr.read() if opener.stderr else "").strip()
                    if detail:
                        errors.append("Terminal.app: " + detail[:240])
                except (OSError, subprocess.SubprocessError) as exc:
                    errors.append(f"Terminal.app: {type(exc).__name__}")
            # Some macOS installations have a stale/broken LaunchServices
            # database (notably after replacing Chromium.app in-place).  In
            # that case both `open` and the Terminal bridge return
            # kLSNoExecutableErr even though the Mach-O is present and signed.
            # Fall back to the real executable so the CDP readiness probe can
            # decide whether the current desktop session can host Chromium;
            # callers then get the precise SIGABRT/session diagnostic instead
            # of a misleading missing-executable error.
            direct_fallback = os.environ.get("FPWB_DIRECT_FALLBACK", "1").lower() not in {
                "0", "false", "no", "off",
            }
            if direct_fallback:
                try:
                    return subprocess.Popen(
                        args,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=0,
                    )
                except OSError as exc:
                    errors.append(f"直接启动: {type(exc).__name__}: {exc}")
            suffix = f"：{' | '.join(errors)}" if errors else ""
            raise RuntimeError(
                "macOS 未能通过 LaunchServices 启动 Chromium.app"
                + suffix
                + "；请在已登录桌面会话中运行启动工作台脚本"
            )
    return subprocess.Popen(  # nosec B603 参数来自本地受控配置
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )


async def start(
    *,
    profile_id: str,
    user_data_dir: Path,
    fingerprint_data: dict,
    target_os: str,
    proxy: Optional[ProxyConfig],
    launch: LaunchConfig,
    headless: Optional[bool] = None,
    start_url: Optional[str] = None,
    mobile_mode: bool = False,        # 新增：移动端仿真模式
    mobile_target_os: Optional[str] = None,  # 新增：android/ios
) -> dict[str, Any]:
    import asyncio

    exe = find_executable()
    if not exe:
        raise RuntimeError(is_available()[1])

    # 指纹种子：直接使用该环境固定的 canvas 噪声种子（跨启动稳定）
    seed = int((fingerprint_data.get("seeds") or {}).get("canvas", 0) or 0)
    if seed <= 0:
        raise RuntimeError("环境指纹数据缺少种子，请重新生成指纹")

    nav = {}
    fp_section = fingerprint_data.get("fingerprint") or {}
    mobile_extra = fp_section.get("mobileExtra") or {}
    if fingerprint_data.get("mode") == "preset" and fingerprint_data.get("preset"):
        nav = (fingerprint_data.get("preset") or {}).get("navigator") or {}
    else:
        nav = (fingerprint_data.get("fingerprint") or {}).get("navigator") or {}

    # 平台版本：移动端优先取机型档案中的真实系统版本（与 UA/UA-CH 一致）
    platform_version = launch.fp_platform_version
    if not platform_version and target_os in ("android", "ios"):
        platform_version = mobile_extra.get("platformVersionCh")
    if not platform_version:
        platform_version = _stable_platform_version(target_os, seed)

    port = _free_port()
    args = [
        exe,
        f"--fingerprint={seed}",
        f"--fingerprint-platform={_PLATFORM_ARG.get(target_os, 'windows')}",
        f"--fingerprint-platform-version={platform_version}",
        "--fingerprint-brand=Chrome",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--restore-last-session=false",
        "--disable-session-crashed-bubble",
        # WebRTC 默认禁用非代理 UDP，防 IP 泄露（该内核默认行为，显式声明）
        "--disable-non-proxied-udp",
    ]
    # 移动端必须关闭 canvas 伪装：该类别会给 WebGL readPixels 叠加稀疏噪声
    # （实测内核 152：256×256 命中 19 像素、512×512 命中 18、1024×1024 命中
    # 66，41×41 恰好为 0），BrowserScan 的零知识探针「纯红三角形 R 必须等于
    # A」会直接判失败（"WebGL exception" -5%）。该噪声与尺寸相关、不可预测，
    # 注入层也无法还原原始像素（kernel 已在其上做过改写），只能整类关闭。
    # 代价：2D canvas 像素哈希回到宿主栅格结果（F-57 已认定为不可修面）；
    # 实测关闭后字体度量与 canvas 伪装开启时逐项一致（arial 647.75390625 /
    # roboto 667.8719482421875 / helvetica 647.75390625 / sans 667.8719482421875
    # / times 620.05078125），因此不引入字体栅格回归。
    # 注意：内核 148 时代 readPixels 对照曾保持纯净，但 152 已重新引入噪声，
    # 旧结论不再成立；若后续版本恢复纯净，可通过 launch.fp_disable_spoofing
    # 显式移除 canvas 做回归对拍。
    # audio 同理：内核音频伪装在渲染输出上叠加宽带噪声，检测站的
    # 「IIR 截止频带能量」探针直接判定为防关联浏览器人工加噪
    # （实测低通滤波器截止带能量 -11.8dB > -75dB 阈值）；真机 Chromium
    # 的 Web Audio DSP 是纯软件实现，同引擎跨设备近一致，直接关闭
    # 加噪即回到真机口径（详见 docs/移动端大厂检测对抗优化方案.md §2.9）。
    # gpu 同理，但原因不同：该伪装只改写 UNMASKED_VENDOR/RENDERER 两个字符串，
    # 且用**缓存返回**实现——实测 getParameter 吞吐从原生 Z≈10 掉到 Z≈1500
    # （调用数/毫秒），BrowserScan 的 E() 计时器 Z>100 即判「WebGL 被伪造」。
    # 移动端这两个字符串本就由注入层在页面/OOPIF/Worker 三处覆写为声明的
    # Adreno/Mali，内核伪装纯属冗余，只留下时序破绽（ledger :1854 要求
    # 原生路径先行，缓存返回恰好破坏该防御）。
    disabled_spoofing = [s for s in (launch.fp_disable_spoofing or "").split(",") if s]
    if mobile_mode or mobile_target_os in ("android", "ios"):
        for forced in ("canvas", "audio", "font", "gpu"):
            if forced not in disabled_spoofing:
                disabled_spoofing.append(forced)
    if disabled_spoofing:
        args.append(f"--disable-spoofing={','.join(disabled_spoofing)}")
    hw = nav.get("hardwareConcurrency")
    if hw:
        args.append(f"--fingerprint-hardware-concurrency={int(hw)}")
    ua_version = None
    ua = nav.get("userAgent") or ""
    if "Chrome/" in ua:
        ua_version = ua.split("Chrome/")[1].split(" ")[0].split(".")[0]
    if ua_version:
        args.append(f"--fingerprint-brand-version={ua_version}")
    if launch.locale:
        args += [f"--lang={launch.locale}", f"--accept-lang={launch.locale}"]
    # 移动端不给 --timezone：该参数一旦下发，CDP Emulation.setTimezoneOverride
    # 会静默失效（返回成功但 Intl/Date 保持命令行值，实测 fp-chromium 152：
    # 无参数时 override 立即生效并跨导航/iframe/worker 保持，有参数时覆盖无效）。
    # 移动端时区由 CDP 注入层下发（可随新页面/重连重发），命令行参数留给
    # 桌面内核（无常驻 CDP 会话）。
    if launch.timezone and not (mobile_mode or mobile_target_os in ("android", "ios")):
        args.append(f"--timezone={launch.timezone}")
    if launch.block_webrtc:
        args.append("--disable-webrtc")
    if proxy is not None:
        # 该内核命令行代理不支持账号密码（Chromium 限制）
        args.append(f"--proxy-server={proxy.scheme}://{proxy.host}:{proxy.port}")
    
    effective_headless = headless if headless is not None else launch.headless
    if effective_headless:
        # 不加 --disable-gpu：软件渲染是风控特征（SwiftShader 渲染器）
        args.append("--headless=new")
    
    # --- 移动端仿真模式特殊处理 ---
    mobile_viewport = None
    mobile_dpr = 1.0
    mobile_ua = ua
    mobile_platform = nav.get("platform", "")
    mobile_max_touch = int(nav.get("maxTouchPoints") or 5)
    mobile_start_url = start_url or launch.start_url or ""
    if mobile_mode or mobile_target_os in ("android", "ios"):
        # 从指纹数据读取移动端屏幕信息（物理分辨率 + CSS 逻辑尺寸）
        screen = fp_section.get("screen", {})
        if screen.get("width") and screen.get("height"):
            mobile_viewport = {
                "width": screen["width"],
                "height": screen["height"],
            }
            mobile_dpr = screen.get("devicePixelRatio", 2.5)

        # 确保 UA 正确。Android 必须收敛成真机的 UA 缩减形态：内核命令行
        # 的 --user-agent 在 CDP 注入落达前就已生效，存量档案里的缩减前串
        # （`Linux; Android 16; <Build.MODEL>`）会在这个窗口里原样发出去。
        if mobile_target_os == "android":
            from ..mobile_fingerprints import (
                _reduced_android_ua, ANDROID_REDUCED_NAV_PLATFORM)
            mobile_ua = _reduced_android_ua(mobile_ua or nav.get("userAgent", ""))
            # navigator.platform 同属缩减范围，取值被 Blink 硬编码
            mobile_platform = ANDROID_REDUCED_NAV_PLATFORM
        elif not mobile_ua:
            mobile_ua = nav.get("userAgent", "")

        # 桌面内核上的移动仿真：窗口尺寸按 CSS 逻辑像素（物理 / DPR）预留，
        # UA / UA-CH / 屏幕 / GPU / 色深等由 CDP 注入层补齐（mobile_cdp_injector）
        if mobile_viewport:
            css_w = screen.get("cssWidth") or round(mobile_viewport["width"] / (mobile_dpr or 1.0))
            css_h = screen.get("cssHeight") or round(mobile_viewport["height"] / (mobile_dpr or 1.0))
            args.append(f"--window-size={css_w},{css_h + 130}")
        args.append("--touch-events=enabled")
        if mobile_ua:
            args.append(f"--user-agent={mobile_ua}")

    # 移动端：先 about:blank 静默启动，等 CDP 注入完成后再导航到 start_url，
    # 避免首个页面在注入生效前以桌面指纹加载
    if mobile_mode or mobile_target_os in ("android", "ios"):
        args.append("about:blank")
    else:
        args.append(start_url or launch.start_url or "about:blank")

    proc = _launch(exe, args, port, headless=bool(effective_headless))

    ws_endpoint: Optional[str] = None
    # 回环地址探测 CDP：忽略系统/环境代理，否则 HTTP_PROXY 会把 127.0.0.1 请求转发出去
    # 导致「端口未就绪」误报并把已启动的浏览器杀掉
    async with httpx.AsyncClient(trust_env=False) as client:
        for _ in range(40):
            await asyncio.sleep(0.5)
            if isinstance(proc, _MacOSAppProcess) and proc._browser_pid is None:
                proc.attach(_browser_pid_for_port(port))
            if proc.poll() is not None:
                if isinstance(proc, _MacOSAppProcess) and proc._browser_pid is None:
                    # ``open`` has returned, but LaunchServices may still be
                    # registering the app. Continue to probe CDP; endpoint
                    # success below is also the fallback when process listing
                    # is denied by the host policy.
                    pass
                else:
                    code = proc.poll()
                    if sys.platform == "darwin" and code == -signal.SIGABRT:
                        # macOS may abort a Chromium GUI process before it
                        # opens the debugging port when the caller is running
                        # without a usable desktop session (for example, a
                        # restricted automation sandbox).  The generic exit
                        # code makes this look like a bad profile or a broken
                        # model, while the actual remedy is to start the
                        # management service from a logged-in desktop session.
                        raise RuntimeError(
                            "浏览器进程被 macOS 终止（SIGABRT/_RegisterApplication）；"
                            "请在登录的桌面会话且不受限的运行环境中启动管理端后重试"
                        )
                    raise RuntimeError(f"浏览器进程提前退出（exit={code}）")
            try:
                r = await client.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
                ws_endpoint = r.json().get("webSocketDebuggerUrl")
                if ws_endpoint:
                    if isinstance(proc, _MacOSAppProcess):
                        proc.mark_running()
                    break
            except Exception:
                continue
    if not ws_endpoint:
        _kill(proc)
        raise RuntimeError("CDP 调试端口在 20 秒内未就绪")

    # 返回额外信息供 launcher 层使用（用于 Playwright 上下文配置）
    result = {
        "process": proc,
        "debug_port": port,
        "ws_endpoint": ws_endpoint,
        "info": {
            "kernel": KERNEL_NAME,
            "debug_port": port,
            "ws_endpoint": ws_endpoint,
            "fingerprint_seed": seed,
            "mobile_viewport": mobile_viewport,
            "mobile_dpr": mobile_dpr,
            "mobile_ua": mobile_ua,
            "mobile_platform": mobile_platform,
            "mobile_max_touch": mobile_max_touch,
            "mobile_start_url": mobile_start_url,
            "note": "fingerprint-chromium 内核：内核层指纹伪装 + 标准 CDP 端点，"
                    "Selenium/Puppeteer/Playwright 可直连 ws_endpoint。",
        },
    }
    
    return result


def _kill(proc: subprocess.Popen) -> None:
    if sys.platform == "win32":
        subprocess.run(  # nosec B609
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True
        )
    else:
        proc.terminate()


# ---- F-15：孤儿实例回收 ----
# 宿主进程被 kill、探针脚本异常退出、或服务端崩溃时，浏览器子进程与
# profile 目录锁（SingletonLock/SingletonCookie/SingletonSocket 三个符号
# 链接）会一直留存：同一环境再次启动会撞锁，profile 目录被永久占住。
# 这里在服务启动时扫一遍「本工作台起的浏览器进程」，只回收父进程已经
# 不在的——父进程还活着说明那是另一个正在跑的实例（多实例部署共用
# FPWB_HOME 时尤其要守住这条），绝不能动。
_PROFILE_ROOT_ARG = "--user-data-dir="


def _iter_browser_processes() -> list[tuple[int, int, str]]:
    """(pid, ppid, user-data-dir) —— 只认命令行带 --user-data-dir 的浏览器。

    额外要求 `--remote-debugging-port=`：本内核启动时必带（见 start()），
    而包装脚本的 `python -c "...--user-data-dir=..."` 只是**提到**这个
    字符串，不该被当成浏览器——否则回收时会把拉起它的父进程一起杀掉。
    """
    if sys.platform == "win32":
        return []
    try:
        out = subprocess.run(  # nosec B603 无外部输入
            ["ps", "-eo", "pid=,ppid=,args="],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in out.splitlines():
        fields = line.split(None, 2)
        if len(fields) < 3:
            continue
        pid_s, ppid_s, args = fields
        if "--remote-debugging-port=" not in args:
            continue
        marker = args.find(_PROFILE_ROOT_ARG)
        if marker < 0:
            continue
        data_dir = args[marker + len(_PROFILE_ROOT_ARG):].split()[0]
        if not data_dir:
            continue
        try:
            found.append((int(pid_s), int(ppid_s), data_dir))
        except ValueError:
            continue
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def reclaim_orphans(profile_root: Path) -> list[int]:
    """回收本工作台遗留的孤儿浏览器，返回被终止的 pid 列表。

    判据（两条同时成立才动手）：
      1. 浏览器进程的 --user-data-dir 落在 profile_root 下——是本工作台的；
      2. 它的父进程已经不是本工作台的浏览器——孤儿。

    只杀「父进程已消失」的：探针脚本异常退出后子进程会被 reparent 到
    launchd(1)；而正常运行的实例父进程是 Python 服务进程，仍然存活。
    收尾时顺手清掉 profile 目录里的三个 Singleton* 符号链接——目标
    （hostname-pid 与临时 socket 路径）早已失效，留着会让下一次启动
    误判「已有实例在用」。
    """
    # macOS 上 /tmp 是 /private/tmp 的符号链接：进程命令行里出现的可能是
    # 任一种写法，两个前缀都要认，否则 profile 目录会被判成「不属于本工作台」。
    roots = []
    for candidate in (profile_root, Path(os.path.abspath(profile_root))):
        text = str(candidate)
        if text not in roots:
            roots.append(text)
        try:
            resolved = str(candidate.resolve())
        except OSError:
            resolved = ""
        if resolved and resolved not in roots:
            roots.append(resolved)
    if not roots:
        return []

    def _owned(data_dir: str) -> bool:
        return any(data_dir.startswith(r + os.sep) for r in roots)

    procs = _iter_browser_processes()
    by_pid = {pid: (ppid, data_dir) for pid, ppid, data_dir in procs}
    killed: list[int] = []
    orphan_dirs: set[str] = set()
    for pid, ppid, data_dir in procs:
        if not _owned(data_dir):
            continue
        # 父进程仍是本工作台的浏览器 → 这是它的子进程，不是孤儿
        parent = by_pid.get(ppid)
        if parent is not None and _owned(parent[1]):
            continue
        # 父进程活着且不是 launchd：可能由探针脚本或另一个服务实例直接
        # 拉起，归属不明，保守起见不动。
        if ppid != 1 and _alive(ppid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            continue
        killed.append(pid)
        orphan_dirs.add(data_dir)
    if orphan_dirs:
        _clear_singleton_links(roots, orphan_dirs)
    return killed


def _clear_singleton_links(roots: list, dirs: set) -> None:
    """清掉孤儿 profile 目录里残留的 Singleton* 符号链接。"""
    for data_dir in dirs:
        path = Path(data_dir)
        if not any(str(path).startswith(r + os.sep) for r in roots):
            continue
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            link = path / name
            try:
                if link.is_symlink():
                    link.unlink()
            except OSError:
                continue


async def stop(instance: dict[str, Any]) -> None:
    import asyncio

    proc: subprocess.Popen = instance["process"]
    if proc.poll() is None:
        _kill(proc)
        await asyncio.to_thread(proc.wait, timeout=10)
