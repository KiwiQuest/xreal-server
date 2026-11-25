"""Ariel Core / space - AR kalibrace kamera -> displej.

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

from .config import DATA_DIR
from .state import _lock, state
from .camera import eye


# ── AR: kalibrace kamera Eye → displej bryli (optical see-through) ─────────────
# Tablet ukazuje znacku ArUco, HUD zamerovac na znamem miste displeje. Uzivatel srovna
# zamerovac se znackou (pohybem hlavy nebo tabletu) a potvrdi. Z bodu (kamera u,v) ↔
# (displej x,y) se spocita homografie H: displej = H · kamera (vse normalizovane 0..1,
# kamera = cely zaber 1920x1080, displej = cele okno AR stranky).
CALIB_FILE = DATA_DIR / "calib.json"
AR_FILE    = DATA_DIR / "ar_settings.json"
CALIB_TARGETS = [(0.5, 0.5), (0.15, 0.15), (0.85, 0.15), (0.85, 0.85), (0.15, 0.85),
                 (0.5, 0.15), (0.85, 0.5), (0.5, 0.85), (0.15, 0.5)]
AR_DEFAULT = {"fill": True, "fill_alpha": 0.35, "eye": "both", "dwell": True, "dwell_s": 1.5, "labels": True, "outline": True, "boxes": True, "min_conf": 0.4, "smooth": 0.5,
              "head_comp": False, "px_deg": 38.0, "inv_x": False, "inv_y": False, "lag_ms": 0,
              "font": 1.0, "width": 3, "ocr": True, "validate": False}


class Calibrator:
    def __init__(self, src):
        self.src = src
        self.active, self.idx, self.points = False, 0, []
        self.marker = None              # {"uv": [u, v], "t": cas, "size": px}
        self.hist = []                  # posledni detekce (median pri potvrzeni)
        self.H, self.err, self.n_used = None, None, 0
        self.thread = None
        self.dwell_t0, self.dwell, self.last_cap = None, 0.0, None   # auto potvrzeni drzenim v klidu
        self.lock = threading.Lock()
        try:
            c = json.loads(CALIB_FILE.read_text(encoding="utf-8"))
            self.H, self.err, self.n_used = c["H"], c.get("err_px"), c.get("n", 0)
            print(f"[CAL] kalibrace nactena (chyba {self.err} px, {self.n_used} bodu)")
        except Exception:
            pass
        self.ar = dict(AR_DEFAULT)
        try:
            self.ar.update({k: v for k, v in json.loads(AR_FILE.read_text(encoding="utf-8")).items()
                            if k in AR_DEFAULT and k != "validate"})
        except Exception:
            pass
        self._publish()

    # -- detekce znacky (jen behem kalibrace / kontroly)
    def _need_detect(self):
        return self.active or self.ar.get("validate")

    def _ensure_thread(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._detect_loop, daemon=True)
            self.thread.start()

    def _detect_loop(self):
        import cv2
        import numpy as np
        d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
        prm = cv2.aruco.DetectorParameters()
        prm.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        det = cv2.aruco.ArucoDetector(d, prm)
        self.src.add_client()
        try:
            if not self.src.running():
                self.src.start()
            last = self.src.seq
            while self._need_detect():
                jpg, seq = self.src.wait_frame(last, timeout=3)
                if seq == last:
                    if not self.src.running():
                        self.src.start()
                    continue
                last = seq
                img, vmap = self.src.view(seq)
                if img is None:
                    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                    vmap = (0.0, 0.0, 1.0, 1.0)
                if img is None:
                    continue
                h, w = img.shape[:2]
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                corners, ids, _ = det.detectMarkers(gray)
                m = None
                if ids is not None:
                    for c, i in zip(corners, ids.flatten()):
                        if int(i) == 0:
                            pts = c.reshape(4, 2)
                            cx, cy = pts.mean(axis=0)
                            zx, zy, zw, zh = vmap        # do souradnic celeho zaberu
                            m = {"uv": [round(float(zx + cx / w * zw), 5), round(float(zy + cy / h * zh), 5)],
                                 "t": time.time(), "size": round(float(np.linalg.norm(pts[0] - pts[2])), 1)}
                            break
                with self.lock:
                    self.marker = m if m else (self.marker if self.marker and time.time() - self.marker["t"] < 0.3 else None)
                    if m:
                        self.hist = [x for x in self.hist if time.time() - x["t"] < 1.5] + [m]
                self._dwell_step(m)
                self._publish()
        except Exception as e:
            print(f"[CAL] detekce: {type(e).__name__}: {e}")
        finally:
            self.src.remove_client()
            with self.lock:
                self.marker = None
            self._publish()

    def _dwell_step(self, m):
        """Bez tabletu / bez ruky: znacka v kamere chvili v klidu = uzivatel mi zamerovacem -> potvrdit."""
        now = time.time()
        if not (self.active and self.ar.get("dwell") and m):
            self.dwell_t0, self.dwell = None, 0.0
            return
        if self.last_cap and math.hypot(m["uv"][0] - self.last_cap[0], m["uv"][1] - self.last_cap[1]) < 0.05:
            self.dwell_t0, self.dwell = None, 0.0          # po potvrzeni nejdriv pohnout hlavou k dalsimu bodu
            return
        recent = [x for x in self.hist if now - x["t"] < 0.6]
        if len(recent) < 4:
            self.dwell_t0, self.dwell = None, 0.0
            return
        cu = sum(x["uv"][0] for x in recent) / len(recent)
        cv = sum(x["uv"][1] for x in recent) / len(recent)
        spread = max(math.hypot(x["uv"][0] - cu, x["uv"][1] - cv) for x in recent)
        if spread > 0.006:                                  # hybe se -> znovu od nuly
            self.dwell_t0, self.dwell = None, 0.0
            return
        if self.dwell_t0 is None:
            self.dwell_t0 = now
        self.dwell = min(1.0, (now - self.dwell_t0) / max(0.3, float(self.ar.get("dwell_s", 1.5))))
        if self.dwell >= 1.0:
            self.dwell_t0, self.dwell = None, 0.0
            r = self.confirm()
            with _lock:
                state["calib_msg"] = {"t": time.time(), "auto": True, **r}

    # -- kalibrace
    def start(self):
        self.src.set_zoom({"reset": True})              # kalibruje se cely zaber
        with self.lock:
            self.active, self.idx, self.points, self.hist = True, 0, [], []
            self.last_cap, self.dwell_t0, self.dwell = None, None, 0.0
        print("[CAL] start")
        self._ensure_thread()
        self._publish()

    def confirm(self):
        if not self.active:
            return {"ok": False, "msg": "kalibrace nebezi"}
        with self.lock:
            fresh = [x for x in self.hist if time.time() - x["t"] < 0.5]
        if not fresh:
            return {"ok": False, "msg": "kamera značku nevidí"}
        import statistics
        uv = [statistics.median(x["uv"][0] for x in fresh), statistics.median(x["uv"][1] for x in fresh)]
        tx, ty = CALIB_TARGETS[self.idx]
        with self.lock:
            self.points.append({"cam": uv, "disp": [tx, ty]})
            self.idx += 1
            self.last_cap, self.dwell_t0, self.dwell = uv, None, 0.0
        print(f"[CAL] bod {self.idx}/{len(CALIB_TARGETS)}: kamera {uv} -> displej {[tx, ty]}")
        if self.idx >= len(CALIB_TARGETS):
            return self.finish()
        self._publish()
        return {"ok": True}

    def skip(self):
        if self.active:
            self.idx += 1
            if self.idx >= len(CALIB_TARGETS):
                return self.finish()
            self._publish()
        return {"ok": True}

    def back(self):
        if self.active and self.idx > 0:
            with self.lock:
                self.idx -= 1
                tgt = list(CALIB_TARGETS[self.idx])
                self.points = [p for p in self.points if p["disp"] != tgt]
            self._publish()
        return {"ok": True}

    def cancel(self):
        self.active = False
        self._publish()
        return {"ok": True}

    def finish(self):
        import cv2
        import numpy as np
        self.active = False
        pts = self.points
        if len(pts) < 4:
            self._publish()
            return {"ok": False, "msg": f"málo bodů ({len(pts)}), potřeba aspoň 4"}
        cam = np.float32([p["cam"] for p in pts])
        disp = np.float32([p["disp"] for p in pts])
        H, mask = cv2.findHomography(cam, disp, cv2.RANSAC if len(pts) >= 6 else 0, 0.03)
        if H is None:
            self._publish()
            return {"ok": False, "msg": "z bodů nejde spočítat převod – zkus znovu"}
        proj = cv2.perspectiveTransform(cam.reshape(-1, 1, 2), H).reshape(-1, 2)
        e = (proj - disp) * np.float32([1920, 1080])
        used = int(mask.sum()) if mask is not None else len(pts)
        err = round(float(np.sqrt((e ** 2).sum(axis=1).mean())), 1)
        self.H, self.err, self.n_used = H.tolist(), err, used
        CALIB_FILE.write_text(json.dumps({"H": self.H, "err_px": err, "n": used, "points": pts,
                                          "t": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=1), encoding="utf-8")
        print(f"[CAL] hotovo: {used}/{len(pts)} bodu, prumerna chyba {err} px")
        self._publish()
        return {"ok": True, "err_px": err, "n": used}

    def set_ar(self, b):
        for k, v in b.items():
            if k in AR_DEFAULT:
                if k == "eye" and v not in ("both", "left", "right"):
                    continue
                self.ar[k] = type(AR_DEFAULT[k])(v) if not isinstance(AR_DEFAULT[k], bool) else bool(v)
        try:
            AR_FILE.write_text(json.dumps({k: v for k, v in self.ar.items() if k != "validate"}, indent=1),
                               encoding="utf-8")
        except Exception:
            pass
        if self.ar.get("validate"):
            self._ensure_thread()
        self._publish()
        return dict(self.ar)

    def _publish(self):
        with self.lock:
            m = self.marker
            c = {"active": self.active, "idx": self.idx, "n": len(CALIB_TARGETS),
                 "target": list(CALIB_TARGETS[self.idx]) if self.active and self.idx < len(CALIB_TARGETS) else None,
                 "seen": bool(m and time.time() - m["t"] < 0.3), "uv": m["uv"] if m else None,
                 "size": m["size"] if m else None, "H": self.H, "err": self.err, "n_used": self.n_used,
                 "points": len(self.points), "dwell": round(self.dwell, 2)}
        with _lock:
            state["calib"] = c
            state["ar"] = dict(self.ar)


calib = Calibrator(eye)
