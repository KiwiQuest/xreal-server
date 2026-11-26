"""Lokální server kořenové plochy (jen 127.0.0.1, bez knihoven navíc).

  GET  /            plocha (koren/ui/index.html)
  GET  /state       aktuální stav (JSON)
  GET  /events      Server-Sent Events: stav ~2x za sekundu (prohlížeč se sám znovu připojí)
  POST /cmd         {"module": "...", "action": "..."} -> fronta hlavního vlákna kořene
  GET/POST /layout  rozložení oken plochy (koren_ui_layout.json)

Stav skládá hlavní vlákno kořene a jen ho sem vloží (`publish`), server ho pouze čte.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import APP_DIR

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
LAYOUT_FILE = os.path.join(APP_DIR, "koren_ui_layout.json")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png"}


class UiServer:
    def __init__(self, post, port: int = 8790, log=print):
        self.post, self.port, self.log = post, port, log
        self._state = b"{}"
        self._ver = 0
        self._cond = threading.Condition()
        self.httpd = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def publish(self, state: dict):
        data = json.dumps(state, ensure_ascii=False).encode("utf-8")
        with self._cond:
            self._state, self._ver = data, self._ver + 1
            self._cond.notify_all()

    def start(self) -> bool:
        srv = self
        for port in (self.port, self.port + 1, self.port + 2):
            try:
                self.httpd = ThreadingHTTPServer(("127.0.0.1", port), _handler(srv))
                self.port = port
                break
            except OSError:
                continue
        if self.httpd is None:
            self.log("[UI] server nejde spustit (porty obsazené)")
            return False
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, name="ui-server", daemon=True).start()
        self.log(f"[UI] plocha na {self.url}")
        return True

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()

    def wait(self, ver: int, timeout: float):
        with self._cond:
            if self._ver == ver:
                self._cond.wait(timeout)
            return self._ver, self._state


def _handler(srv: UiServer):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json; charset=utf-8"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            if host not in ("127.0.0.1", "localhost"):
                return self._send(403, b"{}")
            path = self.path.split("?")[0]
            if path == "/state":
                return self._send(200, srv._state)
            if path == "/events":
                return self._events()
            if path == "/layout":
                try:
                    with open(LAYOUT_FILE, "rb") as f:
                        return self._send(200, f.read())
                except OSError:
                    return self._send(200, b"{}")
            name = "index.html" if path in ("/", "") else path.lstrip("/")
            full = os.path.normpath(os.path.join(UI_DIR, name))
            if not full.startswith(UI_DIR) or not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
            with open(full, "rb") as f:
                self._send(200, f.read(), TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            ver = -1
            try:
                while True:
                    ver, data = srv.wait(ver, 15.0)
                    self.wfile.write(b"data: " + data + b"\n\n")
                    self.wfile.flush()
            except (OSError, ValueError):
                return

        def _trusted(self) -> bool:
            """Jen naše stránka: správný Host (proti DNS rebinding) a vlastní hlavička, kterou cizí
            web bez CORS preflightu poslat nesmí (proti podvrženým POST z jiných stránek)."""
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost") and self.headers.get("X-Koren") == "1"

        def do_POST(self):
            if not self._trusted():
                return self._send(403, b'{"error":"forbidden"}')
            n = int(self.headers.get("Content-Length") or 0)
            if n > 200_000:
                return self._send(413, b"{}")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                body = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                return self._send(400, b'{"error":"json"}')
            path = self.path.split("?")[0]
            if path == "/cmd":
                srv.post(("ui_cmd", str(body.get("module", "")), str(body.get("action", "")), body.get("value")))
                return self._send(200, b'{"ok":true}')
            if path == "/layout":
                try:
                    tmp = LAYOUT_FILE + ".tmp"
                    with open(tmp, "wb") as f:
                        f.write(json.dumps(body, ensure_ascii=False, indent=1).encode("utf-8"))
                    os.replace(tmp, LAYOUT_FILE)
                    return self._send(200, b'{"ok":true}')
                except OSError as e:
                    return self._send(500, json.dumps({"error": str(e)}).encode())
            self._send(404, b"{}")
    return H
