"""RPA 引擎（Phase 4）：fp-chromium 内核 CDP 直连执行验证。

覆盖：
1. 桌面（windows）环境：navigate/extract/evaluate 基础链路
2. 移动端（android）环境：click/scroll 走触摸语义（真机事件序列：触摸事件 +
   兼容鼠标事件 + click:pointerType=touch；不得出现 pointerdown:mouse / wheel）

  python scripts/rpa_fpchromium_test.py
  python scripts/rpa_fpchromium_test.py --management-url http://127.0.0.1:18080
"""
import asyncio
import argparse
import contextlib
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.fingerprint_engine import create_fingerprint  # noqa: E402
from app.kernels import fp_chromium_kernel  # noqa: E402
from app.launcher import LaunchManager  # noqa: E402
from app.task_engine import TaskEngine  # noqa: E402

PASS, FAIL = [], []

_MOBILE_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0">
<button id="btn" style="display:block;margin:24px auto;padding:16px 32px">点我</button>
<div id="out">idle</div>
<div style="height:3000px"></div>
<script>
window.__ev = [];
['pointerdown','mousedown','touchstart','pointerup','touchend','touchmove','wheel','click']
  .forEach(function (t) {
    document.addEventListener(t, function (e) {
      window.__ev.push(e.type + (e.pointerType !== undefined ? ':' + e.pointerType : ''));
    }, true);
  });
document.getElementById('btn').addEventListener('click', function () {
  document.getElementById('out').textContent = 'clicked';
});
</script>
</body></html>"""

MOBILE_PAGE = "data:text/html;charset=utf-8," + quote(_MOBILE_HTML)


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail and not cond else ""))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--management-url",
        default=os.environ.get("FPWB_MANAGEMENT_URL"),
        help=("通过已运行的管理端执行 RPA 回归（macOS 受限终端推荐）；"
              "例如 http://127.0.0.1:18080"),
    )
    args = parser.parse_args()
    if args.management_url:
        await _run_via_api(args.management_url)
        return

    ok, err = fp_chromium_kernel.is_available()
    if not ok:
        print("fp-chromium 未安装，跳过:", err)
        return
    db.init_db()
    manager = LaunchManager()
    engine = TaskEngine(manager)
    profile = db.create_profile(
        name="rpa-fpc-test", group_name="测试", notes="",
        kernel="fp-chromium", target_os="windows",
        proxy=None, fingerprint=create_fingerprint("windows", "generate"),
        launch={"preset": "standard"},
    )
    steps = [
        {"action": "navigate", "url": "https://example.com", "timeout": 60000},
        {"action": "extract", "selector": "p", "var": "t"},
        {"action": "evaluate", "expression": "document.title", "var": "title"},
        {"action": "evaluate", "expression": "navigator.userAgent", "var": "ua"},
    ]
    task = {"id": "t", "name": "fpc", "steps": steps, "webhook_url": None}
    run_ids = await engine.run(task, [profile], headless=True, auto_close=True)
    for _ in range(60):
        await asyncio.sleep(2)
        r = db.get_run(run_ids[0])
        if r["status"] != "running":
            break
    r = db.get_run(run_ids[0])
    for e in r["results"]:
        print(f"    #{e['index']} {e['action']:10s} {e['status']:8s} {e['detail'][:60]}")
    results = r.get("results") or []
    extracted = results[1].get("extracted") or [] if len(results) > 1 else []
    check("fp-chromium 任务执行成功", r["status"] == "success", r.get("error"))
    check("页面抽取正常（example.com 正文段落非空）",
          bool(extracted) and all(isinstance(v, str) and v.strip() for v in extracted),
          f"{extracted[:2]}")
    check("页面标题为 Example Domain（网络渲染正常）",
          len(results) > 2 and "Example Domain" in str(results[2].get("value")))
    check("UA 为 Chrome 系", len(results) > 3 and "Chrome" in str(results[3].get("value")))

    db.delete_profile(profile["id"])
    shutil.rmtree(Path("profiles") / profile["id"], ignore_errors=True)

    # ---- 移动端（android）：click/scroll 必须走触摸语义（真机事件序列）----
    print("\n== 移动端（android）：click/scroll 触摸语义 ==")
    m_profile = db.create_profile(
        name="rpa-fpc-mobile-test", group_name="测试", notes="",
        kernel="fp-chromium", target_os="android",
        proxy=None, fingerprint=create_fingerprint("android", "generate"),
        launch={"preset": "standard"},
    )
    m_steps = [
        {"action": "navigate", "url": MOBILE_PAGE, "timeout": 60000},
        {"action": "evaluate", "expression": "navigator.maxTouchPoints", "var": "mtp"},
        {"action": "click", "selector": "#btn"},
        {"action": "evaluate",
         "expression": "document.getElementById('out').textContent", "var": "out"},
        {"action": "evaluate", "expression": "window.__ev.join(',')", "var": "ev1"},
        {"action": "scroll", "amount": 600},
        {"action": "evaluate", "expression": "window.scrollY", "var": "sy"},
        {"action": "evaluate", "expression": "window.__ev.join(',')", "var": "ev2"},
    ]
    task_m = {"id": "tm", "name": "fpc-mobile", "steps": m_steps, "webhook_url": None}
    run_ids_m = await engine.run(task_m, [m_profile], headless=True, auto_close=True)
    for _ in range(60):
        await asyncio.sleep(2)
        r2 = db.get_run(run_ids_m[0])
        if r2["status"] != "running":
            break
    r2 = db.get_run(run_ids_m[0])
    for e in r2["results"]:
        print(f"    #{e['index']} {e['action']:10s} {e['status']:8s} {e['detail'][:60]}")
    res2 = r2["results"]
    check("移动端任务执行成功", r2["status"] == "success", r2.get("error"))
    mobile_ok = r2.get("status") == "success"
    mtp = res2[1].get("value") if len(res2) > 1 else None
    out = str(res2[3].get("value") or "") if len(res2) > 3 else ""
    ev1 = str(res2[4].get("value") or "") if len(res2) > 4 else ""
    sy = float(res2[6].get("value") or 0) if len(res2) > 6 else 0
    ev2 = str(res2[7].get("value") or "") if len(res2) > 7 else ""
    check("maxTouchPoints == 5（触控仿真生效）", mtp == 5, f"实际 {mtp}")
    check("click 步骤在触摸语义下完成点击（#out == clicked）",
          out == "clicked", ev1[:120])
    check("点击事件为触摸序列（pointerdown:touch/touchstart/click:touch）",
          mobile_ok and "pointerdown:touch" in ev1 and "touchstart" in ev1
          and "click:touch" in ev1,
          ev1[:140])
    check("点击无桌面指针事件（无 pointerdown:mouse）",
          mobile_ok and "pointerdown:mouse" not in ev1, ev1[:140])
    check("scroll 步骤滚动生效", mobile_ok and sy > 300, f"scrollY={sy}")
    check("滚动为触摸拖拽（有 touchmove、无 wheel）",
          mobile_ok and "touchmove" in ev2 and "wheel" not in ev2, ev2[:160])

    db.delete_profile(m_profile["id"])
    shutil.rmtree(Path("profiles") / m_profile["id"], ignore_errors=True)
    print(f"\n结果：{len(PASS)} 通过，{len(FAIL)} 失败")
    if FAIL:
        sys.exit(1)


async def _api_json(client: httpx.AsyncClient, method: str, path: str,
                    **kwargs) -> dict:
    """Call the local management API and unwrap its standard response."""
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"{method} {path}: {payload.get('msg') or payload}")
    return payload.get("data")


async def _wait_run(client: httpx.AsyncClient, run_id: str) -> dict:
    """Wait for a task run without assuming that failed runs have results."""
    for _ in range(240):
        run = await _api_json(client, "GET", f"/task-runs/{run_id}")
        if run.get("status") not in ("running", "pending"):
            return run
        await asyncio.sleep(0.5)
    raise TimeoutError(f"任务运行超时: {run_id}")


def _print_run(run: dict) -> list[dict]:
    results = run.get("results") or []
    for entry in results:
        print(f"    #{entry.get('index')} {entry.get('action', '?'):10s} "
              f"{entry.get('status', '?'):8s} {str(entry.get('detail') or '')[:60]}")
    return results


async def _run_via_api(management_url: str) -> None:
    """Run both RPA chains through a desktop management service.

    Directly starting a Chromium GUI from a restricted macOS terminal can
    produce SIGABRT before CDP is ready.  The management service owns the
    logged-in GUI session, so this mode exercises the same API path users use
    in production and cleans up its temporary profiles/tasks afterwards.
    """
    api_root = management_url.rstrip("/")
    if not api_root.endswith("/api/v1"):
        api_root += "/api/v1"
    desktop_profile = mobile_profile = None
    desktop_task = mobile_task = None
    async with httpx.AsyncClient(base_url=api_root, timeout=90,
                                 trust_env=False) as client:
        try:
            await _api_json(client, "GET", "/status")
            desktop_profile = await _api_json(client, "POST", "/profiles", json={
                "name": "rpa-fpc-api-desktop",
                "kernel": "fp-chromium", "target_os": "windows",
                "fingerprint_mode": "generate",
                "launch": {"headless": True, "preset": "standard"},
            })
            desktop_task = await _api_json(client, "POST", "/tasks", json={
                "name": "rpa-fpc-api-desktop-task", "steps": [
                    {"action": "navigate", "url": "https://example.com", "timeout": 60000},
                    {"action": "extract", "selector": "p", "var": "t"},
                    {"action": "evaluate", "expression": "document.title", "var": "title"},
                    {"action": "evaluate", "expression": "navigator.userAgent", "var": "ua"},
                ],
            })
            run_info = await _api_json(
                client, "POST", f"/tasks/{desktop_task['id']}/run",
                json={"profile_ids": [desktop_profile["id"]],
                      "headless": True, "auto_close": True},
            )
            desktop_run = await _wait_run(client, run_info["run_ids"][0])
            print("== 桌面（管理端 API） ==")
            desktop_results = _print_run(desktop_run)
            desktop_extracted = (desktop_results[1].get("extracted") or
                                 [] if len(desktop_results) > 1 else [])
            check("fp-chromium 任务执行成功", desktop_run.get("status") == "success",
                  desktop_run.get("error"))
            check("页面抽取正常（example.com 正文段落非空）",
                  bool(desktop_extracted) and all(isinstance(v, str) and v.strip()
                                                  for v in desktop_extracted),
                  str(desktop_extracted[:2]))
            check("页面标题为 Example Domain（网络渲染正常）",
                  len(desktop_results) > 2 and
                  "Example Domain" in str(desktop_results[2].get("value")))
            check("UA 为 Chrome 系", len(desktop_results) > 3 and
                  "Chrome" in str(desktop_results[3].get("value")))

            mobile_profile = await _api_json(client, "POST", "/profiles", json={
                "name": "rpa-fpc-api-mobile", "kernel": "fp-chromium",
                "target_os": "android", "fingerprint_mode": "generate",
                "launch": {"headless": True, "preset": "standard"},
            })
            mobile_task = await _api_json(client, "POST", "/tasks", json={
                "name": "rpa-fpc-api-mobile-task", "steps": [
                    {"action": "navigate", "url": MOBILE_PAGE, "timeout": 60000},
                    {"action": "evaluate", "expression": "navigator.maxTouchPoints", "var": "mtp"},
                    {"action": "click", "selector": "#btn"},
                    {"action": "evaluate", "expression": "document.getElementById('out').textContent", "var": "out"},
                    {"action": "evaluate", "expression": "window.__ev.join(',')", "var": "ev1"},
                    {"action": "scroll", "amount": 600},
                    {"action": "evaluate", "expression": "window.scrollY", "var": "sy"},
                    {"action": "evaluate", "expression": "window.__ev.join(',')", "var": "ev2"},
                ],
            })
            run_info = await _api_json(
                client, "POST", f"/tasks/{mobile_task['id']}/run",
                json={"profile_ids": [mobile_profile["id"]],
                      "headless": True, "auto_close": True},
            )
            mobile_run = await _wait_run(client, run_info["run_ids"][0])
            print("\n== 移动端（管理端 API） ==")
            mobile_results = _print_run(mobile_run)
            mtp = mobile_results[1].get("value") if len(mobile_results) > 1 else None
            out = str(mobile_results[3].get("value") or "") if len(mobile_results) > 3 else ""
            ev1 = str(mobile_results[4].get("value") or "") if len(mobile_results) > 4 else ""
            sy = float(mobile_results[6].get("value") or 0) if len(mobile_results) > 6 else 0
            ev2 = str(mobile_results[7].get("value") or "") if len(mobile_results) > 7 else ""
            check("移动端任务执行成功", mobile_run.get("status") == "success",
                  mobile_run.get("error"))
            check("maxTouchPoints == 5（触控仿真生效）", mtp == 5, f"实际 {mtp}")
            check("click 步骤在触摸语义下完成点击（#out == clicked）",
                  out == "clicked", ev1[:120])
            check("点击事件为触摸序列（pointerdown:touch/touchstart/click:touch）",
                  "pointerdown:touch" in ev1 and "touchstart" in ev1
                  and "click:touch" in ev1, ev1[:140])
            check("点击无桌面指针事件（无 pointerdown:mouse）",
                  "pointerdown:mouse" not in ev1, ev1[:140])
            check("scroll 步骤滚动生效", sy > 300, f"scrollY={sy}")
            check("滚动为触摸拖拽（有 touchmove、无 wheel）",
                  "touchmove" in ev2 and "wheel" not in ev2, ev2[:160])
            print(f"\n结果：{len(PASS)} 通过，{len(FAIL)} 失败")
            if FAIL:
                raise RuntimeError("RPA 回归存在失败项: " + ", ".join(FAIL))
        finally:
            for task in (desktop_task, mobile_task):
                if task:
                    with contextlib.suppress(Exception):
                        await _api_json(client, "DELETE", f"/tasks/{task['id']}")
            for profile in (desktop_profile, mobile_profile):
                if profile:
                    with contextlib.suppress(Exception):
                        await _api_json(client, "POST", "/browser/stop",
                                        json={"profile_id": profile["id"]})
                    with contextlib.suppress(Exception):
                        await _api_json(client, "DELETE", f"/profiles/{profile['id']}")


if __name__ == "__main__":
    asyncio.run(main())
