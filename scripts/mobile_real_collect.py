#!/usr/bin/env python3
"""真机读数采集端：起一个 HTTPS 页面，手机打开后采集并回传 JSON。

为什么必须是 HTTPS：传感器事件（devicemotion / deviceorientation）只在
「安全上下文」里派发。手机通过局域网 IP 访问 `http://192.168.x.x` 不是安全
上下文，页面能开、读数能拿，但传感器计数恒为 0——看起来像「仿真有真机无」，
实际是采集方式的问题。`http://localhost` 算安全上下文，所以 Android 也可以
走 `adb reverse` 后用 localhost 访问（见 --help 尾部提示）。

页面里的读数 JS 与仿真侧完全同一份（scripts/mobile_probe_js.py），
所以 diff 出来的差异只可能来自环境，不来自代码漂移。

用法:
    .venv/bin/python scripts/mobile_real_collect.py            # 起服务，打印地址
    .venv/bin/python scripts/mobile_real_collect.py --port 8443 --out /tmp/phone.json

手机打开打印出的 https 地址 → 点「授权传感器并开始采集」→「上传到电脑」
（或「复制 JSON」手动存文件）。

对拍:
    .venv/bin/python scripts/mobile_probe_compare.py /tmp/sim.json /tmp/phone.json
"""
import argparse
import ipaddress
import json
import socket
import ssl
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import mobile_probe_js  # noqa: E402

PAGE_HTML = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>fpwb 真机读数采集</title>
<style>
  body {{ margin: 0; padding: 16px; font: 15px/1.6 -apple-system, system-ui, sans-serif;
         background: #f5f5f7; color: #111; }}
  h1 {{ font-size: 17px; margin: 0 0 6px; }}
  p {{ margin: 0 0 10px; opacity: .7; }}
  code {{ background: #e8e8ed; padding: 1px 4px; border-radius: 4px; }}
  #fill {{ height: 60vh; }}
</style></head>
<body>
<h1>fpwb 真机读数采集</h1>
<p>安全上下文：<code id="sc">…</code> · 协议 <code id="proto">…</code></p>
<p>点下方按钮 → 允许传感器权限 → 采集约 3 秒。结果可上传或复制。</p>
<div id="fill"></div>
<script>
  var PROBE_FN_SRC = {probe_fn_json};
  var fpwbProbe = new Function(PROBE_FN_SRC + "\\nreturn fpwbProbe;")();
  document.getElementById('sc').textContent = isSecureContext ? '是' : '否（传感器不会派发）';
  document.getElementById('proto').textContent = location.protocol;
  fpwbProbe({{ hooks: true }});
</script>
</body></html>
"""


def _lan_candidates() -> list[tuple[str, str]]:
    """枚举本机 IPv4：返回 [(地址, 网卡)]，按「手机能不能连上」排序。

    不能用 UDP connect 探测出口地址：开了 VPN（utun）时它会选中 198.18.x.x
    这类隧道地址，手机根本路由不到。这里改为枚举网卡，优先 192.168/10/172.16-31
    网段，其次其它非回环地址。
    """
    import subprocess

    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return [("127.0.0.1", "loopback")]

    found: list[tuple[str, str]] = []
    iface = ""
    for line in out.splitlines():
        if line and not line[0].isspace() and ":" in line:
            iface = line.split(":", 1)[0]
        elif line.strip().startswith("inet "):
            addr = line.split()[1]
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if ip.is_loopback or ip.is_link_local:
                continue
            found.append((addr, iface))

    def rank(item: tuple[str, str]) -> int:
        ip = ipaddress.ip_address(item[0])
        if ip in ipaddress.ip_network("192.168.0.0/16"):
            return 0
        if ip in ipaddress.ip_network("10.0.0.0/8"):
            return 1
        if ip in ipaddress.ip_network("172.16.0.0/12"):
            return 2
        if iface.startswith(("utun", "gif", "stf")):
            return 9
        return 3

    found.sort(key=rank)
    return found or [("127.0.0.1", "loopback")]


def _lan_ip() -> str:
    return _lan_candidates()[0][0]


def _ensure_cert(cert_dir: Path, hosts: list[str]) -> tuple[Path, Path]:
    """生成自签证书（SAN 覆盖全部候选地址），已存在且覆盖当前地址则复用。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime as _dt

    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "cert.pem"
    key_path = cert_dir / "key.pem"
    if cert_path.exists() and key_path.exists():
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            not_after = getattr(cert, "not_valid_after_utc", None) or cert.not_valid_after
            san = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value
            covered = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
            if (not_after > _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=1)
                    and set(hosts) <= covered):
                return cert_path, key_path
        except Exception:
            pass

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    san = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    for host in hosts:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if ip not in (ipaddress.ip_address("127.0.0.1"),):
            san.append(x509.IPAddress(ip))
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fpwb-mobile-probe")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(minutes=5))
            .not_valid_after(now + _dt.timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .sign(key, hashes.SHA256()))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def build_handler(out_path: Path):
    state = {"count": 0}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] not in ("/", "/index.html"):
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            html = PAGE_HTML.format(probe_fn_json=json.dumps(mobile_probe_js.PROBE_FN_JS))
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self):  # noqa: N802
            if self.path.split("?")[0] != "/submit":
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw)
            except ValueError:
                self._send(400, b"bad json", "text/plain; charset=utf-8")
                return
            payload.setdefault("source", "real-device")
            payload["received_at"] = datetime.now().isoformat(timespec="seconds")
            payload["received_from"] = self.client_address[0]
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n",
                                encoding="utf-8")
            state["count"] += 1
            ua = (payload.get("parts", {}).get("main") or {}).get("userAgent", "")
            print(f"\n✅ 收到第 {state['count']} 份读数 → {out_path}", flush=True)
            print(f"   UA: {ua}", flush=True)
            self._send(200, f"已保存 {out_path.name}（{len(raw)} 字节）".encode("utf-8"),
                       "text/plain; charset=utf-8")

        def log_message(self, *args):
            pass

    return Handler


def main() -> int:
    ap = argparse.ArgumentParser(
        description="真机读数采集端（HTTPS）",
        epilog="Android 也可用 adb reverse（localhost 同样算安全上下文，且免证书警告）：\n"
               "  adb reverse tcp:8443 tcp:8443\n"
               "  然后手机访问 https://localhost:8443/")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址（默认全部网卡）")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--out", default="phone_probe.json", help="手机回传的读数写到哪")
    ap.add_argument("--cert-dir", default=str(REPO / ".probe-certs"),
                    help="自签证书目录（默认 .probe-certs/，已 gitignore）")
    ap.add_argument("--lan-ip", help="手机访问用的本机地址（默认自动挑选，VPN 环境建议显式指定）")
    args = ap.parse_args()

    out_path = Path(args.out).expanduser()
    candidates = _lan_candidates()
    if args.lan_ip:
        lan = args.lan_ip
        hosts = [lan] + [a for a, _ in candidates]
    else:
        lan, _iface = candidates[0]
        hosts = [a for a, _ in candidates]
    cert, key = _ensure_cert(Path(args.cert_dir), hosts)

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))

    srv = ThreadingHTTPServer((args.host, args.port), build_handler(out_path))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    lines = [
        "=" * 68,
        "真机读数采集端已启动",
        f"  电脑:  https://localhost:{args.port}/",
        f"  手机:  https://{lan}:{args.port}/",
    ]
    others = [a for a, _ in candidates if a != lan][:3]
    if others:
        lines.append("  其它网卡（手机连的不是默认网段时试这些）: "
                     + "  ".join(f"https://{a}:{args.port}/" for a in others))
    lines += [
        "",
        "  证书是自签的，手机会提示「不安全」——选「高级 / 继续访问」。",
        f"  Android 想免掉证书警告：adb reverse tcp:{args.port} tcp:{args.port}，"
        f"再访问 https://localhost:{args.port}/",
        f"  回传落盘: {out_path}",
        "=" * 68,
        "Ctrl-C 结束。",
    ]
    print("\n".join(lines), flush=True)

    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        srv.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
