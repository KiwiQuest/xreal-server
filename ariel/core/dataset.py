"""Ariel Core / dataset - Snimky pro dataset na tlacitko bryli.

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
from .head_mouse import SETTINGS_FILE
from .camera import eye


# ── DATASET: snimky ve full HD z kamery Eye na tlacitko bryli (pro trenink YOLO) ──
DATASET_DIR = Path(os.environ.get("DATASET_DIR", str(DATA_DIR / "dataset" / "raw")))

DS_DEFAULT = {"kid": 1, "burst_s": 0.5, "best_of": 5, "sound": True, "on": True}


class DatasetCapture:
    def __init__(self, src):
        self.src = src
        self.cfg = dict(DS_DEFAULT)
        try:
            self.cfg.update({k: v for k, v in json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                             .get("dataset", {}).items() if k in DS_DEFAULT})
        except Exception:
            pass
        self.learn = False
        self.held = False
        self.lock = threading.Lock()
        self.count = len(list(DATASET_DIR.glob("*.jpg"))) if DATASET_DIR.exists() else 0
        self.last = None
        self._publish()

    def _save_cfg(self):
        try:
            d = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.exists() else {}
            d["dataset"] = self.cfg
            SETTINGS_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[DS] nastaveni nejde ulozit: {e}")

    def _publish(self, flash=False):
        with _lock:
            state["dataset"] = {"count": self.count, "last": self.last, "dir": str(DATASET_DIR),
                                "learn": self.learn, "t": time.time() if flash else
                                (state.get("dataset") or {}).get("t", 0), **self.cfg}

    def set(self, b):
        for k in DS_DEFAULT:
            if k in b:
                self.cfg[k] = type(DS_DEFAULT[k])(b[k]) if not isinstance(DS_DEFAULT[k], bool) else bool(b[k])
        if "learn" in b:
            self.learn = bool(b["learn"])
            if self.learn:
                print("[DS] uceni tlacitka: zmackni na brylich tlacitko, kterym chces fotit")
        self._save_cfg()
        self._publish()
        return dict(self.cfg, learn=self.learn, count=self.count)

    # -- tlacitko bryli
    def button(self, kid, st):
        if st == 1 and self.learn:
            self.learn = False
            self.cfg["kid"] = int(kid)
            self._save_cfg()
            print(f"[DS] tlacitko pro snimky nastaveno: id {kid}")
            self._beep(2)
            self._publish(flash=True)
            return
        if not self.cfg.get("on") or int(kid) != int(self.cfg["kid"]):
            return
        if st == 1 and not self.held:
            self.held = True
            threading.Thread(target=self._burst, daemon=True).start()
        elif st == 2:
            self.held = False

    def _burst(self):
        self.shoot("tlacitko")
        t0 = time.time()
        while self.held and self.cfg.get("burst_s", 0) > 0:      # podrzeni = serie
            time.sleep(0.05)
            if time.time() - t0 >= max(0.6, float(self.cfg["burst_s"])):
                t0 = time.time()
                if self.held:
                    self.shoot("serie")

    def trigger(self, why="tablet"):
        threading.Thread(target=self.shoot, args=(why,), daemon=True).start()
        return {"ok": True}

    # -- snimek
    def shoot(self, why=""):
        import cv2
        import numpy as np
        with self.lock:
            src = self.src
            src.add_client()
            try:
                if not src.running():
                    if not src.start():
                        print(f"[DS] kamera: {src.status}")
                        return None
                if not src.raw_mode:
                    print("[DS] full HD jen v rezimu EYE_RAW=1")
                    return None
                # par cerstvych snimku po stisku -> ulozit nejostrejsi (stisk tlacitka hlavou cukne)
                t_press, best, best_s, last = time.time(), None, -1.0, src.raw_seq
                n = max(1, int(self.cfg.get("best_of", 5)))
                while time.time() - t_press < 1.5:
                    with src.raw_cond:
                        src.raw_cond.wait_for(lambda: src.raw_seq != last, timeout=0.5)
                        raw, last = src.raw, src.raw_seq
                    if raw is None:
                        continue
                    small = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_REDUCED_GRAYSCALE_4)
                    if small is None:
                        continue
                    sharp = float(cv2.Laplacian(small, cv2.CV_64F).var())
                    if sharp > best_s:
                        best, best_s = raw, sharp
                    n -= 1
                    if n <= 0:
                        break
                if best is None:
                    print("[DS] zadny snimek z kamery")
                    return None
                DATASET_DIR.mkdir(parents=True, exist_ok=True)
                now = time.time()
                lt = time.localtime(now)
                name = time.strftime("%Y-%m-%d_%H-%M-%S", lt) + f"-{int(now * 1000) % 1000:03d}.jpg"
                path = DATASET_DIR / name
                path.write_bytes(best)                    # originalni JPEG z kamery (1920x1080)
                idx = DATASET_DIR / "index.csv"
                new = not idx.exists()
                with idx.open("a", encoding="utf-8") as f:
                    if new:
                        f.write("soubor,cas,epoch,ostrost,yaw,pitch,zdroj\n")
                    f.write(f"{name},{time.strftime('%Y-%m-%d %H:%M:%S', lt)}.{int(now * 1000) % 1000:03d},"
                            f"{now:.3f},{best_s:.1f},{state.get('yaw', 0):.2f},{state.get('pitch', 0):.2f},{why}\n")
                self.count += 1
                self.last = name
                print(f"[DS] {name} ({len(best) // 1024} kB, ostrost {best_s:.0f}, {why}) – celkem {self.count}")
                self._beep(1)
                self._publish(flash=True)
                return name
            finally:
                src.remove_client()

    def _beep(self, n=1):
        if not self.cfg.get("sound"):
            return
        def go():
            try:
                import winsound
                for _ in range(n):
                    winsound.Beep(1800, 70)
                    time.sleep(0.05)
            except Exception:
                pass
        threading.Thread(target=go, daemon=True).start()


dataset = DatasetCapture(eye)
