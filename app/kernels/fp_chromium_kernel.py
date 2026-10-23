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
import signal
import socket
import subprocess
import sys
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
        # 真实二进制在 Contents/MacOS/ 下（必须用真实路径启动，
        # 经符号链接启动会让 dyld 按链接路径解析框架而失败）
        for app_dir in [*tools.glob("*.app"), *tools.rglob("*.app")]:
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
    # 内核 canvas 伪装会给 WebGL readPixels 注入种子化像素噪声（R≠A），
    # 被 BrowserScan 的「纯红三角形 R 应恒等于 A」零知识检查捕获 →
    # "WebGL exception"（实测：3 个噪声像素 [254,1,0,255]）。
    # 真机 readPixels 输出纯净，移动端必须关闭该类别；与用户显式配置合并。
    # audio 同理：内核音频伪装在渲染输出上叠加宽带噪声，检测站的
    # 「IIR 截止频带能量」探针直接判定为防关联浏览器人工加噪
    # （实测低通滤波器截止带能量 -11.8dB > -75dB 阈值）；真机 Chromium
    # 的 Web Audio DSP 是纯软件实现，同引擎跨设备近一致，直接关闭
    # 加噪即回到真机口径（详见 docs/移动端大厂检测对抗优化方案.md §2.9）。
    disabled_spoofing = [s for s in (launch.fp_disable_spoofing or "").split(",") if s]
    if mobile_mode or mobile_target_os in ("android", "ios"):
        for forced in ("canvas", "audio", "font"):
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
    if launch.timezone:
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

        # 确保 UA 正确
        if not mobile_ua:
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

    proc = subprocess.Popen(  # nosec B603 参数来自本地受控配置
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )

    ws_endpoint: Optional[str] = None
    # 回环地址探测 CDP：忽略系统/环境代理，否则 HTTP_PROXY 会把 127.0.0.1 请求转发出去
    # 导致「端口未就绪」误报并把已启动的浏览器杀掉
    async with httpx.AsyncClient(trust_env=False) as client:
        for _ in range(40):
            await asyncio.sleep(0.5)
            if proc.poll() is not None:
                raise RuntimeError(f"浏览器进程提前退出（exit={proc.returncode}）")
            try:
                r = await client.get(f"http://127.0.0.1:{port}/json/version", timeout=2)
                ws_endpoint = r.json().get("webSocketDebuggerUrl")
                if ws_endpoint:
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
