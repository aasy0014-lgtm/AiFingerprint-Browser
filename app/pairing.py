"""插件配对码：浏览器插件与工作台的一键配置机制。

流程：
1. 用户在工作台（设置页或 API）生成 6 位配对码（5 分钟有效，一次性）
2. 在插件中输入配对码 → 插件调用 /api/v1/pair/exchange 换取 API Key
3. 插件保存密钥完成配置，全程无需手动复制粘贴长密钥

安全：配对码短时效 + 一次性 + 尝试次数上限（5 次）；
exchange 接口不需要 API Key（配对码本身就是凭证），仅限本机默认部署场景。
"""
import secrets
import time
from typing import Optional

# 内存态：code -> {api_key, expires_at, used}
_codes: dict[str, dict] = {}
# 失败尝试按"全局"累计：错误码根本不在表中，无法挂到具体条目上；
# 早期实现把 attempts 记在匹配到的条目上，而匹配成功后立即置 used，
# 计数恒为 1，MAX_ATTEMPTS 形同虚设，起不到防枚举作用。
_failed_attempts = 0

TTL_SECONDS = 300
MAX_ATTEMPTS = 5


def create_pairing(api_key: str) -> tuple[str, int]:
    """生成配对码，返回 (code, ttl)。"""
    global _failed_attempts
    # 清理过期
    now = time.time()
    for c in [k for k, v in _codes.items() if v["expires_at"] < now]:
        _codes.pop(c)
    _failed_attempts = 0  # 重新生成即重置失败计数
    code = "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
    _codes[code] = {"api_key": api_key, "expires_at": now + TTL_SECONDS, "used": False}
    return code, TTL_SECONDS


def exchange(code: str) -> Optional[str]:
    """用配对码换取 API Key；无效/过期/已用/尝试过多返回 None。"""
    global _failed_attempts
    key = (code or "").strip().upper()
    item = _codes.get(key)
    if not item or item["used"] or time.time() > item["expires_at"]:
        _failed_attempts += 1
        if _failed_attempts >= MAX_ATTEMPTS:
            # 疑似暴力枚举：作废全部待用配对码，迫使用户回到工作台重新生成
            _codes.clear()
            _failed_attempts = 0
        _codes.pop(key, None)
        return None
    item["used"] = True
    return item["api_key"]
