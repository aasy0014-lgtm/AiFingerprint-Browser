#!/usr/bin/env python3
"""Worker 全局 API 面探针（诊断用，P2-12 配套）。

用途：验证「平台 API 差集」在 **Worker 全局作用域**的抹除时机与结果。核心实测
结论（本探针的证据输出）：

1. Worker 全局对象上的接口对象（FileSystemObserver / IdleDetector /
   WebSocketStream / ImageDecoder / Scheduler / scheduler）在「暂停态注入」与
   「注入侧定时器」阶段都还不存在（`typeof === 'undefined'`），它们是在
   **Worker 首脚本开始执行时**才安装的——暂停态删除会落空、随后被装回；
2. 因此 `_attach_worker` 挂 `beforeScriptExecution` 插桩断点，在「脚本首行
   执行前」补删（`build_worker_drop_patch`）——修复后 Worker 脚本**首行**
   读到的已是 `undefined`。

读数结构：`timing` = Worker 脚本首行 / 微任务 / 定时器三个时机的
`typeof self.FileSystemObserver`；`names` = 逐名 `[typeof, in, own 描述符]`。

用法: .venv/bin/python scripts/worker_global_probe.py [android|ios] [market_name]
"""
import asyncio
import copy
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("FPWB_HOME", tempfile.mkdtemp(prefix="fpwb_wkprobe_"))

target_os = sys.argv[1] if len(sys.argv) > 1 else "android"
device_name = sys.argv[2] if len(sys.argv) > 2 else (
    "Xiaomi 15" if target_os == "android" else "iPhone 17")

lib = json.loads((REPO / "app/data/mobile_devices.json").read_text(encoding="utf-8"))
DEV = next(d for d in lib["devices"] if d["market_name"] == device_name)

import app.mobile_fingerprints as mf  # noqa: E402

mf._pick_device = lambda target_os, manufacturer: copy.deepcopy(DEV)

from app.config import ensure_dirs  # noqa: E402
from app.fingerprint_engine import create_fingerprint  # noqa: E402
from app.launcher import LaunchManager  # noqa: E402

PAGE = ("<!doctype html><html><head><meta charset='utf-8'><title>wk</title>"
        "</head><body>worker probe</body></html>")

# 名单 = Worker 全局差集（双平台并集）+ 两个「必须保持」的正对照
WORKER_NAMES = [
    "IdleDetector", "WebSocketStream", "ImageDecoder", "FileSystemObserver",
    "Scheduler", "scheduler", "VideoDecoder", "BarcodeDetector",
]

WORKER_JS = r"""() => new Promise((resolve, reject) => {
  const names = __NAMES__;
  const src = 'self.__t = {};'
    + ' self.__t.firstLine = String(typeof self.FileSystemObserver);'
    + ' Promise.resolve().then(function () { self.__t.micro = String(typeof self.FileSystemObserver); });'
    + ' setTimeout(function () { self.__t.timer = String(typeof self.FileSystemObserver); }, 0);'
    + ' self.onmessage = function () {'
    + '   const names = ' + JSON.stringify(names) + ';'
    + '   const out = { timing: self.__t, names: {} };'
    + '   for (const n of names) {'
    + '     const d = Object.getOwnPropertyDescriptor(self, n);'
    + '     out.names[n] = {'
    + '       typeof: String(typeof self[n]),'
    + '       in: (n in self),'
    + '       own: d ? [d.writable, d.enumerable, d.configurable,'
    + '                 ("value" in d) ? "data" : "accessor"] : null,'
    + '     };'
    + '   }'
    + '   postMessage(out);'
    + ' };';
  try {
    const w = new Worker(URL.createObjectURL(new Blob([src])));
    const timer = setTimeout(() => reject(new Error('worker timeout')), 8000);
    w.onmessage = (e) => { clearTimeout(timer); resolve(e.data); };
    w.onerror = () => { clearTimeout(timer); reject(new Error('worker error')); };
    w.postMessage(1);
  } catch (e) { reject(e); }
})""".replace("__NAMES__", json.dumps(WORKER_NAMES))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


async def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"

    fp = create_fingerprint(target_os, "generate")
    print(f"== Worker 全局面探针: {fp['manufacturer']} {fp['device_model']} ==",
          file=sys.stderr)

    profile = {
        "id": f"wkprobe-{target_os}",
        "name": f"wkprobe-{target_os}",
        "kernel": "fp-chromium",
        "target_os": target_os,
        "fingerprint": fp,
        "launch": {
            "headless": True, "geoip": False, "humanize": False,
            "locale": "zh-CN", "timezone": "America/Los_Angeles",
            "start_url": url,
        },
        "proxy": None,
    }
    manager = LaunchManager()
    try:
        res = await manager.start(profile)
        await asyncio.sleep(6)

        from playwright.async_api import async_playwright
        pw = await async_playwright().start()
        try:
            browser = await pw.chromium.connect_over_cdp(res["ws_endpoint"])
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            except Exception as exc:
                print("goto warn:", exc, file=sys.stderr)
            out = await page.evaluate(WORKER_JS)
            out["target_os"] = target_os
            print(json.dumps(out, ensure_ascii=False, indent=1))
            try:
                await browser.close()
            except Exception:
                pass
        finally:
            await pw.stop()
    finally:
        try:
            await manager.stop(profile["id"])
        except Exception:
            pass
        server.shutdown()
    return 0


if __name__ == "__main__":
    ensure_dirs()
    raise SystemExit(asyncio.run(main()))
