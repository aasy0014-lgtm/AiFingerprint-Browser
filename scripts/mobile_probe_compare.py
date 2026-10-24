#!/usr/bin/env python3
"""仿真读数 vs 真机读数逐项对拍。

两侧读数都由 scripts/mobile_probe_js.py 的同一份 JS 产生：
  仿真: scripts/mobile_bigtech_probe.py ... --json sim.json
  真机: scripts/mobile_real_collect.py（手机上传 / 复制）

归一化：先按路径把「本来就会不同」的字段抹平（时间戳、端口、随机种子驱动的
机型参数、传感器数值与时序），再逐路径比较。剩下的差异分三类：

  only-sim    仿真有、真机无   → 多半是注入伪造出来的面（重点看）
  only-real   真机有、仿真无   → 缺口，需要补注入或补数据
  value       两侧都有但值不同 → 值域/格式问题（如 UA 形态、windowId 格式）

已知差异（架构性，短期不打算改）写在 KNOWN 里，输出为「已知」，不计入失败。
退出码：有「未知」差异则为 1，否则 0。

用法:
    .venv/bin/python scripts/mobile_probe_compare.py sim.json phone.json
    .venv/bin/python scripts/mobile_probe_compare.py sim.json phone.json --only only-real
    .venv/bin/python scripts/mobile_probe_compare.py sim.json phone.json --json diff.json
"""
import argparse
import json
import re
import sys
from pathlib import Path

# ---- 归一化 -------------------------------------------------------------
# 路径用「点 + [*]」表示数组元素；值用 repr 匹配。
EXACT_PATHS = {
    # 环境/时间相关
    "collected_at", "received_at", "received_from", "source", "href",
    "parts.main.href", "parts.main.isSecureContext",
    # 传感器读数本身（真机是真实运动，仿真按机型表推算）
    "parts.sensor.counts", "parts.sensor.samples", "parts.sensor.intervals",
    "parts.sensor.sensorApi", "parts.sensor.orientationAbsoluteInProto",
    # 存储配额随设备剩余空间变化
    "parts.main.storage",
}

REGEX_PATHS = [
    # 屏幕几何：对拍的是「口径」而不是「同尺寸」——真机与所仿真机型未必同款
    (re.compile(r"^parts\.main\.(screen|win|visualViewport|screenOrientation)"),
     "屏幕/视口几何随设备而变"),
    (re.compile(r"^parts\.main\.envSafeArea"), "安全区随设备刘海/圆角而变"),
    # 机型相关的随机种子参数（生成模式每次不同）
    (re.compile(r"^parts\.main\.uaChFull(\.|$)"), "机型参数"),
    (re.compile(r"^parts\.main\.(userAgent|platform)$"), "机型参数"),
    (re.compile(r"^parts\.main\.uaChMobile$"), "机型参数"),
    (re.compile(r"^parts\.main\.connection\."), "网络参数由种子随机"),
    (re.compile(r"^parts\.main\.webgl"), "GPU 随机型而变"),
    (re.compile(r"^parts\.main\.webglExts"), "GPU 扩展随机型而变"),
    (re.compile(r"^parts\.main\.media"), "编解码能力随平台而变（且与机型绑定）"),
    (re.compile(r"^parts\.main\.notification\."), "通知权限是用户态"),
    (re.compile(r"^parts\.main\.battery\."), "电量/充电状态是用户态"),
    (re.compile(r"^parts\.main\.storage\."), "配额随设备剩余空间"),
    (re.compile(r"^parts\.sensor\.counts\."), "传感器时序"),
    (re.compile(r"^parts\.sensor\.samples\."), "传感器数值"),
    (re.compile(r"^parts\.sensor\.intervals\."), "传感器时序"),
    (re.compile(r"^parts\.sensor\.sensorApi"), "传感器 API 读数"),
    (re.compile(r"^parts\.main\.plugins"), "插件列表随浏览器版本"),
    (re.compile(r"^parts\.main\.mimeTypesLen"), "随浏览器版本"),
    (re.compile(r"^parts\.main\.perfMemory"), "随设备内存"),
    (re.compile(r"^parts\.main\.languages"), "随系统语言"),
    (re.compile(r"^parts\.main\.hardwareConcurrency$"), "随机型"),
    (re.compile(r"^parts\.main\.deviceMemory$"), "随机型"),
    (re.compile(r"^parts\.main\.maxTouchPoints$"), "随机型"),
    (re.compile(r"^parts\.crios\.padDetection\."), "机型判据（随真机而变）"),
    (re.compile(r"^parts\.crios\.standalone\."), "PWA 安装态是用户态"),
    (re.compile(r"^parts\.main\.vendor$"), "随浏览器"),
    (re.compile(r"^parts\.engine\.fullscreen\.(elementOwn|documentOwn)$"),
     "全屏家族成员随浏览器版本增删"),
    (re.compile(r"^parts\.engine\.video\."), "视频全屏随浏览器版本"),
    (re.compile(r"^parts\.platform\.worker$"), "Worker 全局随浏览器版本"),
]

# 输入链路：仿真侧记录的是 CDP 派发结果，真机侧没有等价读数
EXACT_PATHS |= {
    "input_mouse_click_events", "input_playwright_touchscreen_tap",
    "input_touch_tap_events", "input_touch_scroll",
}

# ---- 已知差异（架构性） ---------------------------------------------------
# 命中即归为「已知」，不判失败；条目要在报告里可读。
KNOWN = [
    {
        "pattern": re.compile(r"^parts\.main\.(vendor|userAgent|uaChFull)$"),
        "reason": "引擎是 Chromium 而非 WebKit/Blink-移动版，版本号口径必然不同",
    },
    {
        "pattern": re.compile(r"^parts\.engine\.safari\."),
        "reason": "iOS 走 Chromium 内核，WebKit 专属全局面靠注入伪造，成员全集与真机有出入",
    },
    {
        "pattern": re.compile(r"^parts\.main\.css\."),
        "reason": "-webkit-* CSS 支持度是内核级能力，无法注入",
    },
    {
        "pattern": re.compile(r"^parts\.platform\.(win|doc|nav|kept|keptDoc|keptNav)\."),
        "reason": "平台 API 差集是内核级增删，注入只能补成员、不能删内核自带面",
    },
]


def _flatten(obj, prefix="", out=None):
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(v, f"{prefix}.{k}" if prefix else k, out)
    elif isinstance(obj, list):
        if not obj:
            out[prefix] = "[]"
        else:
            for i, v in enumerate(obj):
                _flatten(v, f"{prefix}[{i}]", out)
    else:
        out[prefix] = obj
    return out


def _path_matches(path: str, pattern: str) -> bool:
    """pattern 支持 [*] 通配数组下标。"""
    if "[*]" not in pattern:
        return path == pattern
    rx = re.escape(pattern).replace(r"\[\*\]", r"\[\d+\]")
    return re.fullmatch(rx, path) is not None


def _excluded(path: str) -> str | None:
    for p in EXACT_PATHS:
        if _path_matches(path, p):
            return "环境相关（归一化）"
    for rx, why in REGEX_PATHS:
        if rx.match(path):
            return why
    return None


def _known(path: str) -> str | None:
    for item in KNOWN:
        if item["pattern"].match(path):
            return item["reason"]
    return None


def _sim_parts(doc: dict) -> dict:
    """兼容两种产物：探针新版（parts）与真机上传（parts）。"""
    if "parts" in doc:
        return doc["parts"]
    return {k: doc[k] for k in ("signals", "sensor", "engine", "platform") if k in doc}


def _normalize_doc(doc: dict) -> dict:
    parts = _sim_parts(doc)
    if "signals" in parts:  # 旧版探针字段名
        parts = dict(parts, main=parts.pop("signals"))
    return {"parts": parts}


def compare(sim_doc: dict, real_doc: dict) -> list[dict]:
    sim = _flatten(_normalize_doc(sim_doc))
    real = _flatten(_normalize_doc(real_doc))

    rows = []
    for path in sorted(set(sim) | set(real)):
        why = _excluded(path)
        known = _known(path)
        if why:
            continue
        a, b = sim.get(path, "<absent>"), real.get(path, "<absent>")
        if a == b:
            continue
        if a == "<absent>":
            kind = "only-real"
        elif b == "<absent>":
            kind = "only-sim"
        else:
            kind = "value"
        rows.append({"path": path, "kind": kind, "sim": a, "real": b,
                     "known": known})
    return rows


KIND_LABEL = {
    "only-sim": "仿真有 / 真机无",
    "only-real": "真机有 / 仿真无",
    "value": "值不同",
}


def _fmt(v) -> str:
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False)
    else:
        s = repr(v)
    return s if len(s) <= 120 else s[:117] + "..."


def _real_sensor_warning(real_doc: dict) -> str | None:
    """真机传感器零读数几乎总是采集方式问题，不是环境差异。"""
    parts = _sim_parts(real_doc)
    sensor = parts.get("sensor") or {}
    counts = sensor.get("counts") or {}
    if not counts:
        return None
    numeric = (counts.get("motionNumeric", 0) + counts.get("orientationNumeric", 0))
    if numeric:
        return None
    perm = real_doc.get("sensor_permission") or {}
    return ("真机传感器没有任何数值读数（权限 "
            f"{json.dumps(perm, ensure_ascii=False)}）——"
            "检查页面是否在安全上下文（https / localhost），iOS 是否点了「允许」；"
            "传感器面的差异先别当结论。")


def main() -> int:
    ap = argparse.ArgumentParser(description="仿真读数 vs 真机读数对拍")
    ap.add_argument("sim", help="仿真读数 JSON（mobile_bigtech_probe.py --json 产物）")
    ap.add_argument("real", help="真机读数 JSON（mobile_real_collect.py 回传产物）")
    ap.add_argument("--only", choices=["only-sim", "only-real", "value"],
                    help="只看某一类差异")
    ap.add_argument("--json", dest="json_out", help="把差异表写成 JSON")
    ap.add_argument("--show-known", action="store_true", help="同时列出已知差异")
    args = ap.parse_args()

    sim_doc = json.loads(Path(args.sim).read_text(encoding="utf-8"))
    real_doc = json.loads(Path(args.real).read_text(encoding="utf-8"))

    rows = compare(sim_doc, real_doc)
    unknown = [r for r in rows if not r["known"]]
    known = [r for r in rows if r["known"]]
    if args.only:
        unknown = [r for r in unknown if r["kind"] == args.only]
        known = [r for r in known if r["kind"] == args.only]

    print("=" * 72)
    print(f"仿真: {args.sim}")
    print(f"真机: {args.real}")
    sim_main = _sim_parts(sim_doc).get("main") or {}
    real_main = _sim_parts(real_doc).get("main") or {}
    print(f"  仿真机型: {sim_doc.get('device') or '?'}  UA {str(sim_main.get('userAgent'))[:70]}")
    print(f"  真机上报: {real_main.get('userAgent') or '?'}"[:110])
    print(f"差异：未知 {len(unknown)} 项 / 已知 {len(known)} 项")
    print("=" * 72)

    warn = _real_sensor_warning(real_doc)
    if warn:
        print(f"\n⚠️  {warn}")

    for kind in ("only-sim", "only-real", "value"):
        group = [r for r in unknown if r["kind"] == kind]
        if not group:
            continue
        print(f"\n## {KIND_LABEL[kind]}（{len(group)}）")
        for r in group:
            if kind == "value":
                print(f"  {r['path']}\n      仿真 {_fmt(r['sim'])}\n      真机 {_fmt(r['real'])}")
            else:
                print(f"  {r['path']}  = {_fmt(r['sim'] if kind == 'only-sim' else r['real'])}")

    if args.show_known and known:
        print(f"\n## 已知差异（架构性，不计失败，{len(known)}）")
        for r in known:
            print(f"  {r['path']}  [{KIND_LABEL[r['kind']]}]  — {r['known']}")

    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps({"unknown": unknown, "known": known},
                       ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"\n差异表已写入 {args.json_out}")

    print()
    if unknown:
        print(f"❌ {len(unknown)} 项未知差异")
        return 1
    print("✅ 除归一化字段外，两侧读数一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
