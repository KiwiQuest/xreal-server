"""Ariel Core / head_mouse - Mys ovladana hlavou.

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
from .winput import _mouse_move


# ── mys ovladana hlavou (IMU bryli) + nastaveni ulozena v settings.json ─────────
SETTINGS_FILE = DATA_DIR / "settings.json"
HEAD = {"on": False, "gain": 22.0, "dead": 2.0, "inv_x": False, "inv_y": False, "swap": False}
try:
    HEAD.update({k: v for k, v in json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("head", {}).items()
                 if k in HEAD and k != "on"})          # po startu je head tracking vzdy vypnuty
except Exception:
    pass


def _save_settings():
    try:
        d = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")) if SETTINGS_FILE.exists() else {}
        d["head"] = {k: v for k, v in HEAD.items() if k != "on"}      # ostatni sekce (dataset) nechat
        SETTINGS_FILE.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[SET] nelze ulozit: {e}")


class _HeadMouse:
    """Uhlova rychlost hlavy (°/s) -> pohyb kurzoru. Mrtva zona potlaci tres a drift, mirne
    zrychleni: pomaly pohyb = presne mireni, rychly = pres celou obrazovku."""
    def __init__(self):
        self.ax = self.ay = 0.0
        self.t_flush = 0.0

    def feed(self, wy, wp, dt):
        if HEAD["swap"]:
            wy, wp = wp, wy
        g, dead = float(HEAD["gain"]), float(HEAD["dead"])
        for i, w in enumerate((wy, wp)):
            a = abs(w)
            if a <= dead:
                continue
            v = (a - dead) * (1.0 + min(a, 120.0) / 60.0)     # zrychleni do 3x
            px = math.copysign(v * dt * g, w)
            if i == 0:
                self.ax += -px if HEAD["inv_x"] else px
            else:
                self.ay += px if HEAD["inv_y"] else -px      # hlava nahoru = kurzor nahoru
        now = time.time()
        if now - self.t_flush >= 0.008:                       # ~120x za sekundu
            self.t_flush = now
            dx, dy = int(self.ax), int(self.ay)
            if dx or dy:
                self.ax -= dx
                self.ay -= dy
                _mouse_move(dx, dy)

    def reset(self):
        self.ax = self.ay = 0.0


head_mouse = _HeadMouse()
state["head"] = dict(HEAD)


def _set_head(cmd: dict):
    for k in ("gain", "dead"):
        if k in cmd:
            HEAD[k] = round(min(max(float(cmd[k]), 0.0), 200.0), 2)
    for k in ("inv_x", "inv_y", "swap"):
        if k in cmd:
            HEAD[k] = bool(cmd[k])
    if "on" in cmd or cmd.get("toggle"):
        HEAD["on"] = (not HEAD["on"]) if cmd.get("toggle") else bool(cmd["on"])
        head_mouse.reset()
        print(f"[HEAD] mys hlavou {'ZAPNUTA' if HEAD['on'] else 'vypnuta'} ({HEAD})")
    _save_settings()
    with _lock:
        state["head"] = dict(HEAD)
    return dict(HEAD)
