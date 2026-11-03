#!/usr/bin/env python3
"""自有本地测试表单回放 + 脱敏网络元数据追踪。

脚本只访问 localhost/回环地址上的测试应用，通过管理端启动指定环境，再经
CDP/Playwright 执行一个 JSON 步骤文件，同时收集请求、响应、重定向、失败、
WebSocket 和页面运行时状态。输出是
HAR 风格的**元数据**而不是原始 TLS 包：URL 查询值、Cookie、Authorization、
表单值和响应正文均不落盘，只保留主机/路径、状态码、内容类型、长度、哈希和
风险信号。公网目标、第三方注册站点和跨站重定向会被拒绝或拦截。

默认 dry-run：标记为 ``submit=true`` 的最后提交步骤会被记录但跳过；显式传入
``--allow-submit`` 才会执行它。脚本不处理验证码，也不修改挑战/验证响应。

步骤文件示例（运行本地自有测试应用）：

    [
      {"action": "goto", "url": "http://127.0.0.1:3000/signup"},
      {"action": "fill", "selector": "input[name=firstName]", "value": "ROLE_A"},
      {"action": "click", "selector": "button[type=submit]", "submit": true}
    ]

用法：

    .venv/bin/python scripts/registration_trace.py \
      --management-url http://127.0.0.1:18080 \
      --profile-id PROFILE_ID --flow-file flow.json

没有 ``--profile-id`` 时会创建临时 Android 环境；临时环境只在脚本结束时删除。
已有环境不会删除，若它本来在运行也不会停止。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import ipaddress
import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from playwright.async_api import BrowserContext, Page, Request, Response, async_playwright


DEFAULT_MANAGEMENT_URL = "http://127.0.0.1:18080"
# This harness is intentionally restricted to loopback test apps.  It is useful
# for exercising an application's own signup form without automating accounts
# on third-party services.
DEFAULT_START_URL = "http://127.0.0.1:3000/signup"
SENSITIVE_KEY_RE = re.compile(
    r"(?:pass(?:word)?|token|secret|auth|cookie|session|sid|tl|dsh|code|otp|"
    r"email|phone|address|key|credential|assertion|challenge)", re.I
)
SENSITIVE_HEADER_RE = re.compile(
    r"^(?:authorization|cookie|set-cookie|proxy-authorization|x-api-key|"
    r"x-auth|x-csrf|x-goog-authuser|x-goog-visitor-id)$",
    re.I,
)
CHALLENGE_RE = re.compile(
    r"captcha|recaptcha|turnstile|challenge|devicephoneverification|"
    r"crossflowverification|samedevice|unusual|suspicious|verify[-_ ]?(?:your|phone|device)|"
    r"too many|rate limit|session expired",
    re.I,
)
SAFE_HEADER_NAMES = {
    "accept", "content-length", "content-type", "location", "origin",
    "referer", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
    "sec-fetch-user", "accept-language", "upgrade-insecure-requests",
    # Client Hints are non-secret diagnostics.  Keeping them lets the network
    # capture compare the HTTP surface with navigator.userAgentData without
    # persisting cookies, flow tokens, or authorization material.
    "sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform",
    "sec-ch-ua-platform-version", "sec-ch-ua-model", "sec-ch-ua-form-factors",
    "user-agent", "x-requested-with",
}

STATE_JS = r"""() => {
  const n = navigator;
  const m = (q) => !!(window.matchMedia && matchMedia(q).matches);
  return {
    href: location.href,
    title: document.title || '',
    secure: !!window.isSecureContext,
    ua: n.userAgent || '',
    platform: n.platform || '',
    webdriver: n.webdriver,
    language: n.language || '',
    languages: Array.from(n.languages || []),
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
    touch: Number(n.maxTouchPoints || 0),
    touchEvents: 'ontouchstart' in window,
    pointerCoarse: m('(pointer: coarse)'),
    anyPointerCoarse: m('(any-pointer: coarse)'),
    anyHover: m('(any-hover: hover)'),
    screen: `${screen.width}x${screen.height}`,
    viewport: `${innerWidth}x${innerHeight}`,
    dpr: window.devicePixelRatio || 1,
    cookieEnabled: n.cookieEnabled !== false,
    storage: {
      local: (() => { try { localStorage.setItem('__trace__', '1'); localStorage.removeItem('__trace__'); return true; } catch (_) { return false; } })(),
      session: (() => { try { sessionStorage.setItem('__trace__', '1'); sessionStorage.removeItem('__trace__'); return true; } catch (_) { return false; } })(),
      indexedDB: typeof indexedDB !== 'undefined',
    },
    uaCh: n.userAgentData ? {
      platform: n.userAgentData.platform || '',
      mobile: !!n.userAgentData.mobile,
    } : null,
  };
}"""


def _redacted_url(value: str) -> str:
    """保留 URL 结构和参数名，所有查询值统一打码。"""
    try:
        parsed = urlsplit(str(value or ""))
        pairs = []
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            pairs.append((key, "<redacted>"))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                           urlencode(pairs), ""))
    except ValueError:
        return "<invalid-url>"


def _host(value: str) -> str:
    try:
        return (urlsplit(value).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _is_loopback_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_test_url(value: str, *, allow_blank: bool = False) -> None:
    """Restrict navigation to a local test service; never automate public signups."""
    raw = str(value or "").strip()
    if allow_blank and raw == "about:blank":
        return
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError as exc:
        raise ValueError("目标地址格式无效") from exc
    if (parsed.scheme not in {"http", "https"} or not host
            or parsed.username is not None or parsed.password is not None
            or not _is_loopback_host(host)):
        raise ValueError("追踪器仅允许访问 localhost/回环地址上的自有测试应用")


def _private_ip_observation(observed: dict[str, Any], salt: bytes) -> dict[str, Any]:
    """Keep within-run exit-change detection without persisting the raw IP."""
    ip = str(observed.get("ip") or "")
    return {
        "ip_fingerprint": hashlib.sha256(salt + ip.encode("utf-8")).hexdigest()[:16] if ip else None,
        "country": observed.get("country"),
        "timezone": observed.get("timezone"),
        "proxy": observed.get("proxy"),
        "hosting": observed.get("hosting"),
    }


def _message_meta(value: str) -> dict[str, Any]:
    raw = str(value or "").encode("utf-8", errors="replace")
    return {"length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _redacted_headers(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_name, raw_value in dict(headers or {}).items():
        name = str(raw_name).lower()
        if SENSITIVE_HEADER_RE.search(name):
            out[name] = "<redacted>"
        elif name in SAFE_HEADER_NAMES:
            value = str(raw_value)
            out[name] = _redacted_url(value) if name in {"location", "referer"} else value[:300]
    return out


def _body_meta(data: str | None, content_type: str = "") -> dict[str, Any] | None:
    if data is None:
        return None
    raw = data.encode("utf-8", errors="replace")
    meta: dict[str, Any] = {
        "length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    # 只记录字段名，不记录字段值；方便看出是否提交了邮箱/电话/令牌类字段。
    keys: list[str] = []
    try:
        if "json" in content_type.lower():
            payload = json.loads(data)
            if isinstance(payload, dict):
                keys = [str(k) for k in payload.keys()]
        elif "form-urlencoded" in content_type.lower():
            keys = [str(k) for k, _ in parse_qsl(data, keep_blank_values=True)]
    except (TypeError, ValueError, UnicodeError):
        pass
    if keys:
        meta["keys"] = sorted(set(keys))[:100]
    return meta


def _safe_text(value: str, limit: int = 280) -> str:
    text = str(value or "")
    text = re.sub(r"(?i)(password|token|secret|authorization|cookie|session|code)=([^&\s]+)",
                  r"\1=<redacted>", text)
    text = re.sub(r"https?://[^\s]+",
                  lambda m: _redacted_url(m.group(0)), text)
    return text[:limit]


def _extract_ip_payload(host: str, body: str) -> dict[str, Any] | None:
    # Only inspect JSON served by the loopback test application.  The harness
    # must not make outbound IP-check requests to a third-party service.
    if not _is_loopback_host(host):
        return None
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    ip = payload.get("ip") or payload.get("query")
    if not ip:
        return None
    return {
        "ip": str(ip),
        "country": payload.get("country") or payload.get("countryCode")
        or payload.get("country_code"),
        "timezone": (payload.get("timezone") or {}).get("id")
        if isinstance(payload.get("timezone"), dict) else payload.get("timezone"),
        "proxy": payload.get("proxy"),
        "hosting": payload.get("hosting"),
    }


class TraceCollector:
    def __init__(self) -> None:
        self.started = time.monotonic()
        self.events: list[dict[str, Any]] = []
        self.pages: list[dict[str, Any]] = []
        self.ip_observations: list[dict[str, Any]] = []
        self._ip_salt = os.urandom(16)
        self._attached_pages: set[int] = set()

    def _ts(self) -> float:
        return round(time.monotonic() - self.started, 3)

    async def request(self, request: Request) -> None:
        headers = _redacted_headers(request.headers)
        entry: dict[str, Any] = {
            "kind": "request",
            "t": self._ts(),
            "method": request.method,
            "url": _redacted_url(request.url),
            "host": _host(request.url),
            "resource_type": request.resource_type,
            "headers": headers,
            "body": _body_meta(request.post_data, headers.get("content-type", "")),
        }
        redirected_from = request.redirected_from
        if redirected_from:
            entry["redirected_from"] = _redacted_url(redirected_from.url)
        self.events.append(entry)

    async def response(self, response: Response) -> None:
        host = _host(response.url)
        entry: dict[str, Any] = {
            "kind": "response",
            "t": self._ts(),
            "status": response.status,
            "url": _redacted_url(response.url),
            "host": host,
            "resource_type": response.request.resource_type,
            "headers": _redacted_headers(response.headers),
        }
        if _is_loopback_host(host) and "json" in str(response.headers.get("content-type", "")).lower():
            try:
                observed = _extract_ip_payload(host, await response.text())
            except Exception:
                observed = None
            if observed:
                private_observed = _private_ip_observation(observed, self._ip_salt)
                self.ip_observations.append({"t": self._ts(), "host": host, **private_observed})
                entry["ip_observation"] = private_observed
        self.events.append(entry)

    async def request_failed(self, request: Request) -> None:
        failure = str(request.failure or "unknown")
        self.events.append({
            "kind": "request_failed",
            "t": self._ts(),
            "method": request.method,
            "url": _redacted_url(request.url),
            "host": _host(request.url),
            "failure": "present" if failure else "unknown",
            "failure_meta": _message_meta(failure),
        })

    async def websocket(self, websocket: Any) -> None:
        self.events.append({
            "kind": "websocket",
            "t": self._ts(),
            "url": _redacted_url(websocket.url),
            "host": _host(websocket.url),
        })

    async def console(self, message: Any) -> None:
        # Console output is page-controlled and can contain submitted PII or
        # tokens; keep only metadata rather than attempting brittle redaction.
        self.events.append({
            "kind": "console",
            "t": self._ts(),
            "type": str(message.type),
            "message": _message_meta(message.text),
            "challenge_signal": bool(CHALLENGE_RE.search(str(message.text or ""))),
        })

    async def page_error(self, error: Any) -> None:
        self.events.append({
            "kind": "page_error",
            "t": self._ts(),
            "message": _message_meta(str(error)),
            "challenge_signal": bool(CHALLENGE_RE.search(str(error or ""))),
        })

    async def attach_page(self, page: Page) -> None:
        marker = id(page)
        if marker in self._attached_pages:
            return
        self._attached_pages.add(marker)
        page.on("console", self.console)
        page.on("pageerror", self.page_error)

    async def detach_page(self, page: Page) -> None:
        marker = id(page)
        if marker not in self._attached_pages:
            return
        self._attached_pages.discard(marker)
        for event, handler in (("console", self.console), ("pageerror", self.page_error)):
            try:
                page.remove_listener(event, handler)
            except Exception:
                pass

    async def snapshot_page(self, page: Page, label: str) -> dict[str, Any]:
        await self.attach_page(page)
        try:
            state = await page.evaluate(STATE_JS)
        except Exception as exc:
            state = {"error": type(exc).__name__}
        if isinstance(state, dict) and state.get("href"):
            state["href"] = _redacted_url(str(state["href"]))
        title = ""
        body_markers: list[str] = []
        try:
            title = await page.title()
            body = (await page.locator("body").inner_text(timeout=5000))[:6000]
            for marker in (
                "captcha", "recaptcha", "turnstile", "verify", "phone",
                "device", "suspicious", "unusual", "too many", "rate limit",
            ):
                if marker in body.lower() or marker in title.lower():
                    body_markers.append(marker)
        except Exception:
            pass
        snap = {
            "label": label,
            "t": self._ts(),
            "url": _redacted_url(page.url),
            "title": _safe_text(title, 200),
            "body_markers": sorted(set(body_markers)),
            "state": state,
        }
        self.pages.append(snap)
        return snap


def _risk_items(collector: TraceCollector) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    blocked_external = [e for e in collector.events if e.get("kind") == "blocked_external"]
    if blocked_external:
        hosts = sorted({str(e.get("host") or "") for e in blocked_external if e.get("host")})
        risks.append({
            "id": "external_navigation_blocked",
            "severity": "medium",
            "evidence": f"已阻止 {len(blocked_external)} 个跨站请求"
            + (f"（主机: {', '.join(hosts[:5])}）" if hosts else ""),
            "advice": "为自有测试应用提供本地模拟依赖；不要把真实账号、验证码或令牌交给外部站点",
        })
    challenge_events = [
        e for e in collector.events
        if CHALLENGE_RE.search(str(e.get("url", "")))
        or bool(e.get("challenge_signal"))
    ]
    challenge_pages = [
        p for p in collector.pages
        if p.get("body_markers")
        or CHALLENGE_RE.search(str(p.get("url", "")))
        or CHALLENGE_RE.search(str(p.get("title", "")))
    ]
    if challenge_events or challenge_pages:
        risks.append({
            "id": "challenge_surface",
            "severity": "high",
            "evidence": f"检测到 {len(challenge_events)} 个网络/控制台挑战信号，{len(challenge_pages)} 个页面挑战信号",
            "advice": "在自有测试环境核对验证流程与服务端日志；不要自动处理挑战或重复提交",
        })

    denied = [e for e in collector.events
              if e.get("kind") == "response" and int(e.get("status") or 0) in {401, 403, 409, 429}]
    if denied:
        statuses = Counter(str(e.get("status")) for e in denied)
        risks.append({
            "id": "denied_or_rate_limited",
            "severity": "high" if statuses.get("429") else "medium",
            "evidence": f"HTTP 状态 {dict(statuses)}",
            "advice": "核对测试账号、服务端策略和响应日志；不要用重试掩盖服务端拒绝",
        })

    failed = [e for e in collector.events if e.get("kind") == "request_failed"]
    if failed:
        risks.append({
            "id": "network_failures",
            "severity": "medium",
            "evidence": f"{len(failed)} 个请求失败",
            "advice": "按 host 与 failure 对照代理连通性、DNS、TLS 和验证域可达性",
        })

    ips = [str(row.get("ip_fingerprint")) for row in collector.ip_observations
           if row.get("ip_fingerprint")]
    unique_ips = list(dict.fromkeys(ips))
    if len(unique_ips) > 1:
        risks.append({
            "id": "exit_ip_changed",
            "severity": "high",
            "evidence": "同一测试链路观测到多个出口指纹：" + " → ".join(unique_ips[:6]),
            "advice": "检查自有测试网络配置与会话日志，确认出口变化是否符合预期",
        })

    hosts = {_host(str(e.get("url", ""))) for e in collector.events if e.get("url")}
    insecure = [p for p in collector.pages
                if str((p.get("state") or {}).get("secure")).lower() == "false"]
    if insecure:
        risks.append({
            "id": "insecure_context",
            "severity": "medium",
            "evidence": f"{len(insecure)} 个页面不是安全上下文",
            "advice": "登录/注册页面使用 HTTPS，并检查被代理或重定向到 HTTP 的资源",
        })

    for page in collector.pages:
        state = page.get("state") or {}
        if state.get("webdriver") not in (False, None, 0):
            risks.append({
                "id": "webdriver_signal",
                "severity": "high",
                "evidence": f"{page.get('label')}: navigator.webdriver={state.get('webdriver')}",
                "advice": "将该值作为自动化测试环境信号记录；验证业务策略不依赖绕过检测",
            })
        langs = state.get("languages") or []
        if state.get("language") and langs and langs[0] != state.get("language"):
            risks.append({
                "id": "locale_mismatch",
                "severity": "medium",
                "evidence": f"{page.get('label')}: language={state.get('language')} languages[0]={langs[0]}",
                "advice": "记录各语言来源的差异，作为自有应用兼容性诊断信息",
            })
        if int(state.get("touch") or 0) <= 0 and state.get("pointerCoarse"):
            risks.append({
                "id": "touch_surface_mismatch",
                "severity": "medium",
                "evidence": f"{page.get('label')}: coarse pointer 但 maxTouchPoints=0",
                "advice": "记录触控媒体状态差异，作为自有应用兼容性诊断信息",
            })

    # 不因「请求过多」本身判风险；只提供网络面统计，便于复盘。
    if len(hosts) == 0:
        risks.append({
            "id": "no_network_events",
            "severity": "low",
            "evidence": "没有捕获到页面网络事件",
            "advice": "确认页面已加载、CDP 连接指向正确环境，或延长等待时间",
        })
    return risks


def _markdown(report: dict[str, Any]) -> str:
    analysis = report.get("analysis") or {}
    lines = [
        "# 注册链路风控追踪报告",
        "",
        f"- 生成时间：{report.get('created_at')}",
        f"- 环境：{report.get('profile_id') or '(临时环境)'}",
        f"- 步骤：{len(report.get('steps') or [])}",
        f"- 网络事件：{len(report.get('events') or [])}",
        "",
        "## 风控项",
        "",
    ]
    risks = analysis.get("risks") or []
    if not risks:
        lines.append("未发现脚本可观测的高/中风险信号；这不代表服务端策略一定放行。")
    else:
        lines.extend([
            "| 等级 | 项目 | 证据 | 建议 |",
            "|---|---|---|---|",
        ])
        for item in risks:
            lines.append("| {severity} | {id} | {evidence} | {advice} |".format(**item))
    lines.extend(["", "## 出口观测", ""])
    for row in report.get("ip_observations") or []:
        lines.append(
            f"- {row.get('host')}: fingerprint={row.get('ip_fingerprint') or '(none)'} "
            f"{row.get('country') or ''} {row.get('timezone') or ''}"
        )
    if not report.get("ip_observations"):
        lines.append("未观测到 IP 信息服务响应。")
    lines.extend(["", "## 页面运行时状态", "", "```json",
                  json.dumps(report.get("pages") or [], ensure_ascii=False, indent=2),
                  "```", ""])
    return "\n".join(lines)


async def _api(client: httpx.AsyncClient, method: str, path: str,
               **kwargs: Any) -> dict[str, Any]:
    response = await client.request(method, path, **kwargs)
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"管理端响应不是 JSON（HTTP {response.status_code}）") from exc
    if response.status_code >= 400 or payload.get("code") != 0:
        raise RuntimeError(payload.get("msg") or f"管理端请求失败（HTTP {response.status_code}）")
    return payload.get("data")


def _load_steps(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    steps = payload.get("steps") if isinstance(payload, dict) else payload
    if not isinstance(steps, list):
        raise ValueError("步骤文件必须是数组，或包含 steps 数组的对象")
    if len(steps) > 100:
        raise ValueError("步骤数超过 100")
    return [dict(step) for step in steps if isinstance(step, dict)]


def _resolve_value(value: Any) -> str:
    text = str(value or "")
    if text.startswith("env:"):
        return os.environ.get(text[4:], "")
    return text


def _validate_steps(steps: list[dict[str, Any]]) -> None:
    """Validate all navigations before launching an environment."""
    for index, step in enumerate(steps, 1):
        if str(step.get("action") or "").lower() != "goto":
            continue
        url = _resolve_value(step.get("url"))
        try:
            _validate_test_url(url)
        except ValueError as exc:
            raise ValueError(f"步骤 {index} 的 goto 不允许: {exc}") from exc


async def _run_step(page: Page, step: dict[str, Any], allow_submit: bool) -> dict[str, Any]:
    action = str(step.get("action") or "").lower()
    selector = str(step.get("selector") or "")
    timeout = int(step.get("timeout") or 30_000)
    result: dict[str, Any] = {"action": action, "selector": selector or None}
    if action == "goto":
        url = _resolve_value(step.get("url"))
        _validate_test_url(url)
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        result["url"] = _redacted_url(url)
    elif action in {"fill", "type"}:
        value = _resolve_value(step.get("value"))
        await page.locator(selector).fill(value, timeout=timeout)
        result["value_present"] = bool(value)
        result["value_length"] = len(value)
    elif action == "click":
        if bool(step.get("submit")) and not allow_submit:
            result["skipped"] = "submit step requires --allow-submit"
            return result
        await page.locator(selector).click(timeout=timeout)
    elif action == "press":
        await page.locator(selector).press(str(step.get("key") or "Enter"), timeout=timeout)
    elif action == "wait":
        await page.wait_for_timeout(max(0, int(step.get("ms") or 1000)))
    elif action == "wait_for":
        await page.locator(selector).wait_for(state=str(step.get("state") or "visible"), timeout=timeout)
    elif action == "select":
        await page.locator(selector).select_option(str(step.get("value") or ""), timeout=timeout)
    else:
        raise ValueError(f"不支持的步骤 action: {action}")
    result["ok"] = True
    return result


async def _trace_connected_browser(browser: Any, collector: TraceCollector,
                                   steps: list[dict[str, Any]],
                                   args: argparse.Namespace) -> list[dict[str, Any]]:
    context: BrowserContext = browser.contexts[0]

    async def guard_external(route: Any) -> None:
        request_url = route.request.url
        if _is_loopback_host(_host(request_url)):
            await route.continue_()
        else:
            collector.events.append({
                "kind": "blocked_external",
                "t": collector._ts(),
                "url": _redacted_url(request_url),
                "host": _host(request_url),
            })
            await route.abort("blockedbyclient")

    await context.route("**/*", guard_external)
    try:
        context.on("request", collector.request)
        context.on("response", collector.response)
        context.on("requestfailed", collector.request_failed)
        context.on("websocket", collector.websocket)
        for existing_page in context.pages:
            if existing_page.url and existing_page.url != "about:blank":
                _validate_test_url(existing_page.url)
            await collector.attach_page(existing_page)
        page = context.pages[-1] if context.pages else await context.new_page()
        await collector.attach_page(page)
        if args.start_url and not page.url.startswith(args.start_url):
            await page.goto(args.start_url, wait_until="domcontentloaded", timeout=45_000)
        await collector.snapshot_page(page, "before")
        step_results: list[dict[str, Any]] = []
        for index, step in enumerate(steps, 1):
            try:
                item = await _run_step(page, step, bool(args.allow_submit))
                item["index"] = index
                step_results.append(item)
            except Exception as exc:
                step_results.append({
                    "index": index,
                    "action": step.get("action"),
                    "selector": step.get("selector"),
                    "ok": False,
                    "error": type(exc).__name__,
                })
                if args.stop_on_error:
                    break
            await collector.snapshot_page(page, f"step-{index}")
        await page.wait_for_timeout(max(0, int(args.settle * 1000)))
        await collector.snapshot_page(page, "after")
        return step_results
    finally:
        # CDP connects to an existing context; always remove the network guard
        # so a diagnostic run cannot affect the user's later browsing session.
        for event, handler in (
            ("request", collector.request),
            ("response", collector.response),
            ("requestfailed", collector.request_failed),
            ("websocket", collector.websocket),
        ):
            try:
                context.remove_listener(event, handler)
            except Exception:
                pass
        for page in context.pages:
            await collector.detach_page(page)
        try:
            await context.unroute("**/*", guard_external)
        except Exception:
            pass


async def run_trace(args: argparse.Namespace) -> dict[str, Any]:
    steps = _load_steps(args.flow_file)
    _validate_test_url(args.start_url, allow_blank=True)
    _validate_steps(steps)
    profile_id = args.profile_id or os.environ.get("FPWB_PROFILE_ID")
    temporary = False
    started_by_trace = False
    endpoint: dict[str, Any] | None = None
    collector = TraceCollector()
    async with httpx.AsyncClient(base_url=args.management_url.rstrip("/"), timeout=120,
                                 trust_env=False) as client:
        if not profile_id:
            profile = await _api(client, "POST", "/api/v1/profiles", json={
                "name": "registration-trace-temp",
                "kernel": "fp-chromium",
                "target_os": "android",
                "fingerprint_mode": "preset",
                "manufacturer": args.manufacturer,
                "device_model": args.device_model,
                "launch": {
                    "headless": bool(args.headless),
                    # Local test harness: avoid contacting external IP/geo
                    # services while preparing the temporary environment.
                    "geoip": False,
                    "humanize": False,
                    "start_url": args.start_url,
                },
            })
            profile_id = profile["id"]
            temporary = True
        try:
            profiles = await _api(client, "GET", "/api/v1/profiles")
            existing = next((p for p in profiles if p.get("id") == profile_id), None)
            if not existing:
                raise RuntimeError(f"环境不存在: {profile_id}")
            configured_start = (existing.get("launch") or {}).get("start_url") or "about:blank"
            _validate_test_url(configured_start, allow_blank=True)
            if existing.get("running"):
                endpoint = await _api(client, "GET", f"/api/v1/browser/{profile_id}/endpoint")
            else:
                payload: dict[str, Any] = {"profile_id": profile_id, "headless": bool(args.headless)}
                if args.start_url:
                    payload["start_url"] = args.start_url
                endpoint = await _api(client, "POST", "/api/v1/browser/start", json=payload)
                started_by_trace = True

            async with async_playwright() as playwright:
                browser = await playwright.chromium.connect_over_cdp(endpoint["ws_endpoint"])
                try:
                    step_results = await _trace_connected_browser(browser, collector, steps, args)
                finally:
                    await browser.close()

            risks = _risk_items(collector)
            return {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "profile_id": profile_id,
                "temporary_profile": temporary,
                "start_url": _redacted_url(args.start_url),
                "allow_submit": bool(args.allow_submit),
                "steps": step_results,
                "pages": collector.pages,
                "events": collector.events,
                "ip_observations": collector.ip_observations,
                "analysis": {
                    "risks": risks,
                    "event_counts": dict(Counter(e.get("kind") for e in collector.events)),
                    "host_counts": dict(Counter(_host(str(e.get("url", "")))
                                             for e in collector.events if e.get("url"))),
                },
            }
        finally:
            if started_by_trace:
                try:
                    await _api(client, "POST", "/api/v1/browser/stop",
                               json={"profile_id": profile_id})
                except Exception:
                    pass
            if temporary and profile_id:
                try:
                    await _api(client, "DELETE", f"/api/v1/profiles/{profile_id}")
                except Exception:
                    pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--management-url", default=os.environ.get("FPWB_MANAGEMENT_URL", DEFAULT_MANAGEMENT_URL))
    parser.add_argument("--profile-id", default=None, help="已有环境 ID；省略则创建临时 Android 环境")
    parser.add_argument("--flow-file", default=None, help="JSON 步骤文件")
    parser.add_argument("--start-url", default=DEFAULT_START_URL,
                        help="仅支持 localhost/回环地址上的自有测试应用")
    parser.add_argument("--manufacturer", default="Xiaomi")
    parser.add_argument("--device-model", default="Xiaomi 15 Ultra")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--allow-submit", action="store_true", help="执行步骤中 submit=true 的最终提交动作")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--settle", type=float, default=2.0, help="最终页面等待秒数")
    parser.add_argument("--output", default=None, help="输出 JSON 路径；默认写入 /tmp")
    args = parser.parse_args()
    try:
        report = asyncio.run(run_trace(args))
    except Exception as exc:
        error_message = _safe_text(str(exc), 300)
        output = Path(args.output) if args.output else None
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            failure_report = {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "ok": False,
                "start_url": _redacted_url(args.start_url),
                "error": {"type": type(exc).__name__, "message": error_message},
                "analysis": {"risks": [{
                    "id": "trace_start_failed",
                    "severity": "medium",
                    "evidence": f"追踪器未能启动本地测试链路（{type(exc).__name__}）",
                    "advice": "检查本地管理端、浏览器桌面会话和测试应用状态",
                }]},
            }
            output.write_text(json.dumps(failure_report, ensure_ascii=False, indent=2), encoding="utf-8")
            output.with_suffix(".md").write_text(
                "# 注册链路追踪报告\n\n"
                f"- 状态：失败\n- 错误类型：{type(exc).__name__}\n"
                f"- 错误摘要：{error_message}\n",
                encoding="utf-8",
            )
            print(json.dumps({"ok": False, "json": str(output),
                              "markdown": str(output.with_suffix('.md'))}, ensure_ascii=False))
        else:
            print(f"trace failed: {type(exc).__name__}: {error_message}")
        return 2

    output = Path(args.output or f"/tmp/fpwb-registration-trace-{int(time.time())}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = output.with_suffix(".md")
    markdown.write_text(_markdown(report), encoding="utf-8")
    risks = report.get("analysis", {}).get("risks", [])
    print(json.dumps({
        "json": str(output),
        "markdown": str(markdown),
        "steps": len(report.get("steps", [])),
        "events": len(report.get("events", [])),
        "risk_items": len(risks),
        "risk_ids": [item.get("id") for item in risks],
    }, ensure_ascii=False, indent=2))
    return 0 if not any(item.get("severity") == "high" for item in risks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
