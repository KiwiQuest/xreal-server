#!/usr/bin/env python3
"""xreal-web – IMU → WebSocket → Browser HUD + Tablet remote control
Spuštění:  python server.py
Browser:   http://localhost:8080
WebSocket: ws://localhost:8765
Tablet:    http://[IP notebooku]:8080/tablet.html
"""
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

# ── IMU konstanty (XREAL One Pro) ─────────────────────────────────────────────
IP       = "169.254.2.1"
PORT_IMU = 52998
MAGIC    = bytes.fromhex("283600000080")
PKT      = 134
TYPE_IMU = 11

WS_PORT   = 8765
HTTP_PORT = 8080


# ── log do server.log (vedle server.py) + konzole ─────────────────────────────
class _Tee:
    def __init__(self, stream, path):
        self.stream = stream
        self.file = open(path, "a", encoding="utf-8", buffering=1)

    def write(self, s):
        try:
            self.stream.write(s)
        except Exception:
            pass
        self.buf = getattr(self, "buf", "") + s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.file.write(time.strftime("%H:%M:%S ") + line + "\n")
        return len(s)

    def flush(self):
        try:
            self.stream.flush()
        except Exception:
            pass
        self.file.flush()


_LOG_PATH = Path(__file__).resolve().parent / "server.log"
try:
    if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > 2_000_000:
        _LOG_PATH.unlink()
    sys.stdout = _Tee(sys.stdout, _LOG_PATH)
    sys.stderr = _Tee(sys.stderr, _LOG_PATH)
except Exception:
    pass
threading.excepthook = lambda a: print(f"[ERR] vlakno {a.thread.name}: {a.exc_type.__name__}: {a.exc_value}")
print("=" * 50 + "\nstart " + time.ctime())

# ── sdílený stav ──────────────────────────────────────────────────────────────
state = {
    "yaw":   0.0,
    "pitch": 0.0,
    "hz":    0.0,
    "connected": False,
    "calibrating": True,
    "status": "Čekám na brýle…",
    "overlay_mod": 0,
    "head": {},
}
_clients: set = set()
_lock = threading.Lock()

# ── Windows input (tablet remote) ─────────────────────────────────────────────
try:
    import ctypes
    import ctypes.wintypes as _wt
    _u32 = ctypes.windll.user32
    _HAS_INPUT = True
    print("[INPUT] Windows SendInput OK")
except Exception as _e:
    _u32 = None
    _wt = None
    _HAS_INPUT = False
    print(f"[INPUT] Windows input nedostupný: {_e}")

def _mouse_move(dx, dy):
    """Relativní pohyb myši."""
    if _HAS_INPUT:
        _u32.mouse_event(0x0001, int(dx), int(dy), 0, 0)  # MOUSEEVENTF_MOVE

def _mouse_click(btn: str):
    """Kliknutí myší: 'left', 'right', 'middle'."""
    if not _HAS_INPUT:
        return
    flags = {
        'left':   (0x0002, 0x0004),   # LEFTDOWN, LEFTUP
        'right':  (0x0008, 0x0010),   # RIGHTDOWN, RIGHTUP
        'middle': (0x0020, 0x0040),   # MIDDLEDOWN, MIDDLEUP
    }.get(btn)
    if flags:
        _u32.mouse_event(flags[0], 0, 0, 0, 0)
        _u32.mouse_event(flags[1], 0, 0, 0, 0)

_MBTN = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
_held_btns: set = set()
_last_mouse_t = [0.0]


def _mouse_button(btn: str, down: bool):
    """Drzeni tlacitka mysi (tazeni / click & drag)."""
    if not _HAS_INPUT or btn not in _MBTN:
        return
    _last_mouse_t[0] = time.time()
    if down and btn not in _held_btns:
        _held_btns.add(btn)
        _u32.mouse_event(_MBTN[btn][0], 0, 0, 0, 0)
    elif not down and btn in _held_btns:
        _held_btns.discard(btn)
        _u32.mouse_event(_MBTN[btn][1], 0, 0, 0, 0)


def _release_buttons():
    for b in list(_held_btns):
        _mouse_button(b, False)


def _mouse_scroll(delta: float, hdelta: float = 0.0):
    """Scroll kolečkem (i jemne - zlomky zarazky). delta > 0 = nahoru, hdelta > 0 = doprava."""
    if not _HAS_INPUT:
        return
    v, h = int(round(float(delta) * 120)), int(round(float(hdelta) * 120))
    if v:
        _u32.mouse_event(0x0800, 0, 0, v, 0)   # MOUSEEVENTF_WHEEL
    if h:
        _u32.mouse_event(0x1000, 0, 0, h, 0)   # MOUSEEVENTF_HWHEEL

def _key_vk(vk: int):
    """Stisknutí + uvolnění klávesy podle Virtual Key kódu."""
    if _HAS_INPUT:
        _u32.keybd_event(vk, 0, 0, 0)
        _u32.keybd_event(vk, 0, 0x0002, 0)  # KEYEVENTF_KEYUP

# ── plna klavesnice z tabletu: kombinace (Ctrl+C, Alt+Tab, Win+D…) a drzene klavesy
_MOD_VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}
# klavesy, ktere Windows ocekava jako "extended" (jinak napr. sipky na numpadu delaji cisla)
_EXT_VK = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0x2C}
_held_keys: set = set()          # klavesy drzene z tabletu (zamceny Ctrl/Alt/Shift/Win)
_last_key_t = [0.0]


def _key_event(vk: int, up: bool):
    if not _HAS_INPUT:
        return
    scan = _u32.MapVirtualKeyW(vk, 0) & 0xFF
    flags = (0x0002 if up else 0) | (0x0001 if vk in _EXT_VK else 0)
    _u32.keybd_event(vk, scan, flags, 0)


def _key_combo(vk: int, mods=()):
    """Modifikatory dolu, klavesa, modifikatory nahoru (ty, co uz drzi zamek, se nechaji byt)."""
    _last_key_t[0] = time.time()
    mvk = [_MOD_VK[m] for m in mods if m in _MOD_VK and _MOD_VK[m] not in _held_keys]
    for m in mvk:
        _key_event(m, False)
    if vk:
        _key_event(vk, False)
        _key_event(vk, True)
    for m in reversed(mvk):
        _key_event(m, True)


def _key_hold(vk: int, down: bool):
    _last_key_t[0] = time.time()
    if down and vk not in _held_keys:
        _held_keys.add(vk)
        _key_event(vk, False)
    elif not down and vk in _held_keys:
        _held_keys.discard(vk)
        _key_event(vk, True)


def _release_held():
    for vk in list(_held_keys):
        _key_hold(vk, False)


def _held_watchdog():
    """Pojistka: kdyz tablet zmizi s drzenym Ctrl/Alt, po 60 s bez klaves se pusti."""
    while True:
        time.sleep(2)
        if _held_keys and time.time() - _last_key_t[0] > 60:
            print(f"[CMD] drzene klavesy {sorted(_held_keys)} pusteny (60 s bez aktivity)")
            _release_held()
        if _held_btns and time.time() - max(_last_mouse_t[0], _last_key_t[0]) > 120:
            print(f"[CMD] drzene tlacitko mysi {sorted(_held_btns)} pusteno (120 s bez aktivity)")
            _release_buttons()


threading.Thread(target=_held_watchdog, daemon=True).start()


# SendInput: struktury MUSI mit presnou velikost (64bit Windows: INPUT = 40 B), jinak Windows
# volani tise zahodi (vrati 0). Proto cela unie MOUSEINPUT/KEYBDINPUT/HARDWAREINPUT.
if _HAS_INPUT:
    _ULONG_PTR = ctypes.c_size_t

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long), ("mouseData", _wt.DWORD),
                    ("dwFlags", _wt.DWORD), ("time", _wt.DWORD), ("dwExtraInfo", _ULONG_PTR)]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", _wt.WORD), ("wScan", _wt.WORD), ("dwFlags", _wt.DWORD),
                    ("time", _wt.DWORD), ("dwExtraInfo", _ULONG_PTR)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", _wt.DWORD), ("wParamL", _wt.WORD), ("wParamH", _wt.WORD)]

    class _INPUT_U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", _wt.DWORD), ("u", _INPUT_U)]

    _u32le = ctypes.WinDLL("user32", use_last_error=True)
    _u32le.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(_INPUT), ctypes.c_int]
    _u32le.SendInput.restype = ctypes.c_uint
    print(f"[INPUT] SendInput struktura {ctypes.sizeof(_INPUT)} B")


def _send_keys(items):
    """items = [(vk, scan, flags)] -> jedno volani SendInput; pri chybe zapis do logu."""
    if not items:
        return
    arr = (_INPUT * len(items))()
    for i, (vk, scan, flags) in enumerate(items):
        arr[i].type = 1                                   # INPUT_KEYBOARD
        arr[i].u.ki.wVk, arr[i].u.ki.wScan, arr[i].u.ki.dwFlags = vk, scan, flags
    sent = _u32le.SendInput(len(items), arr, ctypes.sizeof(_INPUT))
    if sent != len(items):
        err = ctypes.get_last_error()
        hint = (" (cilove okno bezi jako spravce - spust server taky jako spravce)" if err == 5 else "")
        print(f"[INPUT] SendInput poslal {sent}/{len(items)}, chyba Windows {err}{hint}")


def _type_text(text: str, enter: bool = True):
    """Zapíše text přes SendInput (Unicode), volitelne pak Enter."""
    if not _HAS_INPUT:
        return
    _last_key_t[0] = time.time()
    KEYEVENTF_UNICODE, KEYEVENTF_KEYUP = 0x0004, 0x0002
    items = []
    data = text.encode("utf-16-le")                       # emoji apod. = dve UTF-16 jednotky
    for i in range(0, len(data), 2):
        code = data[i] | (data[i + 1] << 8)
        items.append((0, code, KEYEVENTF_UNICODE))
        items.append((0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
    if enter:
        items += [(0x0D, 0x1C, 0), (0x0D, 0x1C, KEYEVENTF_KEYUP)]
    _send_keys(items)

def _clipboard_text() -> str:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command",
                            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-Clipboard -Raw"],
                           capture_output=True, timeout=8, creationflags=0x08000000)
        return r.stdout.decode("utf-8", "replace").replace("\r\n", "\n").rstrip("\n")[:20000]
    except Exception as e:
        print(f"[INPUT] schranku nejde precist: {e}")
        return ""


def _insert_text(text: str, back: int = 0, nl: str = "shift"):
    """Navrh z tabletu: smazat `back` znaku (rozepsany zacatek), vlozit text. Novy radek =
    Shift+Enter (chat okno zpravu neodesle) nebo Enter; {} v textu = kam postavit kurzor."""
    if not _HAS_INPUT:
        return
    _last_key_t[0] = time.time()
    for tag in ("{schránka}", "{schranka}"):              # sablona: obsah schranky Windows
        if tag in text:
            text = text.replace(tag, _clipboard_text())
    U, UP, EXT = 0x0004, 0x0002, 0x0001
    items = []
    for _ in range(max(0, min(int(back), 500))):
        items += [(0x08, 0x0E, 0), (0x08, 0x0E, UP)]
    after = 0
    if "{}" in text:
        i = text.index("{}")
        text = text[:i] + text[i + 2:]
        after = len(text) - i                              # kolikrat sipka vlevo
    for li, line in enumerate(text.split("\n")):
        if li:
            if nl == "enter":
                items += [(0x0D, 0x1C, 0), (0x0D, 0x1C, UP)]
            else:
                items += [(0x10, 0x2A, 0), (0x0D, 0x1C, 0), (0x0D, 0x1C, UP), (0x10, 0x2A, UP)]
        data = line.encode("utf-16-le")
        for k in range(0, len(data), 2):
            code = data[k] | (data[k + 1] << 8)
            items += [(0, code, U), (0, code, U | UP)]
    for _ in range(min(after, 2000)):
        items += [(0x25, 0x4B, EXT), (0x25, 0x4B, EXT | UP)]
    _send_keys(items)


# overlay moduly
_OVERLAY_MODS = ['GAZE', 'INFO', 'PINY']

_cmd_stats: dict = {}


def _stats_thread():
    """Kazdych 10 s zapise do logu, kolik prikazu z tabletu prislo."""
    while True:
        time.sleep(10)
        if _cmd_stats:
            print("[CMD] za 10 s: " + ", ".join(f"{k}={v}" for k, v in sorted(_cmd_stats.items())))
            _cmd_stats.clear()


def _handle_cmd(cmd: dict):
    """Zpracuj příkaz přijatý přes WebSocket (tablet remote)."""
    t = cmd.get("t")
    _cmd_stats[t] = _cmd_stats.get(t, 0) + 1

    if t in ("mouse_move", "mouse_scroll", "mouse_click"):
        _last_mouse_t[0] = time.time()

    if t == "mouse_move":
        dx = cmd.get("dx", 0)
        dy = cmd.get("dy", 0)
        _mouse_move(dx, dy)

    elif t == "mouse_down":                 # click & drag: {"t":"mouse_down","btn":"left"}
        _mouse_button(cmd.get("btn", "left"), True)

    elif t == "mouse_up":
        _mouse_button(cmd.get("btn", "left"), False)

    elif t == "mouse_click":
        btn = cmd.get("btn", "left")
        _mouse_click(btn)
        print(f"[CMD] click {btn}")

    elif t == "mouse_scroll":
        _mouse_scroll(cmd.get("delta", 0) or 0, cmd.get("hdelta", 0) or 0)

    elif t == "key":
        vk = cmd.get("vk", 0)
        if vk:
            _key_vk(vk)
            print(f"[CMD] key VK={hex(vk)}")

    elif t == "type_text":
        text = cmd.get("text", "")
        if text:
            _type_text(text)
            print(f"[CMD] type_text: {text!r}")

    elif t == "head":                       # mys hlavou: {"t":"head","toggle":1} / {"on":true,"gain":30,...}
        _set_head(cmd)

    elif t == "calib":                      # z tabletu: {"t":"calib","action":"confirm"}
        r = _calib_action(cmd)
        if not r.get("ok"):
            print(f"[CAL] {r.get('msg')}")
        with _lock:
            state["calib_msg"] = {"t": time.time(), **r}

    elif t == "dataset":                    # z tabletu: {"t":"dataset","shoot":1} / {"learn":true} / nastaveni
        if cmd.get("shoot"):
            dataset.trigger("tablet")
        dataset.set({k: v for k, v in cmd.items() if k not in ("t", "shoot")})

    elif t == "yolo_cfg":                   # z tabletu: {"t":"yolo_cfg","task":"seg"}
        yolo.set_config({k: v for k, v in cmd.items() if k in ("size", "task", "backend")})

    elif t == "ar":                         # z tabletu: {"t":"ar","labels":false,...}
        calib.set_ar({k: v for k, v in cmd.items() if k != "t"})

    elif t == "insert":                     # navrh: {"t":"insert","text":"...","back":3}
        _insert_text(str(cmd.get("text", ""))[:20000], int(cmd.get("back", 0) or 0), cmd.get("nl", "shift"))

    elif t == "char":                       # znak z klavesnice tabletu (Unicode, bez Enteru)
        text = str(cmd.get("text", ""))[:64]
        if text:
            _type_text(text, enter=False)

    elif t == "key_combo":                  # {"vk":0x43,"mods":["ctrl"]} = Ctrl+C
        _key_combo(int(cmd.get("vk", 0)), cmd.get("mods") or [])
        if cmd.get("mods"):
            print(f"[CMD] {'+'.join(cmd['mods'])}+VK={hex(int(cmd.get('vk', 0)))}")

    elif t == "key_hold":                   # zamceny modifikator: {"vk":0x12,"down":true}
        _key_hold(int(cmd.get("vk", 0)), bool(cmd.get("down")))

    elif t == "release_all":
        _release_held()
        _release_buttons()

    elif t == "ocr":                        # z tabletu: {"t":"ocr","read":1,"speak":1} (bezi mimo WS smycku)
        def run(c=dict(cmd)):
            try:
                _ocr_action(c)
            except Exception as e:
                ocr.error = f"{type(e).__name__}: {e}"
                print(f"[OCR] {ocr.error}")
        threading.Thread(target=run, daemon=True).start()

    elif t == "zoom":                   # z tabletu: {"t":"zoom","dz":1.25} / {"reset":1} / {"dx":0.1}
        z = eye.set_zoom(cmd)
        print(f"[CMD] zoom {z['z']}x")

    elif t == "overlay":
        sub = cmd.get("cmd", "")
        with _lock:
            cur = state["overlay_mod"]
            if sub == "next":
                cur = (cur + 1) % len(_OVERLAY_MODS)
            elif sub == "prev":
                cur = (cur - 1) % len(_OVERLAY_MODS)
            state["overlay_mod"] = cur
            mod_name = _OVERLAY_MODS[cur]
        print(f"[CMD] overlay → {mod_name}")

    else:
        print(f"[CMD] neznámý: {cmd}")


# ── mys ovladana hlavou (IMU bryli) + nastaveni ulozena v settings.json ─────────
SETTINGS_FILE = Path(__file__).resolve().parent / "settings.json"
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


# ── WebSocket server ──────────────────────────────────────────────────────────
async def _ws_handler(ws):
    _clients.add(ws)
    print(f"[WS] připojen {ws.remote_address}  (celkem: {len(_clients)})")
    try:
        async for msg in ws:
            try:
                cmd = json.loads(msg)
                _handle_cmd(cmd)
            except Exception as e:
                print(f"[WS] chyba cmd: {e}")
    except Exception:
        pass
    finally:
        _clients.discard(ws)
        print(f"[WS] odpojen  (celkem: {len(_clients)})")

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


# ── XREAL Eye RGB kamera → MJPEG stream (/eye.mjpg) ───────────────────────────
EYE_NAME   = "UVC Camera 1"          # obe kamery Eye se ve Windows jmenuji takhle
EYE_WIDTH  = int(os.environ.get("EYE_WIDTH", "960"))   # zpracovani (YOLO, AR); video v okne: set EYE_WIDTH=1920
TABLET_W   = int(os.environ.get("TABLET_W", "960"))    # tablet dostava zmensenou kopii (WiFi), /eye.mjpg?w=960
EYE_FPS    = int(os.environ.get("EYE_FPS", "15"))
EYE_Q      = int(os.environ.get("EYE_Q", "6"))         # kvalita JPEG 2 (nejlepsi) .. 31
EYE_IDLE_S = int(os.environ.get("EYE_IDLE_S", "60"))  # bez divaku -> po 60 s zastavit ffmpeg
# 1 = ffmpeg jen predava plne 1080p snimky z kamery (bez prekodovani) a vyrez/zmenseni dela server
#     -> ostry digitalni zoom a plne rozliseni pro OCR;  0 = stary rezim (ffmpeg rovnou zmensuje, bez zoomu)
EYE_RAW    = os.environ.get("EYE_RAW", "1") != "0"
ZOOM_MAX   = float(os.environ.get("EYE_ZOOM_MAX", "8"))
EYE_JPEG_Q = int(os.environ.get("EYE_JPEG_Q", "82"))   # kvalita JPEG po zpracovani (rezim EYE_RAW)
EYE_ENABLER = Path(__file__).resolve().parent.parent / "xreal-eye-rgb" / "eye_rgb_enable.py"
EYE_FFLOG   = Path(__file__).resolve().parent / "eye_ffmpeg.log"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# Windows "job": vse, co do nej dame, Windows zabije, jakmile skonci server
# (i pri zavreni okna nebo Ctrl+C) -> ffmpeg uz nikdy nezustane drzet kameru.
_JOB = None
if sys.platform == "win32":
    try:
        import ctypes as _ct
        from ctypes import wintypes as _w

        class _IOC(_ct.Structure):
            _fields_ = [(n, _ct.c_ulonglong) for n in ("r", "w", "o", "rt", "wt", "ot")]

        class _BASIC(_ct.Structure):
            _fields_ = [("PerProcessUserTimeLimit", _ct.c_longlong), ("PerJobUserTimeLimit", _ct.c_longlong),
                        ("LimitFlags", _w.DWORD), ("MinimumWorkingSetSize", _ct.c_size_t),
                        ("MaximumWorkingSetSize", _ct.c_size_t), ("ActiveProcessLimit", _w.DWORD),
                        ("Affinity", _ct.c_size_t), ("PriorityClass", _w.DWORD), ("SchedulingClass", _w.DWORD)]

        class _EXT(_ct.Structure):
            _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IOC),
                        ("ProcessMemoryLimit", _ct.c_size_t), ("JobMemoryLimit", _ct.c_size_t),
                        ("PeakProcessMemoryUsed", _ct.c_size_t), ("PeakJobMemoryUsed", _ct.c_size_t)]

        _k32 = _ct.WinDLL("kernel32", use_last_error=True)
        _k32.CreateJobObjectW.restype = _w.HANDLE
        _k32.SetInformationJobObject.argtypes = [_w.HANDLE, _ct.c_int, _ct.c_void_p, _w.DWORD]
        _k32.AssignProcessToJobObject.argtypes = [_w.HANDLE, _w.HANDLE]
        _JOB = _k32.CreateJobObjectW(None, None)
        _info = _EXT()
        _info.BasicLimitInformation.LimitFlags = 0x2000          # KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(_JOB, 9, _ct.byref(_info), _ct.sizeof(_info)):
            _JOB = None
    except Exception as _e:
        print(f"[EYE] job object nejde vytvorit: {_e}")
        _JOB = None


def _bind_to_server(proc):
    if _JOB is not None:
        try:
            _k32.AssignProcessToJobObject(_JOB, int(proc._handle))
        except Exception as e:
            print(f"[EYE] nelze svazat ffmpeg se serverem: {e}")


def _kill_orphan_ffmpeg():
    """Ukonci ffmpeg, ktery po minulem serveru zustal drzet kameru Eye."""
    if sys.platform != "win32":
        return
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
          "Where-Object { $_.CommandLine -like '*-f dshow*' -and $_.CommandLine -like '*pipe:1*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                           text=True, timeout=20, creationflags=_NO_WINDOW)
        pids = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
        if pids:
            print(f"[EYE] ukoncen osirely ffmpeg z minula (PID {', '.join(pids)}) – kamera uvolnena")
    except Exception as e:
        print(f"[EYE] kontrola osirelych ffmpeg selhala: {e}")


def _find_ffmpeg():
    for p in (r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\ffmpeg.exe"):
        if os.path.exists(p):
            return p
    return shutil.which("ffmpeg")


def split_jpegs(buf: bytearray):
    """Vytahne z bufferu cele JPEG snimky (SOI ffd8 .. EOI ffd9), zbytek necha."""
    out = []
    while True:
        s = buf.find(b"\xff\xd8")
        if s < 0:
            buf.clear()
            break
        e = buf.find(b"\xff\xd9", s + 2)
        if e < 0:
            if s > 0:
                del buf[:s]
            break
        out.append(bytes(buf[s:e + 2]))
        del buf[:e + 2]
    return out


class EyeStream:
    """Jeden ffmpeg proces cte RGB kameru (MJPEG) a vsichni divaci dostavaji posledni snimek."""

    def __init__(self):
        self.cond = threading.Condition()
        self.start_lock = threading.Lock()
        self.frame, self.seq = None, 0
        self.proc = None
        self.clients, self.last_seen = 0, 0.0
        self.status, self.devnum, self.devname = "idle", None, EYE_NAME
        self.frames, self.errlog = 0, None
        # plny snimek z kamery (1080p JPEG) a zpracovany pohled (zoom, 960 px) pro vsechny divaky
        self.raw_mode = EYE_RAW
        self.raw, self.raw_seq, self.raw_cond = None, 0, threading.Condition()
        self.img, self.vmap = None, (0.0, 0.0, 1.0, 1.0)   # vmap = vyrez v plnem snimku (x0, y0, w, h) 0..1
        self.zoom = {"z": 1.0, "cx": 0.5, "cy": 0.5}
        self.proc_ms = 0.0
        threading.Thread(target=self._watchdog, daemon=True).start()
        threading.Thread(target=self._process, daemon=True).start()

    # -- detekce kamery
    def _run(self, args, timeout=15):
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
            return (r.stdout or "") + (r.stderr or "")
        except Exception as e:
            return str(e)

    def _find_rgb(self, ff):
        """Najde RGB kameru Eye podle schopnosti (MJPEG 1920x1080), ne podle nazvu.
        -> (cislo zarizeni nebo None, pocet kandidatu); jmeno ulozi do self.devname."""
        out = self._run([ff, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
        # "(video)" = kamera jde otevrit, "(none)" = Windows ji desktopovym aplikacim prave neotevre
        listed = re.findall(r'"([^"]+)"\s*\((video|none)\)', out)
        names = [n for n, _ in listed]
        self.unopenable = [n for n, kind in listed if kind == "none"]
        skip = ("integrated", "meta quest", " ir ", "virtual", "obs ")
        cands, seen = [], {}
        for n in names:
            num = seen.get(n, 0)
            seen[n] = num + 1
            if not any(k in f" {n.lower()} " for k in skip):
                cands.append((n, num))
        cands.sort(key=lambda c: 0 if any(k in c[0].lower() for k in ("uvc", "xreal", "eye")) else 1)
        virtual = ("meta quest", "virtual", "obs ")
        # zadna fyzicka kamera (ani vestavena) = problem Windows (opravneni / Frame Server), ne bryli
        self.no_physical = not any(not any(k in f" {n.lower()} " for k in virtual)
                                   for n in names if n not in self.unopenable)
        self.rgb_cands = []
        for n, num in cands:
            opts = self._run([ff, "-hide_banner", "-list_options", "true", "-f", "dshow",
                              "-video_device_number", str(num), "-i", f"video={n}"])
            if "vcodec=mjpeg" in opts and "1920x1080" in opts:
                self.rgb_cands.append((n, num))
        if self.rgb_cands:
            good = getattr(self, "good", None)              # naposledy fungujici ma prednost
            self.rgb_cands.sort(key=lambda c: 0 if c == good else 1)
            n, num = self.rgb_cands[0]
            self.devname = n
            return num, len(cands)
        if time.time() - getattr(self, "last_names_log", 0) > 60:
            self.last_names_log = time.time()
            print(f"[EYE] kamery ve Windows: {names or '(zadne)'}; kandidati: {[c[0] for c in cands]}")
            if self.unopenable and cands:
                n, num = cands[0]
                err = self._run([ff, "-hide_banner", "-list_options", "true", "-f", "dshow",
                                 "-video_device_number", str(num), "-i", f"video={n}"])
                last = [l.split("]", 1)[-1].strip() for l in err.splitlines() if l.strip()][-3:]
                print(f"[EYE] Windows kamery nabizi jen jmenem, otevrit nejdou ({self.unopenable}); "
                      f"ffmpeg u \"{n}\": {' | '.join(last)}")
        return None, len(cands)

    def _enable_cameras(self) -> bool:
        """Zapne kamery pres HID - nejvys jednou za 2 minuty (kazde zapnuti bryle znovu pripoji)."""
        if not EYE_ENABLER.exists():
            print(f"[EYE] zapinac nenalezen: {EYE_ENABLER}")
            return False
        if time.time() - getattr(self, "last_enable", 0) < 120:
            return False
        self.last_enable = time.time()
        print("[EYE] zapinam kamery Eye pres HID …")
        out = self._run([sys.executable, str(EYE_ENABLER), "--no-probe"], timeout=90)
        for line in out.splitlines():
            if any(k in line for k in ("USB funkce", "HID", "selhal", "Chyba", "nenalezeno", "Error", "error")):
                print("[EYE]   " + line.strip()[:160])
        return True

    # -- beh
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> bool:
        self.last_seen = time.time()               # zadost o start = nekdo se chce divat
        with self.start_lock:
            if self.running():
                return True
            ff = _find_ffmpeg()
            if not ff:
                self.status = "ffmpeg nenalezen"
                return False
            self.status = "hledam kameru"
            num, count = None, 0
            for attempt in range(3):                # kamera muze byt chvili "busy" po predchozim ffmpeg
                num, count = self._find_rgb(ff)
                if num is not None:
                    break
                if attempt == 0 and count:
                    _kill_orphan_ffmpeg()           # kamera je v systemu, ale neodpovida -> drzi ji nekdo
                time.sleep(2)
            if num is None and getattr(self, "no_physical", False):
                self.status = ("Windows kamery ukazuje, ale desktopovym aplikacim je neotevre (i vestavenou) - "
                               "Nastaveni > Ochrana osobnich udaju > Fotoaparat > pristup pro desktopove "
                               "aplikace, pripadne restart Windows")
                if time.time() - getattr(self, "last_np_log", 0) > 60:
                    self.last_np_log = time.time()
                    print(f"[EYE] {self.status}")
                return False
            if num is None and count == 0 and self._enable_cameras():   # kamery v systemu nejsou
                self.status = "zapinam kameru"
                for _ in range(5):
                    time.sleep(3)
                    num, count = self._find_rgb(ff)
                    if num is not None:
                        break
            if num is None:
                self.status = ("kamera obsazena jinou aplikaci" if count
                               else "kamera Eye neni v systemu - odpoj a pripoj bryle")
                print(f"[EYE] {self.status} (kamer Eye v systemu: {count})")
                return False
            # kandidati (stejne jmeno "UVC Camera 1", poradi se muze menit): ten, co do 8 s posle snimek
            cands = getattr(self, "rgb_cands", None) or [(self.devname, num)]
            for n, dnum in cands:
                if self._launch(ff, n, dnum):
                    self.good = (n, dnum)
                    return True
                print(f"[EYE] \"{n}\" #{dnum}: do 8 s zadny snimek – zkousim dalsi")
                self.stop()
            self.status = "kamera neposila obraz - mas bryle nasazene (Eye sviti)?"
            print(f"[EYE] {self.status}")
            return False

    def _launch(self, ff, name, num, wait_s=8.0) -> bool:
        """Spusti ffmpeg pro jednu kameru a pocka na prvni snimek."""
        self.devnum, self.devname = num, name
        # stejne vstupni volby jako overeny test (eye_cam_map.py), ktery snimky dal
        cmd = [ff, "-hide_banner", "-loglevel", "warning",
               "-f", "dshow", "-rtbufsize", "256M", "-video_device_number", str(num),
               "-video_size", "1920x1080", "-vcodec", "mjpeg",
               "-i", f"video={name}"]
        if self.raw_mode:       # snimky z kamery beze zmeny (zadne dekodovani v ffmpeg)
            cmd += ["-c:v", "copy", "-f", "mjpeg", "-flush_packets", "1", "pipe:1"]
        else:
            cmd += ["-vf", f"scale={EYE_WIDTH}:-2", "-r", str(EYE_FPS), "-q:v", str(EYE_Q),
                    "-f", "mjpeg", "-flush_packets", "1", "pipe:1"]
        try:
            self.errlog = open(EYE_FFLOG, "a", encoding="utf-8", errors="replace")
            self.errlog.write(f"\n=== {time.ctime()}\n{' '.join(cmd)}\n")
            self.errlog.flush()
        except Exception:
            self.errlog = subprocess.DEVNULL
        self.frames = 0
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.errlog, creationflags=_NO_WINDOW)
        _bind_to_server(self.proc)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.status = "spoustim kameru"
        print(f"[EYE] spoustim \"{name}\" #{num} ({EYE_WIDTH}px, {EYE_FPS} fps, "
              f"{'plne 1080p + zoom' if self.raw_mode else 'zmensuje ffmpeg, bez zoomu'})")
        t0 = time.time()
        while time.time() - t0 < wait_s:
            if self.frames > 0:
                self.status = "bezi"
                return True
            if not self.running():
                return False
            time.sleep(0.1)
        return False

    def stop(self, fast=False):
        """Setrne zastaveni: ffmpeg dostane 'q', uzavre DirectShow a kameru uvolni.
        fast=True: zaseknuty ffmpeg (na 'q' nereaguje) rovnou ukoncit."""
        p = self.proc
        if p and p.poll() is None:
            try:
                p.stdin.write(b"q")
                p.stdin.flush()
                p.wait(1 if fast else 4)
            except Exception:
                pass
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(3)
                except Exception:
                    p.kill()
        self.status = "zastaveno"

    def _reader(self, proc):
        buf = bytearray()
        read = getattr(proc.stdout, "read1", proc.stdout.read)
        while True:
            chunk = read(65536)
            if not chunk:
                break
            buf += chunk
            for jpg in split_jpegs(buf):
                if self.frames == 0:
                    print(f"[EYE] prvni snimek ({len(jpg)} B)")
                self.frames += 1
                self.last_frame_t = time.time()
                if self.raw_mode:                   # zpracuje _process (zoom, zmenseni)
                    with self.raw_cond:
                        self.raw = jpg
                        self.raw_seq += 1
                        self.raw_cond.notify_all()
                    continue
                with self.cond:
                    self.frame = jpg
                    self.seq += 1
                    self.cond.notify_all()
        with self.cond:
            self.cond.notify_all()
        rc = proc.wait()
        tail = ""
        try:
            tail = " | ".join(l.strip() for l in open(EYE_FFLOG, encoding="utf-8",
                                                      errors="replace").read().splitlines()[-4:])
        except Exception:
            pass
        print(f"[EYE] ffmpeg skoncil (kod {rc}, snimku {self.frames}) {tail}")
        if self.proc is proc:
            self.status = "zastaveno"

    def _watchdog(self):
        while True:
            time.sleep(2)
            if self.start_lock.locked():
                continue                            # prave se spousti -> nechat byt
            if self.running() and self.clients == 0 and time.time() - self.last_seen > EYE_IDLE_S:
                print("[EYE] zadny divak – zastavuji stream")
                self.stop()
            elif (self.running() and self.clients > 0 and self.frames > 0
                  and time.time() - getattr(self, "last_frame_t", time.time()) > 6):
                # kamera prestala posilat bez chyby (napr. bryle sundane/uspane) -> restart ffmpeg
                print("[EYE] kamera 6 s neposlala snimek – restartuji stream")
                self.restarting = True
                try:
                    self.stop(fast=True)
                    self.start()
                finally:
                    self.restarting = False

    def add_client(self):
        with self.cond:
            self.clients += 1
            self.last_seen = time.time()

    def remove_client(self):
        with self.cond:
            self.clients = max(0, self.clients - 1)
            self.last_seen = time.time()

    def wait_frame(self, last_seq, timeout=5.0):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq or not self.running(), timeout=timeout)
            return self.frame, self.seq

    def view(self, seq):
        """Dekodovany pohled (numpy) a jeho vyrez v plnem snimku pro dany seq, jinak (None, None)."""
        with self.cond:
            if seq == self.seq and self.img is not None:
                return self.img, self.vmap
        return None, None

    # -- zoom: vyrez z plneho 1080p snimku, vystup vzdy EYE_WIDTH px
    def _crop_rect(self, z, cx, cy):
        cw = ch = 1.0 / z
        x0 = min(max(cx - cw / 2, 0.0), 1.0 - cw)
        y0 = min(max(cy - ch / 2, 0.0), 1.0 - ch)
        return x0, y0, cw, ch

    def set_zoom(self, b: dict):
        """z = absolutni zoom, dz = nasobek (kolecko), at = [u, v] bod v obrazu, ktery zustane na miste,
        dx/dy = posun v obrazovkach, cx/cy = stred 0..1, reset = 1x."""
        z0, cx, cy = self.zoom["z"], self.zoom["cx"], self.zoom["cy"]
        x0, y0, cw, ch = self._crop_rect(z0, cx, cy)
        if b.get("reset"):
            z, cx, cy = 1.0, 0.5, 0.5
        else:
            z = float(b["z"]) if "z" in b else z0 * float(b.get("dz", 1.0))
            z = min(max(z, 1.0), ZOOM_MAX)
            if "cx" in b or "cy" in b:
                cx, cy = float(b.get("cx", cx)), float(b.get("cy", cy))
            elif isinstance(b.get("at"), (list, tuple)) and len(b["at"]) == 2:
                u, v = (min(max(float(t), 0.0), 1.0) for t in b["at"])
                px, py = x0 + u * cw, y0 + v * ch          # bod v plnem snimku
                cx, cy = px - u / z + 0.5 / z, py - v / z + 0.5 / z
            else:
                cx, cy = x0 + cw / 2, y0 + ch / 2         # z orezaneho (realneho) stredu
            cx += float(b.get("dx", 0.0)) / z
            cy += float(b.get("dy", 0.0)) / z
        nx0, ny0, ncw, nch = self._crop_rect(z, cx, cy)
        self.zoom = {"z": round(z, 3), "cx": round(nx0 + ncw / 2, 4), "cy": round(ny0 + nch / 2, 4)}
        return self.zoom_info()

    def zoom_info(self):
        return {**self.zoom, "max": ZOOM_MAX, "available": self.raw_mode}

    def _render(self, raw):
        import cv2
        import numpy as np
        z = self.zoom["z"]
        if z <= 1.001:      # bez zoomu: JPEG rovnou dekodovat v polovicnim rozliseni (rychle)
            flag = cv2.IMREAD_REDUCED_COLOR_2 if EYE_WIDTH <= 960 else cv2.IMREAD_COLOR
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), flag)
            rect = (0.0, 0.0, 1.0, 1.0)
        else:
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return None, None
            h, w = img.shape[:2]
            rect = self._crop_rect(z, self.zoom["cx"], self.zoom["cy"])
            x0, y0, cw, ch = rect
            img = img[int(y0 * h):int((y0 + ch) * h), int(x0 * w):int((x0 + cw) * w)]
        if img is None or img.size == 0:
            return None, None
        h, w = img.shape[:2]
        ow = EYE_WIDTH
        oh = int(round(ow * h / w / 2)) * 2
        if (w, h) != (ow, oh):
            interp = cv2.INTER_AREA if w > ow else cv2.INTER_CUBIC
            img = cv2.resize(img, (ow, oh), interpolation=interp)
        return img, rect

    def _process(self):
        """Z plnych snimku kamery dela pohled pro divaky: zoom, zmenseni, JPEG; max EYE_FPS."""
        try:
            import cv2
        except Exception as e:
            print(f"[EYE] OpenCV chybi ({e}) – zoom nepujde, prepinam na stary rezim")
            self.raw_mode = False
            return
        last, tnext, fails = 0, 0.0, 0
        period = 1.0 / max(1, EYE_FPS)
        while True:
            with self.raw_cond:
                self.raw_cond.wait_for(lambda: self.raw_seq != last, timeout=2)
                raw, rs = self.raw, self.raw_seq
            if raw is None or rs == last or not self.raw_mode:
                continue
            last = rs
            now = time.time()
            if now < tnext:                         # kamera posila az 60 fps, divakum staci EYE_FPS
                continue
            tnext = max(tnext + period, now - period)
            t = time.time()
            try:
                img, rect = self._render(raw)
            except Exception as e:
                img, rect = None, None
                if fails == 0:
                    print(f"[EYE] zpracovani snimku: {type(e).__name__}: {e}")
            if img is None:
                fails += 1
                if fails == 30:                     # snimky z kamery nejdou dekodovat -> stary rezim
                    print("[EYE] snimky z kamery nejdou dekodovat – prepinam na stary rezim (bez zoomu)")
                    self.raw_mode = False
                    threading.Thread(target=self._restart, daemon=True).start()
                continue
            fails = 0
            if rect == (0.0, 0.0, 1.0, 1.0) and img.shape[1] == EYE_WIDTH and EYE_WIDTH >= 1920:
                jpg = raw                           # bez zoomu v plnem rozliseni: originalni JPEG z kamery
            else:
                ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, EYE_JPEG_Q])
                if not ok:
                    continue
                jpg = enc.tobytes()
            self.proc_ms = round((time.time() - t) * 1000, 1)
            with self.cond:
                self.frame, self.img, self.vmap = jpg, img, rect
                self.seq += 1
                self.cond.notify_all()

    def _restart(self):
        self.restarting = True
        try:
            self.stop(fast=True)
            self.start()
        finally:
            self.restarting = False


eye = EyeStream()


# ── YOLO nad Eye streamem → /eye_yolo.mjpg + /yolo.json + state["yolo"] ──────
# Modely: yolo11{n,s,m}[-seg], beh: PyTorch (CPU) nebo OpenVINO (CPU / GPU Arc / NPU).
YOLO_IMGSZ = int(os.environ.get("YOLO_IMGSZ", "640"))
YOLO_CONF  = float(os.environ.get("YOLO_CONF", "0.35"))
YOLO_DIR   = Path(__file__).resolve().parent            # sem se stahuji vahy a OpenVINO exporty
YOLO_CFG_FILE = YOLO_DIR / "yolo_config.json"
YOLO_SIZES, YOLO_TASKS = ("n", "s", "m"), ("det", "seg")
YOLO_BACKENDS = ("torch:cpu", "openvino:cpu", "openvino:gpu", "openvino:npu")
DEFAULT_YOLO_CFG = {"size": "n", "task": "det", "backend": "torch:cpu"}


def _ov_devices():
    try:
        import openvino as ov
        return [d.split(".")[0] for d in ov.Core().available_devices]
    except Exception:
        return []


def _clean_cfg(c):
    c = {**DEFAULT_YOLO_CFG, **(c or {})}
    if c["size"] not in YOLO_SIZES:
        c["size"] = "n"
    if c["task"] not in YOLO_TASKS:
        c["task"] = "det"
    if c["backend"] not in YOLO_BACKENDS:
        c["backend"] = "torch:cpu"
    return {k: c[k] for k in DEFAULT_YOLO_CFG}


def _load_cfg():
    try:
        return _clean_cfg(json.loads(YOLO_CFG_FILE.read_text(encoding="utf-8")))
    except Exception:
        return dict(DEFAULT_YOLO_CFG)


def _cfg_label(c):
    return f"yolo11{c['size']}{'-seg' if c['task'] == 'seg' else ''} · {c['backend']}"


def build_model(cfg, log=print):
    """-> (model, kwargs pro predict). OpenVINO export se udela jen poprve."""
    from ultralytics import YOLO
    stem = f"yolo11{cfg['size']}{'-seg' if cfg['task'] == 'seg' else ''}"
    pt = YOLO_DIR / f"{stem}.pt"
    kind, dev = cfg["backend"].split(":")
    if kind == "torch":
        return YOLO(str(pt) if pt.exists() else pt.name), {"device": "cpu"}
    # ultralytics pozna OpenVINO model jen podle koncovky slozky "_openvino_model"
    ovdir = YOLO_DIR / f"{stem}_{YOLO_IMGSZ}_openvino_model"
    old = YOLO_DIR / f"{stem}_ov{YOLO_IMGSZ}"                 # nazev z predchozi verze
    if old.exists() and not ovdir.exists():
        shutil.move(str(old), str(ovdir))
    if not (ovdir / f"{stem}.xml").exists():
        log(f"[YOLO] prevadim {stem} do OpenVINO (jen poprve, chvili to trva) …")
        src = YOLO(str(pt) if pt.exists() else pt.name)
        try:                                    # novejsi ultralytics: quantize=16 (FP16)
            out = Path(src.export(format="openvino", quantize=16, imgsz=YOLO_IMGSZ))
        except Exception:                       # starsi verze: half=True
            out = Path(src.export(format="openvino", half=True, imgsz=YOLO_IMGSZ))
        if out.resolve() != ovdir.resolve():
            if ovdir.exists():
                shutil.rmtree(ovdir)
            shutil.move(str(out), str(ovdir))
    model = YOLO(str(ovdir), task="segment" if cfg["task"] == "seg" else "detect")
    return model, {"device": f"intel:{dev}"}


def _predict(model, kw, img):
    try:
        return model.predict(img, imgsz=YOLO_IMGSZ, conf=YOLO_CONF, verbose=False, **kw)[0]
    except Exception as e:
        if kw.get("device", "cpu") != "cpu" and "device" in str(e).lower():
            print(f"[YOLO] zarizeni {kw['device']} nepodporovane touto verzi ultralytics ({e}) – zkousim AUTO")
            kw.pop("device", None)
            return model.predict(img, imgsz=YOLO_IMGSZ, conf=YOLO_CONF, verbose=False, **kw)[0]
        raise


def _poly(xy, max_pts=96):
    """Zjednodusi obrys masky na <= max_pts bodu tak, aby zustal tvar (Douglas-Peucker),
    normalizovane 0..1, + plocha (podil snimku). Pro AR vyplne pres realny predmet."""
    pts = [(float(x), float(y)) for x, y in xy]
    if len(pts) < 3:
        return [], 0.0
    area = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        area += x1 * y2 - x2 * y1
    try:
        import cv2
        import numpy as np
        c = np.float32(pts).reshape(-1, 1, 2)
        eps, out = 0.0015, c
        while len(out) > max_pts and eps < 0.05:
            out = cv2.approxPolyDP(c, eps, True)
            eps *= 1.5
        if len(out) < 3:
            out = c
        pts = [tuple(p) for p in out.reshape(-1, 2)]
    except Exception:
        step = max(1, len(pts) // max_pts)
        pts = pts[::step]
    return [[round(float(x), 4), round(float(y), 4)] for x, y in pts], round(min(1.0, abs(area) / 2), 4)


# ── textury pres objekty (AR "pretexturovani") ────────────────────────────────
TEXTURE_DIR = Path(__file__).resolve().parent / "static" / "textures"   # vlastni obrazky .jpg/.png
_tex_cache: dict = {}


def _proc_texture(name, n=512):
    """Proceduralni textury bez souboru: drevo, mramor, cihly, sachovnice."""
    import cv2
    import numpy as np
    rng = np.random.default_rng(7)

    def noise(octaves=5):
        acc = np.zeros((n, n), np.float32)
        for o in range(octaves):
            k = 4 * 2 ** o
            acc += cv2.resize(rng.random((k, k)).astype(np.float32), (n, n),
                              interpolation=cv2.INTER_CUBIC) / 2 ** o
        return acc / acc.max()

    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32) / n
    if name == "mramor":
        v = np.sin((xx * 6 + yy * 3 + noise() * 5) * np.pi) * 0.5 + 0.5
        v = v ** 0.35
        img = np.dstack([215 + 35 * v, 210 + 40 * v, 200 + 50 * v])
        img -= (1 - v)[..., None] * np.array([120, 115, 105])
    elif name == "cihly":
        img = np.zeros((n, n, 3), np.float32)
        img[:] = (160, 165, 170)                            # malta (BGR)
        bh, bw = n // 8, n // 4
        for r in range(8):
            off = (bw // 2) * (r % 2)
            for c in range(-1, 5):
                x0, y0 = c * bw + off + 3, r * bh + 3
                shade = 0.8 + 0.4 * rng.random()
                cv2.rectangle(img, (x0, y0), (x0 + bw - 6, y0 + bh - 6),
                              (40 * shade, 60 * shade, 150 * shade), -1)
        img *= (0.85 + 0.3 * noise(4))[..., None]
    elif name == "sachovnice":
        v = ((np.floor(xx * 8) + np.floor(yy * 8)) % 2).astype(np.float32)
        img = np.dstack([v * 230 + 15] * 3)
    else:                                                    # drevo
        r = np.sqrt((xx - 0.5) ** 2 * 0.15 + (yy - 0.2) ** 2) * 28 + noise() * 3
        v = (np.sin(r * np.pi) * 0.5 + 0.5) ** 0.6
        img = np.dstack([40 + 30 * v, 85 + 50 * v, 140 + 70 * v])
        img *= (0.9 + 0.2 * noise(3))[..., None]
    return np.clip(img, 0, 255).astype(np.uint8)


def texture_names():
    names = ["drevo", "mramor", "cihly", "sachovnice"]
    if TEXTURE_DIR.exists():
        names += sorted(p.stem for p in TEXTURE_DIR.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    return names


def get_texture(name):
    if name not in _tex_cache:
        import cv2
        tex = None
        for ext in (".jpg", ".jpeg", ".png"):
            f = TEXTURE_DIR / f"{name}{ext}"
            if f.exists():
                tex = cv2.imread(str(f), cv2.IMREAD_COLOR)
                break
        _tex_cache[name] = tex if tex is not None else _proc_texture(name)
    return _tex_cache[name]


def apply_textures(out, raw, r, dets, tex_cfg):
    """Vyplni masky vybranych objektu texturou; stiny a svetlo bere z realneho obrazu."""
    import cv2
    import numpy as np
    H, W = out.shape[:2]
    tex = get_texture(tex_cfg.get("name", "drevo"))
    wanted = set(tex_cfg.get("classes") or [])
    polys = list(r.masks.xy) if getattr(r, "masks", None) is not None else []
    for i, d in enumerate(dets):
        if wanted and d["name"] not in wanted:
            continue
        x1, y1, x2, y2 = (int(round(v)) for v in (d["box"][0] * W, d["box"][1] * H, d["box"][2] * W, d["box"][3] * H))
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue
        mask = np.zeros((y2 - y1, x2 - x1), np.uint8)
        if i < len(polys) and len(polys[i]) >= 3:
            pts = (np.asarray(polys[i], np.float32) - [x1, y1]).astype(np.int32)
            cv2.fillPoly(mask, [pts], 255)
        else:
            mask[:] = 255                                   # bez segmentace = cely ramecek
        mask = cv2.GaussianBlur(mask, (5, 5), 0).astype(np.float32) / 255 * 0.92
        t = cv2.resize(tex, (x2 - x1, y2 - y1), interpolation=cv2.INTER_LINEAR).astype(np.float32)
        luma = cv2.cvtColor(raw[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY).astype(np.float32)
        shade = np.clip(0.35 + 0.75 * luma / max(float(luma.mean()), 1.0), 0.2, 1.6)[..., None]
        region = out[y1:y2, x1:x2].astype(np.float32)
        m = mask[..., None]
        out[y1:y2, x1:x2] = np.clip(region * (1 - m) + np.clip(t * shade, 0, 255) * m, 0, 255).astype(np.uint8)
        if i < len(polys) and len(polys[i]) >= 3:
            c = d["color"].lstrip("#")
            bgr = (int(c[4:6], 16), int(c[2:4], 16), int(c[0:2], 16))
            cv2.polylines(out, [np.asarray(polys[i], np.int32)], True, bgr, 2)
    return out


class YoloWorker:
    """Bere vzdy posledni snimek z Eye, pusti YOLO, nakresli vysledky a posle dal.
    Pomale snimky se preskakuji, takze zpozdeni nenarusta. Model jde prepnout za behu."""

    def __init__(self, src):
        self.src = src
        self.cond = threading.Condition()
        self.start_lock = threading.Lock()
        self.frame, self.seq = None, 0
        self.dets, self.ms, self.fps = [], 0.0, 0.0
        self.clients, self.last_seen = 0, 0.0
        self.video_viewers, self.nseq = 0, 0          # divaci videa s ramecky; poradi detekci
        self.status, self.model, self.thread, self.stop_flag = "idle", None, None, False
        self.cfg, self.kw, self.pending = _load_cfg(), {}, None
        self.bench = {"running": False, "results": [], "current": ""}
        self.tex = {"on": False, "name": "drevo", "classes": []}
        threading.Thread(target=self._watchdog, daemon=True).start()

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def _load(self):
        if self.model is not None:
            return True
        try:
            import cv2      # noqa: F401
            import numpy    # noqa: F401
            import ultralytics  # noqa: F401
        except Exception as e:
            self.status = "chybi ultralytics - spust yolo_install.bat"
            print(f"[YOLO] {self.status} ({e})")
            return False
        self.status = "nacitam model"
        print(f"[YOLO] nacitam {_cfg_label(self.cfg)} …")
        try:
            self.model, self.kw = build_model(self.cfg)
        except Exception as e:
            print(f"[YOLO] {_cfg_label(self.cfg)} nejde nacist: {e} – zkousim PyTorch CPU")
            try:
                self.cfg = {**self.cfg, "backend": "torch:cpu"}
                self.model, self.kw = build_model(self.cfg)
            except Exception as e2:
                self.status = f"model nejde nacist: {e2}"
                print(f"[YOLO] {self.status}")
                return False
        return True

    def set_config(self, cfg):
        cfg = _clean_cfg({**self.cfg, **(cfg or {})})
        YOLO_CFG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        if self.running():
            self.pending = cfg                      # prepne vlakno pred dalsim snimkem
        else:
            self.cfg, self.model = cfg, None        # nacte se pri dalsim startu
        print(f"[YOLO] nove nastaveni: {_cfg_label(cfg)}")
        return cfg

    def _apply_pending(self):
        cfg, self.pending = self.pending, None
        self.status = f"prepinam na {_cfg_label(cfg)}"
        print(f"[YOLO] {self.status} …")
        try:
            t = time.time()
            self.model, self.kw = build_model(cfg)
            self.cfg = cfg
            self.status = "bezi"
            print(f"[YOLO] prepnuto za {time.time() - t:.1f} s")
        except Exception as e:
            self.status = "bezi"
            print(f"[YOLO] prepnuti selhalo ({type(e).__name__}: {e}) – zustava {_cfg_label(self.cfg)}")

    def start(self) -> bool:
        with self.start_lock:
            if self.running():
                return True
            if not self._load():
                return False
            self.src.add_client()                   # drzi Eye stream naziva (uz behem spousteni)
            if not self.src.start():
                self.src.remove_client()
                self.status = "Eye: " + self.src.status
                return False
            self.stop_flag = False
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            self.status = "bezi"
            print(f"[YOLO] bezi ({_cfg_label(self.cfg)}, imgsz {YOLO_IMGSZ}, conf {YOLO_CONF})")
            return True

    def _loop(self):
        import cv2
        import numpy as np
        last = self.src.seq - 1 if self.src.frame is not None else self.src.seq
        n, t0 = 0, time.time()
        try:
            while not self.stop_flag:
                if self.pending:
                    self._apply_pending()
                jpg, seq = self.src.wait_frame(last, timeout=5)
                if jpg is None or seq == last:
                    if not self.src.running() and not getattr(self.src, "restarting", False):
                        if not self.src.start():            # kamera spadla -> zkusit znovu, YOLO bezi dal
                            self.status = "Eye: " + self.src.status
                            time.sleep(2)
                            continue
                        self.status = "bezi"
                    continue
                last = seq
                img, vmap = self.src.view(seq)          # uz dekodovany pohled (setri cas)
                if img is None:
                    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                    vmap = (0.0, 0.0, 1.0, 1.0) if not self.src.raw_mode else self.src.vmap
                if img is None:
                    continue
                zx, zy, zw, zh = vmap
                t = time.time()
                r = _predict(self.model, self.kw, img)
                ms = round((time.time() - t) * 1000, 1)
                ts = time.time()
                dets = []
                polys = list(r.masks.xyn) if getattr(r, "masks", None) is not None else []
                for i, b in enumerate(r.boxes):
                    x1, y1, x2, y2 = (round(float(v), 4) for v in b.xyxyn[0])
                    c = int(b.cls[0])
                    det = {"name": r.names[c], "cls": c, "color": _cls_color(c),
                           "conf": round(float(b.conf[0]), 2), "box": [x1, y1, x2, y2]}
                    if i < len(polys):
                        det["poly"], det["area"] = _poly(polys[i])
                    if zw < 0.999:                      # zoom: box i v souradnicich celeho snimku
                        det["sbox"] = [round(zx + x1 * zw, 4), round(zy + y1 * zh, 4),
                                       round(zx + x2 * zw, 4), round(zy + y2 * zh, 4)]
                    dets.append(det)
                ok, enc = False, None
                if self.video_viewers > 0 or recorder.active:      # AR rezim videa nepotrebuje
                    out = r.plot()
                    if self.tex["on"] and dets:
                        try:
                            out = apply_textures(out, img, r, dets, self.tex)
                        except Exception as e:
                            print(f"[TEX] chyba: {type(e).__name__}: {e}")
                            self.tex["on"] = False
                    ok, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 85])
                n += 1
                if time.time() - t0 >= 2:
                    self.fps, n, t0 = round(n / (time.time() - t0), 1), 0, time.time()
                with self.cond:
                    if ok:
                        self.frame = enc.tobytes()
                        self.seq += 1
                    self.dets, self.ms, self.dt = dets, ms, ts
                    self.raw = jpg
                    self.cond.notify_all()
                if ok:
                    recorder.on_frame(self.frame, dets, ts, self.seq)
                self.nseq += 1
                with _lock:
                    state["yolo"] = {"dets": dets, "ms": ms, "fps": self.fps,
                                     "t": round(ts, 3), "seq": self.nseq, "model": _cfg_label(self.cfg),
                                     "zoom": self.src.zoom_info(),
                                     "head": [state.get("yaw", 0.0), state.get("pitch", 0.0)],
                                     "task": self.cfg.get("task", "det"),
                                     "video": ok}
        except Exception as e:
            self.status = f"chyba: {e}"
            print(f"[YOLO] chyba: {type(e).__name__}: {e}")
        finally:
            self.src.remove_client()
            with self.cond:
                self.cond.notify_all()
            with _lock:
                state.pop("yolo", None)
            if self.status == "bezi":
                self.status = "zastaveno"
            print("[YOLO] zastaveno")

    # -- benchmark: stejne snimky, ruzne modely a zarizeni
    def start_bench(self, sizes=None, backends=None, task=None):
        if self.bench["running"]:
            return False
        devs = _ov_devices()
        sizes = [s for s in (sizes or ["n", "s"]) if s in YOLO_SIZES]
        backends = [b for b in (backends or YOLO_BACKENDS) if b in YOLO_BACKENDS
                    and (b.startswith("torch") or b.split(":")[1].upper() in devs)]
        task = task if task in YOLO_TASKS else self.cfg["task"]
        self.bench = {"running": True, "results": [], "current": "", "task": task}
        threading.Thread(target=self._bench, args=(sizes, backends, task), daemon=True).start()
        return True

    def _bench(self, sizes, backends, task):
        import cv2
        import numpy as np
        img = None
        if self.src.frame is not None:
            img = cv2.imdecode(np.frombuffer(self.src.frame, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            img = np.full((540, 960, 3), 90, np.uint8)
        print(f"[BENCH] start: {sizes} × {backends} ({task})")
        for s in sizes:
            for b in backends:
                cfg = {"size": s, "task": task, "backend": b}
                self.bench["current"] = _cfg_label(cfg)
                row = {"model": f"yolo11{s}{'-seg' if task == 'seg' else ''}", "backend": b}
                try:
                    t = time.time()
                    m, kw = build_model(cfg)
                    for _ in range(3):                      # zahrati (u GPU/NPU kompilace)
                        _predict(m, kw, img)
                    row["load_s"] = round(time.time() - t, 1)
                    times = []
                    for _ in range(15):
                        t1 = time.time()
                        _predict(m, kw, img)
                        times.append((time.time() - t1) * 1000)
                    times.sort()
                    row["ms"] = round(sum(times[2:-2]) / len(times[2:-2]), 1)
                    row["fps"] = round(1000 / row["ms"], 1)
                    del m
                except Exception as e:
                    row["error"] = f"{type(e).__name__}: {e}"[:160]
                print(f"[BENCH] {row}")
                self.bench["results"].append(row)
        self.bench["running"], self.bench["current"] = False, ""
        print("[BENCH] hotovo")

    def _watchdog(self):
        while True:
            time.sleep(2)
            if (self.running() and self.clients == 0 and not recorder.active
                    and time.time() - self.last_seen > EYE_IDLE_S):
                self.stop_flag = True

    def add_client(self):
        with self.cond:
            self.clients += 1
            self.last_seen = time.time()

    def remove_client(self):
        with self.cond:
            self.clients = max(0, self.clients - 1)
            self.last_seen = time.time()

    def keepalive(self):
        """AR stranka: YOLO ma bezet i bez video divaku (staci detekce pres WebSocket)."""
        self.last_seen = time.time()
        if not self.running():
            self.start()

    def wait_frame(self, last_seq, timeout=5.0):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq or not self.running(), timeout=timeout)
            return self.frame, self.seq

    def info(self):
        return {"status": self.status, "running": self.running(), "model": _cfg_label(self.cfg),
                "cfg": self.cfg, "pending": bool(self.pending), "devices": _ov_devices_cached(),
                "ms": self.ms, "fps": self.fps, "dets": self.dets,
                "t": round(getattr(self, "dt", 0.0), 3), "seq": self.seq, "bench": self.bench,
                "texture": self.tex, "textures": texture_names(), "zoom": self.src.zoom_info()}

    def set_texture(self, b):
        t = dict(self.tex)
        if "on" in b:
            t["on"] = bool(b["on"])
        if b.get("name") in texture_names():
            t["name"] = b["name"]
        if isinstance(b.get("classes"), list):
            t["classes"] = [str(c) for c in b["classes"]][:20]
        if b.get("toggle_class"):
            c = str(b["toggle_class"])
            t["classes"] = [x for x in t["classes"] if x != c] if c in t["classes"] else t["classes"] + [c]
            t["on"] = True
        self.tex = t
        print(f"[TEX] {t}")
        return t


_ov_cache = {"t": 0.0, "v": []}


def _ov_devices_cached():
    if time.time() - _ov_cache["t"] > 30:
        _ov_cache["v"], _ov_cache["t"] = _ov_devices(), time.time()
    return _ov_cache["v"]


# ── barvy trid stejne jako kresli YOLO (ultralytics), zaloha = jejich paleta ──
_PALETTE = ["042AFF", "0BDBEB", "F3F3F3", "00DFB7", "111F68", "FF6FDD", "FF444F", "CCED00",
            "00F344", "BD00FF", "00B4FF", "DD00BA", "00FFFF", "26C000", "01FFB3", "7D24FF",
            "7B0068", "FF1B6C", "FC6D2F", "A2FF0B"]
_color_cache: dict = {}


def _cls_color(c: int) -> str:
    if c not in _color_cache:
        try:
            from ultralytics.utils.plotting import colors as _uc
            r, g, b = _uc(c, False)
            _color_cache[c] = "#%02x%02x%02x" % (int(r), int(g), int(b))
        except Exception:
            _color_cache[c] = "#" + _PALETTE[c % len(_PALETTE)]
    return _color_cache[c]


# ── nahravani (overlay video + data zvlast) a snimky → captures/ ─────────────
CAPTURE_DIR = Path(__file__).resolve().parent / "captures"
SRC_W, SRC_H = 1920, 1080                      # rozliseni RGB kamery Eye (pro souradnice v px)


def _tstr(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int(ts * 1000) % 1000:03d}"


def _px(box):
    x1, y1, x2, y2 = box
    return round(x1 * SRC_W), round(y1 * SRC_H), round((x2 - x1) * SRC_W), round((y2 - y1) * SRC_H)


class Recorder:
    """Video s YOLO overlayem (MJPEG v .mkv, skutecne casy) a CSV s detekcemi - kazde zvlast."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active, self.video, self.csv, self.dir = False, None, None, None
        self.t0, self.frames, self.rows = 0.0, 0, 0

    def start(self, video=True, data=True):
        with self.lock:
            if self.active:
                return self.info()
            d = CAPTURE_DIR / time.strftime("rec_%Y%m%d_%H%M%S")
            d.mkdir(parents=True, exist_ok=True)
            self.video = self.csv = None
            ff = _find_ffmpeg() if video else None
            if ff:
                self.video = subprocess.Popen(
                    [ff, "-hide_banner", "-loglevel", "error", "-use_wallclock_as_timestamps", "1",
                     "-f", "mjpeg", "-i", "pipe:0", "-c:v", "copy", "-y", str(d / "yolo_overlay.mkv")],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=_NO_WINDOW)
                _bind_to_server(self.video)
            if data:
                self.csv = open(d / "yolo_data.csv", "w", encoding="utf-8", newline="")
                self.csv.write("time,epoch,frame,class,cls,conf,x,y,w,h,x_norm,y_norm,w_norm,h_norm,mask_area\n")
            self.dir, self.active, self.t0, self.frames, self.rows = d, True, time.time(), 0, 0
            print(f"[REC] start {d} (video={self.video is not None}, data={self.csv is not None})")
            return self.info()

    def on_frame(self, jpg, dets, ts, seq):
        if not self.active:
            return
        with self.lock:
            if not self.active:
                return
            if self.video is not None:
                try:
                    self.video.stdin.write(jpg)
                    self.frames += 1
                except Exception as e:
                    print(f"[REC] video chyba: {e}")
                    self.video = None
            if self.csv is not None:
                for det in dets:
                    sb = det.get("sbox", det["box"])        # pri zoomu souradnice v celem snimku
                    x, y, w, h = _px(sb)
                    x1, y1, x2, y2 = sb
                    self.csv.write(f"{_tstr(ts)},{ts:.3f},{seq},{det['name']},{det['cls']},{det['conf']},"
                                   f"{x},{y},{w},{h},{x1},{y1},{round(x2 - x1, 4)},{round(y2 - y1, 4)},{det.get('area', '')}\n")
                    self.rows += 1

    def stop(self):
        with self.lock:
            if not self.active:
                return self.info()
            self.active = False
            if self.video is not None:
                try:
                    self.video.stdin.close()
                    self.video.wait(8)
                except Exception:
                    self.video.kill()
                self.video = None
            if self.csv is not None:
                self.csv.close()
                self.csv = None
            print(f"[REC] stop {self.dir} ({self.frames} snimku, {self.rows} detekci)")
            return self.info()

    def info(self):
        return {"active": self.active, "dir": str(self.dir) if self.dir else None,
                "frames": self.frames, "rows": self.rows,
                "secs": round(time.time() - self.t0, 1) if self.active else 0}


recorder = Recorder()


def save_snapshot(image_b64: str, dets, ts, live: bool):
    """Ulozi presne to, co je videt (overlay), k tomu cisty snimek a detekce jako JSON."""
    import base64
    ts = ts or time.time()
    d = CAPTURE_DIR / (time.strftime("snap_%Y%m%d_%H%M%S", time.localtime(ts)) + f"_{int(ts * 1000) % 1000:03d}")
    d.mkdir(parents=True, exist_ok=True)
    if image_b64:
        data = image_b64.split(",", 1)[-1]
        (d / "yolo_overlay.jpg").write_bytes(base64.b64decode(data))
    if live and getattr(yolo, "raw", None):
        (d / "raw.jpg").write_bytes(yolo.raw)
    if live and eye.raw_mode and eye.raw:            # plne 1080p z kamery (i pri zoomu cely zaber)
        (d / "full_1080p.jpg").write_bytes(eye.raw)
    rows = []
    for det in dets or []:
        x, y, w, h = _px(det.get("sbox", det["box"]))
        rows.append({**det, "x": x, "y": y, "w": w, "h": h})
    (d / "detections.json").write_text(json.dumps({"time": _tstr(ts), "epoch": ts, "source": [SRC_W, SRC_H],
                                                   "zoom": eye.zoom_info(),
                                                   "detections": rows}, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    r = getattr(ocr, "result", None) if "ocr" in globals() else None
    if r and time.time() - r.get("t", 0) < 15:      # cerstve precteny text k snimku
        (d / "ocr.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "ocr.txt").write_text(r.get("text", ""), encoding="utf-8")
    print(f"[SNAP] {d}")
    return str(d)


LAYOUT_FILE = Path(__file__).resolve().parent / "hud_layout.json"

# ── vlastni slovniky: slova, casti vet, cele vety (navrhy nad klavesnici tabletu) ─
PHRASES_FILE = Path(__file__).resolve().parent / "phrases.json"
PHRASE_LISTS = ("words", "phrases", "sentences")
_phr_lock = threading.Lock()
_PHRASES_DEFAULT = {
    "words": ["Claude", "XREAL", "prosím", "děkuji", "nastavení", "klávesnice", "notebook", "rozlišení"],
    "phrases": ["prosím tě", "můžeš mi", "co si o tom myslíš", "pro jistotu", "v podstatě", "jak to funguje"],
    "sentences": [
        {"text": "Ahoj Claude, prosím projdi {} a napiš mi, co je potřeba opravit.", "key": "ahc", "pin": True},
        {"text": "Shrň mi to v několika bodech, stručně a česky.", "key": "shr"},
        {"text": "Vysvětli mi to jednoduše, krok za krokem.", "key": "vys"},
        {"text": "Děkuji, funguje to perfektně.", "key": "dek"},
    ],
}


def _phr_item(x, i):
    if isinstance(x, str):
        x = {"text": x}
    return {"id": str(x.get("id") or f"{int(time.time() * 1000):x}{i:03d}"),
            "text": str(x.get("text", ""))[:20000], "key": str(x.get("key", ""))[:32],
            "pin": bool(x.get("pin", False)), "uses": int(x.get("uses", 0) or 0)}


def _phr_clean(d):
    out = {k: [_phr_item(x, i) for i, x in enumerate(d.get(k, []) or []) if (x if isinstance(x, str) else x.get("text"))]
           for k in PHRASE_LISTS}
    learned = d.get("learned", {}) or {}
    out["learned"] = dict(sorted(((str(w)[:48], int(c)) for w, c in learned.items() if str(w).strip()),
                                 key=lambda kv: -kv[1])[:5000])
    out["version"] = 1
    return out


def phrases_load():
    try:
        return _phr_clean(json.loads(PHRASES_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return phrases_save(_PHRASES_DEFAULT)        # hned ulozit, at maji polozky stala ID
    except Exception as e:
        print(f"[PHR] phrases.json nejde precist ({e}) - pouzivam zalohu/vychozi")
        try:
            return _phr_clean(json.loads(PHRASES_FILE.with_suffix(".bak.json").read_text(encoding="utf-8")))
        except Exception:
            return _phr_clean(_PHRASES_DEFAULT)


def phrases_save(d):
    d = _phr_clean(d)
    with _phr_lock:
        if PHRASES_FILE.exists():
            try:
                PHRASES_FILE.with_suffix(".bak.json").write_text(PHRASES_FILE.read_text(encoding="utf-8"),
                                                                 encoding="utf-8")
            except Exception:
                pass
        tmp = PHRASES_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, PHRASES_FILE)
    return d


def phrases_update(b):
    """Male zmeny bez prepsani celeho souboru: pouziti polozky, naucena slova."""
    d = phrases_load()
    if b.get("use"):
        lst, pid = b["use"].get("list"), str(b["use"].get("id"))
        for x in d.get(lst, []) if lst in PHRASE_LISTS else []:
            if x["id"] == pid:
                x["uses"] += 1
    for w in (b.get("learn") or [])[:200]:
        w = str(w).strip()[:48]
        if len(w) >= 3:
            d["learned"][w] = d["learned"].get(w, 0) + 1
    return phrases_save(d)


yolo = YoloWorker(eye)


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


# ── AR: kalibrace kamera Eye → displej bryli (optical see-through) ─────────────
# Tablet ukazuje znacku ArUco, HUD zamerovac na znamem miste displeje. Uzivatel srovna
# zamerovac se znackou (pohybem hlavy nebo tabletu) a potvrdi. Z bodu (kamera u,v) ↔
# (displej x,y) se spocita homografie H: displej = H · kamera (vse normalizovane 0..1,
# kamera = cely zaber 1920x1080, displej = cele okno AR stranky).
CALIB_FILE = Path(__file__).resolve().parent / "calib.json"
AR_FILE    = Path(__file__).resolve().parent / "ar_settings.json"
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


# ── DATASET: snimky ve full HD z kamery Eye na tlacitko bryli (pro trenink YOLO) ──
DATASET_DIR = Path(os.environ.get("DATASET_DIR", str(Path(__file__).resolve().parent / "dataset" / "raw")))
PORT_CTRL, MSG_KEY = 52999, 0x2E          # ridici kanal bryli: zprava 0x2E = tlacitko (u32 id, u32 stav 1/2)
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
                            dataset.button(kid, st)
                        except Exception as e:
                            print(f"[DS] {type(e).__name__}: {e}")
        except OSError:
            time.sleep(3)


def _calib_action(b: dict):
    a = b.get("action")
    if a == "start":
        return calib.start() or {"ok": True}
    if a == "confirm":
        return calib.confirm()
    if a == "skip":
        return calib.skip()
    if a == "back":
        return calib.back()
    if a in ("cancel", "stop"):
        return calib.cancel()
    if a == "finish":
        return calib.finish()
    if a == "reset":
        calib.H, calib.err, calib.n_used = None, None, 0
        try:
            CALIB_FILE.unlink()
        except Exception:
            pass
        calib._publish()
        return {"ok": True}
    return {"ok": False, "msg": "neznama akce"}


def calib_marker_png(px=900):
    """Znacka ArUco + jasne oznaceny STRED (na ten se miri zamerovacem): cervena tecka
    s bilym lemem a cervene rysky v bilem okraji. Overeno, ze detekci nevadi."""
    import cv2
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    m = cv2.aruco.generateImageMarker(d, 0, px)
    b = px // 9                                         # bily okraj (nutny pro detekci)
    m = cv2.copyMakeBorder(m, b, b, b, b, cv2.BORDER_CONSTANT, value=255)
    m = cv2.cvtColor(m, cv2.COLOR_GRAY2BGR)
    n = m.shape[0]
    c = n // 2
    red = (0, 0, 230)
    t = max(4, px // 90)
    for (x1, y1, x2, y2) in ((c, 0, c, b - 6), (c, n - b + 6, c, n), (0, c, b - 6, c), (n - b + 6, c, n, c)):
        cv2.line(m, (x1, y1), (x2, y2), red, t)           # rysky k stredu v okraji
    r = max(8, px // 40)
    cv2.circle(m, (c, c), r + max(3, r // 2), (255, 255, 255), -1)
    cv2.circle(m, (c, c), r, red, -1)
    ok, enc = cv2.imencode(".png", m)
    return enc.tobytes()


def _ocr_action(b: dict):
    """POST /ocr: {read}, {live}, {lang}, {speak, lines?}, {stop_speak}, {copy, lines?}, {type, lines?, delay?}"""
    out = {}
    if "lang" in b:
        ocr.lang = b["lang"] or None
    if "live" in b:
        ocr.set_live(b["live"])
    if b.get("read"):
        out["result"] = ocr.read()
    text = b.get("text") or ocr.text(b.get("lines"))
    if b.get("speak"):
        lang = (ocr.result or {}).get("lang") or "cs"
        out["voice"] = ocr._engine().speak(text, lang)
        print(f"[OCR] nahlas ({out['voice']}): {text[:60]!r}")
    if b.get("stop_speak"):
        ocr._engine().stop_speaking()
    if b.get("copy"):
        out["copied"] = ocr._engine().copy_to_clipboard(text)
        print(f"[OCR] do schranky {len(text)} znaku")
    if b.get("type"):
        delay = min(max(float(b.get("delay", 0)), 0.0), 10.0)

        def later():
            time.sleep(delay)
            _type_text(text.replace("\n", " "), enter=False)
            print(f"[OCR] napsano {len(text)} znaku")
        threading.Thread(target=later, daemon=True).start()
        out["typing_in"] = delay
    out.update({k: v for k, v in ocr.info().items() if k != "result"})
    return out


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
    static = Path(__file__).parent / "static"
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


# ── main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    os.chdir(YOLO_DIR)                        # vahy a exporty modelu se ukladaji vedle serveru
    local_ip = _get_local_ip()
    _kill_orphan_ffmpeg()                     # uvolnit kameru po minulem serveru
    import atexit
    atexit.register(eye.stop)
    atexit.register(recorder.stop)
    threading.Thread(target=_imu_thread, daemon=True).start()
    threading.Thread(target=_ctrl_thread, daemon=True).start()
    threading.Thread(target=_http_thread, daemon=True).start()
    threading.Thread(target=_stats_thread, daemon=True).start()
    L, R = f"http://localhost:{HTTP_PORT}", f"http://{local_ip}:{HTTP_PORT}"
    print("=" * 66)
    print("xreal-web startuje…")
    print("  V BRYLICH (F11 / klavesa F = cela obrazovka):")
    print(f"    AR overlay ........ {L}/ar.html      C kalibrace, V kontrola, M segmentace")
    print(f"    video + YOLO ...... {L}/yolo.html    okna, zoom, OCR, nahravani")
    print("  NA DISPLEJI NOTEBOOKU / TELEFONU:")
    print(f"    kalibracni znacka . {L}/calib.html   (telefon: {R}/calib.html)")
    print(f"    slovniky a vety ... {L}/phrases.html")
    print("  NA TABLETU:")
    print(f"    ovladac ........... {R}/tablet.html")
    print(f"    kamera Eye ........ {R}/eye.mjpg")
    print("  IP adresy notebooku: " + ", ".join(_all_ips() or ["(zadna)"]))
    print(f"  Porty: HTTP {HTTP_PORT}, WebSocket {WS_PORT} (oba musi projit firewallem)")
    print(f"  Kalibrace AR: {'±' + str(calib.err) + ' px' if calib.H else 'zatim neni'}")
    print(f"  Dataset: {DATASET_DIR}  ({dataset.count} snimku, tlacitko bryli id {dataset.cfg['kid']})")
    print(f"  Log:   {_LOG_PATH}")
    print("=" * 66)
    try:
        asyncio.run(_ws_main())
    except Exception as e:
        print(f"[ERR] WebSocket server spadl: {type(e).__name__}: {e}")
        raise
