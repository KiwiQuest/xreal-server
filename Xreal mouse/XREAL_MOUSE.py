"""
XREAL One Pro - Head Mouse
==========================
Ovladani kurzoru pohybem hlavy + circle click + tlacitko na brylích.

Spusteni:
    python XREAL_MOUSE.py
    python XREAL_MOUSE.py --dry      # nic nehybe (test bez bryli)
    python XREAL_MOUSE.py dump.bin   # prehrani zaznamu

Klavesy (globalni):
    F8 / Pause   zapnout / vypnout
    F9           kalibrovat gyro (drz bryle v klidu ~1s)
    F10          konec
"""
import math
import socket
import struct
import sys
import threading
import time
from collections import deque

# ── AR overlay (volitelne – vyzaduje xreal_suite/ v adresari programu) ────────
import os as _os, sys as _sys
_AR_SUITE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "xreal_suite")
if _os.path.isdir(_AR_SUITE) and _AR_SUITE not in _sys.path:
    _sys.path.insert(0, _AR_SUITE)
try:
    from xreal_suite.xreal.ar import (ArState, ArItem, cam_angle, to_screen, lookup,
                                       world_points, items_from_cv, items_from_boxes)
    from xreal_suite.xreal.ar_overlay import ArOverlay
    from xreal_suite.xreal.hud_overlay import HudOverlay
    from xreal_suite.xreal.eye import VideoStream, unpack_nibbles, W as EYE_W, H as EYE_H
    from xreal_suite.xreal.config import Config as ArConfig
    AR_AVAILABLE = True
except Exception as _ar_e:
    AR_AVAILABLE = False
    print(f"[AR] nedostupne: {_ar_e}")

# ──────────────────────────── konstanty ──────────────────────────────────────
IP   = "169.254.2.1"
PORT_IMU  = 52998
PORT_CTRL = 52999
MAGIC = bytes.fromhex("283600000080")
PKT   = 134
TYPE_IMU = 11
MSG_KEY  = 0x2E          # tlacitko bryli: u32 id, u32 stav 1=dolu 2=nahoru
X_BTN_ID = 4             # X tlacitko – potvrzeno logem (kid=4)
# TOP_BTN_ID je v Cfg.top_btn_id (default 1) – nastav po zjisteni sniff.py

IS_WIN = sys.platform == "win32"
try:
    import msvcrt
except ImportError:
    msvcrt = None

G = 9.80665
KP = 1.0                 # Mahony korekce naklonu z akcelerometru
CALIB_N   = 1000         # vzorku pro kalibraci gyra (~1 s)
CALIB_MAX = 0.15         # rad/s max rozptyl pri kalibraci
SPEED_TAU = 0.08         # s vyhlazeni rychlosti hlavy
SEND_HZ   = 250          # Hz posilani pohybu mysi
START_DELAY = 0.20       # s klidu pred krouzkem

# ──────────────────────────── Windows API ────────────────────────────────────
if IS_WIN:
    import ctypes
    from ctypes import wintypes
    _set_cursor_abs = lambda x,y: ctypes.windll.user32.SetCursorPos(int(round(x)), int(round(y)))

    _u32 = ctypes.windll.user32

    class _MI(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _IU(ctypes.Union):
        _fields_ = [("mi", _MI)]

    class _IN(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _IU)]

    _u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_IN), ctypes.c_int]
    _u32.SendInput.restype  = wintypes.UINT

    def _mouse(flags, dx=0, dy=0):
        i = _IN(type=0); i.u.mi = _MI(dx, dy, 0, flags, 0, 0)
        _u32.SendInput(1, ctypes.byref(i), ctypes.sizeof(_IN))

    def move_mouse(dx, dy):   _mouse(0x0001, int(dx), int(dy))
    def set_cursor(x, y):     _set_cursor_abs(x, y)
    def left_down():           _mouse(0x0002)
    def left_up():             _mouse(0x0004)
    def left_click():          _mouse(0x0002); _mouse(0x0004)
    def right_click():         _mouse(0x0008); _mouse(0x0010)

    def key_state(vk):
        st = _u32.GetAsyncKeyState(vk)
        return bool(st & 0x8000), bool(st & 0x0001)

    def beep(freq, ms):
        threading.Thread(target=lambda: __import__('winsound').Beep(freq, ms),
                         daemon=True).start()

    def get_vanish():
        v = wintypes.BOOL()
        _u32.SystemParametersInfoW(0x1020, 0, ctypes.byref(v), 0)
        return bool(v.value)

    def set_vanish(on):
        _u32.SystemParametersInfoW(0x1021, 0, ctypes.c_void_p(1 if on else 0), 0)

    _set_cursor_abs_noop = lambda x,y: None
else:
    _set_cursor_abs = lambda x,y: None
    def move_mouse(dx, dy): pass
    def set_cursor(x, y): pass
    def left_down():        pass
    def left_up():          pass
    def left_click():       pass
    def right_click():      pass
    def key_state(vk):      return False, False
    def beep(freq, ms):     pass
    def get_vanish():       return None
    def set_vanish(on):     pass

# ──────────────────────────── protokol ───────────────────────────────────────
def imu_packets(chunks):
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

def parse_imu(p):
    if struct.unpack_from("<I", p, 0x1E)[0] != TYPE_IMU: return None
    ts, = struct.unpack_from("<Q", p, 0x0E)
    g = struct.unpack_from("<fff", p, 0x22)
    a = struct.unpack_from("<fff", p, 0x2E)
    if any(math.isnan(v) for v in g+a): return None
    return ts, g, a

def ctrl_frames(buf):
    out = []
    while True:
        p = buf.find(bytes([0x27]))
        if p == -1: buf.clear(); return out
        if p > 0: del buf[:p]
        if len(buf) < 6: return out
        n = int.from_bytes(buf[2:6], "big")
        if buf[2:4] != b"\x00\x00" or n > 4096: del buf[:1]; continue
        if len(buf) < 6+n: return out
        out.append((buf[1], bytes(buf[6:6+n]))); del buf[:6+n]

def socket_chunks(port):
    s = socket.create_connection((IP, port), timeout=5.0)
    try:
        while True:
            d = s.recv(4096)
            if not d: return
            yield d
    finally:
        s.close()

def file_chunks(path):
    with open(path, "rb") as f:
        while True:
            d = f.read(4096)
            if not d: return
            yield d

# ──────────────────────────── orientace (Mahony) ─────────────────────────────
def _dot(a, b): return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]
def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
def _qnorm(q):
    n = math.sqrt(sum(c*c for c in q)) or 1.0
    return tuple(c/n for c in q)
def _qmul(a, b):
    aw,ax,ay,az = a; bw,bx,by,bz = b
    return (aw*bw-ax*bx-ay*by-az*bz, aw*bx+ax*bw+ay*bz-az*by,
            aw*by-ax*bz+ay*bw+az*bx, aw*bz+ax*by-ay*bx+az*bw)
def _arc(a, b):
    d = _dot(a, b)
    if d < -0.9999:
        ax = _cross(a, (1,0,0))
        if _dot(ax,ax) < 1e-6: ax = _cross(a, (0,1,0))
        return _qnorm((0.,)+ax)
    c = _cross(a, b); return _qnorm((1+d, c[0], c[1], c[2]))
def _up(q):
    w,x,y,z = q
    return (2*(x*z-w*y), 2*(y*z+w*x), 1-2*(x*x+y*y))

class Orientation:
    def __init__(self):
        self.q = (1.,0.,0.,0.); self.bias = [0.,0.,0.]
        self.calibrating = True; self._cal = []; self._last = None
        self._win = deque(); self.hz = 0.; self.dt = 0.
        self.yaw_dps = 0.; self.pitch_dps = 0.; self.pitch_deg = 0.
        self.status = "Kalibrace – drž brýle v klidu"

    def recalibrate(self):
        self.calibrating = True; self._cal = []
        self.status = "Kalibrace – drž brýle v klidu"

    def update(self, ts, gyro, acc):
        g = gyro; a = acc
        an = math.sqrt(_dot(a,a))
        if an < 1e-3: return False
        ah = tuple(c/an for c in a)
        self._win.append(ts)
        while self._win and ts-self._win[0] > 1_000_000_000: self._win.popleft()
        sp = (ts-self._win[0])*1e-9
        self.hz = (len(self._win)-1)/sp if sp > 0 else 0.
        self.dt = (ts-self._last)*1e-9 if self._last else 0.
        self._last = ts
        if self.calibrating:
            self.q = _arc(ah, (0.,0.,1.)); self._cal.append(g)
            if len(self._cal) >= CALIB_N:
                rng = [max(c[i] for c in self._cal)-min(c[i] for c in self._cal) for i in range(3)]
                if max(rng) < CALIB_MAX:
                    n = len(self._cal)
                    self.bias = [sum(c[i] for c in self._cal)/n for i in range(3)]
                    self.calibrating = False; self.status = "OK"
                else:
                    self._cal = []; self.status = "Kalibrace – hýbeš se, zastav"
            return False
        if self.dt <= 0 or self.dt > .1: return False
        up = _up(self.q)
        e = _cross(ah, up)
        k = KP if .85 < an/G < 1.15 else 0.
        om = tuple(g[i]-self.bias[i]+k*e[i] for i in range(3))
        dq = _qmul(self.q, (0., om[0], om[1], om[2]))
        self.q = _qnorm(tuple(self.q[i]+.5*dq[i]*self.dt for i in range(4)))
        up2 = _up(self.q)
        yaw   = _dot(om, up2)
        xh    = (1-up2[0]*up2[0], -up2[0]*up2[1], -up2[0]*up2[2])
        xn    = math.sqrt(_dot(xh,xh))
        pitch = _dot(om, xh)/xn if xn > .2 else 0.
        self.yaw_dps   = math.degrees(yaw)
        self.pitch_dps = math.degrees(pitch)
        self.pitch_deg = math.degrees(math.asin(max(-1.,min(1.,up2[2]))))
        return True

# ──────────────────────────── circle click ───────────────────────────────────
class Dwell:
    def __init__(self): self.locked=False; self.reset()
    def reset(self): self.state="IDLE"; self.still=0.; self.prog=0.; self.dx=self.dy=0.; self.locked=False
    @property
    def active(self): return self.state=="DWELL"
    def block(self): self.state,self.prog = "COOLDOWN", 0.
    def force_start(self):
        """Okamzite spusti nezrusitelny odpocet (gesto tres hlavou)."""
        self.state, self.prog, self.dx, self.dy = "DWELL", 0., 0., 0.
        self.locked = True
    def update(self, cfg, dt, speed, px, py):
        cancel = max(cfg.cancel_speed, cfg.start_speed+1.)
        if self.state=="IDLE":
            if speed < cfg.start_speed:
                self.still += dt
                if self.still >= START_DELAY: self.state,self.prog="DWELL",0.; self.dx=self.dy=0.
            else: self.still = 0.
        elif self.state=="DWELL":
            self.dx+=px; self.dy+=py
            if not self.locked:
                if speed>cancel or math.hypot(self.dx,self.dy)>cfg.max_drift:
                    self.reset(); return False
            self.prog += dt/max(.1, cfg.dwell_time)
            if self.prog >= 1.:
                self.state,self.prog="COOLDOWN",0.
                fired_locked = self.locked
                self.locked=False
                return "locked" if fired_locked else True
        elif self.state=="COOLDOWN":
            if speed>cancel: self.reset()
        return False

# ──────────────────────────── tlacitko ───────────────────────────────────────
class Button:
    """stisk=lklik  drzeni=drag  dvojstisk=pklik  dvojstisk+drzeni=toggle dwell"""
    def __init__(self, cfg, on_lclick, on_ldown, on_lup, on_rclick, on_toggle):
        self.cfg=cfg; self._lc,self._ld,self._lu,self._rc,self._tg = on_lclick,on_ldown,on_lup,on_rclick,on_toggle
        self._lock=threading.RLock(); self.state="IDLE"; self.t=0.
    @property
    def busy(self): return self.state!="IDLE"
    def down(self, now):
        with self._lock:
            if self.state=="IDLE":   self.state,self.t="DOWN1",now
            elif self.state=="WAIT2":self.state,self.t="DOWN2",now
    def up(self, now):
        with self._lock:
            s=self.state
            if s=="DOWN1":   self.state,self.t="WAIT2",now
            elif s=="DRAG":  self._lu(); self.state="IDLE"
            elif s=="DOWN2": self._rc(); self.state="IDLE"
            elif s=="CONSUMED": self.state="IDLE"
    def tick(self, now):
        with self._lock:
            h=self.cfg.hold_ms/1000.; d=self.cfg.double_ms/1000.
            if self.state=="DOWN1" and now-self.t>=h: self._ld(); self.state="DRAG"
            elif self.state=="WAIT2" and now-self.t>=d: self._lc(); self.state="IDLE"
            elif self.state=="DOWN2" and now-self.t>=h: self._tg(); self.state="CONSUMED"
    def abort(self):
        with self._lock:
            if self.state=="DRAG": self._lu()
            self.state="IDLE"


# ──────────────────────────── gesta hlavou ───────────────────────────────────
class GestureDetector:
    """
    yaw_shake  = rychly tresen ze strany na stranu  → toggle circle click
    pitch_nod  = rychle kyvnuti nahoru a dolu        → vycentrovat mys na stred
    """
    def __init__(self, min_dps=45., window_s=0.45, cooldown_s=1.0):
        self.min_dps    = min_dps
        self.window_s   = window_s
        self.cooldown_s = cooldown_s
        self._yaw_buf   = deque()
        self._pitch_buf = deque()
        self._yaw_dir   = 0
        self._pitch_dir = 0
        self._cool_yaw  = 0.
        self._cool_pitch= 0.

    def update(self, now, yaw_dps, pitch_dps):
        ry = self._axis(now, yaw_dps,   self._yaw_buf,   "_yaw_dir",   "_cool_yaw",  "yaw_shake")
        rp = self._axis(now, pitch_dps, self._pitch_buf, "_pitch_dir", "_cool_pitch", "pitch_nod")
        return ry or rp

    def _axis(self, now, dps, buf, adir, acool, name):
        if now < getattr(self, acool):
            return None
        while buf and now - buf[0][0] > self.window_s:
            buf.popleft()
        if abs(dps) >= self.min_dps:
            cur = 1 if dps > 0 else -1
            if cur != getattr(self, adir):
                setattr(self, adir, cur)
                if buf:
                    buf.append((now, cur))
                    dirs = [d for _, d in buf]
                    changes = sum(1 for i in range(1, len(dirs)) if dirs[i] != dirs[i-1])
                    if changes >= 2:
                        buf.clear()
                        setattr(self, acool, now + self.cooldown_s)
                        return name
                else:
                    buf.append((now, cur))
        return None


def get_xreal_monitor():
    """Vrati XREAL monitor detekovany podle EDID (DeviceID obsahuje 'MRG').

    Funguje i kdyz je viko laptopu zavrene a XREAL se stane primarnim
    monitorem nebo se ocislovani zmenı – detekce podle hardware ID,
    ne podle poradı.  Fallback: prvni neprimarni → prvni monitor.
    """
    if not IS_WIN:
        return None
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class MONINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    class DISPLAY_DEVICE(ctypes.Structure):
        _fields_ = [("cb",           wintypes.DWORD),
                    ("DeviceName",   wintypes.WCHAR * 32),
                    ("DeviceString", wintypes.WCHAR * 128),
                    ("StateFlags",   wintypes.DWORD),
                    ("DeviceID",     wintypes.WCHAR * 128),
                    ("DeviceKey",    wintypes.WCHAR * 128)]

    mons = []
    PT = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p,
                             ctypes.POINTER(RECT), wintypes.LPARAM)
    def cb(hmon, hdc, lp, lparam):
        mi = MONINFO(); mi.cbSize = ctypes.sizeof(MONINFO)
        if _u32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
            r = mi.rcMonitor
            mons.append({"x": r.left, "y": r.top,
                         "w": r.right - r.left, "h": r.bottom - r.top,
                         "primary": bool(mi.dwFlags & 1),
                         "dev": mi.szDevice})  # napr. "\\\\.\\DISPLAY2"
        return True
    try:
        _u32.EnumDisplayMonitors(None, None, PT(cb), 0)
    except Exception:
        return None

    # ── pokus 1: EDID identifikace – funguje i po zavreni vika ──
    EDD_GET_DEVICE_INTERFACE_NAME = 1
    for m in mons:
        try:
            dd = DISPLAY_DEVICE(); dd.cb = ctypes.sizeof(DISPLAY_DEVICE)
            ok = ctypes.windll.user32.EnumDisplayDevicesW(
                    m["dev"], 0, ctypes.byref(dd),
                    EDD_GET_DEVICE_INTERFACE_NAME)
            if ok:
                dev_id  = dd.DeviceID.upper()
                dev_str = dd.DeviceString.upper()
                # MRG4100 = XREAL One Pro EDID manufacturer ID
                if ("MRG" in dev_id or "XREAL" in dev_id or "XREAL" in dev_str):
                    return m
        except Exception:
            pass

    # ── pokus 2: prvni neprimarni (puvodni chovani) ──
    for m in mons:
        if not m["primary"]:
            return m

    # ── pokus 3: kdykoli je jen jeden monitor (viko zavrene, XREAL = primary) ──
    return mons[0] if mons else None

def center_mouse():
    m = get_xreal_monitor()
    if m:
        set_cursor(m["x"] + m["w"]//2, m["y"] + m["h"]//2)

# ──────────────────────────── AR adaptery a kamera loop ──────────────────────
class _ArRt:
    """Adapter: St -> rt interface ocekavany ArOverlay (view_h / view_v / view_hist)."""
    def __init__(self, st): self._st = st
    @property
    def view_h(self): return self._st.ar_ey
    @property
    def view_v(self): return self._st.ar_ep
    @property
    def view_hist(self): return self._st.view_hist

class _ArEng:
    """Adapter: poskytuje engine.monitor pro ArOverlay (vraci XREAL monitor dict)."""
    def __init__(self): self._m = None; self._t = 0.
    @property
    def monitor(self):
        if time.time() - self._t > 2.:
            self._m = get_xreal_monitor(); self._t = time.time()
        return self._m

def _ar_camera_loop(st):
    """Daemon vlakno: cte eye kameru (port 52997), detekuje kontury -> st.ar_state.

    Vrstva \"slam\" v ArState je aktualizovana ~15fps (frekvence eye kamery).
    Kontury jsou prevadeny na svetove uhly pomoci pozice hlavy z view_hist
    (latency kompenzace: hlava kde byla v case zachyceni snimku).
    """
    if not AR_AVAILABLE or st.ar_state is None:
        return
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("[AR] cv2/numpy nedostupne – AR kamera vypnuta")
        return

    vs = VideoStream(host=IP, save_dump=False)
    vs.start()
    last_t = 0.0
    ar_cfg = ArConfig()
    cal = ar_cfg.ar_cal.get("slam", {"fov": 100.0, "ox": 0.0, "oy": 0.0,
                                      "flip_x": False, "flip_y": False, "lens": "fisheye"})

    while st.running:
        with vs.lock:
            left  = vs.left
            t_cap = vs.rx_time

        if left is None or t_cap <= last_t:
            time.sleep(0.033)
            continue
        last_t = t_cap

        try:
            # 4-bit -> 8-bit grayscale (512x378)
            arr = np.frombuffer(unpack_nibbles(left), dtype=np.uint8).reshape(EYE_H, EYE_W)

            # Kontury prahovanim
            _, thr = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            contours, _ = cv2.findContours(thr, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            # Pozice hlavy v okamziku snimku (kompenzuje zpozdeni kamery)
            head_h, head_v = 0.0, 0.0
            if st.view_hist:
                t_comp = t_cap - ar_cfg.ar_latency_ms / 1000.0
                head_h, head_v = lookup(st.view_hist, t_comp)

            # Filtrace malych kontur, format pro items_from_cv
            objs = []
            for c in contours:
                if cv2.contourArea(c) < 200:
                    continue
                x, y, w, h = cv2.boundingRect(c)
                objs.append((float(x), float(y), float(w), float(h), c))

            items = items_from_cv(objs, EYE_W, EYE_H, cal, head_h, head_v, cv2=cv2)
            st.ar_state.set_layer("slam", items)
        except Exception:
            pass

# ──────────────────────────── konfigurace + stav ─────────────────────────────
class Cfg:
    sens        = 15.0   # px/stupen
    deadzone    = 1.0    # deg/s
    dwell_on    = True
    dwell_time  = 1.2    # s
    start_speed = 5.0    # deg/s
    cancel_speed= 15.0   # deg/s
    max_drift   = 80.0   # px
    hold_ms     = 350.0
    double_ms   = 300.0
    button_on   = True
    flip_h      = False
    flip_v      = False
    top_btn_id  = 1      # kid horniho tlacitka – zjisti sniff.py a uprav
    center_snap = 0.5    # stupne – v teto zone se kurzor uchyti presne na stred

class St:  # runtime stav
    yaw_int      = 0.0   # integrovany uhel yaw (stupne)
    pitch_int    = 0.0   # integrovany uhel pitch
    calib_yaw    = 0.0   # IMU yaw pri posledni kalibraci stredu
    calib_pitch  = 0.0   # IMU pitch pri posledni kalibraci stredu
    enabled      = False
    running      = True
    status       = "Startuji..."
    ctrl_status  = "Tlačítko: připojuji..."
    hz           = 0.
    speed        = 0.
    dwell_active = False
    dwell_prog   = 0.
    flash_until  = 0.
    toast        = ""
    toast_ok     = True
    toast_until  = 0.
    last_action  = "-"
    calib_req    = False
    quit_req     = False
    last_key_id  = None  # posledni kid – pro debug horniho tlacitka
    # ── AR overlay ──────────────────────────────────────────────────────────
    view_hist    = None  # deque[(t, ar_yaw_eff, ar_pitch_eff)] – init v main()
    ar_yaw_int   = 0.0   # integrovany AR yaw (vzdy, i kdyz je cursor vypnuto)
    ar_pitch_int = 0.0   # integrovany AR pitch
    ar_calib_yaw  = 0.0  # AR yaw v cas posledni kalibrace stredu
    ar_calib_pitch= 0.0  # AR pitch v cas posledni kalibrace stredu
    ar_ey        = 0.0   # aktualni efektivni AR yaw od reference
    ar_ep        = 0.0   # aktualni efektivni AR pitch od reference
    ar_state     = None  # ArState instance – nastaven v main() pokud AR_AVAILABLE
    ar_pins      = []    # seznam (wh, wv, label, color) – piny v prostoru (F3 = pridat, F4 = smazat)

# ──────────────────────────── klávesy ────────────────────────────────────────
BINDINGS = {
    "toggle":    [0x77, 0x13],  # F8, Pause
    "calib":     [0x78],        # F9
    "quit":      [0x79],        # F10
    "sens_dn":   [0x7A],        # F11 (drz)
    "sens_up":   [0x7B],        # F12 (drz)
    "center":    [0x04],        # klik koleckem
    "pin_add":   [0x72],        # F3 – pridat pin v prostoru na aktualni smer pohledu
    "pin_clear": [0x73],        # F4 – smazat vsechny piny
}
CONSOLE = {"m":"toggle","c":"calib","q":"quit","-":"sens_dn","+":"sens_up","=":"sens_up"}

class Keys(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.events = deque(); self.held = {k:False for k in BINDINGS}; self.stop = False
    def run(self):
        prev = {k:False for k in BINDINGS}
        while not self.stop:
            for a, vks in BINDINGS.items():
                states = [key_state(v) for v in vks]
                dn = any(d for d,_ in states); tp = any(t for _,t in states)
                self.held[a] = dn
                if a not in ("sens_dn","sens_up") and ((dn and not prev[a]) or tp):
                    self.events.append(a)
                prev[a] = dn
            if msvcrt:
                while msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00","\xe0"): msvcrt.getwch(); continue
                    a = CONSOLE.get(ch.lower())
                    if a: self.events.append(a+"_step" if a in ("sens_dn","sens_up") else a)
            time.sleep(0.005)

# ──────────────────────────── řídicí kanál brýlí ─────────────────────────────
class CtrlWorker(threading.Thread):
    def __init__(self, cfg, st, btn, btn2=None):
        super().__init__(daemon=True)
        self.cfg,self.st,self.btn,self.btn2 = cfg,st,btn,btn2
    def run(self):
        cfg,st,btn,btn2 = self.cfg,self.st,self.btn,self.btn2
        while st.running:
            try:
                sock = socket.create_connection((IP, PORT_CTRL), timeout=5.)
                sock.settimeout(0.01); st.ctrl_status="Tlačítko: připojeno"; buf=bytearray()
                while st.running:
                    try: d=sock.recv(4096); buf.extend(d) if d else (_ for _ in ()).throw(OSError())
                    except socket.timeout: pass
                    now=time.time()
                    for mid,body in ctrl_frames(buf):
                        if mid!=MSG_KEY or len(body)<8: continue
                        kid,state = struct.unpack_from("<II",body,0)
                        st.last_key_id = kid
                        if not cfg.button_on: continue
                        # Routing podle kid – kazde tlacitko ma vlastni Button
                        if kid == X_BTN_ID:
                            b = btn if st.enabled else None
                        elif btn2 is not None and kid == cfg.top_btn_id:
                            b = btn2   # horni tlacitko funguje i kdyz vypnuto (toggle)
                        else:
                            b = None
                        if b is None: continue
                        if state==1: b.down(now)
                        elif state==2: b.up(now)
                    btn.tick(time.time())
                    if btn2: btn2.tick(time.time())
                sock.close()
            except OSError as e:
                btn.abort()
                if btn2: btn2.abort()
                st.ctrl_status=f"Tlačítko: {e} – zkouším znovu"
            time.sleep(2.)

# ──────────────────────────── GUI overlay (krouzek) ──────────────────────────
RING=128; R0=34; SPIN=220.; KEY="#010101"
import math as _m

def _circle(cv, mid, r, **kw): cv.create_oval(mid-r,mid-r,mid+r,mid+r,**kw)

def build_overlay(root, st):
    import tkinter as tk
    ov=tk.Toplevel(root); ov.overrideredirect(True); ov.attributes("-topmost",True)
    ov.configure(bg=KEY)
    try: ov.attributes("-transparentcolor",KEY)
    except: pass
    cv=tk.Canvas(ov,width=RING,height=RING,bg=KEY,highlightthickness=0); cv.pack()
    ov.geometry(f"{RING}x{RING}+0+0"); ov.update_idletasks()
    if IS_WIN:
        import ctypes as _c
        from ctypes import wintypes as _w
        u=_c.windll.user32
        hwnd=u.GetAncestor(ov.winfo_id(),2) or ov.winfo_id()
        style=u.GetWindowLongW(hwnd,-20)
        u.SetWindowLongW(hwnd,-20,style|0x80000|0x20|0x80|0x08000000)
        u.SetLayeredWindowAttributes(hwnd,1|(1<<8)|(1<<16),0,0x1)
    ov.withdraw()
    mid=RING//2; vis={"on":False}; anim={"prev":False,"t_in":0.,"mode":None,"t0":0.,"p":0.}

    def draw(now):
        cv.delete("all"); a=anim; p=st.dwell_prog
        if a["mode"]=="click":
            t=min(1.,(now-a["t0"])/.30)
            if t<1: cv.create_oval(mid-R0*(1+.6*t),mid-R0*(1+.6*t),mid+R0*(1+.6*t),mid+R0*(1+.6*t),
                                   outline="#9dffd6",width=max(1.5,10*(1-t)))
            else: a["mode"]=None
        elif a["mode"]=="cancel":
            t=min(1.,(now-a["t0"])/.16)
            r=R0*(1-.5*t); w=max(1.5,8*(1-t))
            if t<1:
                cv.create_oval(mid-r,mid-r,mid+r,mid+r,outline="#5a2f28",width=w)
                cv.create_arc(mid-r,mid-r,mid+r,mid+r,start=90,extent=-max(40,359.9*a["p"]),
                              style="arc",outline="#e1704d",width=w)
            else: a["mode"]=None
        if st.dwell_active:
            k=min(1.,(now-a["t_in"])/.18); r=R0*(.55+.45*(1-(1-k)**3))
            cv.create_oval(mid-r,mid-r,mid+r,mid+r,outline="#3a4250",width=8)
            ext=-max(40.,359.9*min(p,1.)); start=90.-(now*SPIN)%360.
            cv.create_arc(mid-r,mid-r,mid+r,mid+r,start=start,extent=ext,style="arc",
                          outline="#4de1a1",width=8)
            hx=mid+r*_m.cos(_m.radians(start+ext)); hy=mid-r*_m.sin(_m.radians(start+ext))
            cv.create_oval(hx-6,hy-6,hx+6,hy+6,fill="#eafff5",outline="")
        if now<st.toast_until:
            col="#4de1a1" if st.toast_ok else "#e1704d"
            cv.create_oval(mid-R0-4,mid-R0-4,mid+R0+4,mid+R0+4,outline=col,width=6)
            cv.create_text(mid,mid,text=st.toast,fill=col,justify="center",
                           font=("Segoe UI",11 if "\n" in st.toast else 15,"bold"))

    def tick_ov():
        now=time.time(); a=anim; active=st.enabled and st.dwell_active
        if active and not a["prev"]: a["t_in"]=now; a["mode"]=None
        if a["prev"] and not active:
            a["mode"]="click" if now<st.flash_until else "cancel"
            a["t0"]=now; a["p"]=st.dwell_prog
        if not st.enabled: a["mode"]=None
        a["prev"]=active
        toast=now<st.toast_until
        show=active or a["mode"] or toast
        if show:
            x,y=root.winfo_pointerxy()
            ov.geometry(f"+{x-RING//2}+{y-RING//2}"); draw(now)
        if show:
            if not vis["on"]:
                ov.deiconify(); ov.attributes("-topmost",True)
                if IS_WIN:
                    import ctypes as _c
                    from ctypes import wintypes as _w
                    u=_c.windll.user32; hwnd=u.GetAncestor(ov.winfo_id(),2) or ov.winfo_id()
                    style=u.GetWindowLongW(hwnd,-20)
                    u.SetWindowLongW(hwnd,-20,style|0x80000|0x20|0x80|0x08000000)
                    u.SetLayeredWindowAttributes(hwnd,1|(1<<8)|(1<<16),0,0x1)
                vis["on"]=True
        elif vis["on"]: ov.withdraw(); vis["on"]=False
        root.after(16, tick_ov)
    tick_ov()

# ──────────────────────────── hlavní GUI ─────────────────────────────────────
def build_gui(cfg, st, engine_start_calib, engine_quit):
    import tkinter as tk
    from tkinter import ttk

    root=tk.Tk(); root.title("XREAL Head Mouse"); root.resizable(False,False)
    root.attributes("-topmost",True)
    frm=ttk.Frame(root,padding=16); frm.grid()

    # stav
    sv=tk.StringVar()
    ttk.Label(frm,textvariable=sv).grid(row=0,column=0,columnspan=2,sticky="w")

    # zapnout/vypnout
    btn_var=tk.StringVar()
    def toggle(): st.enabled=not st.enabled
    ttk.Button(frm,textvariable=btn_var,command=toggle).grid(
        row=1,column=0,columnspan=2,sticky="ew",pady=(10,10),ipady=10)

    # circle click checkbox
    dwell_var=tk.BooleanVar(value=cfg.dwell_on)
    ttk.Checkbutton(frm,text="Circle click (klikání po zastavení)",
                    variable=dwell_var,
                    command=lambda: setattr(cfg,"dwell_on",dwell_var.get())
                    ).grid(row=2,column=0,columnspan=2,sticky="w")

    # rychlost - jediný slider
    ttk.Label(frm,text="Rychlost (px/°)").grid(row=3,column=0,sticky="w",pady=(12,0))
    sens_var=tk.DoubleVar(value=cfg.sens)
    sens_lbl=ttk.Label(frm,text=f"{cfg.sens:.0f}",width=6,anchor="e")
    sens_lbl.grid(row=3,column=1,pady=(12,0))
    def on_sens(_=None):
        v=sens_var.get(); cfg.sens=v; sens_lbl.config(text=f"{v:.0f}")
    ttk.Scale(frm,from_=2,to=60,variable=sens_var,length=260,
              command=on_sens).grid(row=4,column=0,columnspan=2,sticky="ew",pady=(2,10))

    # diagnostika
    diag_var=tk.StringVar()
    ttk.Label(frm,textvariable=diag_var,foreground="#3a6fb0",justify="left").grid(
        row=5,column=0,columnspan=2,sticky="w")

    # speed bar
    ttk.Label(frm,text="Rychlost hlavy").grid(row=6,column=0,columnspan=2,sticky="w",pady=(8,2))
    BW,BH=320,16
    bar=tk.Canvas(frm,width=BW,height=BH,bg="#20242b",highlightthickness=0)
    bar.grid(row=7,column=0,columnspan=2)

    # tlacitka
    bf=ttk.Frame(frm); bf.grid(row=8,column=0,columnspan=2,pady=(14,0),sticky="ew")
    ttk.Button(bf,text="Kalibrovat gyro (F9)",command=engine_start_calib).pack(side="left")
    ttk.Button(bf,text="Konec (F10)",command=engine_quit).pack(side="right")

    # nastaveni kid horniho tlacitka
    top_frame = ttk.Frame(frm); top_frame.grid(row=9,column=0,columnspan=2,pady=(10,0),sticky="ew")
    ttk.Label(top_frame,text="Horní tlač. kid:").pack(side="left")
    top_kid_var = tk.IntVar(value=cfg.top_btn_id)
    top_kid_sp  = ttk.Spinbox(top_frame, from_=0, to=255, width=5,
                               textvariable=top_kid_var,
                               command=lambda: setattr(cfg,"top_btn_id",top_kid_var.get()))
    top_kid_sp.pack(side="left",padx=(4,0))
    ttk.Label(top_frame,text="  (zjisti sniff.py – kid= v diagnostice)",
              foreground="#888").pack(side="left")

    ttk.Label(frm,text="F8/Pause zap-vyp  |  F9 kalibrace  |  F10 konec  |  "
                       "kolečko myši: uvolnit/držet střed",
              foreground="#777").grid(row=10,column=0,columnspan=2,sticky="w",pady=(8,0))

    # AR overlay (pruhledne Toplevel pres celou obrazovku bryli)
    if AR_AVAILABLE and st.ar_state is not None:
        _ar_cfg = ArConfig()
        _ar_rt  = _ArRt(st)
        _ar_eng = _ArEng()
        ArOverlay(root, _ar_cfg, _ar_rt, _ar_eng, st.ar_state,
                  get_source=lambda: "slam")

    build_overlay(root, st)
    n={"i":0}
    def tick():
        if st.quit_req: root.destroy(); return
        sv.set(f"{st.status}   |   {st.hz:.0f} Hz   |   {st.ctrl_status}")
        btn_var.set("ZAPNUTO – klikni pro vypnutí" if st.enabled else "VYPNUTO – klikni pro zapnutí")
        if abs(sens_var.get()-cfg.sens)>.5: sens_var.set(cfg.sens); on_sens()
        if dwell_var.get()!=cfg.dwell_on: dwell_var.set(cfg.dwell_on)
        n["i"]+=1
        if n["i"]%4==0:
            kid_str = f"  |  kid={st.last_key_id}" if st.last_key_id is not None else ""
            diag_var.set(f"yaw {st.last_yaw:+6.1f}  pitch {st.last_pitch:+6.1f} °/s  |  {st.last_action}{kid_str}")
            bar.delete("all")
            BMAX=60.
            bar.create_rectangle(0,0,min(st.speed,BMAX)/BMAX*BW,BH,fill="#4d8fe1",width=0)
            for val,col in ((cfg.start_speed,"#4de1a1"),(max(cfg.cancel_speed,cfg.start_speed+1),"#e14d4d")):
                xx=min(val,BMAX)/BMAX*BW
                bar.create_line(xx,0,xx,BH,fill=col,width=3)
        root.after(16, tick)
    tick()
    root.protocol("WM_DELETE_WINDOW", engine_quit)
    return root

# ──────────────────────────── engine ─────────────────────────────────────────
def run(cfg, st, replay=None, dry=False):
    ori  = Orientation()
    dwell= Dwell()
    gest = GestureDetector()
    keys = Keys(); keys.start()

    # Inicializace AR (view_hist + kamera vlakno) – pouze kdyz je AR dostupne
    if AR_AVAILABLE and st.ar_state is not None:
        st.view_hist = deque(maxlen=300)
        threading.Thread(target=_ar_camera_loop, args=(st,), daemon=True).start()

    acc_x=acc_y=0.; send_dx=send_dy=0; last_send=0.; speed=0.
    _prev=[0.,0.]          # [yaw_int, pitch_int] – mutable, pristupne z callbacku
    send_abs=None
    _mon_t = [0.0]
    _mon_v = [None]

    def toast(text, ok, secs, action, freq=0, ms=0):
        st.toast,st.toast_ok,st.toast_until,st.last_action=text,ok,time.time()+secs,action
        if freq and not dry: beep(freq, ms)

    def lclick():
        st.last_action="levý klik"
        if not dry: left_click()
    def ldown():
        st.last_action="tažení (držím)"
        if not dry: left_down()
    def lup():
        st.last_action="tažení (pustil)"
        if not dry: left_up()
    def rclick():
        st.last_action="pravý klik"
        if not dry: right_click()
    def toggle_dwell():
        cfg.dwell_on=not cfg.dwell_on
        toast("ZAP" if cfg.dwell_on else "VYP", cfg.dwell_on, 1.,
              f"circle click {'ZAPNUT' if cfg.dwell_on else 'VYPNUT'}", 1300 if cfg.dwell_on else 500, 140)

    # ── horni tlacitko (btn2) ──────────────────────────────────────────────────
    # klik          → levy klik
    # dvojklik      → pravy klik
    # drzet         → zapnout / vypnout program (st.enabled)
    # kratky+dlouhy → vycentrovat kurzor na stred
    def top_toggle():
        st.enabled = not st.enabled
        toast("ZAPNUTO" if st.enabled else "VYPNUTO", st.enabled, 1.0,
              f"horní tlač: {'zapnuto' if st.enabled else 'vypnuto'}",
              1300 if st.enabled else 500, 140)

    def top_center():
        # kalibruj: aktualni poloha hlavy = stred displeje
        st.calib_yaw   = st.yaw_int
        st.calib_pitch = st.pitch_int
        st.ar_calib_yaw   = st.ar_yaw_int   # AR reference shodna s kurzorem
        st.ar_calib_pitch = st.ar_pitch_int
        mon = get_xreal_monitor()
        if mon and not dry:
            set_cursor(mon["x"] + mon["w"]//2, mon["y"] + mon["h"]//2)
        toast("STŘED", True, 0.8, "horní tlač: střed", 900, 80)

    btn  = Button(cfg, lclick, ldown,      lup,          rclick, toggle_dwell)
    btn2 = Button(cfg, lclick, top_toggle, lambda: None, rclick, top_center)
    CtrlWorker(cfg, st, btn, btn2).start()

    st.last_yaw=st.last_pitch=0.; st.last_key_id=None

    wall0=ts0=None; last_ui=0.

    while st.running:
        try:
            chunks = file_chunks(replay) if replay else socket_chunks(PORT_IMU)
            st.status="Připojeno"
            for pkt in imu_packets(chunks):
                if not st.running: return
                r=parse_imu(pkt)
                if not r: continue
                ts,gyro,acc=r
                if replay:
                    if wall0 is None: wall0,ts0=time.time(),ts
                    d=wall0+(ts-ts0)*1e-9-time.time()
                    if d>0: time.sleep(d)
                valid=ori.update(ts,gyro,acc)
                now=time.time()
                if not valid: continue

                raw=math.hypot(ori.yaw_dps,ori.pitch_dps)
                speed+=(1.-math.exp(-ori.dt/SPEED_TAU))*(raw-speed)
                st.speed=speed; st.hz=ori.hz
                st.last_yaw=ori.yaw_dps; st.last_pitch=ori.pitch_dps

                # ── gesta ──
                if not ori.calibrating:
                    gest_r = gest.update(now, ori.yaw_dps, ori.pitch_dps)
                    if gest_r == "yaw_shake":
                        if st.enabled and not ori.calibrating:
                            dwell.force_start()
                            st.last_action = "gesto: odpočet..."  
                    elif gest_r == "pitch_nod":
                        st.calib_yaw   = st.yaw_int
                        st.calib_pitch = st.pitch_int
                        st.ar_calib_yaw   = st.ar_yaw_int
                        st.ar_calib_pitch = st.ar_pitch_int
                        mon = get_xreal_monitor()
                        if mon and not dry:
                            set_cursor(mon["x"] + mon["w"]//2, mon["y"] + mon["h"]//2)
                        toast("STŘED", True, 0.8, "gesto: střed", 900, 80)

                # ── AR head tracking (vzdy, nezavisle na st.enabled) ──────────
                if AR_AVAILABLE and st.ar_state is not None and not ori.calibrating:
                    _sy_ar = 1 if cfg.flip_h else -1
                    _sp_ar = 1 if cfg.flip_v else -1
                    st.ar_yaw_int   += _sy_ar * ori.yaw_dps   * ori.dt
                    st.ar_pitch_int += _sp_ar * ori.pitch_dps * ori.dt
                    st.ar_ey = st.ar_yaw_int - st.ar_calib_yaw
                    st.ar_ep = st.ar_pitch_int - st.ar_calib_pitch
                    if st.view_hist is not None:
                        st.view_hist.append((now, st.ar_ey, st.ar_ep))
                    # ── osvezi vrstvu pinu kazdy tick (visible_items ma max_age 0.6s) ──
                    if st.ar_pins:
                        _pin_items = [
                            ArItem("dot", [(wh, wv)], lbl, col, world_locked=True)
                            for wh, wv, lbl, col in st.ar_pins
                        ]
                        if len(st.ar_pins) >= 2:
                            _all_pts = [(wh, wv) for wh, wv, _lbl, _col in st.ar_pins]
                            _pin_items.insert(0, ArItem("fill", _all_pts, "", "#00ff80", world_locked=True))
                        st.ar_state.set_layer("pins", _pin_items)

                if not st.enabled:
                    btn.abort(); dwell.reset()
                    st.yaw_int=st.pitch_int=0.
                    st.calib_yaw=st.calib_pitch=0.
                    _prev[0]=_prev[1]=0.

                dx=dy=0.
                if st.enabled and not ori.calibrating:
                    sy=1 if cfg.flip_h else -1; sp=1 if cfg.flip_v else -1
                    _dz=cfg.deadzone
                    def dz_(x): return 0. if abs(x)<=_dz else x-math.copysign(_dz,x)

                    # integrace uhlu - kurzor sleduje hlavu absolutne
                    st.yaw_int   += sy * dz_(ori.yaw_dps)   * ori.dt
                    st.pitch_int += sp * dz_(ori.pitch_dps) * ori.dt

                    # efektivni uhly relativne ke kalibrovanemu stredu
                    ey = st.yaw_int   - st.calib_yaw
                    ep = st.pitch_int - st.calib_pitch
                    # sticky center – v teto zone uchyti presne na stred
                    if abs(ey) < cfg.center_snap: ey = 0.
                    if abs(ep) < cfg.center_snap: ep = 0.

                    # poloha kurzoru z monitoru
                    import time as _tt
                    if _tt.time() - _mon_t[0] > 2.:
                        _mon_v[0] = get_xreal_monitor(); _mon_t[0] = _tt.time()
                    mon = _mon_v[0]
                    cx = cy = 0.
                    if mon:
                        cx = mon["x"] + mon["w"] / 2.0
                        cy = mon["y"] + mon["h"] / 2.0
                        tx = cx + ey * cfg.sens
                        ty = cy + ep * cfg.sens
                        # delta od predchozi kalibrované pozice
                        pey = _prev[0] - st.calib_yaw
                        pep = _prev[1] - st.calib_pitch
                        if abs(pey) < cfg.center_snap: pey = 0.
                        if abs(pep) < cfg.center_snap: pep = 0.
                        prev_x = max(mon["x"], min(mon["x"]+mon["w"]-1, cx + pey * cfg.sens))
                        prev_y = max(mon["y"], min(mon["y"]+mon["h"]-1, cy + pep * cfg.sens))
                        cur_x  = max(mon["x"], min(mon["x"]+mon["w"]-1, tx))
                        cur_y  = max(mon["y"], min(mon["y"]+mon["h"]-1, ty))
                        dx = cur_x - prev_x
                        dy = cur_y - prev_y
                    else:
                        dx = sy * dz_(ori.yaw_dps) * ori.dt * cfg.sens
                        dy = sp * dz_(ori.pitch_dps) * ori.dt * cfg.sens

                    _prev[0], _prev[1] = st.yaw_int, st.pitch_int

                    if mon:
                        # absolutni pozice s kalibraci – zadny drift
                        _raw_x = cx + ey*cfg.sens
                        _raw_y = cy + ep*cfg.sens
                        _abs_x = max(mon["x"], min(mon["x"]+mon["w"]-1, _raw_x))
                        _abs_y = max(mon["y"], min(mon["y"]+mon["h"]-1, _raw_y))
                        _clamped = (_raw_x != _abs_x) or (_raw_y != _abs_y)
                        if _clamped and abs(_raw_x - _abs_x) + abs(_raw_y - _abs_y) > 50:
                            print(f"[EDGE] raw=({_raw_x:.0f},{_raw_y:.0f}) clamped=({_abs_x:.0f},{_abs_y:.0f}) mon={mon['x']},{mon['y']} {mon['w']}x{mon['h']}", flush=True)
                        send_abs = (_abs_x, _abs_y)
                    else:
                        print(f"[NO-MON] delta fallback dx={dx:.1f} dy={dy:.1f}", flush=True)
                        send_abs = None
                        acc_x+=dx; acc_y+=dy
                        ix,iy=int(acc_x),int(acc_y); acc_x-=ix; acc_y-=iy
                        send_dx+=ix; send_dy+=iy
                    if btn.busy: dwell.block()
                    if cfg.dwell_on or dwell.locked:
                        result = dwell.update(cfg,ori.dt,speed,dx,dy)
                        if result:
                            if not dry:
                                if send_abs:
                                    set_cursor(*send_abs); send_abs=None
                                elif send_dx or send_dy:
                                    move_mouse(send_dx,send_dy)
                            send_dx=send_dy=0
                            if not dry: left_click()
                            st.flash_until=now+.25
                            if result == "locked":
                                cfg.dwell_on = False   # po gestu klik → krouzek vypnut
                                st.last_action="gesto: klikl, kroužek vypnut"
                            else:
                                st.last_action="circle click"
                    if not cfg.dwell_on and not dwell.locked:
                        dwell.reset()
                st.dwell_active=st.enabled and dwell.active
                st.dwell_prog=dwell.prog

                if now-last_send >= 1./SEND_HZ:
                    last_send=now
                    if not dry:
                        if send_abs:
                            set_cursor(*send_abs)
                        elif send_dx or send_dy:
                            move_mouse(send_dx,send_dy)
                    send_dx=send_dy=0; send_abs=None

                if now-last_ui >= .05:
                    last_ui=now
                    while keys.events:
                        ev=keys.events.popleft()
                        if ev=="toggle":   st.enabled=not st.enabled
                        elif ev=="calib":  st.calib_req=True
                        elif ev=="center":
                            pass  # rezerva
                        elif ev=="sens_dn_step": cfg.sens=max(1.,cfg.sens*.9)
                        elif ev=="sens_up_step": cfg.sens=min(200.,cfg.sens*1.1)
                        elif ev=="quit":   st.quit_req=True
                        elif ev=="pin_add" and AR_AVAILABLE and st.ar_state is not None:
                            n = len(st.ar_pins) + 1
                            PIN_COLORS = ["#00ff80","#ff4040","#4dc3ff","#ffff00","#ff80ff"]
                            col = PIN_COLORS[(n-1) % len(PIN_COLORS)]
                            st.ar_pins.append((st.ar_ey, st.ar_ep, f"#{n}", col))
                            print(f"[PIN] #{n} wh={st.ar_ey:.2f}° wv={st.ar_ep:.2f}°", flush=True)
                        elif ev=="pin_clear" and st.ar_pins:
                            st.ar_pins.clear()
                            if AR_AVAILABLE and st.ar_state is not None:
                                st.ar_state.set_layer("pins", [])
                            print("[PIN] vsechny piny smazany", flush=True)
                    if keys.held["sens_dn"]: cfg.sens=max(1.,cfg.sens*.97)
                    if keys.held["sens_up"]: cfg.sens=min(200.,cfg.sens*1.03)
                    if st.calib_req:
                        st.calib_req=False; ori.recalibrate(); dwell.reset()
                    st.status = ori.status if ori.calibrating else "Připojeno – OK"
            st.status="Odpojeno" if not replay else "Přehrávání hotovo"
        except OSError as e:
            st.status=f"Chyba: {e} – zkouším znovu"
        if replay: st.running=False; return
        time.sleep(2.)

# ──────────────────────────── main ───────────────────────────────────────────
def main():
    args=sys.argv[1:]
    replay=next((a for a in args if not a.startswith("--")),None)
    dry="--dry" in args or replay is not None or not IS_WIN

    cfg=Cfg(); st=St()

    # Inicializace AR stavu (sdileny mezi run() a build_gui())
    if AR_AVAILABLE:
        st.ar_state = ArState()

    vanish=get_vanish()
    if not dry and vanish: set_vanish(False)

    threading.Thread(target=run, args=(cfg,st,replay,dry), daemon=True).start()

    try:
        root=build_gui(cfg, st,
                       lambda: setattr(st,"calib_req",True),
                       lambda: setattr(st,"quit_req",True))
        root.mainloop()
    finally:
        st.running=False
        if vanish: set_vanish(True)

if __name__=="__main__":
    main()
