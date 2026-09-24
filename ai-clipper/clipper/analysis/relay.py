"""A tiny local HTTP relay so ffmpeg can seek inside online videos.

ffmpeg reads http://127.0.0.1:PORT/N and jumps around with Range requests; Python fetches those byte
ranges from the real URL. That way only a clip's worth of bytes is downloaded from a 200-hour video,
proxies set for Python are honoured, and ffmpeg never resolves a host name itself (some static
ffmpeg builds crash on DNS lookups).
"""
from __future__ import annotations

import shutil
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PASS_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Last-Modified", "ETag")


class Relay:
    def __init__(self, targets: list[tuple[str, dict]]) -> None:
        self.targets = targets
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args) -> None:  # quiet
                pass

            def _target(self):
                try:
                    return relay.targets[int(self.path.strip("/").split("?")[0])]
                except (ValueError, IndexError):
                    return None

            def do_HEAD(self) -> None:
                self._forward(head=True)

            def do_GET(self) -> None:
                self._forward(head=False)

            def _forward(self, head: bool) -> None:
                t = self._target()
                if t is None:
                    self.send_error(404)
                    return
                url, headers = t
                req = urllib.request.Request(url, headers=dict(headers), method="HEAD" if head else "GET")
                if self.headers.get("Range"):
                    req.add_header("Range", self.headers["Range"])
                try:
                    resp = urllib.request.urlopen(req, timeout=60)
                except urllib.error.HTTPError as exc:
                    resp = exc
                except Exception:
                    self.send_error(502)
                    return
                with resp:
                    self.send_response(resp.status if hasattr(resp, "status") else resp.code)
                    for h in PASS_HEADERS:
                        if resp.headers.get(h):
                            self.send_header(h, resp.headers[h])
                    self.send_header("Connection", "close")
                    self.end_headers()
                    if not head:
                        try:
                            shutil.copyfileobj(resp, self.wfile, 256 * 1024)
                        except (BrokenPipeError, ConnectionResetError):
                            pass  # ffmpeg seeks by dropping the connection

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="relay")

    def url(self, n: int) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/{n}"

    def __enter__(self) -> "Relay":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()
