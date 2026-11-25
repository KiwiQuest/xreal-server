"""Ariel Core / config - Konstanty, porty a slozky Ariel.

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



ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LOGS_DIR = ROOT / "logs"
MODELS_DIR = ROOT / "models"
STATIC_DIR = ROOT / "static"
for _d in (DATA_DIR, LOGS_DIR, MODELS_DIR, STATIC_DIR):
    _d.mkdir(exist_ok=True)

# ── IMU konstanty (XREAL One Pro) ─────────────────────────────────────────────
IP       = "169.254.2.1"
PORT_IMU = 52998
MAGIC    = bytes.fromhex("283600000080")
PKT      = 134
TYPE_IMU = 11

WS_PORT   = 8765
HTTP_PORT = 8080


PORT_CTRL, MSG_KEY = 52999, 0x2E          # ridici kanal bryli: zprava 0x2E = tlacitko (u32 id, u32 stav 1/2)
