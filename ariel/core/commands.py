"""Ariel Core / commands - Prikazy z tabletu / HUD (WebSocket i HTTP).

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
from .winput import _insert_text, _key_combo, _key_hold, _key_vk, _last_mouse_t, _mouse_button, _mouse_click, _mouse_move, _mouse_scroll, _release_buttons, _release_held, _type_text
from .head_mouse import _set_head
from .camera import eye
from .ocr import ocr
from .vision import yolo
from .space import CALIB_FILE, calib
from .dataset import dataset


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
