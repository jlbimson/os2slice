"""Phase 2 spike: an Onshape Element right panel that logs what Onshape posts to it.

    python3 scripts/spike_panel.py [port]    # default 8766, binds 127.0.0.1

Extension (Location: Element right panel, Context: Part Studio), Action URL:
    http://localhost:8766/panel?d={$documentId}&wv={$workspaceOrVersion}&wvid={$workspaceOrVersionId}&e={$elementId}

The page sends `applicationInit`, then relays every message whose origin is
https://cad.onshape.com back to this server, which prints it. Read-only.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

ONSHAPE = "https://cad.onshape.com"
MAX_BODY = 64_000

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>os2slice panel spike</title>
<style>body{font:13px system-ui;margin:8px}pre{white-space:pre-wrap;font-size:11px}</style>
</head><body>
<b>os2slice panel spike</b>
<p id="st">starting…</p>
<pre id="log"></pre>
<script src="/panel.js"></script>
</body></html>
"""

SCRIPT = """
const ONSHAPE = __ONSHAPE__;
const q = new URLSearchParams(location.search);
const st = document.getElementById("st"), out = document.getElementById("log");
function relay(kind, data) {
  fetch("/log", {method: "POST", headers: {"Content-Type": "application/json"},
                 body: JSON.stringify({kind, data})});
}
relay("query", Object.fromEntries(q));
const server = q.get("server");
if (server !== ONSHAPE) {
  st.textContent = "server param " + server + " is not " + ONSHAPE + "; not initialising";
  relay("error", {server});
} else {
  const init = {documentId: q.get("d"), workspaceId: q.get("wvid"), elementId: q.get("e"),
                messageName: "applicationInit"};
  window.parent.postMessage(init, ONSHAPE);
  relay("sent", init);
  st.textContent = "applicationInit sent; select a part, then a face";
  setInterval(() => window.parent.postMessage(
      {documentId: init.documentId, workspaceId: init.workspaceId, elementId: init.elementId,
       messageName: "keepAlive"}, ONSHAPE), 30000);
}
window.addEventListener("message", (ev) => {
  if (ev.origin !== ONSHAPE) { relay("ignored-origin", {origin: ev.origin}); return; }
  relay("message", ev.data);
  out.textContent = JSON.stringify(ev.data, null, 1) + "\\n\\n" + out.textContent.slice(0, 4000);
});
""".replace("__ONSHAPE__", json.dumps(ONSHAPE))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str, frame: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        ancestors = ONSHAPE if frame else "'none'"
        self.send_header(
            "Content-Security-Policy",
            f"default-src 'self'; script-src 'self'; style-src 'unsafe-inline'; "
            f"frame-ancestors {ancestors}",
        )
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/panel":
            print(f"\nGET /panel {dict(parse_qsl(url.query))}", flush=True)
            for k in ("Sec-Fetch-Site", "Sec-Fetch-Dest", "Sec-Fetch-Mode", "Referer"):
                if k in self.headers:
                    print(f"  header {k}: {self.headers[k]}", flush=True)
            self._send(200, PAGE.encode(), "text/html; charset=utf-8", frame=True)
        elif url.path == "/panel.js":
            self._send(200, SCRIPT.encode(), "text/javascript; charset=utf-8")
        else:
            self._send(404, b"not found\n", "text/plain")

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/log":
            self._send(404, b"", "text/plain")
            return
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(min(n, MAX_BODY))
        try:
            msg = json.loads(body)
            print(f"[{msg.get('kind')}] {json.dumps(msg.get('data'))}", flush=True)
        except ValueError:
            print("bad log body", flush=True)
        self._send(204, b"", "text/plain")

    def log_message(self, format: str, *args: object) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    print(f"panel spike on http://127.0.0.1:{port}/panel", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
