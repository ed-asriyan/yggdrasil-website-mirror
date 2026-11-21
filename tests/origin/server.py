#!/usr/bin/env python3
"""Mock origin for the e2e test: stands in for the clearnet sites being mirrored.

One process serves every test domain (they are network aliases of this
container) on the ports used by tests/e2e.py:

  443   HTTPS  site.test       (TARGET_DOMAIN=https://site.test)
  8443  HTTPS  api.test        (ADDITIONAL_DOMAINS https://api.test:8443)
  8080  HTTP   cdn.test        (ADDITIONAL_DOMAINS http://cdn.test:8080)

Every response reports what the mirror sent upstream in X-Echo-* headers
(headers are not touched by sub_filter, bodies are). Request counters per port
and path with query (X-Origin-Count) show whether a response came from the origin or from the cache.

Routes:
  /            HTML page that loads a script from api.test and an image from cdn.test
  /app.js      script: fetches data from cdn.test and reports success on the page
  /data.json   JSON for app.js (CORS enabled)
  /pixel.svg   image
  /links       every way a real address can appear in a body (rewrite check)
  /redirect    302 to an absolute URL on the same domain, or on ?to=site|api|cdn
  /cache       cacheable response
  /set-cookie  response with Set-Cookie (must not be cached)
  /big-header  response with a ~32 KB header
"""

from __future__ import annotations

import json
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

CERT = sys.argv[1] if len(sys.argv) > 1 else "/certs/origin.crt"
KEY = sys.argv[2] if len(sys.argv) > 2 else "/certs/origin.key"

LINKS = """\
https://site.test/p
http://site.test/p
https://api.test:8443/p
http://api.test:8443/p
http://cdn.test:8080/p
https://cdn.test:8080/p
"site.test"
"api.test:8443"
"cdn.test:8080"
"""

INDEX = """\
<!doctype html>
<html>
<head><meta charset="utf-8"><title>Mirror e2e</title></head>
<body>
<h1>Mirror e2e</h1>
<p id="status">loading</p>
<a id="self" href="https://site.test/links">links</a>
<img id="pixel" src="http://cdn.test:8080/pixel.svg" alt="pixel">
<script src="https://api.test:8443/app.js"></script>
</body>
</html>
"""

APP_JS = """\
fetch("http://cdn.test:8080/data.json")
  .then((r) => r.json())
  .then((d) => { document.getElementById("status").textContent = "all loaded: " + d.marker; })
  .catch((e) => { document.getElementById("status").textContent = "failed: " + e; });
"""

PIXEL = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"><rect width="1" height="1"/></svg>'

# Public address of the site served on each port.
BASE_URLS = {443: "https://site.test", 8443: "https://api.test:8443", 8080: "http://cdn.test:8080"}
SITES = {"site": 443, "api": 8443, "cdn": 8080}

counts: dict[str, int] = {}
counts_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mock-origin"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(
            f"{self.server.server_address[1]} host={self.headers.get('Host')} "
            f"sni={getattr(self.connection, 'sni', None)} {fmt % args}\n"
        )

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        url = urlsplit(self.path)
        path, query = url.path, dict(parse_qsl(url.query))
        key = f"{self.server.server_address[1]} {self.path}"
        with counts_lock:
            counts[key] = counts.get(key, 0) + 1
            count = counts[key]

        status, headers, body = 200, {}, b""
        if path == "/":
            headers["Content-Type"] = "text/html; charset=utf-8"
            body = INDEX.encode()
        elif path == "/app.js":
            headers["Content-Type"] = "application/javascript"
            body = APP_JS.encode()
        elif path == "/data.json":
            headers["Content-Type"] = "application/json"
            headers["Access-Control-Allow-Origin"] = "*"
            body = json.dumps({"marker": "cdn-ok"}).encode()
        elif path == "/pixel.svg":
            headers["Content-Type"] = "image/svg+xml"
            body = PIXEL.encode()
        elif path == "/links":
            headers["Content-Type"] = "text/plain; charset=utf-8"
            body = LINKS.encode()
        elif path == "/redirect":
            status = 302
            # absolute URL, as real sites build it from their public address
            port = SITES[query["to"]] if "to" in query else self.server.server_address[1]
            headers["Location"] = f"{BASE_URLS[port]}/target"
        elif path == "/cache":
            headers["Content-Type"] = "text/plain"
            body = f"count={count}\n".encode()
        elif path == "/set-cookie":
            headers["Content-Type"] = "text/plain"
            headers["Set-Cookie"] = f"session={count}; Path=/"
            body = f"count={count}\n".encode()
        elif path == "/big-header":
            headers["Content-Type"] = "text/plain"
            headers["X-Big"] = "b" * 32 * 1024
            body = b"big\n"
        else:
            status = 404
            headers["Content-Type"] = "text/plain"
            body = b"not found\n"

        self.send_response(status)
        self.send_header(
            "X-Echo-Host",
            self.headers.get("Host", "").replace("\r", "").replace("\n", ""),
        )
        self.send_header("X-Echo-Sni", getattr(self.connection, "sni", None) or "")
        self.send_header("X-Echo-Scheme", self.scheme())
        self.send_header("X-Echo-Port", str(self.server.server_address[1]))
        self.send_header(
            "X-Echo-Accept-Encoding",
            self.headers.get("Accept-Encoding", "").replace("\r", "").replace("\n", ""),
        )
        self.send_header("X-Origin-Count", str(count))
        for k, v in headers.items():
            self.send_header(k, v.replace("\r", "").replace("\n", ""))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def scheme(self) -> str:
        return "https" if isinstance(self.connection, ssl.SSLSocket) else "http"


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, tls: ssl.SSLContext | None):
        super().__init__(("0.0.0.0", port), Handler)
        self.tls = tls

    def finish_request(self, request, client_address) -> None:
        # TLS handshake in the request thread, not in accept(): a slow or broken
        # client must not block the listener.
        if self.tls is None:
            return super().finish_request(request, client_address)
        try:
            conn = self.tls.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError) as e:
            sys.stderr.write(f"{self.server_address[1]} TLS handshake failed: {e}\n")
            return
        try:
            super().finish_request(conn, client_address)
        finally:
            conn.close()


def tls_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)

    def remember_sni(sock: ssl.SSLSocket, name: str | None, _ctx: ssl.SSLContext) -> None:
        sock.sni = name  # read back by the handler (X-Echo-Sni)

    ctx.sni_callback = remember_sni
    return ctx


def main() -> None:
    ctx = tls_context()
    servers = [Server(443, ctx), Server(8443, ctx), Server(8080, None)]
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print("mock origin listening on 443 (https), 8443 (https), 8080 (http)", flush=True)
    servers[0].serve_forever()


if __name__ == "__main__":
    main()
