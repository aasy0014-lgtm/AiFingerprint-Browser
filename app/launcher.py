"""启动管理器：维护「环境 → 运行中的浏览器实例」的生命周期。

三内核：camoufox/fp-chromium/chromium
移动端仿真：Android/iOS 仅 fp-chromium（CDP 设备仿真，见 mobile_cdp_injector）
"""
import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .kernels import camoufox_kernel, chromium_kernel, fp_chromium_kernel
from .config import PROFILE_ROOT
from . import db, security
from .models import LaunchConfig, ProxyConfig
from .proxy_dns import is_ip_address, resolve_real_ip
from .proxy_forwarder import AuthProxyForwarder, needs_forwarder
from .proxy_test import locale_for_country, resolve_exit_geo
from .risk_presets import apply_preset
from .mobile_cdp_injector import MobileEmulationSession

log = logging.getLogger(__name__)


class LaunchError(Exception):
    pass


@dataclass
class RunningInstance:
    profile_id: str
    kernel: str
    started_at: float
    info: dict
    stop: Callable[[], Awaitable[None]]
    # camoufox 内核持有的页面引用（供 navigate/screenshot/evaluate 使用）
    page: Any = None
    context: Any = None
    process: Any = None
    extra: dict = field(default_factory=dict)

    def is_alive(self) -> bool:
        if self.kernel == "camoufox":
            return self.context is not None and not self.context.is_closed()
        return self.process is not None and self.process.poll() is None


class LaunchManager:
    def __init__(self) -> None:
        self._running: dict[str, RunningInstance] = {}
        self._lock = asyncio.Lock()
        # 允许同时拉起多个实例（批量启动并行；限制并发防资源打满）
        self._launch_sem = asyncio.Semaphore(4)
        self._starting: set[str] = set()
        # 后台资源释放任务的强引用（防止事件循环执行前被 GC）
        self._bg_tasks: set[asyncio.Task] = set()

    async def _close_instances(self, insts: list["RunningInstance"]) -> None:
        for inst in insts:
            try:
                await inst.stop()
            except Exception as e:
                log.warning("释放已退出环境 %s 的资源时出错: %s", inst.profile_id, e)

    def _close_soon(self, insts: list["RunningInstance"]) -> None:
        """同步调用点：把已退出实例的资源释放交给事件循环。

        浏览器被用户手动关闭/崩溃时，实例只是从 _running 摘除并不够——
        必须关闭 context 与 playwright，否则 node 驱动进程会一直残留。
        """
        if not insts:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._close_instances(insts))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def is_running(self, profile_id: str) -> bool:
        inst = self._running.get(profile_id)
        if inst is None:
            return False
        if inst.is_alive():
            return True
        # 浏览器已被手动关闭/崩溃：摘除实例并异步释放其驱动进程
        self._running.pop(profile_id, None)
        self._close_soon([inst])
        return False

    def emulation_state(self, profile_id: str) -> Optional[str]:
        """移动端仿真通道状态：ok / reconnecting；None = 非移动端或未运行。

        通道断开期间内核会还原全部 CDP 覆盖，已打开页面退化为仅 JS 补丁
        （媒体查询回落宿主值）、新标签页完全裸奔；看门狗会自动重连自愈，
        面板据此提示用户。
        """
        inst = self._running.get(profile_id)
        if inst is None:
            return None
        sess = inst.extra.get("mobile_session")
        if sess is None:
            return None
        return "ok" if sess.healthy else "reconnecting"

    async def start(
        self,
        profile: dict[str, Any],
        headless: Optional[bool] = None,
        start_url: Optional[str] = None,
    ) -> dict[str, Any]:
        profile_id = profile["id"]
        async with self._lock:
            if self.is_running(profile_id):
                raise LaunchError(f"环境 {profile['name']} 已在运行")
            if profile_id in self._starting:
                raise LaunchError(f"环境 {profile['name']} 正在启动中")
            self._starting.add(profile_id)
        try:
            # 提前初始化：异常清理路径（except 块）需要它，早于代理/配置的失败
            # 都必须能安全走到清理逻辑
            forwarder: AuthProxyForwarder | None = None
            user_data_dir = PROFILE_ROOT / profile_id
            user_data_dir.mkdir(parents=True, exist_ok=True)

            launch = LaunchConfig(**(profile.get("launch") or {}))
            launch = apply_preset(launch)  # 风控预设参数组合
            # 移动端仅 fp-chromium 支持（CDP 设备仿真）：其余内核会把移动端参数
            # 直接透传给 camoufox 启动参数而报错，这里提前给出明确错误
            if (profile.get("target_os") in ("android", "ios")
                    and profile["kernel"] != fp_chromium_kernel.KERNEL_NAME):
                raise LaunchError("移动端目标 OS（android/ios）仅支持 fp-chromium 内核")
            # 环境自身未配代理时，兜底使用全局代理
            proxy_data = profile.get("proxy") or security.get_global_proxy()
            proxy = ProxyConfig(**proxy_data) if proxy_data else None
            # 本机 fake-IP（TUN）会把代理域名解析成 198.18.x.x，改用 DoH 取真实 IP
            if proxy and not is_ip_address(proxy.host):
                real_ip = await resolve_real_ip(proxy.host)
                if real_ip and real_ip != proxy.host:
                    log.info("代理域名 %s 经 DoH 解析为真实 IP %s", proxy.host, real_ip)
                    proxy = proxy.model_copy(update={"host": real_ip})

            # geoip：出口时区 + 语言对齐（fp-chromium 无原生 geoip）。
            # 显式 timezone/locale 优先；探测结果用于内核 --timezone/--lang 参数
            # 与移动端 CDP 的 Emulation.setTimezoneOverride /
            # setUserAgentOverride.acceptLanguage。只对齐时区会让「属地欧美、
            # 语言中文」成为跨域特征（检测站点据此判为代理环境）。
            #
            # 语言对齐仅限移动端：Intl 默认 locale 由内核从宿主系统取得，--lang
            # 与 LANG/LC_ALL 都改不动它，只有 CDP Emulation.setLocaleOverride 能改
            # （实测 Chromium 148）。桌面 fp-chromium 没有常驻 CDP 会话，只改
            # navigator.language 会让它与 Intl 互相矛盾——那比「两者都是中文、
            # 只有时区对不上」更容易被判定为改环境，故桌面保持原状。
            if (launch.geoip and (not launch.timezone or not launch.locale)
                    and proxy is not None
                    and profile["kernel"] == fp_chromium_kernel.KERNEL_NAME):
                mobile_target = profile.get("target_os") in ("android", "ios")
                geo = await resolve_exit_geo(proxy)
                updates = {}
                if not launch.timezone and geo.get("timezone"):
                    updates["timezone"] = geo["timezone"]
                if mobile_target and not launch.locale:
                    locale = locale_for_country(geo.get("country_code"))
                    if locale:
                        updates["locale"] = locale
                if updates:
                    launch = launch.model_copy(update=updates)
                    log.info("geoip：出口属地对齐 %s", updates)
                else:
                    log.info("geoip：出口属地探测失败，保持系统时区与语言")

            # Chromium 系内核不支持带账密代理命令行：本地起认证转发器桥接
            kernel_proxy = proxy
            if (profile["kernel"] in (fp_chromium_kernel.KERNEL_NAME,
                                      chromium_kernel.KERNEL_NAME)
                    and needs_forwarder(proxy)):
                forwarder = await AuthProxyForwarder(proxy).start()
                kernel_proxy = ProxyConfig(
                    scheme="http", host="127.0.0.1", port=forwarder.port
                )

            async with self._launch_sem:
                if profile["kernel"] == camoufox_kernel.KERNEL_NAME:
                    from playwright.async_api import async_playwright

                    pw = await async_playwright().start()
                    try:
                        result = await camoufox_kernel.start(
                            playwright=pw,
                            profile_id=profile_id,
                            user_data_dir=user_data_dir,
                            fingerprint_data=profile["fingerprint"],
                            target_os=profile["target_os"],
                            proxy=proxy,
                            launch=launch,
                            headless=headless,
                            start_url=start_url,
                        )
                    except Exception:
                        await pw.stop()
                        raise
                    result["playwright"] = pw
                    inst = RunningInstance(
                        profile_id=profile_id,
                        kernel=camoufox_kernel.KERNEL_NAME,
                        started_at=time.time(),
                        info=result["info"],
                        stop=lambda: camoufox_kernel.stop(result),
                        page=result["page"],
                        context=result["context"],
                    )
                elif profile["kernel"] == fp_chromium_kernel.KERNEL_NAME:
                    # 检测是否为移动端环境
                    target_os = profile.get("target_os", "windows")
                    is_mobile = target_os in ("android", "ios")
                    
                    result = await fp_chromium_kernel.start(
                        profile_id=profile_id,
                        user_data_dir=user_data_dir,
                        fingerprint_data=profile["fingerprint"],
                        target_os=target_os,
                        proxy=kernel_proxy,
                        launch=launch,
                        headless=headless,
                        start_url=start_url,
                        mobile_mode=is_mobile,
                        mobile_target_os=target_os if is_mobile else None,
                    )
                    
                    _fwd = forwarder
                    
                    # --- 移动端 CDP 注入 ---
                    # 注入规格直接取自存储指纹（保证「存储值 = 运行期值」）
                    info = result.get("info", {})
                    fd = profile.get("fingerprint") or {}
                    fp_section = fd.get("fingerprint") or {}
                    viewport = info.get("mobile_viewport")
                    dpr = info.get("mobile_dpr", 1.0)
                    ua = info.get("mobile_ua", "")
                    platform = info.get("mobile_platform", "")
                    max_touch = int(info.get("mobile_max_touch") or 5)
                    start_url_final = info.get("mobile_start_url") or ""
                    mobile_session = None
                    if is_mobile and fp_section:
                        mobile_session = MobileEmulationSession(
                            info["ws_endpoint"],
                            fingerprint=fp_section,
                            target_os=target_os,
                            locale=launch.locale or "",
                            timezone=launch.timezone or "",
                            start_url=start_url_final,
                        )
                    # 桌面环境不建立移动仿真会话：内核 info 的 mobile_ua 字段对
                    # 桌面环境同样有值（通用 UA 回填），不能作为「需要移动仿真」的
                    # 判据——否则桌面 fp-chromium 启动会因缺少屏幕尺寸而失败。
                    elif is_mobile and (viewport or ua):
                        mobile_session = MobileEmulationSession(
                            info["ws_endpoint"],
                            viewport=viewport, dpr=dpr, ua=ua, platform=platform,
                            max_touch_points=max_touch,
                            locale=launch.locale or "",
                            timezone=launch.timezone or "",
                            start_url=start_url_final,
                        )

                    # 工作台可见性/审计：看门狗断线重连事件写入审计日志，
                    # 仿真通道状态经 LaunchManager.emulation_state 暴露给面板
                    if mobile_session is not None:
                        mobile_session.profile_id = profile_id
                        mobile_session.profile_name = profile.get("name") or ""

                        def _emu_event(_kind: str, count: int,
                                       _name: str = mobile_session.profile_name,
                                       _pid: str = profile_id) -> None:
                            with contextlib.suppress(Exception):
                                db.audit_log(
                                    "browser.emu_reconnect", _name or _pid,
                                    f"第 {count} 次自动重连（仿真通道断开自愈）",
                                )

                        mobile_session.on_session_event = _emu_event

                    async def _setup_mobile_context():
                        """建立移动端 CDP 仿真会话（常驻到环境停止）。

                        CDP 仿真设置随客户端断开会被浏览器还原（与 DevTools
                        设备模式同理），因此必须保持连接；会话同时监听
                        新开标签页并自动应用仿真。
                        """
                        if mobile_session is None:
                            return
                        try:
                            await mobile_session.start()
                        except asyncio.CancelledError:
                            with contextlib.suppress(Exception):
                                await mobile_session.close()
                            raise
                        except Exception as e:
                            log.warning("移动端仿真建立失败：%s", e)
                            with contextlib.suppress(Exception):
                                await mobile_session.close()
                            return
                        inst.extra["mobile_session"] = mobile_session
                        log.info("✅ 移动端仿真已生效 (OS=%s, %s %s, CSS %sx%s @%.2fx, 视口高 %s)",
                                 target_os, fd.get("manufacturer") or "", fd.get("device_model") or "",
                                 mobile_session.css_w, mobile_session.css_h,
                                 mobile_session.dpr, mobile_session.viewport_h)
                    
                    # 输入翻译代理（P2-3）：下游自动化（Selenium/Puppeteer/
                    # Playwright）经 ws_endpoint 发来的鼠标命令转成触摸序列。
                    # 桌面环境不挂：鼠标就是桌面输入，翻译反而失真。
                    input_proxy = None
                    if is_mobile and info.get("ws_endpoint"):
                        from .cdp_input_proxy import CdpInputProxy
                        input_proxy = CdpInputProxy(info["ws_endpoint"])
                        try:
                            real_endpoint = info["ws_endpoint"]
                            info["ws_endpoint"] = await input_proxy.start()
                            info["ws_endpoint_direct"] = real_endpoint
                        except Exception as e:  # noqa: BLE001
                            log.warning("CDP 输入代理启动失败，下游直连内核: %s", e)
                            with contextlib.suppress(Exception):
                                await input_proxy.close()
                            input_proxy = None

                    # 异步建立仿真（不阻塞启动流程），句柄随实例生命周期管理
                    mobile_task = asyncio.create_task(_setup_mobile_context())

                    async def _stop_fpchromium() -> None:
                        if not mobile_task.done():
                            mobile_task.cancel()
                            with contextlib.suppress(asyncio.CancelledError, Exception):
                                await mobile_task
                        sess = inst.extra.get("mobile_session")
                        if sess is not None:
                            with contextlib.suppress(Exception):
                                await sess.close()
                        if input_proxy is not None:
                            with contextlib.suppress(Exception):
                                await input_proxy.close()
                        await fp_chromium_kernel.stop(result)
                        if _fwd:
                            await _fwd.stop()

                    inst = RunningInstance(
                        profile_id=profile_id,
                        kernel=fp_chromium_kernel.KERNEL_NAME,
                        started_at=time.time(),
                        info=result["info"],
                        stop=_stop_fpchromium,
                        process=result["process"],
                    )
                else:
                    result = await chromium_kernel.start(
                        profile_id=profile_id,
                        user_data_dir=user_data_dir,
                        proxy=kernel_proxy,
                        launch=launch,
                        headless=headless,
                        start_url=start_url,
                    )
                    _fwd = forwarder

                    async def _stop_chromium() -> None:
                        await chromium_kernel.stop(result)
                        if _fwd:
                            await _fwd.stop()

                    inst = RunningInstance(
                        profile_id=profile_id,
                        kernel=chromium_kernel.KERNEL_NAME,
                        started_at=time.time(),
                        info=result["info"],
                        stop=_stop_chromium,
                        process=result["process"],
                    )

            if forwarder:
                inst.extra["proxy_forwarder"] = True
                inst.info = {**inst.info,
                             "proxy_via": f"本地认证转发 127.0.0.1:{forwarder.port}"}

            async with self._lock:
                self._running[profile_id] = inst
        except Exception:
            if forwarder:
                await forwarder.stop()
            raise
        finally:
            async with self._lock:
                self._starting.discard(profile_id)

        # 环境使用统计（启动次数/最近启动）
        try:
            db.bump_usage(profile_id)
        except Exception:
            log.debug("使用统计更新失败", exc_info=True)

        return {
            "profile_id": profile_id,
            "profile_name": profile["name"],
            "started_at": inst.started_at,
            **inst.info,
        }

    async def stop(self, profile_id: str) -> None:
        async with self._lock:
            inst = self._running.pop(profile_id, None)
        if inst is None:
            return
        try:
            await inst.stop()
        except Exception as e:
            log.warning("停止环境 %s 时出错: %s", profile_id, e)

    async def stop_all(self) -> None:
        ids = list(self._running.keys())
        for pid in ids:
            await self.stop(pid)

    def active(self) -> list[dict[str, Any]]:
        # 清理已退出的实例（用户手动关掉浏览器窗口的情况），并释放其驱动进程
        dead = [p for p, i in self._running.items() if not i.is_alive()]
        self._close_soon([self._running.pop(p) for p in dead])
        return [
            {
                "profile_id": i.profile_id,
                "kernel": i.kernel,
                "started_at": i.started_at,
                **i.info,
            }
            for i in self._running.values()
        ]

    def get_instance(self, profile_id: str) -> Optional[RunningInstance]:
        inst = self._running.get(profile_id)
        if inst and inst.is_alive():
            return inst
        return None
