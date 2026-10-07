"""代理域名真实解析：用 DoH 绕过本机 fake-IP DNS。

本机若开了 Clash / Surge / Shadowrocket 的 TUN + fake-IP，系统解析会把域名
统一返回 198.18.0.0/15 的假地址，浏览器连上游代理时必然失败。
这里直接查公共 DoH 取真实 A 记录；解析失败返回 None，调用方回退到原始域名。
"""
import ipaddress
import logging
from typing import Optional
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

# 公共 DoH JSON 端点（按顺序回退）
_DOH_ENDPOINTS = (
    "https://dns.alidns.com/resolve",
    "https://cloudflare-dns.com/dns-query",
    "https://doh.pub/dns-query",
)


def is_ip_address(host: str) -> bool:
    """host 本身是否已是 IP 字面量。"""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def is_lan_target(url: str) -> bool:
    """目标是回环/内网地址（localhost、127.0.0.0/8、10/8、172.16/12、192.168/16 等）。

    httpx 默认 trust_env=True，会读取 macOS 系统代理设置；若系统代理开着，
    连发往 127.0.0.1 的请求也会被送进代理，代理对 Host=127.0.0.1 返回 502，
    导致本机/内网服务（webhook、MCP、同步节点）永远连不上。
    这类目标应设置 trust_env=False 直连。
    """
    host = (urlparse(url).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


async def resolve_real_ip(host: str, timeout: float = 6) -> Optional[str]:
    """用 DoH 解析 host 的真实 IPv4；已是 IP 或解析失败返回原值/None。"""
    if not host:
        return None
    if is_ip_address(host):
        return host
    for url in _DOH_ENDPOINTS:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    url,
                    params={"name": host, "type": "A"},
                    headers={"accept": "application/dns-json"},
                )
            if resp.status_code != 200:
                log.debug("DoH %s 返回 %s", url, resp.status_code)
                continue
            for answer in resp.json().get("Answer") or []:
                if str(answer.get("type")) != "1":
                    continue
                ip = str(answer.get("data", "")).strip()
                if is_ip_address(ip):
                    return ip
        except Exception as e:  # 单个端点失败继续回退
            log.debug("DoH %s 解析 %s 失败: %s", url, host, e)
    return None
