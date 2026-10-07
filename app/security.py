"""安全模块：本地 API Key 认证 + 代理密码加密存储。

- API Key：默认关闭；开启后所有 /api/* 请求须携带 X-API-Key 头（/api/v1/auth/verify 除外）
- 密码加密：Fernet 对称加密，密钥首次运行时生成于 data/secret.key；
  加密值以 "enc:" 前缀存储，读取时自动解密（旧明文数据读取兼容、下次保存时自动加密）
"""
import base64
import json
import logging
import secrets
from pathlib import Path
from typing import Any, Optional

from .config import DATA_DIR, ensure_dirs

log = logging.getLogger(__name__)

_SECRET_FILE = DATA_DIR / "secret.key"
_SETTINGS_FILE = DATA_DIR / "settings.json"

_ENCRYPTION_UNAVAILABLE = False
try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # 加密库缺失时降级为明文（功能不中断，安全性降低）
    _ENCRYPTION_UNAVAILABLE = True


def encryption_status() -> dict[str, Any]:
    """加密可用性状态（/status 展示：降级时界面提示补装 cryptography）。"""
    if not _ENCRYPTION_UNAVAILABLE:
        return {"available": True, "note": None}
    return {
        "available": False,
        "note": "cryptography 未安装，代理密码以明文存储；请运行 "
                "pip install cryptography 加固",
    }


# ---------------------------------------------------------------- API Key

def _default_settings() -> dict[str, Any]:
    return {
        "api_key_enabled": False,
        "api_key": secrets.token_hex(16),
        # 自托管同步
        "sync_server_enabled": False,
        "sync_token": secrets.token_hex(16),
        # 本节点作为客户端时指向的同步服务器
        "sync_remote_url": "",
        "sync_remote_token": "",
        # 自动同步（已配置远端后每 30 分钟自动 push）
        "auto_sync": False,
        # 全局代理：新建环境默认继承、启动时兜底（密码加密存储）
        "global_proxy": None,
    }


def load_settings() -> dict[str, Any]:
    ensure_dirs()
    if _SETTINGS_FILE.exists():
        try:
            data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("settings.json 顶层不是对象")
            merged = {**_default_settings(), **data}
            # 默认值里有随机密钥（api_key/sync_token）：补齐缺失键后必须落盘，
            # 否则每次读取都会得到不同的随机值（认证/同步随机失败）
            if set(merged) != set(data):
                save_settings(merged)
            return merged
        except Exception:
            log.warning("settings.json 无法解析，已重建（原文件内容将被覆盖）", exc_info=True)
    settings = _default_settings()
    save_settings(settings)
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    ensure_dirs()
    # 先写临时文件再原子替换：直接覆盖写入若被中断会留下半截 JSON，
    # 下次启动会解析失败并重新生成 api_key/sync_token，把已配置的客户端全部锁死
    tmp = _SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_SETTINGS_FILE)


def update_settings(api_key_enabled: Optional[bool] = None, regenerate: bool = False,
                    sync: Optional[dict] = None,
                    global_proxy: Optional[dict] = None,
                    clear_global_proxy: bool = False) -> dict:
    settings = load_settings()
    if api_key_enabled is not None:
        settings["api_key_enabled"] = api_key_enabled
    if regenerate:
        settings["api_key"] = secrets.token_hex(16)
    if sync:
        for key in ("sync_server_enabled", "sync_remote_url", "sync_remote_token",
                     "auto_sync"):
            if key in sync:
                settings[key] = sync[key]
        if "regenerate_sync_token" in sync:
            settings["sync_token"] = secrets.token_hex(16)
    if clear_global_proxy:
        settings["global_proxy"] = None
    elif global_proxy is not None:
        merged = dict(global_proxy)
        if not merged.get("password"):
            # 密码留空表示沿用原密码（接口回显的是掩码，不回传明文）
            old = settings.get("global_proxy") or {}
            merged["password"] = decrypt_text(old.get("password")) or None
        settings["global_proxy"] = encrypt_proxy(merged)
    save_settings(settings)
    return settings


def check_api_key(provided: Optional[str]) -> bool:
    settings = load_settings()
    if not settings.get("api_key_enabled"):
        return True
    return bool(provided) and secrets.compare_digest(provided, settings.get("api_key", ""))


# ---------------------------------------------------------------- 字段加密

def _fernet() -> Optional["Fernet"]:
    if _ENCRYPTION_UNAVAILABLE:
        return None
    ensure_dirs()
    if _SECRET_FILE.exists():
        key = _SECRET_FILE.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        _SECRET_FILE.write_bytes(key)
    try:
        return Fernet(key)
    except Exception:
        return None


def encrypt_text(value: str) -> str:
    f = _fernet()
    if f is None or not value:
        return value
    return "enc:" + f.encrypt(value.encode()).decode()


def decrypt_text(value: Optional[str]) -> Optional[str]:
    if not value or not value.startswith("enc:"):
        return value
    f = _fernet()
    if f is None:
        return None
    try:
        return f.decrypt(value[4:].encode()).decode()
    except InvalidToken:
        return None


def encrypt_proxy(proxy: Optional[dict]) -> Optional[dict]:
    """入库前：加密 password 字段。"""
    if not proxy:
        return proxy
    out = dict(proxy)
    if out.get("password"):
        out["password"] = encrypt_text(str(out["password"]))
    return out


def decrypt_proxy(proxy: Optional[dict]) -> Optional[dict]:
    """出库后：解密 password 字段（兼容明文）。"""
    if not proxy:
        return proxy
    out = dict(proxy)
    if out.get("password"):
        out["password"] = decrypt_text(str(out["password"]))
    return out


def get_global_proxy() -> Optional[dict]:
    """全局代理（明文，含密码）；未配置返回 None。

    供新建环境继承与启动兜底使用。
    """
    return decrypt_proxy(load_settings().get("global_proxy"))


def global_proxy_masked() -> Optional[dict]:
    """全局代理（接口回显用，密码打码）。"""
    proxy = get_global_proxy()
    if not proxy:
        return None
    out = dict(proxy)
    if out.get("password"):
        out["password"] = "****"
    return out
