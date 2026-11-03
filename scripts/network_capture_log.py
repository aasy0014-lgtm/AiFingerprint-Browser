#!/usr/bin/env python3
"""把浏览器环境的网络抓包落盘到日志，并给出链路分析。

与 ``registration_trace.py`` 的区别：那个脚本会**驱动**页面执行步骤，因此被
硬性限制在 localhost 回环测试应用上，禁止自动化第三方注册。本脚本**不驱动
任何页面**：它连上已在运行的环境（或短暂启动一个环境），只挂监听器，等用户
自己浏览，然后把脱敏后的网络元数据写进日志目录，再产出分析。

落盘内容仍按 ``registration_trace.py`` 的口径脱敏：URL 只保留主机/路径和参数
名，查询值、Cookie、Authorization、表单值、响应正文一律不落盘；请求/响应体
只记录长度、sha256 和字段名。唯一额外记录的是响应正文里的**挑战类信号关键词**
（captcha/verify/device 等），用于分析风控面。

用法：

    # 旁路观测：环境已在界面里打开，用户自己操作 90 秒
    .venv/bin/python scripts/network_capture_log.py \
      --profile-id PROFILE_ID --duration 90

    # 只分析已有日志，不重新抓包
    .venv/bin/python scripts/network_capture_log.py --analyze-only PATH.jsonl

    # 启动一个新环境并打开页面，抓包后自动停止
    .venv/bin/python scripts/network_capture_log.py \
      --new-profile --start-url https://accounts.google.com/ --duration 60

日志目录：``$FPWB_HOME/data/logs/network-capture/``（默认 ``data/logs/network-capture/``），
文件名 ``<时间戳>-<profile前缀>.jsonl``，分析报告同名 ``.md``。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from playwright.async_api import Browser, BrowserContext, Page, Request, Response, async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import DATA_DIR  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from registration_trace import (  # noqa: E402
    CHALLENGE_RE,
    SENSITIVE_HEADER_RE,
    SAFE_HEADER_NAMES,
    _host,
    _message_meta,
    _redacted_url as _redacted_url_strict,
    _safe_text,
)

# 少数查询参数的值是 RPC/接口名而非凭据，脱敏会抹掉唯一能定位挑战下发的线索。
# 只放行白名单内的参数名，其余一律沿用 registration_trace 的全量打码。
URL_VALUE_ALLOWLIST = {"rpcids"}


def _redacted_url(value: str) -> str:
    """保留 URL 结构；仅 rpcids 等白名单参数保留原值，其余查询值打码。"""
    try:
        parsed = urlsplit(str(value or ""))
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if not any(key in URL_VALUE_ALLOWLIST for key, _ in pairs):
            return _redacted_url_strict(value)
        out = [(key, raw if key in URL_VALUE_ALLOWLIST else "<redacted>")
               for key, raw in pairs]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
                           urlencode(out), ""))
    except ValueError:
        return _redacted_url_strict(value)

DEFAULT_MANAGEMENT_URL = "http://127.0.0.1:18080"
LOG_ROOT = DATA_DIR / "logs" / "network-capture"

# 响应正文只做关键词探测，绝不落盘正文本身。
BODY_SIGNAL_RE = re.compile(
    r"captcha|recaptcha|turnstile|challenge|devicephoneverification|"
    r"crossflowverification|samedevice|unusual|suspicious|too many|rate limit|"
    r"verify (?:your )?(?:phone|device)|session expired",
    re.I,
)
# 只在这些正文类型里做关键词探测，避免把大文件读进内存。
TEXTUAL_CONTENT_RE = re.compile(r"(?:text/|json|javascript|xml)", re.I)
MAX_PROBE_BYTES = 512 * 1024

# 设备指纹信标：Google 登录/注册流程在进入表单前必发，内容用于风控建模。
# 只按 URL 路径识别，不读请求体。
DEVICE_BEACON_RE = re.compile(
    r"browserinfo|browsersignin|collector|/j/collect|fingerprint|clienthints|"
    r"challenge/.*/payload", re.I)
# Chromium 主动取消的请求（页面卸载/信标被页面自身 abort）不是网络故障。
BENIGN_FAILURES = {
    "net::ERR_ABORTED", "net::ERR_BLOCKED_BY_CLIENT",
    "net::ERR_BLOCKED_BY_RESPONSE", "net::ERR_CACHE_OPERATION_NOT_SUPPORTED",
}

# browserinfo 的 f.req 是 jspb 数组编码（下标 0 == protobuf 字段 1），字段号取自
# Google 账号页 boq-account-creation-evolution 模块的 _.UIa。仅用于解释结构。
#
# 注意：jspb 数组会把「跳过的字段号」保留成 null。Jv 只写字段 2/3（高/宽），
# Kv 只写字段 1/2/4，所以数组里必然出现 null——这是 schema 空洞，不是采集失败。
BEACON_SLOT_MEANINGS = {
    "fields.f.req[0]": "字段1：协议版本（常量 9）",
    "fields.f.req[1]": "字段2：实验/配置位（Ib(\"LVIXXb\")）",
    "fields.f.req[2]": "字段3：devicePixelRatio",
    "fields.f.req[3]": "字段4：屏幕尺寸（Jv，见下方子项）",
    "fields.f.req[4]": "字段5：视口尺寸（Jv，见下方子项）",
    "fields.f.req[5]": "字段6：平台能力（Kv）",
    "fields.f.req[6]": "字段7：上报器状态（Lv）",
    "fields.f.req[3][0]": "Jv 字段1：未使用（schema 空洞，恒 null）",
    "fields.f.req[3][1]": "Jv 字段2：屏幕高（height*DPR）",
    "fields.f.req[3][2]": "Jv 字段3：屏幕宽（width*DPR）",
    "fields.f.req[4][0]": "Jv 字段1：未使用（schema 空洞，恒 null）",
    "fields.f.req[4][1]": "Jv 字段2：视口高（innerHeight）",
    "fields.f.req[4][2]": "Jv 字段3：视口宽（innerWidth）",
    "fields.f.req[5][0]": "Kv 字段1：平台能力位（Chrome/Android 版本门控）",
    "fields.f.req[5][1]": "Kv 字段2：平台能力位（Chrome/Android 版本门控）",
    "fields.f.req[5][2]": "Kv 字段3：未使用（schema 空洞，恒 null）",
    "fields.f.req[5][3]": "Kv 字段4：window.PushManager",
    "fields.f.req[6][1]": "Lv.1：lJa()——上报器就绪",
    "fields.f.req[6][2]": "Lv.2：e_()——上报间隔档位",
    "fields.f.req[6][3]": "Lv.3：f_()——带宽估计档位",
}

# 挑战面分三档，不能把“加载了 recaptcha 库”当成“触发了挑战”：
#   rendered  —— 可见挑战框真的渲染出来（bframe/payload、/challenge/ 页面）
#   signaled  —— 服务端/页面明确提示需要验证（正文关键词、挑战页导航）
#   library   —— 只是加载了验证库或隐形 anchor，属于正常页面组成
CHALLENGE_RENDERED_RE = re.compile(
    r"/api2/bframe|/api2/payload|recaptcha/api2/anchor[^?]*\?(?=[^ ]*size=normal)"
    r"|/recaptcha/enterprise/bframe|/recaptcha/enterprise/payload"
    r"|challenge/.*/payload|/sorry/index", re.I)
CHALLENGE_LIBRARY_RE = re.compile(
    r"/recaptcha/(?:enterprise/)?(?:anchor|reload|clr|webworker|enterprise\.js)"
    r"|/recaptcha/releases/|/recaptcha/api2/logo|recaptcha/api\.js|/recaptcha/api2/.*\.(?:js|css)"
    r"|hcaptcha\.com|challenges\.cloudflare\.com/turnstile", re.I)
CHALLENGE_PAGE_RE = re.compile(
    r"devicephoneverification|crossflowverification|samedevice|/challenge/"
    r"|speedbump|/v3/signin/challenge", re.I)
# 这些词在 recaptcha 自己的 anchor 页面上恒出现，属于组件自带文案而非挑战提示。
RECAPTCHA_LIBRARY_WORDS = {"recaptcha", "captcha"}

# Google 注册流程的挑战/设备类步骤，来自账号页模块的步骤路由表（_.zB 注册）。
# 这些是「服务端决定跳到这里」的页面，不是客户端能主动触发的。
GOOGLE_CHALLENGE_STEPS = {
    "/steps/signup/crossflowverification/initial": "跨流程验证入口",
    "/steps/signup/crossflowverification/samedevice": "要求同设备验证",
    "/steps/signup/crossflowverification/error": "跨流程验证失败（含 failureReason）",
    "/steps/signup/unlockstorage/deviceattestation": "设备证明（设备检查核心）",
    "/steps/signup/unlockstorage/verifyphone": "手机号验证",
    "/steps/signup/unlockstorage/addphone": "添加手机号",
    "/steps/signup/unlockstorage/enterotp": "输入短信验证码",
    "/steps/signup/startmtsmsidv": "短信身份验证",
    "/steps/signup/ageverification": "年龄验证",
    "/steps/signup/verifyemail": "邮箱验证",
    "/steps/signup/addrecoveryphone": "添加找回手机",
    "/steps/signup/webstartrecoveryphoneverification": "找回手机验证",
    "/steps/signup/silentassertion": "静默断言（无感风控校验）",
    "/steps/signup/unknownerror": "未知错误步骤",
    "/steps/signup/error": "错误步骤",
}

# 挑战步骤的路径模式。`devicephoneverification` 各子页（begin/initiate/consent）
# 是独立文档而不是 lifecycle 步骤，必须靠模式匹配，精确路径表覆盖不到。
CHALLENGE_STEP_RE = re.compile(
    r"/devicephoneverification/|/speedbump|/v3/signin/challenge"
    r"|/steps/signup/crossflowverification/|/steps/signup/unlockstorage/"
    r"|/steps/signup/startmtsmsidv", re.I)

# 已知 batchexecute RPC 的用途。名字取自请求查询参数 rpcids，含义取自
# boq-account-creation-evolution 模块里的 _.Vx 注册表（bdb={ub:"MjyMj",...}）。
GOOGLE_RPC_MEANINGS = {
    "MjyMj": "设备完整性上报（$cb 汇总设备标识/品牌/型号/Play 服务版本后提交；"
             "响应只被当作成功/失败信号，客户端不解析 payload）",
}

# batchexecute 信封的流级标记。每个响应都带 di/e/af.httprm；只有 POST 带 f.mt。
# 这些标记说明流是否正常收尾，本身不是挑战信号——挑战由 "er" 信封和页面步骤决定。
GOOGLE_STREAM_MARKERS = {
    "di": "批次序号",
    "e": "流结束标记（末位是状态位）",
    "af.httprm": "请求耗时统计（客户端上报器读第 2 槽算带宽档位）",
    "f.mt": "POST 响应标记（仅带 data= 的请求出现）",
}

# 第三方主机归类：只做域名后缀归组，便于看出链路依赖。
THIRD_PARTY_BUCKETS = {
    "google": ("google.com", "googleapis.com", "gstatic.com", "googleusercontent.com",
               "google-analytics.com", "googletagmanager.com", "doubleclick.net",
               "youtube.com", "withgoogle.com"),
    "apple": ("apple.com", "icloud.com"),
    "microsoft": ("microsoft.com", "live.com", "msn.com", "bing.com"),
    "cdn": ("cloudflare.com", "cloudfront.net", "akamai.net", "fastly.net", "jsdelivr.net"),
}


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _bucket(host: str) -> str:
    host = (host or "").lower()
    for name, suffixes in THIRD_PARTY_BUCKETS.items():
        if any(host == s or host.endswith("." + s) for s in suffixes):
            return name
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "unknown")


def _headers(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_name, raw_value in dict(headers or {}).items():
        name = str(raw_name).lower()
        if SENSITIVE_HEADER_RE.search(name):
            out[name] = "<redacted>"
        elif name in SAFE_HEADER_NAMES:
            value = str(raw_value)
            out[name] = _redacted_url(value) if name in {"location", "referer"} else value[:300]
    return out


def _request_body_meta(request: Request, content_type: str) -> dict[str, Any] | None:
    # post_data 对二进制体会抛 UnicodeDecodeError（Playwright 无条件按 UTF-8
    # 解码），只记录长度和类型，不吞掉整个抓包流程。
    try:
        data = request.post_data
        meta = _body_meta(data, content_type)
        if meta and data and (DEVICE_BEACON_RE.search(request.url)
                              or "batchexecute" in request.url):
            meta["shape"] = _body_shape(data)
        return meta
    except UnicodeDecodeError:
        return {"binary": True, "content_type": content_type[:80]}
    except Exception:
        return None


def _body_shape(data: str) -> dict[str, Any]:
    """只展开字段结构，不记录任何字段值。

    Google 的 batchexecute/browserinfo 用 ``f.req`` 承载载荷，且该字段常是
    二次编码的 JSON 字符串；逐层解开才能看出信标采集了哪些设备项。

    空值必须区分：``null`` 是“该槽位未采集”，``""`` 是“采集了但为空”，
    两者对判断设备信号缺失的结论完全不同，合并会得出错误诊断。
    """
    def walk(value: Any, depth: int) -> Any:
        # batchexecute 的载荷是「form 字段 → 二次编码 JSON → 外层信封 → RPC 参数」
        # 好几层嵌套，阈值太低会让真正的参数树提前变成 <deep>。
        if depth > 12:
            return "<deep>"
        if isinstance(value, dict):
            return {str(k): walk(v, depth + 1) for k, v in list(value.items())[:60]}
        if isinstance(value, list):
            return [walk(v, depth + 1) for v in value[:20]]
        if value is None:
            return "<null>"
        if isinstance(value, bool):
            return "<bool>"
        if isinstance(value, (int, float)):
            return "<number>"
        if isinstance(value, str):
            text = value.strip()
            # 内层 JSON：继续展开，而不是当成一个普通值。
            if len(text) <= 200_000 and text[:1] in "[{" and text[-1:] in "]}":
                try:
                    return {"$json": walk(json.loads(text), depth + 1)}
                except ValueError:
                    pass
            # 长度是诊断线索（哪一槽扛着载荷），本身不是值，可以保留。
            return f"<empty-str>" if not text else f"<str:{len(text)}>"
        return "<empty>" if value in ([], {}) else "<value>"

    shape: dict[str, Any] = {"encoding": "json"}
    try:
        shape["tree"] = walk(json.loads(data), 0)
        return shape
    except ValueError:
        pass
    if "=" in data and "&" in data:
        pairs = parse_qsl(data, keep_blank_values=True)
        shape["encoding"] = "form"
        shape["fields"] = {str(k): walk(v, 0) for k, v in pairs[:60]}
        return shape
    shape["encoding"] = "other"
    return shape


def _shape_slots(shape: Any, path: str = "") -> list[dict[str, Any]]:
    """把 shape 树压平成空槽位清单，并标注已知信标字段的含义。

    字段号来自 Google 账号页 `boq-account-creation-evolution` 模块的
    `_.UIa`（构造 `Iv` 消息）：f.req 是 jspb 数组编码，下标 0 == 字段 1。
    只用于解释结构，不影响脱敏口径。
    """
    slots: list[dict[str, Any]] = []

    def visit(node: Any, here: str) -> None:
        if isinstance(node, dict):
            if "$json" in node:
                visit(node["$json"], here)
                return
            for key, child in node.items():
                visit(child, f"{here}.{key}" if here else str(key))
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{here}[{index}]")
        elif node in ("<null>", "<empty-str>", "<empty>"):
            slots.append({"path": here, "kind": node,
                          "meaning": _slot_meaning(here)})

    visit(shape, path)
    return slots


def _slot_meaning(path: str) -> str:
    meaning = BEACON_SLOT_MEANINGS.get(path)
    if meaning:
        return meaning
    # 子消息内部的字段下标（[1]、[2] 等）也要解释。
    tail = path.rsplit(".", 1)[-1]
    return BEACON_SLOT_MEANINGS.get(tail, "")


def _is_page_content(resource_type: str, content_type: str) -> bool:
    """只探测页面级响应正文：脚本/图片的 recaptcha 字样是库代码，不是挑战。"""
    return resource_type in {"document", "xhr", "fetch"} and bool(
        TEXTUAL_CONTENT_RE.search(content_type))


def _body_meta(data: str | None, content_type: str = "") -> dict[str, Any] | None:
    if data is None:
        return None
    raw = data.encode("utf-8", errors="replace")
    meta: dict[str, Any] = {"length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    keys: list[str] = []
    try:
        if "json" in content_type.lower():
            payload = json.loads(data)
            if isinstance(payload, dict):
                keys = [str(k) for k in payload.keys()]
            elif isinstance(payload, list) and payload and isinstance(payload[0], dict):
                keys = [str(k) for k in payload[0].keys()]
        elif "form-urlencoded" in content_type.lower():
            keys = [str(k) for k, _ in parse_qsl(data, keep_blank_values=True)]
    except (TypeError, ValueError, UnicodeError):
        pass
    if keys:
        meta["keys"] = sorted(set(keys))[:100]
    return meta


def _batchexecute_shape(data: str) -> dict[str, Any] | None:
    """解析 batchexecute 响应，只保留 RPC 名与结构标记，不保留任何值。

    Google 的响应是 ``)]}'`` 前缀 + 多段换行分隔的 JSON 数组，每段形如
    ``[[["wrb.fr", "<rpcid>", "<json-payload>", null, null, [..]]], ...]``。
    RPC 名和状态位是诊断挑战下发的关键，payload 本身一律只展开结构。

    挑战标记有两处，都不在 payload 里：
    * ``["er", "<rpcid>", <code>, ...]`` —— RPC 级失败信封，code 决定客户端走
      哪条错误分支（例如注册流程据此跳到 crossflowverification/error）。
    * 流级标记信封（如 ``["e", 4, ...]``、``["di", N]``、``["af.httprm", ...]``）
      —— 每个 batchexecute 响应都带，用于判断流是否正常收尾。
    """
    text = data.lstrip()
    if text.startswith(")]}'"):
        text = text[4:]
    rpcs: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    markers: dict[tuple[str, int], dict[str, Any]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("["):
            continue
        try:
            segment = json.loads(line)
        except ValueError:
            continue
        for envelope in _walk_envelopes(segment):
            rpcs.append(envelope)
        for item in _walk_markers(segment):
            tag = item[0][:40]
            if tag == "er":
                errors.append({
                    "rpcid": str(item[1])[:80] if len(item) > 1 else "",
                    "arity": len(item),
                    # 错误码本身是状态枚举而非用户数据，保留原值才能区分失败类型
                    "code": item[2] if len(item) > 2 and isinstance(item[2], (int, str)) else None,
                    "shape": _walk_shape(item[2:], 0),
                })
                continue
            markers.setdefault((tag, len(item)), {
                "tag": tag,
                "arity": len(item),
                "shape": _walk_shape(item, 0),
                # 流级标记里的数字是协议状态（批次数、状态位），不是用户数据
                "numbers": [v for v in item if isinstance(v, (int, float))
                            and not isinstance(v, bool)][:6],
            })
    if not rpcs and not errors and not markers:
        return None
    shape: dict[str, Any] = {"encoding": "batchexecute", "rpcs": rpcs[:40]}
    if errors:
        shape["errors"] = errors[:20]
    if markers:
        shape["markers"] = list(markers.values())[:10]
    return shape


def _walk_envelopes(node: Any, depth: int = 0) -> list[dict[str, Any]]:
    """找出所有 ``["wrb.fr", rpcid, payload, ...]`` 信封并只保留结构。"""
    found: list[dict[str, Any]] = []
    if depth > 12:
        return found
    if isinstance(node, list):
        if (len(node) >= 3 and isinstance(node[0], str) and node[0] == "wrb.fr"
                and isinstance(node[1], str)):
            envelope: dict[str, Any] = {
                "rpcid": node[1][:80],
                "payload_present": node[2] not in (None, "", [], {}),
                "trailing": [("<null>" if v is None else "<value>") for v in node[3:7]],
            }
            payload = node[2]
            if isinstance(payload, str) and payload.strip()[:1] in "[{":
                try:
                    envelope["payload_shape"] = _body_shape(payload)
                except ValueError:
                    pass
            elif isinstance(payload, (list, dict)):
                envelope["payload_shape"] = {"encoding": "json",
                                             "tree": _walk_shape(payload, 0)}
            found.append(envelope)
            return found
        for child in node:
            found.extend(_walk_envelopes(child, depth + 1))
    elif isinstance(node, dict):
        for child in node.values():
            found.extend(_walk_envelopes(child, depth + 1))
    return found


def _walk_markers(node: Any, depth: int = 0) -> list[list[Any]]:
    """找出流级标记信封（``["er", rpcid, code, ...]``、``["di", N]`` 等）。

    这些信封和 ``wrb.fr`` 同级嵌在 ``[[信封], [信封]]`` 里，只看顶层会漏掉。
    """
    found: list[list[Any]] = []
    if depth > 12:
        return found
    if isinstance(node, list):
        if node and isinstance(node[0], str) and node[0] != "wrb.fr":
            found.append(node)
            return found
        for child in node:
            if isinstance(child, list):
                found.extend(_walk_markers(child, depth + 1))
    return found


def _walk_shape(value: Any, depth: int) -> Any:
    if depth > 6:
        return "<deep>"
    if isinstance(value, dict):
        return {str(k): _walk_shape(v, depth + 1) for k, v in list(value.items())[:60]}
    if isinstance(value, list):
        return [_walk_shape(v, depth + 1) for v in value[:20]]
    if value is None:
        return "<null>"
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, (int, float)):
        return "<number>"
    if isinstance(value, str):
        text = value.strip()
        if len(text) <= 200_000 and text[:1] in "[{" and text[-1:] in "]}":
            try:
                return {"$json": _walk_shape(json.loads(text), depth + 1)}
            except ValueError:
                pass
        return "<empty-str>" if not text else f"<str:{len(text)}>"
    return "<empty>" if value in ([], {}) else "<value>"


class CaptureWriter:
    """逐事件追加写 JSONL，崩溃/中断也不丢已抓到的部分。"""
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf-8")
        self.started = time.monotonic()
        self.counts: Counter[str] = Counter()
        self._body_tasks: set[asyncio.Task[Any]] = set()

    def _ts(self) -> float:
        return round(time.monotonic() - self.started, 3)

    def write(self, entry: dict[str, Any]) -> None:
        self.counts[str(entry.get("kind"))] += 1
        self._fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def on_request(self, request: Request) -> None:
        headers = _headers(request.headers)
        entry: dict[str, Any] = {
            "kind": "request",
            "t": self._ts(),
            "method": request.method,
            "url": _redacted_url(request.url),
            "host": _host(request.url),
            "bucket": _bucket(_host(request.url)),
            "resource_type": request.resource_type,
            "headers": headers,
            "body": _request_body_meta(request, headers.get("content-type", "")),
            "challenge_signal": bool(CHALLENGE_RE.search(request.url)),
            "device_beacon": bool(DEVICE_BEACON_RE.search(request.url)),
        }
        redirect_from = request.redirected_from
        if redirect_from:
            entry["redirected_from"] = _redacted_url(redirect_from.url)
        self.write(entry)

    def on_response(self, response: Response) -> None:
        headers = _headers(response.headers)
        host = _host(response.url)
        entry: dict[str, Any] = {
            "kind": "response",
            "t": self._ts(),
            "status": response.status,
            "url": _redacted_url(response.url),
            "host": host,
            "bucket": _bucket(host),
            "resource_type": response.request.resource_type,
            "headers": headers,
            "challenge_signal": bool(CHALLENGE_RE.search(response.url)),
        }
        self.write(entry)
        content_type = str(response.headers.get("content-type") or "")
        if _is_page_content(response.request.resource_type, content_type):
            task = asyncio.create_task(self._probe_body(response, entry, content_type))
            self._body_tasks.add(task)
            task.add_done_callback(self._body_tasks.discard)

    async def _probe_body(self, response: Response, entry: dict[str, Any],
                          content_type: str) -> None:
        # 只记录命中的关键词，不写正文；失败的请求直接跳过。
        try:
            body = await response.text()
        except Exception:
            return
        if len(body) > MAX_PROBE_BYTES:
            body = body[:MAX_PROBE_BYTES]
        shape = None
        if "batchexecute" in entry["url"] or body.lstrip().startswith(")]}'"):
            shape = _batchexecute_shape(body)
        hits = sorted({m.group(0).lower() for m in BODY_SIGNAL_RE.finditer(body)})
        if not hits and not shape:
            return
        record: dict[str, Any] = {
            "kind": "body_signal",
            "t": entry["t"],
            "url": entry["url"],
            "host": entry["host"],
            "bucket": entry["bucket"],
            "status": entry["status"],
            "content_type": content_type[:80],
            "signals": hits,
            "body": _message_meta(body),
        }
        if shape:
            record["rpc_shape"] = shape
        self.write(record)

    def on_request_failed(self, request: Request) -> None:
        failure = str(request.failure or "unknown")
        self.write({
            "kind": "request_failed",
            "t": self._ts(),
            "method": request.method,
            "url": _redacted_url(request.url),
            "host": _host(request.url),
            "bucket": _bucket(_host(request.url)),
            # Chromium 的失败串是固定枚举（net::ERR_*），不含页面数据，直接保留
            # 便于区分“被拦截/被取消”与真正的网络不可达。
            "failure": failure[:120],
            "failure_meta": _message_meta(failure),
        })

    def on_websocket(self, websocket: Any) -> None:
        host = _host(websocket.url)
        self.write({
            "kind": "websocket",
            "t": self._ts(),
            "url": _redacted_url(websocket.url),
            "host": host,
            "bucket": _bucket(host),
        })

    def on_console(self, message: Any) -> None:
        # 控制台输出可能含提交过的个人信息或令牌，只保留元数据。
        self.write({
            "kind": "console",
            "t": self._ts(),
            "type": str(message.type),
            "message": _message_meta(message.text),
            "challenge_signal": bool(CHALLENGE_RE.search(str(message.text or ""))),
        })

    def on_page_error(self, error: Any) -> None:
        self.write({
            "kind": "page_error",
            "t": self._ts(),
            "message": _message_meta(str(error)),
            "challenge_signal": bool(CHALLENGE_RE.search(str(error or ""))),
        })

    def on_navigation(self, page: Page) -> None:
        self.write({
            "kind": "navigation",
            "t": self._ts(),
            "url": _redacted_url(page.url),
            "host": _host(page.url),
            "bucket": _bucket(_host(page.url)),
        })


async def _api(client: httpx.AsyncClient, method: str, path: str,
               **kwargs: Any) -> Any:
    response = await client.request(method, path, **kwargs)
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"管理端响应不是 JSON（HTTP {response.status_code}）") from exc
    if response.status_code >= 400 or payload.get("code") != 0:
        raise RuntimeError(payload.get("msg") or f"管理端请求失败（HTTP {response.status_code}）")
    return payload.get("data")


def _attach(context: BrowserContext, writer: CaptureWriter,
            handlers: list[tuple[Any, str, Any]]) -> None:
    def add(target: Any, event: str, handler: Any) -> None:
        target.on(event, handler)
        handlers.append((target, event, handler))

    def add_page(page: Page) -> None:
        add(page, "pageerror", writer.on_page_error)
        # console 挂在 page 上：BrowserContext 不转发 console 事件，挂 context
        # 会在 on() 时直接抛错。
        add(page, "console", writer.on_console)
        add(page, "framenavigated",
            lambda frame, p=page: writer.on_navigation(p) if frame == p.main_frame else None)

    add(context, "request", writer.on_request)
    add(context, "response", writer.on_response)
    add(context, "requestfailed", writer.on_request_failed)
    add(context, "page", add_page)
    for page in context.pages:
        add_page(page)


def _detach(handlers: list[tuple[Any, str, Any]]) -> None:
    # CDP 连的是用户正在用的上下文，结束前必须摘干净监听器。
    for target, event, handler in handlers:
        try:
            target.remove_listener(event, handler)
        except Exception:
            pass
    handlers.clear()


async def capture(args: argparse.Namespace) -> dict[str, Any]:
    profile_id = args.profile_id or os.environ.get("FPWB_PROFILE_ID")
    temporary = False
    started_here = False
    async with httpx.AsyncClient(base_url=args.management_url.rstrip("/"),
                                 timeout=120, trust_env=False) as client:
        if args.new_profile and not profile_id:
            profile = await _api(client, "POST", "/api/v1/profiles", json={
                "name": "network-capture-temp",
                "kernel": "fp-chromium",
                "target_os": "android",
                "fingerprint_mode": "preset",
                "manufacturer": args.manufacturer,
                "device_model": args.device_model,
                "launch": {
                    "headless": bool(args.headless),
                    "geoip": True,
                    "humanize": False,
                    "start_url": args.start_url,
                },
            })
            profile_id = profile["id"]
            temporary = True
        if not profile_id:
            raise RuntimeError("需要 --profile-id 或 --new-profile")

        profiles = await _api(client, "GET", "/api/v1/profiles")
        existing = next((p for p in profiles if p.get("id") == profile_id), None)
        if not existing:
            raise RuntimeError(f"环境不存在: {profile_id}")
        if existing.get("running"):
            endpoint = await _api(client, "GET", f"/api/v1/browser/{profile_id}/endpoint")
        else:
            payload: dict[str, Any] = {"profile_id": profile_id, "headless": bool(args.headless)}
            if args.start_url:
                payload["start_url"] = args.start_url
            endpoint = await _api(client, "POST", "/api/v1/browser/start", json=payload)
            started_here = True

        path = LOG_ROOT / f"{_now_tag()}-{profile_id[:8]}.jsonl"
        writer = CaptureWriter(path)
        handlers: list[tuple[Any, str, Any]] = []
        writer.write({
            "kind": "session",
            "profile_id": profile_id,
            "temporary_profile": temporary,
            "start_url": _redacted_url(args.start_url or ""),
            "duration": args.duration,
            "started_at": datetime.now(timezone.utc).isoformat(),
        })
        try:
            async with async_playwright() as playwright:
                browser: Browser = await playwright.chromium.connect_over_cdp(endpoint["ws_endpoint"])
                try:
                    context = browser.contexts[0] if browser.contexts else await browser.new_context()
                    _attach(context, writer, handlers)
                    await asyncio.sleep(max(1, int(args.duration)))
                    for task in list(writer._body_tasks):
                        task.cancel()
                finally:
                    _detach(handlers)
                    await browser.close()
        finally:
            writer.close()
            if started_here:
                try:
                    await _api(client, "POST", "/api/v1/browser/stop",
                               json={"profile_id": profile_id})
                except Exception:
                    pass
            if temporary:
                try:
                    await _api(client, "DELETE", f"/api/v1/profiles/{profile_id}")
                except Exception:
                    pass
    return {"log": str(path), "profile_id": profile_id,
            "event_counts": dict(writer.counts)}


def load_events(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    return events


def _challenge_step(path: str) -> str | None:
    """把步骤路径标成挑战性质：精确表优先，模式兜底。"""
    if path in GOOGLE_CHALLENGE_STEPS:
        return GOOGLE_CHALLENGE_STEPS[path]
    if CHALLENGE_STEP_RE.search(path):
        return "设备/身份验证步骤"
    return None


def analyze(events: list[dict[str, Any]]) -> dict[str, Any]:
    session = next((e for e in events if e.get("kind") == "session"), {})
    requests = [e for e in events if e.get("kind") == "request"]
    responses = [e for e in events if e.get("kind") == "response"]
    failures = [e for e in events if e.get("kind") == "request_failed"]
    hard_failures = [e for e in failures
                     if str(e.get("failure") or "") not in BENIGN_FAILURES]
    signals = [e for e in events if e.get("kind") == "body_signal"]
    beacons = [e for e in events if e.get("device_beacon")]

    # batchexecute RPC 清单：rpcids 白名单已保留原值，响应结构里也有 rpcid，
    # 两处交叉核对能看出“发了哪些 RPC、回了哪些 RPC”。
    rpc_calls: list[dict[str, Any]] = []
    for event in events:
        if event.get("kind") != "request" or "batchexecute" not in str(event.get("url") or ""):
            continue
        query = dict(parse_qsl(urlsplit(str(event.get("url") or "")).query))
        body = event.get("body") or {}
        rpc_calls.append({"t": event.get("t"), "sent": query.get("rpcids") or "",
                          "body_length": body.get("length"),
                          "body_shape": body.get("shape"),
                          "responses": []})
    for signal in signals:
        shape = signal.get("rpc_shape") or {}
        for envelope in shape.get("rpcs") or []:
            rpcid = str(envelope.get("rpcid") or "")
            target = next((call for call in reversed(rpc_calls)
                           if rpcid in str(call.get("sent") or "")), None)
            entry = {"t": signal.get("t"), "rpcid": rpcid,
                     "payload_present": envelope.get("payload_present"),
                     "payload_shape": envelope.get("payload_shape")}
            if target is not None:
                target["responses"].append(entry)
            else:
                rpc_calls.append({"t": signal.get("t"), "sent": "",
                                  "responses": [entry]})

    # 重定向链：按 redirected_from 串起来，只取顶层 document 请求。
    redirects: list[dict[str, Any]] = []
    for req in requests:
        if req.get("resource_type") != "document" or not req.get("redirected_from"):
            continue
        redirects.append({"from": req["redirected_from"], "to": req["url"], "t": req.get("t")})

    # 流程步骤：多步表单（Google 注册的 name→birthdaygender→username→password）
    # 是 JS 驱动的新文档导航，HTTP 层没有 3xx，只有 navigation 事件能看出来。
    steps: list[dict[str, Any]] = []
    for event in events:
        if event.get("kind") != "navigation":
            continue
        url = str(event.get("url") or "")
        if not steps or steps[-1]["url"] != url:
            path = urlsplit(url).path
            steps.append({"t": event.get("t"), "url": url,
                          "host": event.get("host"), "path": path,
                          "challenge_step": _challenge_step(path)})
    step_dwell: list[float | None] = []
    for index, step in enumerate(steps):
        nxt = steps[index + 1]["t"] if index + 1 < len(steps) else None
        step_dwell.append(round(nxt - step["t"], 2) if nxt is not None else None)

    # 主机 -> 状态分布，用来找被拦截/限流的主机。
    host_status: dict[str, Counter[str]] = defaultdict(Counter)
    for resp in responses:
        host_status[str(resp.get("host") or "")][str(resp.get("status"))] += 1

    denied = sorted(
        ({"host": h, "status": dict(c)} for h, c in host_status.items()
         if any(int(s) in {401, 403, 409, 429} for s in c)),
        key=lambda row: -sum(row["status"].values()))

    hosts = Counter(str(e.get("host") or "") for e in requests if e.get("host"))
    buckets = Counter(str(e.get("bucket") or "") for e in requests if e.get("bucket"))

    # 挑战面分级：库加载 ≠ 触发。只有 rendered/signaled 才进风险项。
    challenge_rendered: list[str] = []
    challenge_signaled: list[str] = []
    challenge_library: list[str] = []
    challenge_pages: list[str] = []
    for event in events:
        url = str(event.get("url") or "")
        if not url or event.get("kind") not in {"request", "response", "navigation"}:
            continue
        if CHALLENGE_RENDERED_RE.search(url):
            challenge_rendered.append(url)
        elif event.get("kind") == "navigation" and CHALLENGE_PAGE_RE.search(url):
            challenge_pages.append(url)
        elif CHALLENGE_LIBRARY_RE.search(url):
            challenge_library.append(url)
        elif CHALLENGE_PAGE_RE.search(url):
            challenge_signaled.append(url)
    challenge_rendered = sorted(set(challenge_rendered))
    challenge_library = sorted(set(challenge_library))
    challenge_pages = sorted(set(challenge_pages))
    challenge_signaled = sorted(set(challenge_signaled))
    # 只统计与挑战相关的正文命中；recaptcha 字样出现在隐形 anchor 页面上属正常，
    # 不能因为页面加载了验证组件就把整个流程标成「有挑战意图」。
    challenge_signals = sorted({
        f"{row.get('host')} → {', '.join(row.get('signals') or [])}"
        for row in signals
        if set(row.get("signals") or []) - RECAPTCHA_LIBRARY_WORDS
    })

    # batchexecute 的挑战标记：RPC 级 "er" 信封是服务端明确的拒绝，
    # 客户端据此切到错误/验证步骤；空 payload 的成功信封不是挑战。
    rpc_errors: list[dict[str, Any]] = []
    rpc_ok: list[str] = []
    rpc_markers: dict[str, dict[str, Any]] = {}
    for row in signals:
        shape = row.get("rpc_shape") or {}
        for error in shape.get("errors") or []:
            rpc_errors.append({
                "t": row.get("t"),
                "rpcid": error.get("rpcid"),
                "code": error.get("code"),
                "meaning": GOOGLE_RPC_MEANINGS.get(str(error.get("rpcid") or "")),
            })
        for envelope in shape.get("rpcs") or []:
            rpc_ok.append(str(envelope.get("rpcid") or ""))
        for marker in shape.get("markers") or []:
            tag = str(marker.get("tag") or "")
            if tag:
                rpc_markers[tag] = {
                    "tag": tag,
                    "arity": marker.get("arity"),
                    "numbers": marker.get("numbers") or [],
                    "numbers_recorded": "numbers" in marker,
                    "meaning": GOOGLE_STREAM_MARKERS.get(tag),
                }

    # 第三方依赖：不属于主站后缀的主机。主站从 session.start_url 取，旁路
    # 观测时没有 start_url，退回第一条顶层文档请求。
    primary = str(session.get("start_url") or "")
    if not primary:
        primary = next((str(r.get("url") or "") for r in requests
                        if r.get("resource_type") == "document"), "")
    primary_host = _host(primary)
    primary_suffix = ".".join(primary_host.split(".")[-2:]) if primary_host else ""
    third_party = [
        {"host": h, "bucket": _bucket(h), "requests": hosts[h]}
        for h in sorted(hosts, key=lambda h: -hosts[h])
        if h and not (h == primary_host
                      or (primary_suffix and h.endswith("." + primary_suffix)))
    ][:30]

    # 挑战前一跳：挑战页导航前 200ms 内返回的 batchexecute 响应。挑战决定写在
    # payload 里而不是 er 信封里，只能靠时间邻接定位是哪个 RPC 触发的。
    challenge_hops: list[dict[str, Any]] = []
    for step in steps:
        if not step.get("challenge_step"):
            continue
        before = [r for r in responses
                  if r.get("t") is not None and step.get("t") is not None
                  and 0 <= (step["t"] - r["t"]) <= 0.2
                  and "batchexecute" in str(r.get("url") or "")]
        if not before:
            continue
        latest = max(before, key=lambda r: r["t"])
        shape = next((s.get("rpc_shape") or {} for s in signals
                      if s.get("t") == latest.get("t")), {})
        challenge_hops.append({
            "t": step.get("t"),
            "path": step.get("path"),
            "kind": step.get("challenge_step"),
            "delta_ms": round((step["t"] - latest["t"]) * 1000),
            "rpcid": next((e.get("rpcid") for e in shape.get("rpcs") or []), None),
            "payload_shape": next((e.get("payload_shape") for e in shape.get("rpcs") or []), None),
        })

    risks: list[dict[str, Any]] = []
    if challenge_hops and not rpc_errors:
        latest = challenge_hops[0]
        risks.append({
            "id": "challenge_decision_in_payload",
            "severity": "high",
            "evidence": f"挑战页 `{latest.get('path')}` 由 RPC `{latest.get('rpcid') or '-'}` "
                        f"返回后 {latest.get('delta_ms')}ms 触发，响应里没有 `er` 错误信封",
            "advice": "挑战决定写在 RPC 的 payload 里（通常是深层嵌套的下一步指针字符串），"
                      "不是 er 信封；定位风控点要看这个 RPC 请求体里的设备字段，"
                      "不要只等 er",
        })
    if rpc_errors:
        risks.append({
            "id": "rpc_error",
            "severity": "high",
            "evidence": f"{len(rpc_errors)} 个 batchexecute RPC 返回错误信封：" + ", ".join(
                f"{e.get('rpcid')}(code={e.get('code')})" for e in rpc_errors[:5]),
            "advice": "服务端明确拒绝了该 RPC，客户端会据此跳错误/验证步骤；"
                      "这是本环境最直接的挑战标记，对照该 RPC 请求体里的设备字段定位差异",
        })
    if challenge_rendered or challenge_pages:
        risks.append({
            "id": "challenge_triggered",
            "severity": "high",
            "evidence": f"{len(challenge_rendered)} 个可见挑战框资源，"
                        f"{len(challenge_pages)} 个挑战页导航",
            "advice": "挑战已实际触发；对照触发前的信标/表单请求定位风控点，不要自动处理挑战",
        })
    elif challenge_signaled or challenge_signals:
        risks.append({
            "id": "challenge_signaled",
            "severity": "medium",
            "evidence": f"{len(challenge_signaled)} 个挑战类 URL，"
                        f"{len(challenge_signals)} 个正文关键词命中（未见可见挑战框）",
            "advice": "有挑战意图但未渲染；核对页面运行时状态确认是否被静默放行",
        })
    if challenge_library:
        risks.append({
            "id": "challenge_library_loaded",
            "severity": "low",
            "evidence": f"加载了 {len(challenge_library)} 个验证库/隐形 anchor 资源（非触发）",
            "advice": "仅说明页面启用了验证组件；不要当作已触发挑战",
        })
    if denied:
        risks.append({
            "id": "denied_or_rate_limited",
            "severity": "high" if any("429" in row["status"] for row in denied) else "medium",
            "evidence": "; ".join(f"{r['host']} {r['status']}" for r in denied[:5]),
            "advice": "核对出口代理与会话状态；限流通常是会话/IP 级而非指纹级",
        })
    if hard_failures:
        by_host = Counter(str(e.get("host") or "") for e in hard_failures)
        by_reason = Counter(str(e.get("failure") or "") for e in hard_failures)
        risks.append({
            "id": "network_failures",
            "severity": "medium",
            "evidence": f"{len(hard_failures)} 个请求失败：" + ", ".join(
                f"{h}×{n}" for h, n in by_host.most_common(5))
            + " / " + ", ".join(f"{r}×{n}" for r, n in by_reason.most_common(3)),
            "advice": "按主机核对代理连通性、DNS 与 TLS；失败集中在单一主机时优先查该域可达性",
        })
    if beacons:
        risks.append({
            "id": "device_beacon",
            "severity": "low",
            "evidence": f"{len(beacons)} 个设备指纹信标请求：" + ", ".join(
                sorted({f"{e.get('host')}{urlsplit(str(e.get('url') or '')).path}"
                        for e in beacons})[:5]),
            "advice": "信标携带设备信号，是本环境被服务端建模的入口；对照信标前的页面状态定位差异",
        })
    exit_hosts = [h for h in hosts if "ipify" in h or "ipinfo" in h or "ip-api" in h]
    if exit_hosts:
        risks.append({
            "id": "exit_probe_present",
            "severity": "low",
            "evidence": f"页面调用了出口 IP 探测主机：{', '.join(sorted(set(exit_hosts))[:5])}",
            "advice": "这类结果会同时进入页面判定；确认出口与声明的时区/国家一致",
        })

    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "session": session,
        "event_counts": dict(Counter(str(e.get("kind")) for e in events)),
        "request_count": len(requests),
        "response_count": len(responses),
        "host_counts": dict(hosts.most_common()),
        "bucket_counts": dict(buckets.most_common()),
        "redirect_chain": redirects,
        "flow_steps": [dict(step, dwell=step_dwell[i]) for i, step in enumerate(steps)],
        "challenge_hops": challenge_hops,
        "denied_hosts": denied,
        "failure_reasons": dict(Counter(str(e.get("failure") or "")
                                        for e in failures).most_common()),
        "device_beacons": [
            {"t": e.get("t"), "method": e.get("method"), "host": e.get("host"),
             "url": e.get("url"), "body": e.get("body"),
             "slots": _shape_slots((e.get("body") or {}).get("shape"))}
            for e in beacons[:20]
        ],
        "third_party_hosts": third_party[:30],
        "challenge": {
            "rendered": challenge_rendered[:30],
            "pages": challenge_pages[:30],
            "signaled": challenge_signaled[:30],
            "library": challenge_library[:30],
            "body_signals": challenge_signals[:30],
        },
        "rpc_calls": rpc_calls[:40],
        "rpc_errors": rpc_errors[:20],
        "rpc_ok": sorted(set(filter(None, rpc_ok))),
        "rpc_markers": list(rpc_markers.values()),
        "risks": risks,
    }


def markdown(report: dict[str, Any]) -> str:
    session = report.get("session") or {}
    lines = [
        "# 网络抓包分析",
        "",
        f"- 生成时间：{report.get('created_at')}",
        f"- 环境：{session.get('profile_id') or '(未知)'}",
        f"- 起始地址：{session.get('start_url') or '(未知)'}",
        f"- 事件：{json.dumps(report.get('event_counts'), ensure_ascii=False)}",
        "",
        "## 风险项",
        "",
    ]
    risks = report.get("risks") or []
    if not risks:
        lines.append("未发现脚本可观测的风险信号；这不代表服务端策略一定放行。")
    else:
        lines.extend(["| 等级 | 项目 | 证据 | 建议 |", "|---|---|---|---|"])
        for item in risks:
            lines.append("| {severity} | {id} | {evidence} | {advice} |".format(**item))

    lines.extend(["", "## 重定向链", ""])
    chain = report.get("redirect_chain") or []
    if chain:
        for row in chain:
            lines.append(f"- `{row.get('from')}` → `{row.get('to')}`")
    else:
        lines.append("无 HTTP 层重定向。")

    steps = report.get("flow_steps") or []
    if steps:
        lines.extend(["", "## 流程步骤", "",
                      "| # | 时间(s) | 停留(s) | 路径 | 性质 |", "|---|---|---|---|---|"])
        for index, row in enumerate(steps, 1):
            dwell = "-" if row.get("dwell") is None else row["dwell"]
            kind = row.get("challenge_step") or "常规步骤"
            lines.append(f"| {index} | {row.get('t')} | {dwell} "
                         f"| `{row.get('path')}` | {kind} |")

    hops = report.get("challenge_hops") or []
    if hops:
        lines.extend(["", "### 挑战前一跳", "",
                      "挑战页导航前 200ms 内返回的 RPC——挑战决定写在这些 RPC 的 payload 里，"
                      "`er` 信封往往不出现。", "",
                      "| 时间(s) | 跳转路径 | 性质 | 领先(ms) | RPC | payload 结构 |",
                      "|---|---|---|---|---|---|"])
        for hop in hops:
            shape = json.dumps(hop.get("payload_shape"), ensure_ascii=False)
            if len(shape) > 160:
                shape = shape[:160] + "…"
            lines.append(f"| {hop.get('t')} | `{hop.get('path')}` | {hop.get('kind')} "
                         f"| {hop.get('delta_ms')} | `{hop.get('rpcid') or '-'}` | `{shape}` |")

    lines.extend(["", "## 主机分布", "", "| 主机 | 请求数 |", "|---|---|"])
    for host, count in (report.get("host_counts") or {}).items():
        lines.append(f"| {host} | {count} |")

    lines.extend(["", "## 第三方依赖", "", "| 主机 | 归组 | 请求数 |", "|---|---|---|"])
    third = report.get("third_party_hosts") or []
    if third:
        for row in third:
            lines.append(f"| {row['host']} | {row['bucket']} | {row['requests']} |")
    else:
        lines.append("无。")

    lines.extend(["", "## 挑战面", ""])
    challenge = report.get("challenge") or {}
    rendered = challenge.get("rendered") or []
    pages = challenge.get("pages") or []
    signaled = challenge.get("signaled") or []
    library = challenge.get("library") or []
    sigs = challenge.get("body_signals") or []
    lines.append(f"- 可见挑战框资源：{len(rendered)}")
    lines.append(f"- 挑战页导航：{len(pages)}")
    lines.append(f"- 挑战意图 URL：{len(signaled)}")
    lines.append(f"- 验证库/隐形 anchor：{len(library)}（非触发）")
    lines.append(f"- 正文关键词命中：{len(sigs)}")
    if not (rendered or pages or signaled or sigs):
        lines.append("")
        lines.append("未触发挑战；验证库加载属于页面正常组成。")
    for url in rendered:
        lines.append(f"- 可见挑战: `{url}`")
    for url in pages:
        lines.append(f"- 挑战页: `{url}`")
    for url in signaled:
        lines.append(f"- 意图: `{url}`")
    for sig in sigs:
        lines.append(f"- 正文: {sig}")

    rpc_calls = report.get("rpc_calls") or []
    if rpc_calls:
        lines.extend(["", "## batchexecute RPC", "",
                      "| 时间(s) | 发出的 RPC | 字节 | 返回的 RPC | payload |",
                      "|---|---|---|---|---|"])
        for call in rpc_calls:
            responses = call.get("responses") or []
            size = call.get("body_length") or "-"
            if responses:
                for response in responses:
                    lines.append(f"| {response.get('t')} | `{call.get('sent') or '-'}` "
                                 f"| {size} | `{response.get('rpcid')}` "
                                 f"| {'有' if response.get('payload_present') else '空'} |")
            else:
                lines.append(f"| {call.get('t')} | `{call.get('sent') or '-'}` | {size} "
                             f"| - | 无响应结构 |")

    # 请求参数结构：挑战决定看响应，但定位风控点要看请求里带了什么设备字段。
    shaped = [c for c in rpc_calls if c.get("body_shape")]
    if shaped:
        lines.extend(["", "### 请求参数结构", "",
                      "只展开形状，值一律记作 `<str:长度>` / `<number>` / `<null>`。",
                      "", "| 时间(s) | RPC | 参数结构 |", "|---|---|---|"])
        for call in shaped:
            shape = json.dumps((call.get("body_shape") or {}).get("fields"),
                               ensure_ascii=False)
            if len(shape) > 300:
                shape = shape[:300] + "…"
            lines.append(f"| {call.get('t')} | `{call.get('sent') or '-'}` | `{shape}` |")

    rpc_errors = report.get("rpc_errors") or []
    if rpc_errors:
        lines.extend(["", "### RPC 错误信封（挑战标记）", "",
                      "| 时间(s) | RPC | 错误码 | 用途 |", "|---|---|---|---|"])
        for error in rpc_errors:
            lines.append(f"| {error.get('t')} | `{error.get('rpcid') or '-'}` "
                         f"| {error.get('code') if error.get('code') is not None else '-'} "
                         f"| {error.get('meaning') or '未知 RPC'} |")
    rpc_ok = report.get("rpc_ok") or []
    if rpc_ok:
        lines.extend(["", "### 成功的 RPC", "", "| RPC | 用途 |", "|---|---|"])
        for rpcid in rpc_ok:
            lines.append(f"| `{rpcid}` | {GOOGLE_RPC_MEANINGS.get(rpcid, '未知 RPC')} |")
    rpc_markers = report.get("rpc_markers") or []
    if rpc_markers:
        lines.extend(["", "### 流级标记", "",
                      "`di`/`e`/`af.httprm` 是每个响应都带的协议标记，不是挑战信号。",
                      "", "| 标记 | 槽位数 | 数值 | 含义 |", "|---|---|---|---|"])
        for marker in rpc_markers:
            numbers = marker.get("numbers") or []
            value = ", ".join(str(n) for n in numbers)
            if not value:
                value = "-" if marker.get("numbers_recorded") else "（旧抓包未记录数值）"
            lines.append(f"| `{marker.get('tag')}` | {marker.get('arity')} "
                         f"| {value} | {marker.get('meaning') or '未知标记'} |")

    denied = report.get("denied_hosts") or []
    if denied:
        lines.extend(["", "## 被拒/限流", "", "| 主机 | 状态分布 |", "|---|---|"])
        for row in denied:
            lines.append(f"| {row['host']} | {json.dumps(row['status'], ensure_ascii=False)} |")

    beacons = report.get("device_beacons") or []
    if beacons:
        lines.extend(["", "## 设备指纹信标", "",
                      "| 时间(s) | 方法 | 主机 | 路径 | 请求体 |", "|---|---|---|---|---|"])
        for row in beacons:
            path = urlsplit(str(row.get("url") or "")).path
            body = json.dumps(row.get("body"), ensure_ascii=False) if row.get("body") else "-"
            lines.append(f"| {row.get('t')} | {row.get('method')} | {row.get('host')} "
                         f"| `{path}` | `{body}` |")
        slots = [slot for row in beacons for slot in (row.get("slots") or [])]
        lines.extend(["", "### 空槽位", "",
                      "`<null>` = 未采集，`<empty-str>` = 采集了但为空，`<empty>` = 空容器。",
                      "", "| 路径 | 类型 | 含义 |", "|---|---|---|"])
        if slots:
            for slot in slots:
                lines.append(f"| `{slot['path']}` | {slot['kind']} "
                             f"| {slot.get('meaning') or '-'} |")
        else:
            lines.append("| - | 无空槽位 | - |")

    reasons = report.get("failure_reasons") or {}
    if reasons:
        lines.extend(["", "## 请求失败原因", "", "| 原因 | 次数 |", "|---|---|"])
        for reason, count in reasons.items():
            lines.append(f"| {reason} | {count} |")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--management-url", default=os.environ.get(
        "FPWB_MANAGEMENT_URL", DEFAULT_MANAGEMENT_URL))
    parser.add_argument("--profile-id", default=None)
    parser.add_argument("--new-profile", action="store_true",
                        help="创建一个临时环境并在结束时删除")
    parser.add_argument("--start-url", default=None,
                        help="仅 --new-profile 或环境未运行时用于启动页")
    parser.add_argument("--manufacturer", default="Xiaomi")
    parser.add_argument("--device-model", default="Xiaomi 15 Ultra")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--duration", type=int, default=60,
                        help="旁路观测秒数；期间用户自己操作页面")
    parser.add_argument("--analyze-only", default=None,
                        help="只分析已有 JSONL 日志，不抓包")
    parser.add_argument("--output", default=None, help="报告路径前缀")
    args = parser.parse_args()

    if args.analyze_only:
        path = Path(args.analyze_only)
        events = load_events(path)
        prefix = Path(args.output) if args.output else path.with_suffix("")
    else:
        try:
            result = asyncio.run(capture(args))
        except Exception as exc:
            print(f"capture failed: {type(exc).__name__}: {_safe_text(str(exc), 300)}",
                  file=sys.stderr)
            return 2
        path = Path(result["log"])
        events = load_events(path)
        prefix = Path(args.output) if args.output else path.with_suffix("")

    report = analyze(events)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    prefix.with_suffix(".analysis.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    prefix.with_suffix(".analysis.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({
        "log": str(path),
        "analysis_json": str(prefix.with_suffix(".analysis.json")),
        "analysis_md": str(prefix.with_suffix(".analysis.md")),
        "events": len(events),
        "requests": report["request_count"],
        "risk_ids": [r["id"] for r in report["risks"]],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
