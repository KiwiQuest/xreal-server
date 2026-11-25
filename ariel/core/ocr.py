"""Ariel Core / ocr - OCR nad obrazem kamery.

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

from .state import _lock, state
from .camera import eye


# ── OCR nad tim, co je videt (pri zoomu z plneho 1080p vyrezu) → /ocr.json, state["ocr"]
OCR_LIVE_S = float(os.environ.get("OCR_LIVE_S", "1.0"))    # jak casto cte rezim LIVE
OCR_MIN_W  = int(os.environ.get("OCR_MIN_W", "1600"))      # maly vyrez zvetsit (text ma dost pixelu)


class OcrWorker:
    def __init__(self, src):
        self.src = src
        self.live, self.lang, self.result, self.seq = False, None, None, 0
        self.status, self.error = "pripraveno", None
        self.wake = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def _engine(self):
        try:
            import ocr_engine
            return ocr_engine
        except Exception as e:
            raise RuntimeError(f"ocr_engine.py nejde nacist: {e}")

    def _image(self):
        """Obrazek pro OCR: presne vyrez, ktery je videt, ale v plnem rozliseni kamery."""
        import cv2
        import numpy as np
        src = self.src
        img = None
        if src.raw_mode and src.raw:
            full = cv2.imdecode(np.frombuffer(src.raw, np.uint8), cv2.IMREAD_COLOR)
            if full is not None:
                h, w = full.shape[:2]
                x0, y0, cw, ch = src.vmap
                img = full[int(y0 * h):int((y0 + ch) * h), int(x0 * w):int((x0 + cw) * w)]
        if (img is None or img.size == 0) and src.frame:
            img = cv2.imdecode(np.frombuffer(src.frame, np.uint8), cv2.IMREAD_COLOR)
        if img is None or img.size == 0:
            return None
        if img.shape[1] < OCR_MIN_W:
            k = OCR_MIN_W / img.shape[1]
            img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
        return img

    def read(self):
        """Jedno precteni (blokuje ~0.1-0.5 s). Kdyz kamera nebezi, spusti ji."""
        eng = self._engine()
        if not self.src.running():
            if not self.src.start():
                raise RuntimeError("Eye: " + self.src.status)
        if self.src.frame is None:
            self.src.wait_frame(self.src.seq, timeout=8)
        img = self._image()
        if img is None:
            raise RuntimeError("zadny snimek z kamery")
        self.status = "ctu…"
        res = eng.recognize(img, self.lang)
        res.update(t=round(time.time(), 3), zoom=self.src.zoom_info(), size=[img.shape[1], img.shape[0]])
        self.seq += 1
        res["seq"] = self.seq
        self.result, self.error = res, None
        self.status = f"{len(res['lines'])} radku · {res['ms']} ms"
        with _lock:                          # do WS jen radky (bez slov), at je zprava mala
            state["ocr"] = {"seq": self.seq, "t": res["t"], "ms": res["ms"], "lang": res["lang"],
                            "live": self.live,
                            "lines": [{"text": l["text"], "box": l["box"]} for l in res["lines"]]}
        if res["lines"]:
            print(f"[OCR] {len(res['lines'])} radku ({res['engine']} {res['lang']}, {res['ms']} ms): "
                  f"{res['text'][:80]!r}")
        return res

    def set_live(self, on):
        on = bool(on)
        if on and not self.live:
            self.src.add_client()            # LIVE drzi kameru zapnutou
            self.live = True
            self.wake.set()
        elif not on and self.live:
            self.live = False
            self.src.remove_client()
        with _lock:
            if "ocr" in state:
                state["ocr"]["live"] = self.live

    def _loop(self):
        while True:
            self.wake.wait(OCR_LIVE_S)
            self.wake.clear()
            if not self.live:
                continue
            try:
                self.read()
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                self.status = "chyba"
                print(f"[OCR] {self.error}")
                time.sleep(3)

    def info(self):
        st = {"backend": None, "error": None, "langs": []}
        try:
            st = self._engine().status()
        except Exception as e:
            st["error"] = str(e)
        return {"status": self.status, "error": self.error or st.get("error"), "backend": st.get("backend"),
                "langs": st.get("langs", []), "lang": self.lang, "live": self.live, "result": self.result}

    def text(self, lines=None):
        r = self.result or {}
        ls = r.get("lines", [])
        if lines:
            ls = [ls[i] for i in lines if isinstance(i, int) and 0 <= i < len(ls)]
        return "\n".join(l["text"] for l in ls)


ocr = OcrWorker(eye)
