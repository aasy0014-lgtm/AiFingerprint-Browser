#!/usr/bin/env python3
"""Live account-login surface probe for a temporary mobile environment.

The probe does not submit credentials.  It opens the account sign-in surface,
reads only public page/runtime signals, and then removes the temporary profile.
It is intentionally management-API based so macOS GUI registration stays in the
already-running management process.

Usage::

    .venv/bin/python scripts/google_login_probe.py \
      --management-url http://127.0.0.1:18080

``--start-url`` may be a stale ``crossflowverification/samedevice`` or
``devicephoneverification/{initiate,consent}`` URL.  The launcher normalizes
those one-time pages to the account root while keeping normal sign-in URLs
unchanged.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright


DEFAULT_START_URL = "https://accounts.google.com/"
DEFAULT_MANAGEMENT_URL = "http://127.0.0.1:18080"
DEFAULT_MODEL = "Xiaomi 15 Ultra"

STATE_JS = r"""() => {
  const n = navigator;
  const langs = Array.from(n.languages || []);
  return {
    secure: !!window.isSecureContext,
    ua: n.userAgent || '',
    platform: n.platform || '',
    webdriver: n.webdriver,
    language: n.language || '',
    languages: langs,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || '',
    touch: Number(n.maxTouchPoints || 0),
    touchEvents: 'ontouchstart' in window,
    pointerCoarse: !!matchMedia('(pointer: coarse)').matches,
    anyHover: !!matchMedia('(any-hover: hover)').matches,
    uaCh: n.userAgentData ? {
      platform: n.userAgentData.platform,
      mobile: n.userAgentData.mobile,
    } : null,
  };
}"""


def _is_stale_device_url(url: str) -> bool:
    raw = str(url or "")
    try:
        parsed = urlsplit(raw)
        path = (parsed.path or "").lower().rstrip("/")
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        path = raw.lower()
        host = ""
    if host and host != "accounts.google.com":
        return False
    # Keep the probe's classification in sync with the launcher: lifecycle
    # routes append an action (initiate/consent) after the marker, so a plain
    # startswith/endswith check misses some one-time device pages.
    segments = tuple(part for part in path.split("/") if part)
    markers = (
        ("crossflowverification", "samedevice"),
        ("crossflowverification", "devicephoneverification"),
        ("devicephoneverification",),
        ("info", "sessionexpired"),
    )
    return any(
        any(segments[i:i + len(marker)] == marker
            for i in range(len(segments) - len(marker) + 1))
        for marker in markers
    )


def _page_surface(title: str, body: str, url: str) -> dict[str, Any]:
    text = f"{title}\n{body}".lower()
    signup_surface = bool(
        "create a google account" in text
        and ("enter your name" in text or "first name" in text
             or "enter your birthday" in text)
    )
    return {
        "signin_surface": bool(
            "sign in" in text and ("email or phone" in text or "use your google account" in text)
        ),
        "signup_surface": signup_surface,
        "entry_surface": bool(
            ("sign in" in text and
             ("email or phone" in text or "use your google account" in text))
            or signup_surface
        ),
        "same_device_challenge": bool(
            re.search(
                r"same[- ]device|device verification|verify it(?:'|’)s you|"
                r"verify your phone number|phone verification|session ended|"
                r"session expired",
                text,
            )
        ) or _is_stale_device_url(url),
        "browser_error": str(url).startswith("chrome-error:"),
    }


async def _json(client: httpx.AsyncClient, method: str, path: str,
                **kwargs: Any) -> dict[str, Any]:
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(payload.get("msg") or f"API {method} {path} failed")
    return payload["data"]


async def run_probe(management_url: str, start_url: str, manufacturer: str,
                    device_model: str, headless: bool, wait_seconds: float) -> dict[str, Any]:
    profile_id: str | None = None
    async with httpx.AsyncClient(base_url=management_url.rstrip("/"), timeout=120,
                                 trust_env=False) as client:
        profile = await _json(client, "POST", "/api/v1/profiles", json={
            "name": "google-login-probe-temp",
            "kernel": "fp-chromium",
            "target_os": "android",
            "fingerprint_mode": "preset",
            "manufacturer": manufacturer,
            "device_model": device_model,
            "launch": {
                "preset": "login",
                "headless": headless,
                "geoip": True,
                "humanize": False,
                "start_url": start_url,
            },
        })
        profile_id = profile["id"]
        try:
            info = await _json(client, "POST", "/api/v1/browser/start",
                               json={"profile_id": profile_id, "headless": headless})
            async with async_playwright() as playwright:
                browser = await playwright.chromium.connect_over_cdp(info["ws_endpoint"])
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()
                await page.wait_for_timeout(max(0, int(wait_seconds * 1000)))
                url = page.url
                title = await page.title()
                body = (await page.locator("body").inner_text(timeout=15_000))[:4000]
                state = await page.evaluate(STATE_JS)
                surface = _page_surface(title, body, url)
                checks = {
                    "secure": state.get("secure") is True,
                    "webdriver_clear": state.get("webdriver") is False,
                    "android_ua": "Android" in str(state.get("ua") or ""),
                    "mobile_ua_ch": bool((state.get("uaCh") or {}).get("mobile"))
                    and (state.get("uaCh") or {}).get("platform") == "Android",
                    "touch_surface": int(state.get("touch") or 0) >= 1
                    and state.get("touchEvents") is True
                    and state.get("pointerCoarse") is True
                    and state.get("anyHover") is False,
                    "locale_consistent": bool(state.get("language"))
                    and list(state.get("languages") or [""])[0] == state.get("language"),
                    # ``/signup`` intentionally lands on the name step rather
                    # than the sign-in form.  Both are valid account-service
                    # entry surfaces; keeping the raw fields above avoids
                    # conflating the two in future diagnostics.
                    "entry_surface": surface["entry_surface"],
                    "stale_device_normalized": (
                        not _is_stale_device_url(start_url)
                        or not _is_stale_device_url(url)
                    ),
                    "browser_error_clear": not surface["browser_error"],
                    "same_device_challenge_clear": not surface["same_device_challenge"],
                }
                result = {
                    "profile_id": profile_id,
                    "start_url": start_url,
                    "final_url": url,
                    "title": title,
                    "state": state,
                    "surface": surface,
                    "checks": checks,
                    "pass": all(checks.values()),
                }
                await browser.close()
                return result
        finally:
            try:
                await _json(client, "POST", "/api/v1/browser/stop",
                            json={"profile_id": profile_id})
            finally:
                await _json(client, "DELETE", f"/api/v1/profiles/{profile_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--management-url", default=os.environ.get(
        "FPWB_MANAGEMENT_URL", DEFAULT_MANAGEMENT_URL))
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--manufacturer", default="Xiaomi")
    parser.add_argument("--device-model", default=DEFAULT_MODEL)
    parser.add_argument("--headless", action="store_true",
                        help="用无头模式探测；登录回归默认使用有头模式")
    parser.add_argument("--wait", type=float, default=6.0)
    args = parser.parse_args()
    result = asyncio.run(run_probe(
        args.management_url, args.start_url, args.manufacturer,
        args.device_model, args.headless, args.wait))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
