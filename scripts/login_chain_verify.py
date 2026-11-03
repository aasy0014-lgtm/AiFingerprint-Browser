#!/usr/bin/env python3
"""Local OAuth/login-chain regression for a mobile profile.

The fixture deliberately does not contain an account or a provider URL.  It
checks the browser plumbing that a real login page relies on: a user-gesture
popup, secure-context APIs, Cookie/localStorage/sessionStorage/IndexedDB, and
reuse of the same profile across a restart.  Pass ``--target-os ios`` to run
the same chain against the iOS WebKit-shaped profile.

Usage:
    .venv/bin/python scripts/login_chain_verify.py [--target-os android|ios]
    .venv/bin/python scripts/login_chain_verify.py --management-url http://127.0.0.1:18080
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import PROFILE_ROOT  # noqa: E402
from app.fingerprint_engine import create_fingerprint  # noqa: E402
from app.launcher import LaunchManager  # noqa: E402
from app.readiness import _CdpPage  # noqa: E402


MAIN_HTML = """<!doctype html><meta charset=utf-8><title>Login fixture</title>
<form id=f><input id=email type=email><button id=go type=button>Continue</button></form>
<script>
window.__loginResult = null;
window.__loginInput = null;
addEventListener('message', (event) => {
  if (!event.data || event.data.type !== 'oauth-complete') return;
  localStorage.setItem('fixture-auth', event.data.email);
  sessionStorage.setItem('fixture-session', '1');
  document.cookie = 'fixture-cookie=1; Path=/';
  window.__loginResult = 'ok';
  const req = indexedDB.open('fixture-auth', 1);
  req.onupgradeneeded = () => req.result.createObjectStore('tokens');
  req.onsuccess = () => {
    const tx = req.result.transaction('tokens', 'readwrite');
    tx.objectStore('tokens').put('1', 'session');
    tx.oncomplete = () => {};
  };
});
go.addEventListener('pointerdown', (event) => {
  window.__loginInput = event.pointerType || null;
});
go.onclick = () => window.open('/oauth-popup.html', 'oauth', 'width=420,height=700');
</script>"""

POPUP_HTML = """<!doctype html><script>
window.opener.postMessage({type:'oauth-complete', email:'fixture@example.test'}, '*');
setTimeout(() => window.close(), 50);
</script>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = POPUP_HTML if urlparse(self.path).path == "/oauth-popup.html" else MAIN_HTML
        payload = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        return


async def _wait_healthy(manager: LaunchManager, profile_id: str) -> None:
    for _ in range(100):
        if manager.emulation_state(profile_id) == "ok":
            return
        await asyncio.sleep(0.1)
    raise RuntimeError("移动仿真通道未在 10 秒内就绪")


async def _state(page: _CdpPage) -> dict:
    expression = r"""(async () => JSON.stringify(await (async () => {
      const db = await new Promise((resolve) => {
        const req = indexedDB.open('fixture-auth');
        req.onsuccess = () => resolve(true);
        req.onerror = () => resolve(false);
      });
      return {secure: isSecureContext, cookie: document.cookie,
        local: localStorage.getItem('fixture-auth'),
        session: sessionStorage.getItem('fixture-session'),
        result: window.__loginResult, input: window.__loginInput, db,
        language: navigator.language, languages: navigator.languages,
        intlLocale: Intl.DateTimeFormat().resolvedOptions().locale};
    })()))()"""
    raw = await page.evaluate(expression)
    return json.loads(raw)


async def _user_gesture_click(page: _CdpPage) -> None:
    """Use the same CDP touch path as a mobile profile, not Runtime.evaluate.

    A synthetic ``Runtime.evaluate(userGesture=True)`` proves only that the
    DevTools protocol can grant activation.  The login flow must also survive
    the real touch → click compatibility sequence used by the mobile input
    layer; otherwise an OAuth popup can be blocked even when the readiness
    report looks healthy.
    """
    await page.evaluate(
        "(() => { document.getElementById('email').value = 'fixture@example.test';"
        " window.__loginInput = null; return true; })()"
    )
    rect = await page.evaluate(
        "(() => { const r = document.getElementById('go').getBoundingClientRect();"
        " return {x:r.left + r.width / 2, y:r.top + r.height / 2}; })()"
    )
    if not isinstance(rect, dict):
        raise RuntimeError("登录按钮坐标读取失败")
    x = float(rect.get("x") or 0)
    y = float(rect.get("y") or 0)
    if x <= 0 or y <= 0:
        raise RuntimeError("登录按钮不可点击")
    stamp = time.time()
    point = {"x": x, "y": y, "radiusX": 4, "radiusY": 4,
             "force": 0.7, "id": 1}
    await page._send("Input.dispatchTouchEvent", {
        "type": "touchStart", "timestamp": stamp, "touchPoints": [point],
    })
    await asyncio.sleep(0.06)
    await page._send("Input.dispatchTouchEvent", {
        "type": "touchEnd", "timestamp": stamp + 0.06, "touchPoints": [],
    })


def _assert_chain(before: dict, after: dict, after_restart: dict) -> None:
    """Assert the durable/session state contract shared by both transports."""
    assert before["secure"] and before["db"]
    for snapshot in (before, after):
        assert snapshot["language"] == "en-US"
        assert snapshot["languages"] == ["en-US", "en"]
        assert snapshot["intlLocale"] == "en-US"
    assert after["result"] == "ok"
    assert after["input"] == "touch"
    assert after["cookie"] == "fixture-cookie=1"
    assert after["local"] == "fixture@example.test" and after["db"]
    assert after_restart["cookie"] == "fixture-cookie=1"
    assert after_restart["local"] == "fixture@example.test"
    assert after_restart["language"] == "en-US"
    assert after_restart["languages"] == ["en-US", "en"]
    assert after_restart["intlLocale"] == "en-US"
    assert after_restart["db"] and after_restart["session"] is None


async def _run(target_os: str) -> dict:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    start_url = f"http://127.0.0.1:{server.server_port}/"
    profile_id = f"login-chain-{target_os}-fixture"
    shutil.rmtree(PROFILE_ROOT / profile_id, ignore_errors=True)
    profile = {
        "id": profile_id,
        "name": profile_id,
        "kernel": "fp-chromium",
        "target_os": target_os,
        "proxy": None,
        "fingerprint": create_fingerprint(
            target_os, "preset", "Apple" if target_os == "ios" else "Google",
            browser="safari" if target_os == "ios" else "chrome",
        ),
        # Use a non-default locale deliberately: the fixture verifies that a
        # login page and its OAuth popup see the same Intl/Navigator language
        # instead of the profile's historical zh-CN array.
        "launch": {"headless": True, "preset": "login", "locale": "en-US",
                   "start_url": start_url},
    }
    manager = LaunchManager()
    try:
        await manager.start(profile, headless=True, start_url=start_url)
        await _wait_healthy(manager, profile_id)
        inst = manager.get_instance(profile_id)
        page = _CdpPage(inst.info["debug_port"])
        await page.open()
        await page.goto(start_url)
        before = await _state(page)
        await _user_gesture_click(page)
        await asyncio.sleep(1)
        after = await _state(page)
        await page.close()
        await manager.stop(profile_id)

        # A fresh session object must retain durable login state while
        # sessionStorage is reset by the browser context, matching real OAuth.
        await manager.start(profile, headless=True, start_url=start_url)
        await _wait_healthy(manager, profile_id)
        inst = manager.get_instance(profile_id)
        page = _CdpPage(inst.info["debug_port"])
        await page.open()
        await page.goto(start_url)
        after_restart = await _state(page)
        await page.close()
        await manager.stop(profile_id)

        _assert_chain(before, after, after_restart)
        return {"target_os": target_os, "start_url": start_url,
                "before": before, "after_login": after,
                "after_restart": after_restart, "pass": True}
    finally:
        with contextlib.suppress(Exception):
            await manager.stop(profile_id)
        server.shutdown()
        shutil.rmtree(PROFILE_ROOT / profile_id, ignore_errors=True)


async def _api_json(client: httpx.AsyncClient, method: str, path: str,
                    **kwargs) -> dict:
    """Call the local management API and unwrap its standard response."""
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"{method} {path}: {payload.get('msg') or payload}")
    return payload.get("data")


async def _run_via_api(target_os: str, management_url: str) -> dict:
    """Run the same fixture through an already-running management service.

    macOS can abort a Chromium GUI process when a script is launched from a
    restricted/non-Aqua terminal.  The desktop management service is normally
    started from the logged-in session and already owns the correct launch
    context, so this transport keeps the regression useful in CI/agent
    terminals without weakening the browser launch path itself.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    start_url = f"http://127.0.0.1:{server.server_port}/"
    api_root = management_url.rstrip("/")
    if not api_root.endswith("/api/v1"):
        api_root += "/api/v1"
    profile_id: str | None = None
    page: _CdpPage | None = None
    async with httpx.AsyncClient(base_url=api_root, timeout=90,
                                 trust_env=False) as client:
        try:
            await _api_json(client, "GET", "/status")
            body = {
                "name": f"login-chain-{target_os}-api-fixture",
                "kernel": "fp-chromium",
                "target_os": target_os,
                "fingerprint_mode": "preset",
                "manufacturer": "Apple" if target_os == "ios" else "Google",
                "browser": "safari" if target_os == "ios" else "chrome",
                "launch": {
                    "headless": True, "preset": "login", "locale": "en-US",
                    "start_url": start_url,
                },
            }
            profile = await _api_json(client, "POST", "/profiles", json=body)
            profile_id = str(profile["id"])

            info = await _api_json(
                client, "POST", "/browser/start",
                json={"profile_id": profile_id, "headless": True,
                      "start_url": start_url},
            )
            page = _CdpPage(int(info["debug_port"]))
            await page.open()
            await page.goto(start_url)
            before = await _state(page)
            await _user_gesture_click(page)
            await asyncio.sleep(1)
            after = await _state(page)
            await page.close()
            page = None
            await _api_json(client, "POST", "/browser/stop",
                            json={"profile_id": profile_id})

            info = await _api_json(
                client, "POST", "/browser/start",
                json={"profile_id": profile_id, "headless": True,
                      "start_url": start_url},
            )
            page = _CdpPage(int(info["debug_port"]))
            await page.open()
            await page.goto(start_url)
            after_restart = await _state(page)
            await page.close()
            page = None
            await _api_json(client, "POST", "/browser/stop",
                            json={"profile_id": profile_id})

            _assert_chain(before, after, after_restart)
            return {
                "target_os": target_os, "transport": "management-api",
                "management_url": management_url, "start_url": start_url,
                "before": before, "after_login": after,
                "after_restart": after_restart, "pass": True,
            }
        finally:
            if page is not None:
                with contextlib.suppress(Exception):
                    await page.close()
            if profile_id is not None:
                with contextlib.suppress(Exception):
                    await _api_json(client, "POST", "/browser/stop",
                                    json={"profile_id": profile_id})
                with contextlib.suppress(Exception):
                    await _api_json(client, "DELETE", f"/profiles/{profile_id}")
            server.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-os", choices=("android", "ios"), default="android")
    parser.add_argument(
        "--management-url",
        default=os.environ.get("FPWB_MANAGEMENT_URL"),
        help=("通过已运行的管理端完成启动（macOS 受限终端推荐）；"
              "例如 http://127.0.0.1:18080"),
    )
    args = parser.parse_args()
    if args.management_url:
        result = asyncio.run(_run_via_api(args.target_os, args.management_url))
    else:
        result = asyncio.run(_run(args.target_os))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
