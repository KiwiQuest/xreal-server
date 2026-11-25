"""Ariel Core / phrases - Slovniky slov a vet pro psani.

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

from .config import DATA_DIR


# ── vlastni slovniky: slova, casti vet, cele vety (navrhy nad klavesnici tabletu) ─
PHRASES_FILE = DATA_DIR / "phrases.json"
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
