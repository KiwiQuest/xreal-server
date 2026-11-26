"""Šifrovaná databáze osob a fronta trénování: data/faces/people.vault (mimo git).

Šifrování stejně jako ariel/faces/vault.py: AES-256-GCM, náhodný klíč chráněný Windows DPAPI
(přečte ho jen stejný uživatel Windows na stejném PC), každé uložení přepíše celý soubor atomicky.

Obsah po dešifrování (JSON):
  persons: {pid: {name, relation, note, created, updated, first_seen, last_seen, seen,
                  samples: [b64 float32[128]], thumb: b64 jpg}}
  queue:   [{id, t, status, kind, pid?, samples, thumb, msg, cand?, cand_sim?}]
           status: queued (čeká) -> training -> ask (čeká na odpověď ano/ne) -> done / failed
           kind:   new (nová osoba) | add (doplnit snímky známé osobě)
  seq:     pořadové číslo pro jména „Osoba N“

Vše je jen pro domácí použití s lidmi, kteří souhlasí (viz README, oddíl Tváře).
"""
import base64
import ctypes
import json
import os
import secrets
import sys
import threading
import time
from typing import Dict, List, Optional

import numpy as np

MAGIC = b"KORENFV1"
VAULT = "people.vault"
KEY = "people.key"
MAX_SAMPLES = 40          # vzorků na osobu (nejstarší automatické se zahazují)
RELATIONS = ("rodina", "přítel", "kolega", "soused", "návštěva", "jiné")


def emb_enc(e) -> str:
    return base64.b64encode(np.asarray(e, np.float32).tobytes()).decode("ascii")


def emb_dec(s: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(s), np.float32).copy()


def now() -> float:
    return round(time.time(), 1)


# ── klíč ─────────────────────────────────────────────────────────────────────
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    b1 = ctypes.create_string_buffer(data, len(data))
    b2 = ctypes.create_string_buffer(b"koren-faces-v1", 14)
    src = _Blob(len(data), ctypes.cast(b1, ctypes.POINTER(ctypes.c_char)))
    ent = _Blob(14, ctypes.cast(b2, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1, ctypes.byref(out)):
        raise OSError(f"DPAPI selhalo (kód {kernel32.GetLastError()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def load_key(data_dir: str, plain: bool = False) -> bytes:
    """plain=True jen pro testy (klíč nechráněný)."""
    os.makedirs(data_dir, exist_ok=True)
    kf = os.path.join(data_dir, KEY)
    protect = sys.platform == "win32" and not plain
    if os.path.exists(kf):
        with open(kf, "rb") as f:
            raw = f.read()
        return _dpapi(raw, False) if protect else raw
    key = secrets.token_bytes(32)
    with open(kf, "wb") as f:
        f.write(_dpapi(key, True) if protect else key)
    return key


def shred(path: str):
    if not os.path.exists(path):
        return
    try:
        n = os.path.getsize(path)
        with open(path, "r+b") as f:
            f.write(secrets.token_bytes(max(n, 1)))
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    try:
        os.remove(path)
    except OSError:
        pass


# ── databáze ────────────────────────────────────────────────────────────────
class Store:
    def __init__(self, data_dir: str, plain_key: bool = False, log=print):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.dir, self.log = data_dir, log
        self.path = os.path.join(data_dir, VAULT)
        self._plain = plain_key
        self._aes = AESGCM(load_key(data_dir, plain_key))
        self.lock = threading.RLock()
        self.data = self._load()
        self.dirty = False
        self.version = 0                    # roste při každé změně osob (přestavba galerie)
        for j in self.queue:                # trénink přerušený pádem nebo restartem se zopakuje
            if j["status"] == "training":
                j["status"] = "queued"

    def _load(self) -> dict:
        empty = {"v": 1, "persons": {}, "queue": [], "seq": 0}
        if not os.path.exists(self.path):
            return empty
        with open(self.path, "rb") as f:
            blob = f.read()
        if not blob.startswith(MAGIC):
            raise ValueError("people.vault není databáze kořene")
        n = len(MAGIC)
        try:
            raw = self._aes.decrypt(blob[n:n + 12], blob[n + 12:], MAGIC)
        except Exception:
            raise ValueError("databázi tváří nejde dešifrovat (jiný uživatel Windows nebo poškozený soubor)")
        d = json.loads(raw.decode("utf-8"))
        for k, v in empty.items():
            d.setdefault(k, v)
        return d

    def save(self):
        with self.lock:
            raw = json.dumps(self.data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            self.dirty = False
        nonce = secrets.token_bytes(12)
        blob = MAGIC + nonce + self._aes.encrypt(nonce, raw, MAGIC)
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def touch(self, persons_changed: bool = False):
        with self.lock:
            self.dirty = True
            if persons_changed:
                self.version += 1

    # ── osoby ──
    @property
    def persons(self) -> Dict[str, dict]:
        return self.data["persons"]

    @property
    def queue(self) -> List[dict]:
        return self.data["queue"]

    def person(self, pid: str) -> Optional[dict]:
        return self.persons.get(pid)

    def gallery_input(self) -> Dict[str, List[np.ndarray]]:
        with self.lock:
            return {pid: [emb_dec(s) for s in p["samples"]] for pid, p in self.persons.items() if p["samples"]}

    def new_person(self, samples, thumb: Optional[str], seen_t: float) -> str:
        with self.lock:
            self.data["seq"] += 1
            pid = secrets.token_hex(4)
            t = now()
            self.persons[pid] = {
                "name": f"Osoba {self.data['seq']}", "relation": "", "note": "",
                "created": t, "updated": t, "first_seen": seen_t, "last_seen": seen_t, "seen": 1,
                "samples": [emb_enc(e) for e in samples][:MAX_SAMPLES], "auto": 0,
                "thumb": thumb or "",
            }
            self.touch(True)
            return pid

    def add_samples(self, pid: str, samples, thumb: Optional[str] = None, auto: bool = False) -> int:
        with self.lock:
            p = self.person(pid)
            if p is None:
                return 0
            p["samples"].extend(emb_enc(e) for e in samples)
            if auto:
                p["auto"] = p.get("auto", 0) + len(samples)
            if len(p["samples"]) > MAX_SAMPLES:
                # ponechat první (ručně uložené) vzorky, zahodit nejstarší z dalších
                keep = MAX_SAMPLES // 4
                p["samples"] = p["samples"][:keep] + p["samples"][-(MAX_SAMPLES - keep):]
            if thumb and not p.get("thumb"):
                p["thumb"] = thumb
            p["updated"] = now()
            self.touch(True)
            return len(samples)

    def edit(self, pid: str, **fields) -> bool:
        with self.lock:
            p = self.person(pid)
            if p is None:
                return False
            for k in ("name", "relation", "note"):
                if k in fields and fields[k] is not None:
                    v = str(fields[k]).strip()[:200 if k == "note" else 60]
                    if k == "name" and not v:
                        continue
                    p[k] = v
            p["updated"] = now()
            self.touch()
            return True

    def seen(self, pid: str, t: float, new_visit: bool):
        with self.lock:
            p = self.person(pid)
            if p is None:
                return
            p["last_seen"] = round(t, 1)
            if new_visit:
                p["seen"] = p.get("seen", 0) + 1
            self.dirty = True

    def delete(self, pid: str) -> bool:
        with self.lock:
            if self.persons.pop(pid, None) is None:
                return False
            self.data["queue"] = [j for j in self.queue if j.get("pid") != pid and j.get("cand") != pid]
            self.touch(True)
            return True

    # ── fronta trénování ──
    def enqueue(self, samples, thumb: Optional[str], kind: str = "new", pid: Optional[str] = None,
                seen_t: Optional[float] = None) -> dict:
        with self.lock:
            j = {"id": secrets.token_hex(3), "t": now(), "status": "queued", "kind": kind, "pid": pid,
                 "samples": [emb_enc(e) for e in samples], "thumb": thumb or "", "msg": "čeká ve frontě",
                 "seen_t": seen_t or now()}
            self.queue.append(j)
            self.touch()
            return j

    def job(self, jid: str) -> Optional[dict]:
        return next((j for j in self.queue if j["id"] == jid), None)

    def next_job(self) -> Optional[dict]:
        with self.lock:
            return next((j for j in self.queue if j["status"] == "queued"), None)

    def finish(self, j: dict, status: str, msg: str, pid: Optional[str] = None):
        with self.lock:
            j["status"], j["msg"] = status, msg
            if pid:
                j["pid"] = pid
            if status in ("done", "failed"):
                j["samples"], j["thumb"] = [], ""     # hotová úloha už otisky ani výřez nedrží
            j["done_t"] = now()
            # ponechat jen pár posledních hotových, ať je vidět historie
            done = [x for x in self.queue if x["status"] in ("done", "failed")]
            for old in done[:-6]:
                self.queue.remove(old)
            self.touch()

    def wipe(self):
        with self.lock:
            self.data = {"v": 1, "persons": {}, "queue": [], "seq": 0}
            self.touch(True)
        shred(self.path)
        shred(os.path.join(self.dir, KEY))
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self._aes = AESGCM(load_key(self.dir, self._plain))   # nový klíč, starý už nikdo nepřečte
