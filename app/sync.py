"""自托管环境同步：push / pull / LWW / 删除传播。

模型：任意一个工作台实例可在设置中开启"同步服务器"（用 sync_token 保护），
其它实例配置 remote_url + remote_token 后即可：

- push：把本机全部环境配置（含指纹，不含数据目录/密钥）与删除墓碑推到服务器
- pull：从服务器拉取全量配置与墓碑，按 rev（updated_at）LWW 合并

用途：多台机器维护同一矩阵、本地开发机上云服务器备份配置、团队分发环境。
注意：Cookie 等数据目录不同步（体积与安全考量）；同步内容包含代理凭据明文，
服务器必须部署在可信网络并开启 token。
"""
import logging
from typing import Any

import httpx

from . import db, security
from .proxy_dns import is_lan_target

log = logging.getLogger(__name__)

SYNC_TIMEOUT = 60


def _profile_payload(p: dict) -> dict[str, Any]:
    return {
        "sync_id": p["sync_id"],
        "rev": p.get("rev") or p["updated_at"],
        "name": p["name"],
        "group_name": p["group_name"],
        "notes": p["notes"],
        "kernel": p["kernel"],
        "target_os": p["target_os"],
        "proxy": p["proxy"],          # 解密后明文（传输需 HTTPS 或可信内网）
        "fingerprint": p["fingerprint"],
        "launch": p["launch"],
        "owner": p.get("owner", "admin"),
    }


# ---------------------------------------------------------------- 删除墓碑

def _deletes_payload() -> tuple[list[str], dict[str, str]]:
    """本地墓碑 → (sync_id 列表, sync_id→deleted_at 映射)。"""
    rows = db.list_sync_deletes()
    return [r["sync_id"] for r in rows], {r["sync_id"]: r["deleted_at"] for r in rows}


def _deletes_map(body: dict) -> dict[str, str]:
    """解析删除载荷：deletes（sync_id 列表）+ deletes_ts（时间戳映射）。

    兼容老版本只发 deletes 的情况（无时间戳 → 按"当前时间"处理，退回删除优先）。
    """
    ts = body.get("deletes_ts") or {}
    out: dict[str, str] = {}
    for item in body.get("deletes") or []:
        if isinstance(item, dict):  # 容忍直接给对象的形式
            sid = item.get("sync_id")
            if sid:
                out[sid] = item.get("deleted_at") or ts.get(sid, "")
        else:
            out[str(item)] = ts.get(str(item), "")
    for sid, t in ts.items():
        out.setdefault(sid, t)
    return out


def _apply_deletes(body: dict, stats: dict) -> None:
    """应用删除墓碑（LWW）。

    仅当墓碑不早于本地 rev 时才删除；本地版本更新则保留本地并忽略该删除
    （此时也不落墓碑，避免残留旧墓碑在日后把更新的版本又覆盖掉）。
    """
    for sync_id, ts in _deletes_map(body).items():
        local = db.get_profile_by_sync_id(sync_id)
        if local and ts and (local.get("rev") or "") > ts:
            continue
        if local:
            db.delete_profile(local["id"], tombstone=False)
            stats["deleted"] += 1
        db.record_sync_delete(sync_id, ts)


# ---------------------------------------------------------------- 服务器侧

def server_handle_upload(body: dict) -> dict[str, Any]:
    """同步服务器接收端：LWW 合并远端环境与删除墓碑。"""
    stats = {"created": 0, "updated": 0, "skipped": 0, "deleted": 0}
    for item in body.get("profiles", []):
        result = db.upsert_profile_by_sync(
            sync_id=item["sync_id"], rev=item["rev"], name=item["name"],
            group_name=item.get("group_name", "默认分组"), notes=item.get("notes", ""),
            kernel=item.get("kernel", "camoufox"), target_os=item.get("target_os", "windows"),
            proxy=item.get("proxy"), fingerprint=item.get("fingerprint") or
            {"mode": "generate", "seeds": {}},
            launch=item.get("launch") or {"start_url": "about:blank"},
            owner=item.get("owner", "admin"),
        )
        stats[result] += 1
    # 墓碑保留在服务器上（供其它节点拉取传播），仅在更新版本 profile 同步进来时才清除
    _apply_deletes(body, stats)
    return stats


def server_handle_download() -> dict[str, Any]:
    profiles = [_profile_payload(p) for p in db.list_profiles()]
    delete_ids, deletes_ts = _deletes_payload()
    return {"profiles": profiles, "deletes": delete_ids,
            "deletes_ts": deletes_ts, "count": len(profiles)}


# ---------------------------------------------------------------- 客户端侧

async def push_to_remote() -> dict[str, Any]:
    settings = security.load_settings()
    url = settings.get("sync_remote_url", "").rstrip("/")
    token = settings.get("sync_remote_token", "")
    if not url or not token:
        raise ValueError("请先在设置中配置同步服务器地址与令牌")
    delete_ids, deletes_ts = _deletes_payload()
    payload = {
        "node": "fpworkbench",
        "profiles": [_profile_payload(p) for p in db.list_profiles()],
        "deletes": delete_ids,
        "deletes_ts": deletes_ts,
    }
    async with httpx.AsyncClient(timeout=SYNC_TIMEOUT,
                                 trust_env=not is_lan_target(url)) as client:
        r = await client.post(f"{url}/api/sync/upload", json=payload,
                              headers={"X-Sync-Token": token})
        r.raise_for_status()
        result = r.json()
        if isinstance(result, dict) and "data" in result:  # 解开标准响应包
            result = result["data"]
    # 保留本地墓碑：其它节点可能尚未拉取，清掉后其旧副本一旦推送就会让删除被"复活"
    return result


async def pull_from_remote() -> dict[str, Any]:
    settings = security.load_settings()
    url = settings.get("sync_remote_url", "").rstrip("/")
    token = settings.get("sync_remote_token", "")
    if not url or not token:
        raise ValueError("请先在设置中配置同步服务器地址与令牌")
    async with httpx.AsyncClient(timeout=SYNC_TIMEOUT,
                                 trust_env=not is_lan_target(url)) as client:
        r = await client.get(f"{url}/api/sync/download",
                             headers={"X-Sync-Token": token})
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and "data" in data:  # 解开标准响应包
            data = data["data"]

    stats = {"created": 0, "updated": 0, "skipped": 0, "deleted": 0}
    for item in data.get("profiles", []):
        result = db.upsert_profile_by_sync(
            sync_id=item["sync_id"], rev=item["rev"], name=item["name"],
            group_name=item.get("group_name", "默认分组"), notes=item.get("notes", ""),
            kernel=item.get("kernel", "camoufox"), target_os=item.get("target_os", "windows"),
            proxy=item.get("proxy"), fingerprint=item.get("fingerprint") or
            {"mode": "generate", "seeds": {}},
            launch=item.get("launch") or {"start_url": "about:blank"},
            owner=item.get("owner", "admin"),
        )
        stats[result] += 1
    _apply_deletes(data, stats)
    return stats
