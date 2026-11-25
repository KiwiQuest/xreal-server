#!/usr/bin/env python3
"""xreal-sim – Flight simulator HUD (klon xreal-web)
Spuštění:  python server.py
Browser:   http://localhost:8081
WebSocket: ws://localhost:8766
"""
import asyncio
import json
import math
import socket
import struct
import threading
import time
from collections import deque
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

# ── IMU konstanty (XREAL One Pro) ─────────────────────────────────────────────
IP       = "169.254.2.1"
PORT_IMU = 52998
MAGIC    = bytes.fromhex("283600000080")
PKT      = 134
TYPE_IMU = 11

WS_PORT   = 8766   # jiný port než xreal-web (8765)
HTTP_PORT = 8081   # jiný port než xreal-web (8080)

# ── sdílený stav ──────────────────────────────────────────────────────────────
state = {
    "yaw":   0.0,
    "pitch": 0.0,
    "hz":    0.0,
    "connected": False,
    "calibrating": True,
    "status": "Čekám na brýle…",
}
_clients: set = set()
_lock = threading.Lock()

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
        self._win=deque(); self.hz=0.
        self._yaw_int=0.; self._pitch_int=0.
        self._ref_yaw=0.; self._ref_pitch=0.

    def update(self, ts_ns, gyr, acc):
        now = ts_ns*1e-9
        if self._last is None: self._last=now; return
        dt = min(max(now-self._last, 1e-4), 0.05); self._last=now
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

    @property
    def yaw(self):   return self._yaw_int   - self._ref_yaw
    @property
    def pitch(self): return self._pitch_int - self._ref_pitch


class _DummyCtx:
    def __enter__(self): pass
    def __exit__(self, *a): pass

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
            time.sleep(3.0)


async def _ws_handler(ws):
    _clients.add(ws)
    print(f"[WS] připojen {ws.remote_address}")
    try:
        async for _ in ws: pass
    except Exception:
        pass
    finally:
        _clients.discard(ws)

async def _broadcast_loop():
    while True:
        if _clients:
            with _lock:
                msg = json.dumps(state, default=str)
            dead = set()
            for ws in list(_clients):
                try:    await ws.send(msg)
                except: dead.add(ws)
            _clients.difference_update(dead)
        await asyncio.sleep(0.016)

async def _ws_main():
    import websockets
    async with websockets.serve(_ws_handler, "0.0.0.0", WS_PORT):
        print(f"[WS] server na ws://localhost:{WS_PORT}")
        await _broadcast_loop()

def _http_thread():
    static = Path(__file__).parent / "static"
    static.mkdir(exist_ok=True)
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=str(static), **kw)
        def log_message(self, fmt, *args): pass
    srv = HTTPServer(("0.0.0.0", HTTP_PORT), Handler)
    print(f"[HTTP] server na http://localhost:{HTTP_PORT}")
    srv.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=_imu_thread, daemon=True).start()
    threading.Thread(target=_http_thread, daemon=True).start()
    print("xreal-sim startuje…")
    print(f"  Otevři v Chrome: http://localhost:{HTTP_PORT}/sim.html")
    asyncio.run(_ws_main())
