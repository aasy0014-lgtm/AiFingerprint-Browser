#!/usr/bin/env python3
"""在同一环境中逐站采样指纹检测页，输出可复现的差异摘要。

默认创建一次性 Android 环境，避免改动现有登录环境；传入 ``--profile-id``
时才复用指定环境。脚本只读取页面公开信息，不输入账号、不提交表单。需要显式点击的
隐私页会标记 ``manual scan required``，不会把静态帮助文案误判为环境失败。

用法：
    .venv/bin/python scripts/detect_sites_probe.py
    .venv/bin/python scripts/detect_sites_probe.py --profile-id PROFILE_ID
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import DETECT_LINKS


BASE = "http://127.0.0.1:18080/api/v1"

# These signals are useful for regression reports, but they are not failures
# for a mobile Chrome profile.  CreepJS uses a desktop-Blink rule set and marks
# the absence of the desktop PDF plug-in surface (and a taskbar) as
# "headless".  Android Chrome intentionally has the same surface: the
# Chromium Android build has no PDF plug-ins/mime types and a phone has no
# taskbar.  Keep the raw values in the JSON report, while separating them from
# actionable cross-context inconsistencies.
_CREEPJS_ANDROID_EXPECTED_FLAGS = frozenset({
    "noPlugins", "noMimeTypes", "pdfIsDisabled", "noTaskbar",
    # A light mobile theme has the same CSS system-color and media-query
    # answers as the real Android/iOS Chrome baseline.  CreepJS counts both
    # as headless hints even though neither describes automation.
    "hasKnownBgColor", "prefersLightColor",
})


def _is_mobile_sample(row: dict[str, Any]) -> bool:
    """Return whether a probe row describes an Android/iOS user agent."""
    if bool(row.get("uaChMobile")):
        return True
    ua = str(row.get("ua") or "")
    return bool(re.search(r"Android|iPhone|iPad|iPod", ua, re.I))


def _classify_creepjs_signal(row: dict[str, Any], signal: dict[str, Any]) -> None:
    """Annotate CreepJS flags without hiding the raw site result.

    CreepJS's percentage is intentionally left untouched.  The additional
    ``expectedFlags``/``actionableFlags`` fields let callers distinguish a
    mobile-baseline result from a genuine headless/stealth inconsistency.
    """
    flags = signal.get("likeHeadlessFlags")
    if not isinstance(flags, dict):
        return
    truthy = sorted(key for key, value in flags.items() if value)
    expected = sorted(key for key in truthy
                      if _is_mobile_sample(row)
                      and key in _CREEPJS_ANDROID_EXPECTED_FLAGS)
    actionable = sorted(key for key in truthy if key not in expected)
    signal["expectedFlags"] = expected
    signal["actionableFlags"] = actionable
    signal["mobileBaseline"] = bool(_is_mobile_sample(row) and expected)


def _add_summary_item(summary: list[dict[str, Any]], name: str,
                      issues: list[str], notes: list[str]) -> None:
    """Append a compact summary row, retaining notes separately from issues."""
    if issues or notes:
        item: dict[str, Any] = {"name": name}
        if issues:
            item["issues"] = list(dict.fromkeys(issues))
        if notes:
            item["notes"] = list(dict.fromkeys(notes))
        summary.append(item)
SAMPLE_JS = r"""(() => {
  const nav = navigator;
  let webgl = '';
  try {
    const gl = document.createElement('canvas').getContext('webgl');
    const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
    webgl = ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : '';
  } catch (_) {}
  let canvas = '';
  try {
    const c = document.createElement('canvas'); c.width = 160; c.height = 32;
    const x = c.getContext('2d'); x.font = '14px Arial'; x.fillText('fpwb-probe', 2, 20);
    const s = c.toDataURL(); let h = 0;
    for (const ch of s) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
    canvas = `${s.length}:${h}`;
  } catch (_) {}
  const ch = nav.userAgentData || null;
  const resolved = (() => {
    try { return Intl.DateTimeFormat().resolvedOptions(); } catch (_) { return {}; }
  })();
  const bodyText = (document.body?.innerText || '').replace(/\s+/g, ' ').trim();
  const bodyLines = (document.body?.innerText || '').split(/\n+/)
    .map(s => s.replace(/\s+/g, ' ').trim()).filter(Boolean);
  const viewportMeta = document.querySelector('meta[name="viewport" i]');
  const keywords = /(inconsistent|mismatch|detected|leak|warning|risk|unique|bot|automation|mac|apple|desktop|failed|not supported|suspicious)/i;
  const alerts = [...new Set(bodyLines.filter(s => keywords.test(s)))].slice(0, 20);
  const resultRows = [...document.querySelectorAll('td.result')].map(td => ({
    id: td.id || '', text: (td.innerText || '').replace(/\s+/g, ' ').trim(),
    className: td.className || '',
  }));
  const failedRows = resultRows.filter(x => /failed|fail/i.test(x.className + ' ' + x.text));
  const warnRows = resultRows.filter(x => /warn/i.test(x.className + ' ' + x.text));
  const diagnostics = {
    failedRows: failedRows.slice(0, 30), warnRows: warnRows.slice(0, 30),
    canvasMacHint: /It's very likely[^.]*Mac[^.]*\.?/i.exec(bodyText)?.[0] || null,
    canvasUniqueness: /Uniqueness\s+[^\n]{0,100}/i.exec(bodyText)?.[0] || null,
    canvasNoise: /Canvas noise detected(?:\s*\([^)]{0,80}\))?/i.exec(bodyText)?.[0] || null,
    // 只取页面实际结果，不把“Good result: ... classic mismatch”这类帮助文案
    // 当成当前环境失败。
    timezoneMismatch: null,
  };
  const siteSignals = {};
  // Some public checkers put a Cloudflare interstitial in the tab instead of
  // running their fingerprint code.  Preserve that distinction explicitly so
  // a challenge/verification page is reported as an unavailable sample, not
  // as a clean browser result or a fingerprint failure.
  if (/creepjs\.org$/i.test(location.hostname)
      && /security verification|performing security verification|just a moment/i.test(bodyText)) {
    siteSignals.cloudflareChallenge = true;
  }
  // CleanIP occasionally returns an HTML 500 shell or an empty document while
  // closing the remote connection.  It is a network/site availability note;
  // there are no browser signals to classify in that response.
  if (/cleanip\.io$/i.test(location.hostname)
      && (!bodyText || /^500\s*[·•-]/i.test(document.title || ''))) {
    siteSignals.cleanIpUnavailable = true;
  }
  // A reachable CleanIP shell still waits for the visitor to press its
  // ``Start Scan`` control.  Keep that state separate from a completed scan;
  // the landing-page copy alone contains no fingerprint verdict.
  if (/cleanip\.io$/i.test(location.hostname)
      && [...document.querySelectorAll('button,a,[role="button"]')]
        .some(el => /start\s+scan/i.test(el.innerText || ''))) {
    siteSignals.cleanIpManualScan = true;
  }
  // BrowserScan has used two result formats: older pages include the
  // ``NoDetection``/``Detected`` token, while the current page only prints a
  // percentage.  Accept both and keep an absent token as ``null`` rather than
  // guessing a bot state from the score.
  const browserScanBot = /Bot\s+Detection:\s*(?:(NoDetection|Detected)\s*)?(\d+)%/i.exec(bodyText);
  if (browserScanBot) {
    const detectionLanguage = /Detection language:\s*([^()]{1,80})\([^)]*\)\(([^)]+)\)/i.exec(bodyText)
      || /Detection language:\s*([^()]{1,80})\(([^)]+)\)/i.exec(bodyText);
    // The BrowserScan shell sometimes renders ``IP Time Zone: Location``
    // before its async value arrives.  A real IANA zone always contains a
    // slash; require that shape so a following label is not captured as a
    // timezone and later classified as a mismatch.
    const ipTimezone = /IP Time Zone:\s*([A-Za-z][A-Za-z0-9_+-]*(?:\/[A-Za-z0-9_+.-]+)+)/i.exec(bodyText)?.[1] || null;
    const lang = String(nav.language || '').toLowerCase();
    const detectedLocale = detectionLanguage?.[2]?.trim().toLowerCase() || null;
    const browserTimezone = resolved.timeZone || null;
    siteSignals.browserScan = {
      bot: browserScanBot[1] || null, score: Number(browserScanBot[2]),
      timezoneWarning: /Different time zones[^%]*-10%|Your IP timezone does not match[^.]*\./i.exec(bodyText)?.[0] || null,
      languageWarning: /Language mismatch[^%]*-10%/i.exec(bodyText)?.[0] || null,
      ipTimezone,
      browserTimezone,
      detectionLanguage: detectionLanguage?.[1]?.trim() || null,
      detectionLocale: detectedLocale,
      languageConsistent: !!(detectedLocale && lang
        && (lang === detectedLocale || lang.startsWith(detectedLocale + '-'))),
      timezoneConsistent: !!(ipTimezone && browserTimezone && ipTimezone === browserTimezone),
    };
    // Keep the structured warning under ``siteSignals.browserScan``.  Copying
    // it into the generic diagnostics bucket makes the summary report it
    // before it can compare the sampled IP and Intl values.
  }
  const justBrowserScore = /(\d+)\s+Pass\s+(\d+)\s+Warn\s+(\d+)\s+Fail/i.exec(bodyText);
  if (justBrowserScore) {
    siteSignals.justBrowser = {
      pass: Number(justBrowserScore[1]), warn: Number(justBrowserScore[2]),
      fail: Number(justBrowserScore[3]),
      locationWarning: /Geolocation \/ ISP[\s\S]{0,500}?does not match your browser's timezone[\s\S]{0,200}?WARN/i.exec(bodyText)?.[0] || null,
      pluginsWarning: /Plugins\s+None[^W]{0,180}?WARN/i.exec(bodyText)?.[0] || null,
      chromeRuntimeWarning: /chrome\.runtime[^W]{0,180}?WARN/i.exec(bodyText)?.[0] || null,
    };
    // The structured row is the source of truth for this warning; do not also
    // expose it through the generic diagnostics bucket (which would duplicate
    // the issue in the summary).
  } else if (/justbrowser\.app$/i.test(location.hostname)) {
    // The current page renders per-row PASS/WARN labels rather than the old
    // aggregate ``N Pass N Warn N Fail`` footer. Keep a compatible structured
    // result so a missing footer does not hide a real warning.
    const rowPasses = [
      /navigator\.webdriver\s+false[^\n]{0,100}?PASS/i,
      /toString\(\)\s+Integrity[^\n]{0,100}?PASS/i,
      /WebRTC[^\n]{0,100}?PASS/i,
    ].filter(re => re.test(bodyText)).length;
    const rowWarns = [
      /chrome\.runtime\s+Missing[^\n]{0,100}?WARN/i,
      /Plugins\s+None[^\n]{0,100}?WARN/i,
      /Geolocation\s*\/\s*ISP[^\n]{0,240}?WARN/i,
    ].filter(re => re.test(bodyText)).length;
    const rowFails = [
      /navigator\.webdriver\s+true[^\n]{0,100}?FAIL/i,
      /WebRTC[^\n]{0,100}?FAIL/i,
    ].filter(re => re.test(bodyText)).length;
    siteSignals.justBrowser = {
      pass: rowPasses, warn: rowWarns, fail: rowFails,
      locationWarning: /Geolocation\s*\/\s*ISP[^\n]{0,240}?WARN/i.exec(bodyText)?.[0] || null,
      pluginsWarning: /Plugins\s+None[^\n]{0,180}?WARN/i.exec(bodyText)?.[0] || null,
      chromeRuntimeWarning: /chrome\.runtime\s+Missing[^\n]{0,180}?WARN/i.exec(bodyText)?.[0] || null,
    };
    // See the aggregate-format branch above; keep this warning structured.
  }
  // Module completion is asynchronous.  The crowd uniqueness value may still
  // be ``--`` while Spoofing/Leak already have numbers, so parse each score
  // independently instead of requiring a fragile fixed distance between them.
  const proxyLabsUniqueness = /Uniqueness\s+(\d+)\/100/i.exec(bodyText);
  const proxyLabsSpoofing = /Spoofing Risk\s+(\d+)\/100/i.exec(bodyText);
  const proxyLabsLeak = /Leak Risk\s+(\d+)\/100/i.exec(bodyText);
  if (proxyLabsSpoofing && proxyLabsLeak) {
    const spoofingMatch = /Spoofing Detection[\s\S]{0,400}?(\d+)\s+issues found/i.exec(bodyText);
    const spoofingIssues = spoofingMatch ? Number(spoofingMatch[1]) : 0;
    const mobileHeuristicWarnings = [
      /userAgent toString is spoofed/i.test(bodyText) ? 'userAgent toString' : null,
      /0 plugins is suspicious for non-headless/i.test(bodyText) ? 'empty plugins' : null,
      /Locale inconsistency detected:\s*en-US, en/i.test(bodyText) ? 'en-US language list' : null,
    ].filter(Boolean);
    const localeWarning = /Locale inconsistency detected:\s*[A-Za-z-]+(?:\s*,\s*[A-Za-z-]+){1,3}/i.exec(bodyText)?.[0] || null;
    siteSignals.proxyLabs = {
      uniqueness: proxyLabsUniqueness ? Number(proxyLabsUniqueness[1]) : null,
      spoofingRisk: Number(proxyLabsSpoofing[1]),
      leakRisk: Number(proxyLabsLeak[1]),
      canvasNoise: /3 canvas tests[^!]{0,120}Noise detected/i.test(bodyText),
      spoofingIssues,
      mobileHeuristicWarnings,
      actionableSpoofingIssues: Math.max(0, spoofingIssues - mobileHeuristicWarnings.length),
      localeWarning,
      localeConsistent: !!(nav.language && nav.languages?.length
        && String(nav.languages[0]).toLowerCase() === String(nav.language).toLowerCase()),
      proxyDetected: /Proxy detected \([^)]+\)/i.exec(bodyText)?.[0] || null,
      knownWeakHeuristics: [
        /3 canvas tests[^!]{0,120}Noise detected/i.test(bodyText) ? 'canvas noise' : null,
        ...mobileHeuristicWarnings,
      ].filter(Boolean),
    };
  }
  const creepHeadless = /(\d+)%\s+like headless[\s\S]*?(\d+)%\s+headless[\s\S]*?(\d+)%\s+stealth/i.exec(bodyText);
  if (creepHeadless) {
    // CreepJS publishes the complete object on the page after the fingerprint
    // pass.  Capture the individual booleans as well as the rendered
    // percentages; the latter alone cannot tell Android's expected empty PDF
    // surface from a real cross-context headless signal.
    const creepFingerprint = (typeof window !== 'undefined' && window.Fingerprint)
      ? window.Fingerprint.headless : null;
    siteSignals.creepjs = {
      chromium: /chromium:\s*(true|false)/i.exec(bodyText)?.[1] === 'true',
      likeHeadless: Number(creepHeadless[1]), headless: Number(creepHeadless[2]),
      stealth: Number(creepHeadless[3]),
      likeHeadlessFlags: creepFingerprint && creepFingerprint.likeHeadless
        ? {...creepFingerprint.likeHeadless} : null,
      headlessFlags: creepFingerprint && creepFingerprint.headless
        ? {...creepFingerprint.headless} : null,
      stealthFlags: creepFingerprint && creepFingerprint.stealth
        ? {...creepFingerprint.stealth} : null,
      systemFonts: creepFingerprint?.systemFonts || null,
      fontProbe: creepFingerprint?.fonts?.fontFaceLoadFonts || null,
    };
  }
  const readMyBrowser = /Uniqueness\s+(\d+(?:\.\d+)?)\s+bits\s+([^\n]{1,40})/i.exec(bodyText);
  if (readMyBrowser) {
    const rating = /Very high|High|Moderate|Low/i.exec(readMyBrowser[2])?.[0] || readMyBrowser[2].trim();
    siteSignals.readMyBrowser = {
      bits: Number(readMyBrowser[1]),
      rating,
      veryHigh: /very high/i.test(rating),
    };
  }
  // The result header uses the singular form for a one-item result
  // (``1 flag, 0 notes``), so accept both singular and plural.  Keeping the
  // parsed counters lets the summary distinguish the site's network-quality
  // flag from a browser-surface failure instead of silently dropping the row.
  const xdLogin = /Done:\s*(\d+)\s+flags?,\s*(\d+)\s+notes?,\s*(\d+)\s+OK/i.exec(bodyText);
  if (xdLogin) {
    siteSignals.xdLogin = {
      flags: Number(xdLogin[1]), notes: Number(xdLogin[2]), ok: Number(xdLogin[3]),
      timezoneFlag: /Flag\s+Timezone vs IP[\s\S]{0,500}?mismatch/i.test(bodyText),
      networkFlag: /Flag\s+Network type[\s\S]{0,500}?hosting company/i.test(bodyText),
      webdriverOk: /Automation flag[\s\S]{0,180}?isn[’']t true/i.test(bodyText),
      webRtcLeak: /WebRTC[^.]{0,120}?revealed a public IP/i.test(bodyText),
    };
  }
  const browserFingerprint = /Exposure index\s+(\d+)\/100[\s\S]{0,220}?Mismatches\s+(\d+)/i.exec(bodyText);
  if (browserFingerprint) {
    siteSignals.browserFingerprint = {
      exposure: Number(browserFingerprint[1]), mismatches: Number(browserFingerprint[2]),
      timezoneMismatch: /Browser timezone vs network timezone[\s\S]{0,220}?Mismatch/i.test(bodyText),
      fontCount: /Font availability[^\d]{0,80}(\d+)\s+of\s+(\d+)\s+detected/i.exec(bodyText)?.slice(1).map(Number) || null,
      manualScan: false,
    };
  } else if (/Run fingerprint test[\s\S]{0,100}?Nothing runs until you press the button/i.test(bodyText)) {
    siteSignals.browserFingerprint = {manualScan: true};
  }
  const bellCrier = /(\d+)\s*\/\s*5\s+EXPOSED[\s\S]{0,100}?([A-Z][A-Z ]+TRACKABILITY)/i.exec(bodyText);
  if (bellCrier) {
    siteSignals.bellCrier = {
      exposed: Number(bellCrier[1]), rating: bellCrier[2].trim(),
      canvasReadable: /Canvas fingerprint\s+Readable/i.test(bodyText),
      graphicsVisible: /Graphics card\s+Visible/i.test(bodyText),
      fontsHidden: /Installed fonts\s+Hidden/i.test(bodyText),
      webRtcHidden: /Local IP \(WebRTC\)\s+Hidden/i.test(bodyText),
    };
  }
  const pixelscan = /(?:^|\s)(\d+)\s+CLEAN[\s\S]{0,260}?LEAK TESTS[\s\S]{0,160}?([^.!]{0,100}DNS[^.!]{0,100})/i.exec(bodyText);
  if (pixelscan) {
    siteSignals.pixelscan = {
      clean: Number(pixelscan[1]),
      dnsFinding: /multiple DNS resolvers found/i.test(pixelscan[2]),
      directConnection: /VPN \/ PROXY\s+Direct connection/i.test(bodyText),
      networkComplete: /Network complete/i.test(bodyText),
    };
  }
  const dualLoginFonts = /fonts detected\s+(\d+)\s*\/\s*(\d+)/i.exec(bodyText);
  if (dualLoginFonts) {
    siteSignals.dualLogin = {
      fontsDetected: Number(dualLoginFonts[1]), fontCandidates: Number(dualLoginFonts[2]),
      webdriverFalse: /navigator\.webdriver\s+false/i.test(bodyText),
      webRtcClean: /candidates none[^✓]{0,30}✓/i.test(bodyText),
    };
  }
  // A number of privacy-first pages deliberately wait for an explicit button
  // click. Record that state instead of treating their static methodology text
  // as a failed environment check.
  if (!browserFingerprint
      && /Nothing runs until you click|Nothing runs until you press|Run the audit to capture/i.test(bodyText)) {
    siteSignals.manualScanRequired = true;
  }
  if (/CONSENT REQUIRED[\s\S]{0,180}?Authorization Required|AUTHORIZE THE INVESTIGATION/i.test(bodyText)
      && /Nothing has been collected|PENDING INVESTIGATION/i.test(bodyText)) {
    siteSignals.manualConsentRequired = true;
  }
  return {
    url: location.href, title: document.title, ready: document.readyState,
    ua: nav.userAgent, platform: nav.platform, webdriver: nav.webdriver,
    language: nav.language, languages: [...(nav.languages || [])],
    timezone: resolved.timeZone, intlLocale: resolved.locale,
    dateString: new Date(0).toString(), timezoneOffset: new Date().getTimezoneOffset(),
    screen: `${screen.width}x${screen.height}x${screen.colorDepth}`,
    availableScreen: `${screen.availWidth}x${screen.availHeight}`,
    viewport: `${innerWidth}x${innerHeight}`, dpr: devicePixelRatio,
    viewportMeta: viewportMeta ? String(viewportMeta.getAttribute('content') || '') : null,
    visualViewport: window.visualViewport
      ? `${Math.round(window.visualViewport.width)}x${Math.round(window.visualViewport.height)}@${window.visualViewport.scale}`
      : null,
    touch: nav.maxTouchPoints, pointerCoarse: matchMedia('(pointer: coarse)').matches,
    deviceMemory: nav.deviceMemory ?? null,
    hardwareConcurrency: nav.hardwareConcurrency ?? null,
    uaChPlatform: ch && ch.platform, uaChModel: ch && ch.model,
    uaChMobile: ch && ch.mobile,
    webgl, canvas, alerts, diagnostics, siteSignals, body: bodyText.slice(0, 700),
  };
})()"""


def api(client: httpx.Client, method: str, path: str, **kwargs: Any) -> dict:
    response = client.request(method, path, **kwargs)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != 0:
        raise RuntimeError(f"{method} {path}: {payload}")
    return payload["data"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile-id", help="复用已存在环境；默认创建一次性移动环境")
    parser.add_argument("--wait", type=float, default=3.0,
                        help="每个页面加载后的等待秒数（默认 3）")
    parser.add_argument("--only",
                        help="只检测指定名称，逗号分隔；名称来自 app/config.py 的 DETECT_LINKS")
    parser.add_argument("--output", type=Path,
                        help="将逐站结果与汇总写入 JSON 文件")
    args = parser.parse_args()

    selected_links = DETECT_LINKS
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        selected_links = {name: url for name, url in DETECT_LINKS.items()
                          if name in wanted}
        missing = wanted - set(selected_links)
        if missing:
            parser.error("未知检测站名称: " + ", ".join(sorted(missing)))

    created_id = None
    with httpx.Client(base_url=BASE, timeout=90) as client:
        if args.profile_id:
            profile_id = args.profile_id
        else:
            profile = api(client, "POST", "/profiles", json={
                "name": f"检测采样-{uuid.uuid4().hex[:8]}",
                "kernel": "fp-chromium", "target_os": "android",
                "fingerprint_mode": "preset", "manufacturer": "Xiaomi",
                "device_model": "Xiaomi 15 Ultra",
                "launch": {"headless": False, "preset": "standard"},
            })
            profile_id = profile["id"]
            created_id = profile_id
        started_by_us = False
        try:
            active = api(client, "GET", "/browser/active")
            if not any(x.get("profile_id") == profile_id for x in active):
                api(client, "POST", "/browser/start", json={"profile_id": profile_id})
                started_by_us = True
            rows = []
            for name, url in selected_links.items():
                started = time.monotonic()
                row: dict[str, Any] = {"name": name, "url": url}
                try:
                    last_error: Exception | None = None
                    # A few public checkers occasionally return a transient
                    # navigation error after a long cross-site run (rate
                    # limiting, a closed tab, or a slow service worker).  Use
                    # three bounded attempts with a short backoff: one retry
                    # was still occasionally insufficient for the 500 page
                    # response from the fingerprint checker near the end of a
                    # full 37-site run.
                    for attempt in range(3):
                        try:
                            nav = api(client, "POST", f"/browser/{profile_id}/navigate",
                                      json={"url": url})
                            time.sleep(max(0.0, args.wait))
                            sample = api(client, "POST", f"/browser/{profile_id}/evaluate",
                                         json={"expression": SAMPLE_JS})
                            row.update(sample.get("result") or {})
                            row["navigate_url"] = nav.get("url")
                            if attempt:
                                row["retries"] = attempt
                            last_error = None
                            break
                        except Exception as exc:  # noqa: BLE001 - retry once
                            last_error = exc
                            if attempt >= 2:
                                raise
                            time.sleep(1.0 + attempt * 1.5)
                    if last_error is not None:
                        raise last_error
                except Exception as exc:  # noqa: BLE001 - keep probing other sites
                    detail = str(exc)[:200]
                    # httpx's HTTPStatusError string only contains the local
                    # API URL. Preserve the bounded server message as well;
                    # it distinguishes a page's connection reset from a
                    # failed fingerprint assertion in the final summary.
                    response = getattr(exc, "response", None)
                    if response is not None:
                        try:
                            payload = response.json()
                            msg = str(payload.get("msg") or "").strip()
                            if msg:
                                detail = f"{detail}: {msg[:240]}"
                        except Exception:
                            pass
                    row["error"] = f"{type(exc).__name__}: {detail}"
                row["elapsed_s"] = round(time.monotonic() - started, 2)
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False))
            summary = []
            for item in rows:
                diagnostics = item.get("diagnostics") or {}
                issues = []
                notes = []
                if item.get("error"):
                    error = str(item["error"])
                    if (item.get("name") == "CleanIP 指纹"
                            and "ERR_CONNECTION_CLOSED" in error):
                        notes.append("CleanIP: 站点连接被远端关闭，属于网络可达性观测，不是指纹失败")
                    else:
                        issues.append(error)
                if diagnostics.get("failedRows"):
                    failed = diagnostics["failedRows"]
                    # Sannysoft's two plugin checks are desktop-Chrome rules;
                    # Android Chrome has an authentic empty PluginArray.  Keep
                    # the raw rows in the result, but classify those rows as a
                    # mobile expectation instead of an actionable failure.
                    mobile_plugin_rows = []
                    if item.get("name") == "Sannysoft Bot Test" and _is_mobile_sample(item):
                        mobile_plugin_rows = [x for x in failed if x.get("id") in {
                            "plugins-length-result", "plugins-type-result",
                        }]
                    actionable_failed = [x for x in failed if x not in mobile_plugin_rows]
                    if mobile_plugin_rows:
                        notes.append("Sannysoft: empty PluginArray is expected on Android Chrome")
                    if actionable_failed:
                        issues.append("failed: " + ", ".join(
                            x.get("id") or x.get("text", "")
                            for x in actionable_failed))
                screen_parts = str(item.get("screen") or "").split("x")
                screen_css = ("x".join(screen_parts[:2])
                              if len(screen_parts) >= 2 else "")
                if (item.get("name") == "Sannysoft Bot Test"
                        and _is_mobile_sample(item)
                        and not item.get("viewportMeta")
                        and screen_css
                        and item.get("viewport") != screen_css):
                    # This legacy page intentionally omits a viewport meta tag.
                    # Mobile Chromium then exposes its default layout viewport
                    # (while the patched screen remains in CSS pixels); keep the
                    # raw values but do not treat that page-specific behavior as
                    # a failed device emulation.
                    notes.append("Sannysoft: no viewport meta; default mobile layout viewport is site-specific")
                if diagnostics.get("warnRows"):
                    issues.append("warn: " + ", ".join(
                        x.get("id") or x.get("text", "")
                        for x in diagnostics["warnRows"]))
                signals = item.get("siteSignals") or {}
                # Canvas/Mac is an unstructured result and can be reported
                # directly.  BrowserScan/JustBrowser timezone messages are
                # parsed into ``siteSignals`` below, where their sampled IP
                # and Intl values are available.  Adding the generic copy
                # first used to turn a consistent BrowserScan warning into an
                # actionable issue (and duplicated JustBrowser warnings).
                if diagnostics.get("canvasMacHint"):
                    issues.append(str(diagnostics["canvasMacHint"]))
                if (diagnostics.get("timezoneMismatch")
                        and not signals.get("browserScan")
                        and not signals.get("justBrowser")):
                    issues.append(str(diagnostics["timezoneMismatch"]))
                if diagnostics.get("canvasNoise"):
                    if item.get("name") == "ProxyLabs":
                        notes.append("ProxyLabs: " + str(diagnostics["canvasNoise"])
                                  + " (known Chromium/mobile heuristic)")
                    else:
                        issues.append(str(diagnostics["canvasNoise"]))
                if signals.get("browserScan"):
                    bs = signals["browserScan"]
                    if bs.get("timezoneWarning"):
                        if bs.get("timezoneConsistent"):
                            notes.append("BrowserScan: timezone warning contradicts the sampled IP/Intl values")
                        else:
                            issues.append(bs["timezoneWarning"])
                    if bs.get("languageWarning"):
                        if bs.get("languageConsistent"):
                            notes.append("BrowserScan: language warning contradicts the sampled language values")
                        else:
                            issues.append(bs["languageWarning"])
                if signals.get("cloudflareChallenge"):
                    notes.append(f"{item.get('name')}: Cloudflare verification page; scan did not run")
                if signals.get("cleanIpUnavailable"):
                    notes.append("CleanIP 指纹: 远端返回空/500 页面，属于站点可达性观测，不是指纹失败")
                if signals.get("cleanIpManualScan"):
                    notes.append("CleanIP 指纹: 需要手动点击 Start Scan，当前页面未生成扫描结果")
                if signals.get("justBrowser"):
                    jb = signals["justBrowser"]
                    if jb.get("locationWarning"):
                        issues.append(jb["locationWarning"])
                    if jb.get("pluginsWarning"):
                        if _is_mobile_sample(item):
                            notes.append("JustBrowser: empty plugins is expected on Android Chrome")
                        else:
                            issues.append(jb["pluginsWarning"])
                    if jb.get("chromeRuntimeWarning"):
                        # chrome.runtime is a desktop-extension surface; its
                        # absence is normal in a clean mobile Chrome profile.
                        notes.append("JustBrowser: chrome.runtime missing is expected without an extension")
                if signals.get("proxyLabs"):
                    pl = signals["proxyLabs"]
                    actionable = int(pl.get("actionableSpoofingIssues") or 0)
                    if actionable:
                        issues.append(f"ProxyLabs: {actionable} actionable spoofing signal(s)")
                    elif pl.get("spoofingIssues"):
                        expected = ", ".join(pl.get("mobileHeuristicWarnings") or [])
                        notes.append("ProxyLabs: spoofing flags are known mobile/implementation heuristics"
                                     + (f" ({expected})" if expected else ""))
                    if pl.get("localeWarning"):
                        if pl.get("localeConsistent"):
                            notes.append("ProxyLabs: locale warning is the normal locale + language-list shape")
                        else:
                            issues.append(pl["localeWarning"])
                    if pl.get("proxyDetected"):
                        notes.append("ProxyLabs: HTTP proxy marker is site/network metadata; verify the exit network separately")
                if signals.get("creepjs"):
                    cj = signals["creepjs"]
                    _classify_creepjs_signal(item, cj)
                    if cj.get("headless") or cj.get("stealth"):
                        issues.append("CreepJS: "
                                      f"{cj['likeHeadless']}% like headless / "
                                      f"{cj['headless']}% headless / "
                                      f"{cj['stealth']}% stealth")
                    elif cj.get("actionableFlags"):
                        issues.append("CreepJS actionable like-headless flags: "
                                      + ", ".join(cj["actionableFlags"]))
                    elif cj.get("likeHeadless"):
                        expected = ", ".join(cj.get("expectedFlags") or [])
                        notes.append("CreepJS: like-headless score comes from the mobile baseline"
                                     + (f" ({expected})" if expected else ""))
                if signals.get("readMyBrowser"):
                    rb = signals["readMyBrowser"]
                    if rb.get("veryHigh"):
                        notes.append(f"ReadMyBrowser: {rb['bits']} bits / {rb['rating']} (visibility metric)")
                if signals.get("xdLogin"):
                    xd = signals["xdLogin"]
                    if xd.get("timezoneFlag"):
                        issues.append("XDLogin: timezone vs IP mismatch")
                    if xd.get("networkFlag"):
                        issues.append("XDLogin: hosting/datacenter network")
                    if xd.get("webRtcLeak"):
                        issues.append("XDLogin: WebRTC public IP exposed")
                if signals.get("browserFingerprint"):
                    bf = signals["browserFingerprint"]
                    if bf.get("manualScan"):
                        notes.append("BrowserFingerprint.org: manual scan required")
                    else:
                        if bf.get("exposure") is not None and bf["exposure"] >= 80:
                            notes.append(f"BrowserFingerprint.org: exposure {bf['exposure']}/100 (visibility metric)")
                        if bf.get("timezoneMismatch"):
                            issues.append("BrowserFingerprint.org: timezone vs network mismatch")
                if signals.get("bellCrier"):
                    bc = signals["bellCrier"]
                    if bc.get("exposed"):
                        notes.append(f"BellCrier: {bc['exposed']}/5 signals exposed (visibility metric)")
                if signals.get("pixelscan"):
                    ps = signals["pixelscan"]
                    if ps.get("dnsFinding"):
                        notes.append("Pixelscan.dev: multiple DNS resolvers found (network observation)")
                if signals.get("dualLogin"):
                    dl = signals["dualLogin"]
                    if dl.get("fontsDetected") == 0:
                        notes.append("Dual Login: 0/" + str(dl["fontCandidates"])
                                     + " fonts detected (mobile font coverage observation)")
                if signals.get("manualScanRequired") and not any(
                        "manual scan" in str(x).lower() for x in issues + notes):
                    notes.append(f"{item.get('name')}: manual scan required")
                if signals.get("manualConsentRequired"):
                    notes.append(f"{item.get('name')}: consent/start required")
                _add_summary_item(summary, item.get("name"), issues, notes)
            report = {"profile_id": profile_id, "summary": summary, "results": rows}
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        finally:
            if started_by_us:
                try:
                    api(client, "POST", "/browser/stop", json={"profile_id": profile_id})
                except Exception:
                    pass
            if created_id:
                try:
                    client.delete(f"/profiles/{created_id}")
                except Exception:
                    pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
