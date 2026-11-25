"""Ariel Core / winput - Windows vstup: mys a klavesnice (SendInput).

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
