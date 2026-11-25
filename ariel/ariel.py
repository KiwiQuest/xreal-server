#!/usr/bin/env python3
"""Ariel - jadro pro XREAL One Pro.
Spusteni:  python ariel.py   (z teto slozky)
Prohlizec: http://localhost:8080  |  WebSocket: ws://localhost:8765  |  Tablet: http://[IP]:8080/tablet.html
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

from core import log  # noqa: F401  (musi byt prvni: presmeruje vystup do logs/ariel.log)
from core.config import HTTP_PORT, WS_PORT
from core.log import _LOG_PATH
from core.glasses import _ctrl_thread, _imu_thread
from core.camera import _kill_orphan_ffmpeg, eye
from core.vision import YOLO_DIR, recorder
from core.space import calib
from core.dataset import DATASET_DIR, dataset
from core.commands import _stats_thread
from core.ws import _ws_main
from core.http_api import _all_ips, _get_local_ip, _http_thread
from core.glasses import BUTTON_LISTENERS


# ── main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    os.chdir(YOLO_DIR)                        # vahy a exporty modelu se ukladaji vedle serveru
    local_ip = _get_local_ip()
    _kill_orphan_ffmpeg()                     # uvolnit kameru po minulem serveru
    import atexit
    atexit.register(eye.stop)
    atexit.register(recorder.stop)
    BUTTON_LISTENERS.append(dataset.button)   # tlacitko bryli -> snimek do datasetu
    threading.Thread(target=_imu_thread, daemon=True).start()
    threading.Thread(target=eye.start, name="eye-warmup", daemon=True).start()   # kamera se rozbiha hned, ne az s prvnim divakem
    threading.Thread(target=_ctrl_thread, daemon=True).start()
    threading.Thread(target=_http_thread, daemon=True).start()
    threading.Thread(target=_stats_thread, daemon=True).start()
    L, R = f"http://localhost:{HTTP_PORT}", f"http://{local_ip}:{HTTP_PORT}"
    print("=" * 66)
    print("Ariel Core startuje…")
    print("  V BRYLICH (F11 / klavesa F = cela obrazovka):")
    print(f"    AR overlay ........ {L}/ar.html      C kalibrace, V kontrola, M segmentace")
    print(f"    video + YOLO ...... {L}/yolo.html    okna, zoom, OCR, nahravani")
    print(f"    jen prave oko ..... {L}/mono.html    bryle prepni na Full SBS (dvakrat X > Spatial Screen >")
    print( "                        3D Mode > Full SBS), Windows displej 3840x1080; ?test=1 test zare")
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
