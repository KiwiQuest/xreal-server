"""Ariel Core / glasses - Bryle: IMU, orientace, tlacitka.

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

from .config import IP, MAGIC, MSG_KEY, PKT, PORT_CTRL, PORT_IMU, TYPE_IMU
from .state import _lock, state
from .head_mouse import HEAD, head_mouse


# ── IMU parsing ───────────────────────────────────────────────────────────────
def _imu_packets(chunks):
    buf = bytearray()
    for chunk in chunks:
        buf.extend(chunk)
        while True:
            pos = buf.find(MAGIC)
            if pos == -1: del buf[:-len(MAGIC)]; break
            if pos > 0:   del buf[:pos]
            if len(buf) < PKT + len(MAGIC): break
            if buf[PKT:PKT+len(MAGIC)] != MAGIC: del buf[:1]; continue
            yield bytes(buf[:PKT]); del buf[:PKT]

def _parse_imu(p):
    if struct.unpack_from("<I", p, 0x1E)[0] != TYPE_IMU: return None
    ts, = struct.unpack_from("<Q", p, 0x0E)
    g   = struct.unpack_from("<fff", p, 0x22)
    a   = struct.unpack_from("<fff", p, 0x2E)
    if any(math.isnan(v) for v in g + a): return None
    return ts, g, a

# ── Mahony orientation filter ─────────────────────────────────────────────────
def _dot(a, b): return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]
def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
def _qnorm(q):
    n = math.sqrt(sum(c*c for c in q)) or 1.0; return tuple(c/n for c in q)
def _up(q):
    w,x,y,z=q; return (2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y))

class _Orientation:
    KP=0.5; KI=0.005
    def __init__(self):
        self.q=(1.,0.,0.,0.); self.bias=[0.,0.,0.]
        self.calibrating=True; self._cal=[]; self._last=None
        self._win=deque(); self.hz=0.; self.dt=0.
        self._yaw_int=0.; self._pitch_int=0.
        self._ref_yaw=0.; self._ref_pitch=0.

    def update(self, ts_ns, gyr, acc):
        now = ts_ns*1e-9
        if self._last is None: self._last=now; return
        dt = min(max(now-self._last, 1e-4), 0.05); self._last=now
        self.dt=dt
        an = math.sqrt(_dot(acc,acc))
        if 0.5 < an < 2.0:
            an_v = tuple(v/an for v in acc)
            up   = _up(self.q)
            err  = _cross(an_v, up)
            for i in range(3): self.bias[i] += self.KI*err[i]*dt
            gyr2 = tuple(gyr[i] - self.bias[i] + self.KP*err[i] for i in range(3))
        else:
            gyr2 = tuple(gyr[i]-self.bias[i] for i in range(3))
        half = tuple(v*dt*0.5 for v in gyr2)
        w,x,y,z = self.q
        dq = (-x*half[0]-y*half[1]-z*half[2],
               w*half[0]+y*half[2]-z*half[1],
               w*half[1]-x*half[2]+z*half[0],
               w*half[2]+x*half[1]-y*half[0])
        self.q = _qnorm(tuple(self.q[i]+dq[i] for i in range(4)))
        gd = [math.degrees(v) for v in gyr2]
        self._win.append(now)
        while self._win and now-self._win[0]>1.0: self._win.popleft()
        self.hz = len(self._win)
        if self.calibrating:
            self._cal.append((gd[0], gd[2]))
            if len(self._cal) >= int(self.hz*1.2) and self.hz > 20:
                self.calibrating = False
                self._yaw_int = self._pitch_int = 0.
        else:
            self._yaw_int   -= gd[2] * dt
            self._pitch_int +=  gd[0] * dt
            self.rate_yaw, self.rate_pitch = -gd[2], gd[0]     # °/s pro ovladani mysi hlavou

    @property
    def yaw(self):   return self._yaw_int   - self._ref_yaw
    @property
    def pitch(self): return self._pitch_int - self._ref_pitch


# ── IMU čtecí vlákno ──────────────────────────────────────────────────────────
def _imu_thread():
    ori = _Orientation()
    while True:
        try:
            with _lock:
                state["status"] = "Připojuji k brýlím…"
            s = socket.create_connection((IP, PORT_IMU), timeout=5.0)
            with _lock:
                state["connected"] = True
                state["status"] = "Kalibrace – drž brýle v klidu"
            def chunks():
                while True:
                    d = s.recv(4096)
                    if not d: return
                    yield d
            for pkt in _imu_packets(chunks()):
                r = _parse_imu(pkt)
                if not r: continue
                ts, gyr, acc = r
                ori.update(ts, gyr, acc)
                if HEAD["on"] and not ori.calibrating:
                    head_mouse.feed(getattr(ori, "rate_yaw", 0.0), getattr(ori, "rate_pitch", 0.0), ori.dt)
                with _lock:
                    state["yaw"]         = round(ori.yaw, 3)
                    state["pitch"]       = round(ori.pitch, 3)
                    state["hz"]          = round(ori.hz, 1)
                    state["calibrating"] = ori.calibrating
                    state["status"]      = "Kalibrace…" if ori.calibrating else "OK"
        except Exception as e:
            with _lock:
                state["connected"]   = False
                state["calibrating"] = True
                state["status"]      = f"Chyba: {e}"
                if HEAD["on"]:                   # bez IMU nema mys hlavou smysl
                    HEAD["on"] = False
                    state["head"] = dict(HEAD)
            time.sleep(3.0)



BUTTON_LISTENERS = []   # funkce (id, stav) volane pri stisku tlacitka bryli


def _ctrl_thread():
    """Ridici kanal bryli (tlacitka). Ramec: 0x27, typ, delka (4 B big-endian), data."""
    while True:
        try:
            sock = socket.create_connection((IP, PORT_CTRL), timeout=5.0)
            sock.settimeout(1.0)
            print(f"[BTN] tlacitka bryli pripojena ({IP}:{PORT_CTRL})")
            buf = bytearray()
            while True:
                try:
                    d = sock.recv(4096)
                    if not d:
                        raise OSError("spojeni ukonceno")
                    buf.extend(d)
                except socket.timeout:
                    continue
                while True:
                    p = buf.find(b"\x27")
                    if p == -1:
                        buf.clear()
                        break
                    if p > 0:
                        del buf[:p]
                    if len(buf) < 6:
                        break
                    n = int.from_bytes(buf[2:6], "big")
                    if buf[2:4] != b"\x00\x00" or n > 4096:
                        del buf[:1]
                        continue
                    if len(buf) < 6 + n:
                        break
                    mid, body = buf[1], bytes(buf[6:6 + n])
                    del buf[:6 + n]
                    if mid == MSG_KEY and len(body) >= 8:
                        kid, st = struct.unpack_from("<II", body, 0)
                        print(f"[BTN] tlacitko id {kid} {'dolu' if st == 1 else 'nahoru' if st == 2 else st}")
                        with _lock:
                            state["btn"] = {"id": kid, "st": st, "t": time.time()}
                        try:
                            for _cb in list(BUTTON_LISTENERS):   # dataset se registruje v ariel.py
                                _cb(kid, st)
                        except Exception as e:
                            print(f"[DS] {type(e).__name__}: {e}")
        except OSError:
            time.sleep(3)
