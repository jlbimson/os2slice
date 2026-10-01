"""Phase 0 spike: log what an Onshape extension sends to http://localhost.

    python3 scripts/spike_listener.py [port]   # default 8765

Binds 127.0.0.1 only. Prints the path and the headers that matter for
validation (Host, Referer, Origin, Sec-Fetch-*), never cookies.
"""

from __future__ import annotations

import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, urlsplit

SHOW = ("host", "referer", "origin", "user-agent")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        url = urlsplit(self.path)
        print(f"\n{self.command} {url.path}", flush=True)
        for k, v in parse_qsl(url.query, keep_blank_values=True):
            print(f"  param {k} = {v!r}", flush=True)
        for k, v in self.headers.items():
            if k.lower() in SHOW or k.lower().startswith("sec-fetch"):
                print(f"  header {k}: {v}", flush=True)
        body = b"os2slice spike listener: request logged. You can close this tab.\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"listening on http://127.0.0.1:{port}", flush=True)
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
