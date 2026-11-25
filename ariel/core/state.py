"""Ariel Core / state - Sdileny stav jadra.

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
