"""Ariel Core / ws - WebSocket server.

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

from .config import WS_PORT
from .state import _clients, _lock, state
from .commands import _handle_cmd


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
