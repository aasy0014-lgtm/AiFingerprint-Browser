"""代理连通性测试：多端点回退探测出口 IP 与归属地，并识别 IP 质量。

端点顺序：ip-api.com（含 proxy/hosting/mobile 质量字段）→ ipwho.is → ipinfo.io。
出口 IP 的时区/国家信息同时用于提示用户该代理与指纹配置是否匹配。
"""
import time
from typing import Any, Optional

import httpx

from .models import ProxyConfig
from .proxy_dns import is_ip_address, resolve_real_ip

# (url, 字段归一化函数)；归一化失败/数据无效返回 None 触发下一端点
_PROVIDERS: list[tuple[str, Any]] = [
    (
        "http://ip-api.com/json/?fields=status,message,query,country,countryCode,"
        "city,timezone,isp,proxy,hosting,mobile",
        lambda d: {
            "exit_ip": d.get("query"), "country": d.get("country"),
            "country_code": d.get("countryCode"), "city": d.get("city"),
            "timezone": d.get("timezone"), "isp": d.get("isp"),
            "ip_proxy": d.get("proxy"), "ip_hosting": d.get("hosting"),
            "ip_mobile": d.get("mobile"),
        } if d.get("status") == "success" and d.get("query") else None,
    ),
    (
        "https://ipwho.is/?fields=ip,success,country,country_code,city,timezone,connection",
        lambda d: {
            "exit_ip": d.get("ip"), "country": d.get("country"),
            "country_code": d.get("country_code"), "city": d.get("city"),
            "timezone": (d.get("timezone") or {}).get("id")
            if isinstance(d.get("timezone"), dict) else d.get("timezone"),
            "isp": (d.get("connection") or {}).get("isp"),
            "ip_proxy": None, "ip_hosting": None, "ip_mobile": None,
        } if d.get("success") and d.get("ip") else None,
    ),
    (
        "https://ipinfo.io/json",
        lambda d: {
            "exit_ip": d.get("ip"), "country": d.get("country"),
            "country_code": d.get("country"), "city": d.get("city"),
            "timezone": d.get("timezone"), "isp": d.get("org"),
            "ip_proxy": None, "ip_hosting": None, "ip_mobile": None,
        } if d.get("ip") else None,
    ),
]


def _ip_type_label(ip_proxy: Optional[bool], ip_hosting: Optional[bool],
                   ip_mobile: Optional[bool]) -> Optional[str]:
    """ip-api 质量字段 → 可读标签（机房 IP 是风控重扣分项）。"""
    if ip_proxy is None and ip_hosting is None and ip_mobile is None:
        return None
    if ip_mobile:
        return "移动网络"
    if ip_hosting or ip_proxy:
        return "机房/数据中心（风控高风险）"
    return "住宅/原生"


async def test_proxy(proxy: Optional[ProxyConfig], timeout: float = 15) -> dict:
    mode = "proxy" if proxy else "direct"
    t0 = time.perf_counter()
    # 本机 fake-IP（TUN）会把代理域名解析成 198.18.x.x，这里同样用 DoH 取真实 IP
    if proxy and not is_ip_address(proxy.host):
        real_ip = await resolve_real_ip(proxy.host)
        if real_ip and real_ip != proxy.host:
            proxy = proxy.model_copy(update={"host": real_ip})
    errors: list[str] = []
    for url, extract in _PROVIDERS:
        try:
            async with httpx.AsyncClient(
                proxy=proxy.to_url() if proxy else None, timeout=timeout,
                # 直连模式必须忽略系统/环境代理：macOS 系统代理开着时 httpx 默认
                # trust_env=True 会走系统代理，"直连"测出的出口 IP 其实是代理节点的
                trust_env=bool(proxy),
            ) as client:
                resp = await client.get(url)
            data = extract(resp.json())
            if data:
                data.update({
                    "ok": True,
                    "mode": mode,
                    "latency_ms": int((time.perf_counter() - t0) * 1000),
                    "ip_type": _ip_type_label(data.pop("ip_proxy"),
                                              data.pop("ip_hosting"),
                                              data.pop("ip_mobile")),
                })
                return data
            errors.append(f"{url.split('/')[2]}: 数据无效")
        except Exception as e:
            errors.append(f"{url.split('/')[2]}: {type(e).__name__}")
    return {
        "ok": False,
        "mode": mode,
        "error": f"全部探测端点失败（{'; '.join(errors)}）",
        "latency_ms": int((time.perf_counter() - t0) * 1000),
    }


# 出口属地 → 浏览器 locale：geoip 只对齐时区会造成「属地欧美、语言中文」这类
# 跨域特征（检测站点直接判为代理环境）。这里按属地主流语言给出确定性 locale
# （取多数人口语言，不用随机抽样：同一环境每次启动的语言必须一致）。
_COUNTRY_LOCALE: dict[str, str] = {
    # 东亚 / 东南亚
    "CN": "zh-CN", "TW": "zh-TW", "HK": "zh-HK", "MO": "zh-MO", "JP": "ja-JP",
    "KR": "ko-KR", "MN": "mn-MN", "TH": "th-TH", "VN": "vi-VN", "ID": "id-ID",
    "MY": "ms-MY", "SG": "en-SG", "PH": "en-PH", "KH": "km-KH", "MM": "my-MM",
    # 南亚 / 中亚 / 西亚
    "IN": "en-IN", "PK": "en-PK", "BD": "bn-BD", "LK": "si-LK", "NP": "ne-NP",
    "KZ": "ru-KZ", "UZ": "uz-UZ", "AZ": "az-AZ", "GE": "ka-GE", "AM": "hy-AM",
    "TR": "tr-TR", "IL": "he-IL", "IR": "fa-IR",
    # 阿拉伯语区
    "SA": "ar-SA", "AE": "ar-AE", "QA": "ar-QA", "KW": "ar-KW", "BH": "ar-BH",
    "OM": "ar-OM", "EG": "ar-EG", "IQ": "ar-IQ", "JO": "ar-JO", "LB": "ar-LB",
    "MA": "ar-MA", "DZ": "ar-DZ", "TN": "ar-TN", "LY": "ar-LY",
    # 西欧 / 北欧
    "US": "en-US", "CA": "en-CA", "GB": "en-GB", "IE": "en-IE", "AU": "en-AU",
    "NZ": "en-NZ", "DE": "de-DE", "AT": "de-AT", "CH": "de-CH", "FR": "fr-FR",
    "BE": "nl-BE", "NL": "nl-NL", "DK": "da-DK", "SE": "sv-SE", "NO": "nb-NO",
    "FI": "fi-FI", "IS": "is-IS", "ES": "es-ES", "PT": "pt-PT", "IT": "it-IT",
    "GR": "el-GR", "MT": "mt-MT",
    # 中东欧 / 独联体
    "PL": "pl-PL", "CZ": "cs-CZ", "SK": "sk-SK", "HU": "hu-HU", "RO": "ro-RO",
    "BG": "bg-BG", "HR": "hr-HR", "SI": "sl-SI", "RS": "sr-RS", "BA": "bs-BA",
    "MK": "mk-MK", "AL": "sq-AL", "LT": "lt-LT", "LV": "lv-LV", "EE": "et-EE",
    "UA": "uk-UA", "RU": "ru-RU", "BY": "ru-BY", "MD": "ro-MD",
    # 拉美
    "MX": "es-MX", "AR": "es-AR", "CL": "es-CL", "CO": "es-CO", "PE": "es-PE",
    "VE": "es-VE", "EC": "es-EC", "UY": "es-UY", "PY": "es-PY", "BO": "es-BO",
    "CR": "es-CR", "PA": "es-PA", "DO": "es-DO", "GT": "es-GT", "HN": "es-HN",
    "SV": "es-SV", "NI": "es-NI", "PR": "es-PR", "CU": "es-CU", "BR": "pt-BR",
    # 非洲
    "ZA": "en-ZA", "NG": "en-NG", "KE": "en-KE", "GH": "en-GH", "UG": "en-UG",
    "TZ": "sw-TZ", "ET": "am-ET",
}


def locale_for_country(country_code: Optional[str]) -> Optional[str]:
    """出口国家码 → locale；无把握的国家返回 None（保持现状，不做错误猜测）。"""
    if not country_code:
        return None
    return _COUNTRY_LOCALE.get(str(country_code).strip().upper())


# 出口属地缓存：geoip 对齐在每次启动环境时都可能调用，避免频繁探测（TTL 30 分钟）
_GEO_CACHE: dict[tuple, tuple[float, dict[str, Optional[str]]]] = {}
_GEO_CACHE_TTL = 30 * 60


async def resolve_exit_geo(proxy: Optional[ProxyConfig],
                           timeout: float = 8) -> dict[str, Optional[str]]:
    """经代理探测出口 IP 的属地（时区 + 国家码），结果带 TTL 缓存。

    与 test_proxy 走同一组端点；探测失败返回空字段（调用方保持系统设置，
    不阻断环境启动）。
    """
    empty: dict[str, Optional[str]] = {"timezone": None, "country_code": None}
    if proxy is None:
        return dict(empty)
    if not is_ip_address(proxy.host):
        real_ip = await resolve_real_ip(proxy.host)
        if real_ip and real_ip != proxy.host:
            proxy = proxy.model_copy(update={"host": real_ip})
    key = (proxy.scheme, proxy.host, proxy.port, proxy.username or "")
    now = time.time()
    cached = _GEO_CACHE.get(key)
    if cached and now - cached[0] < _GEO_CACHE_TTL:
        return dict(cached[1])
    geo = dict(empty)
    for url, extract in _PROVIDERS:
        try:
            async with httpx.AsyncClient(
                proxy=proxy.to_url(), timeout=timeout, trust_env=True,
            ) as client:
                resp = await client.get(url)
            data = extract(resp.json())
            if data and data.get("timezone"):
                geo["timezone"] = data["timezone"]
                geo["country_code"] = data.get("country_code")
                break
        except Exception:
            continue
    _GEO_CACHE[key] = (now, geo)
    return dict(geo)


async def resolve_exit_timezone(proxy: Optional[ProxyConfig],
                                timeout: float = 8) -> Optional[str]:
    """经代理探测出口 IP 的时区（geoip 时区对齐用），结果带 TTL 缓存。

    探测失败返回 None（调用方保持系统时区，不阻断环境启动）。
    """
    return (await resolve_exit_geo(proxy, timeout)).get("timezone")

