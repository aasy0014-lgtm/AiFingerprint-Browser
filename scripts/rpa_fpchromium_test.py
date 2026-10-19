"""RPA 引擎（Phase 4）：fp-chromium 内核 CDP 直连执行验证。

覆盖：
1. 桌面（windows）环境：navigate/extract/evaluate 基础链路
2. 移动端（android）环境：click/scroll 走触摸语义（真机事件序列：触摸事件 +
   兼容鼠标事件 + click:pointerType=touch；不得出现 pointerdown:mouse / wheel）

  python scripts/rpa_fpchromium_test.py
"""
import asyncio
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

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
    extracted = r["results"][1].get("extracted") or []
    check("fp-chromium 任务执行成功", r["status"] == "success", r.get("error"))
    check("页面抽取正常（example.com 正文段落非空）",
          bool(extracted) and all(isinstance(v, str) and v.strip() for v in extracted),
          f"{extracted[:2]}")
    check("页面标题为 Example Domain（网络渲染正常）",
          "Example Domain" in str(r["results"][2].get("value")))
    check("UA 为 Chrome 系", "Chrome" in str(r["results"][3].get("value")))

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
    mtp = res2[1].get("value")
    ev1 = str(res2[4].get("value") or "")
    sy = float(res2[6].get("value") or 0)
    ev2 = str(res2[7].get("value") or "")
    check("maxTouchPoints == 5（触控仿真生效）", mtp == 5, f"实际 {mtp}")
    check("click 步骤在触摸语义下完成点击（#out == clicked）",
          str(res2[3].get("value")) == "clicked", ev1[:120])
    check("点击事件为触摸序列（pointerdown:touch/touchstart/click:touch）",
          "pointerdown:touch" in ev1 and "touchstart" in ev1 and "click:touch" in ev1,
          ev1[:140])
    check("点击无桌面指针事件（无 pointerdown:mouse）", "pointerdown:mouse" not in ev1,
          ev1[:140])
    check("scroll 步骤滚动生效", sy > 300, f"scrollY={sy}")
    check("滚动为触摸拖拽（有 touchmove、无 wheel）",
          "touchmove" in ev2 and "wheel" not in ev2, ev2[:160])

    db.delete_profile(m_profile["id"])
    shutil.rmtree(Path("profiles") / m_profile["id"], ignore_errors=True)
    print(f"\n结果：{len(PASS)} 通过，{len(FAIL)} 失败")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
