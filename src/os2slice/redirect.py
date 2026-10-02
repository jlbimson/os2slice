"""A plain-HTTP listener that only redirects to the HTTPS /admin page.

Home Assistant's "Open Web UI" button can only point at http(s)://[HOST]:[PORT:n], where
[HOST] is whatever name the browser used for Home Assistant. That can't reach the HTTPS
service (its certificate is for the DuckDNS name, and it refuses any other Host), so this
listener answers every GET/HEAD with a 303 to `https://<server.hosts[0]>/admin`. It serves
no content, reads no request body, echoes nothing from the request, and refuses other
methods with 405.
"""

from __future__ import annotations

import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)


def make_redirect_server(bind: str, port: int, target: str) -> ThreadingHTTPServer:
    """Bind a server that 303s every GET/HEAD to `target` (a URL built from config)."""
    location = target

    class RedirectHandler(BaseHTTPRequestHandler):
        server_version = "os2slice"
        sys_version = ""
        protocol_version = "HTTP/1.0"  # one request per connection, nothing kept open

        def _redirect(self, body: bool) -> None:
            data = b"See the os2slice admin page over HTTPS.\n"
            self.send_response(303)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if body:
                self.wfile.write(data)

        def do_GET(self) -> None:
            self._redirect(body=True)

        def do_HEAD(self) -> None:
            self._redirect(body=False)

        def _refuse(self) -> None:
            self.send_response(405)
            self.send_header("Allow", "GET, HEAD")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def __getattr__(self, name: str) -> object:
            # Every other method (POST, PUT, anything made up) gets 405, not the stdlib's 501.
            if name.startswith("do_"):
                return self._refuse
            raise AttributeError(name)

        def log_message(self, format: str, *args: object) -> None:
            log.debug("redirect listener: request answered")  # nothing from the request

        def log_error(self, format: str, *args: object) -> None:
            log.debug("redirect listener: bad request")

    httpd = ThreadingHTTPServer((bind, port), RedirectHandler)
    httpd.daemon_threads = True
    return httpd
