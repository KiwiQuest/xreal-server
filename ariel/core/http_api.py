"""Ariel Core / http_api - HTTP server a API.

Vznikl rozdelenim xreal-web/server.py; chovani beze zmeny."""
import asyncio
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from .config import HTTP_PORT, STATIC_DIR
from .state import _lock, state
from .camera import EYE_FPS, EYE_WIDTH, eye
from .ocr import ocr
from .vision import LAYOUT_FILE, recorder, save_snapshot, yolo
from .phrases import PHRASE_LISTS, phrases_load, phrases_save, phrases_update
from .space import calib
from .dataset import dataset
from .commands import _calib_action, _handle_cmd, _ocr_action, calib_marker_png


# ── HTTP server (servuje static/) ─────────────────────────────────────────────
def _get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "localhost"


def _all_ips():
    """Vsechny IPv4 adresy notebooku (funguje i bez internetu)."""
    ips = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except Exception:
        pass
    main = _get_local_ip()
    if main != "localhost" and main not in ips:
        ips.insert(0, main)
    return ips

_scale_cache: dict = {}
_scale_lock = threading.Lock()


def _scaled(src, jpg, seq, w):
    """Zmensena kopie snimku (napr. pro tablet pres WiFi): pocita se jednou na snimek pro vsechny."""
    if not w or w <= 0 or jpg is None:
        return jpg
    key = (id(src), w)
    with _scale_lock:
        c = _scale_cache.get(key)
        if c and c[0] == seq:
            return c[1]
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if img is None or img.shape[1] <= w:
        out = jpg
    else:
        h = int(round(img.shape[0] * w / img.shape[1] / 2)) * 2
        ok, enc = cv2.imencode(".jpg", cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA),
                               [cv2.IMWRITE_JPEG_QUALITY, 80])
        out = enc.tobytes() if ok else jpg
    with _scale_lock:
        _scale_cache[key] = (seq, out)
    return out


def _http_thread():
    static = STATIC_DIR
    static.mkdir(exist_ok=True)

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(static), **kw)

        def end_headers(self):
            # tablet nesmi drzet starou verzi stranky v mezipameti
            if not self.path.startswith(("/eye", "/yolo")):
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            super().end_headers()

        def log_message(self, fmt, *args):
            path = self.path.split("?", 1)[0] if hasattr(self, "path") else "?"
            if path not in ("/eye/status", "/state", "/cmd", "/yolo.json", "/rec/status", "/ocr.json",
                            "/ar/alive", "/calib.json"):
                print(f"[HTTP] {self.client_address[0]} {path} -> {args[1] if len(args) > 1 else ''}")

        def _text(self, code, text, ctype="text/plain; charset=utf-8"):
            body = text.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _json_body(self, default):
            n = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw) if raw else default

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                if path == "/cmd":                  # zalozni cesta, kdyz WebSocket 8765 neprojde
                    data = self._json_body([])
                    for cmd in (data if isinstance(data, list) else [data]):
                        if isinstance(cmd, dict):
                            _handle_cmd(cmd)
                    self.send_response(204)
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    return
                if path == "/snap":
                    b = self._json_body({})
                    d = save_snapshot(b.get("image", ""), b.get("dets", []), b.get("t"), bool(b.get("live")))
                    return self._text(200, json.dumps({"dir": d}), "application/json")
                if path == "/rec/start":
                    b = self._json_body({})
                    if not yolo.start():
                        return self._text(503, json.dumps({"error": yolo.status}), "application/json")
                    info = recorder.start(bool(b.get("video", True)), bool(b.get("data", True)))
                    return self._text(200, json.dumps(info), "application/json")
                if path == "/yolo/config":
                    cfg = yolo.set_config(self._json_body({}))
                    return self._text(200, json.dumps(cfg), "application/json")
                if path == "/ocr":
                    try:
                        return self._text(200, json.dumps(_ocr_action(self._json_body({})), ensure_ascii=False),
                                          "application/json")
                    except Exception as e:
                        ocr.error = f"{type(e).__name__}: {e}"
                        print(f"[OCR] {ocr.error}")
                        return self._text(503, json.dumps({"error": ocr.error}, ensure_ascii=False),
                                          "application/json")
                if path == "/phrases":           # cely slovnik (editor)
                    d = phrases_save(self._json_body({}))
                    print(f"[PHR] ulozeno: {', '.join(f'{k} {len(d[k])}' for k in PHRASE_LISTS)}, "
                          f"naucenych {len(d['learned'])}")
                    return self._text(200, json.dumps(d, ensure_ascii=False), "application/json")
                if path == "/phrases/update":    # pouziti + naucena slova (z klavesnice)
                    phrases_update(self._json_body({}))
                    return self._text(200, "ok")
                if path == "/calib":
                    r = _calib_action(self._json_body({}))
                    with _lock:
                        state["calib_msg"] = {"t": time.time(), **r}
                    return self._text(200, json.dumps(r, ensure_ascii=False), "application/json")
                if path == "/ar/settings":
                    return self._text(200, json.dumps(calib.set_ar(self._json_body({}))), "application/json")
                if path == "/dataset":           # {"shoot":1} / {"learn":true} / nastaveni
                    b = self._json_body({})
                    if b.pop("shoot", None):
                        dataset.trigger(b.pop("why", "web"))
                    return self._text(200, json.dumps(dataset.set(b)), "application/json")
                if path == "/ar/alive":          # AR stranka: detekce bezi i bez videa
                    yolo.keepalive()
                    return self._text(200, "ok")
                if path == "/eye/zoom":
                    return self._text(200, json.dumps(eye.set_zoom(self._json_body({}))), "application/json")
                if path == "/yolo/texture":
                    return self._text(200, json.dumps(yolo.set_texture(self._json_body({}))), "application/json")
                if path == "/yolo/bench":
                    b = self._json_body({})
                    ok = yolo.start_bench(b.get("sizes"), b.get("backends"), b.get("task"))
                    return self._text(200 if ok else 409, json.dumps(yolo.bench), "application/json")
                if path == "/rec/stop":
                    return self._text(200, json.dumps(recorder.stop()), "application/json")
                if path == "/layout":
                    b = self._json_body({})
                    LAYOUT_FILE.write_text(json.dumps(b, indent=2), encoding="utf-8")
                    print(f"[HUD] rozlozeni ulozeno: {LAYOUT_FILE.name}")
                    return self._text(200, "ok")
                return self._text(404, "not found")
            except Exception as e:
                print(f"[HTTP] {path} chyba: {type(e).__name__}: {e}")
                self._text(400, str(e))

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/state":
                with _lock:
                    body = json.dumps(state, default=str)
                return self._text(200, body, "application/json")
            if path == "/eye/status":
                return self._text(200, json.dumps({"status": eye.status, "running": eye.running(),
                                                   "clients": eye.clients, "device": eye.devnum,
                                                   "width": EYE_WIDTH, "fps": EYE_FPS,
                                                   "frames": eye.frames, "zoom": eye.zoom_info(),
                                                   "proc_ms": eye.proc_ms}),
                                  "application/json")
            if path in ("/eye.mjpg", "/eye.jpg"):
                return self._eye(eye, single=(path == "/eye.jpg"))
            if path in ("/eye_yolo.mjpg", "/eye_yolo.jpg"):
                return self._eye(yolo, single=(path == "/eye_yolo.jpg"))
            if path == "/yolo.json":
                return self._text(200, json.dumps(yolo.info()), "application/json")
            if path == "/rec/status":
                return self._text(200, json.dumps(recorder.info()), "application/json")
            if path == "/calib":                 # znacka + ovladani kalibrace pro libovolnou obrazovku
                path = "/calib.html"
                self.path = "/calib.html"
            if path == "/calib/marker.png":
                body = calib_marker_png()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/calib.json":
                with _lock:
                    return self._text(200, json.dumps({"calib": state.get("calib"), "ar": state.get("ar")}),
                                      "application/json")
            if path == "/phrases.json":
                return self._text(200, json.dumps(phrases_load(), ensure_ascii=False), "application/json")
            if path == "/ocr.json":
                return self._text(200, json.dumps(ocr.info(), ensure_ascii=False), "application/json")
            if path == "/layout":
                body = LAYOUT_FILE.read_text(encoding="utf-8") if LAYOUT_FILE.exists() else "{}"
                return self._text(200, body, "application/json")
            return super().do_GET()

        def _eye(self, src, single):
            from urllib.parse import parse_qs, urlparse
            try:
                want_w = int(parse_qs(urlparse(self.path).query).get("w", ["0"])[0])
            except ValueError:
                want_w = 0
            src.add_client()
            if src is yolo:
                yolo.video_viewers += 1
            try:
                if not src.start():
                    return self._text(503, f"Eye: {src.status}")
                jpg, seq = src.frame, src.seq
                if jpg is None:
                    jpg, seq = src.wait_frame(seq, timeout=60 if src is yolo else 8)
                if jpg is None:
                    return self._text(503, "Eye: zadny snimek")
                jpg = _scaled(src, jpg, seq, want_w)
                if single:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(jpg)))
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(jpg)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                while True:
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                                     % len(jpg) + jpg + b"\r\n")
                    self.wfile.flush()
                    jpg2, seq2 = src.wait_frame(seq, timeout=5)
                    if seq2 == seq:
                        if not src.running():
                            break
                        continue
                    jpg, seq = _scaled(src, jpg2, seq2, want_w), seq2
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                src.remove_client()
                if src is yolo:
                    yolo.video_viewers = max(0, yolo.video_viewers - 1)

    srv = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    srv.daemon_threads = True
    print(f"[HTTP] server na http://localhost:{HTTP_PORT}")
    srv.serve_forever()
