"""Ariel Core / log - Log do logs/ariel.log + konzole (importovat jako prvni).

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

from .config import LOGS_DIR


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


_LOG_PATH = LOGS_DIR / "ariel.log"
try:
    if _LOG_PATH.exists() and _LOG_PATH.stat().st_size > 2_000_000:
        _LOG_PATH.unlink()
    sys.stdout = _Tee(sys.stdout, _LOG_PATH)
    sys.stderr = _Tee(sys.stderr, _LOG_PATH)
except Exception:
    pass
threading.excepthook = lambda a: print(f"[ERR] vlakno {a.thread.name}: {a.exc_type.__name__}: {a.exc_value}")
print("=" * 50 + "\nstart " + time.ctime())
