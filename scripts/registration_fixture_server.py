#!/usr/bin/env python3
"""Local-only registration fixture for exercising registration_trace.py.

This server is deliberately synthetic: it accepts no real account data, stores
nothing, and never makes outbound requests.  It exists so the trace harness can
be regression-tested without contacting a third-party registration service.
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


FORM = """<!doctype html>
<meta charset="utf-8"><title>Local Registration Fixture</title>
<h1>Local registration fixture</h1>
<form method="post" action="/register">
  <label>First name <input name="firstName" autocomplete="given-name"></label>
  <label>Last name <input name="lastName" autocomplete="family-name"></label>
  <button type="submit">Create test account</button>
</form>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "FPWBFixture/1.0"

    def _send(self, status: int, body: str, content_type: str = "text/html") -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/signup":
            self._send(200, FORM)
        elif self.path == "/health":
            self._send(200, json.dumps({"ok": True}), "application/json")
        elif self.path == "/api/egress":
            self._send(200, json.dumps({"ip": "127.0.0.1", "country": "LOCAL"}), "application/json")
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/register":
            self._send(404, "not found", "text/plain")
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(min(length, 64 * 1024))
        fields = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        # Only field presence is used; submitted values are intentionally never
        # logged or echoed back.
        if not {"firstName", "lastName"}.issubset(fields):
            self._send(400, json.dumps({"ok": False, "error": "missing_fields"}), "application/json")
            return
        self._send(201, "<!doctype html><title>Created</title><p>Test registration accepted.</p>")

    def log_message(self, fmt: str, *args: object) -> None:
        # Avoid logging request paths and bodies into the terminal by default.
        print(f"fixture: {self.command} {self.path.split('?', 1)[0]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("fixture server only binds localhost")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"local registration fixture: http://{args.host}:{args.port}/signup")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
