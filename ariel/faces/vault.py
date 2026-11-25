"""Sifrovany trezor tvari: jeden soubor data/faces/faces.vault (AES-256-GCM, klic z keystore).

Obsah (po desifrovani, JSON):
  records:   [{id, t, exp, mode, src, q, emb, thumb?, note}]   emb = base64 float32[128]
  reviews:   [{id, members, pairs, level, status, t}]           skupiny podezrelych duplicit ke kontrole
  decisions: {"idA|idB": {"v": "same"|"different", "t": ...}}    rozhodnuti cloveka, aby se par neoznacoval znovu

Kazde ulozeni prepise cely soubor s novym nonce (atomicky pres .tmp + os.replace),
takze smazany zaznam nezustane v souboru. Fotky z kamery se nikdy neukladaji na disk.
"""
import base64
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import keystore
from .audit import audit

MAGIC = b"ARIELFV1"
VAULT_FILE = "faces.vault"


def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def pair_key(a, b):
    return "|".join(sorted((a, b)))


def emb_encode(e: np.ndarray) -> str:
    return base64.b64encode(np.asarray(e, np.float32).tobytes()).decode("ascii")


def emb_decode(s: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(s), np.float32)


class Vault:
    def __init__(self, data_dir: Path, cfg: dict):
        self.dir = Path(data_dir)
        self.cfg = cfg
        self.path = self.dir / VAULT_FILE
        self._key = keystore.load_or_create(self.dir)
        self.data = self._load()
        self.purge_expired()

    # ── soubor ───────────────────────────────────────────────────────────────
    def _load(self):
        empty = {"v": 1, "records": [], "reviews": [], "decisions": {}}
        if not self.path.exists():
            return empty
        blob = self.path.read_bytes()
        if not blob.startswith(MAGIC):
            raise ValueError("faces.vault neni trezor Ariel (spatna hlavicka)")
        nonce, ct = blob[len(MAGIC):len(MAGIC) + 12], blob[len(MAGIC) + 12:]
        try:
            raw = AESGCM(self._key).decrypt(nonce, ct, MAGIC)
        except Exception:
            raise ValueError("trezor nejde desifrovat (jiny uzivatel Windows, jine heslo nebo poskozeny soubor)")
        d = json.loads(raw.decode("utf-8"))
        for k, v in empty.items():
            d.setdefault(k, v)
        return d

    def save(self):
        raw = json.dumps(self.data, separators=(",", ":")).encode("utf-8")
        nonce = secrets.token_bytes(12)
        blob = MAGIC + nonce + AESGCM(self._key).encrypt(nonce, raw, MAGIC)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # ── zaznamy ──────────────────────────────────────────────────────────────
    @property
    def records(self):
        return self.data["records"]

    def get(self, rid):
        return next((r for r in self.records if r["id"] == rid), None)

    def add(self, emb, src="camera", quality=0.0, thumb_jpg=None, note=""):
        mode = self.cfg["mode"]
        hours = self.cfg["retention_hours"][mode]
        t = now()
        rec = {
            "id": secrets.token_hex(4),          # pseudonym, nic nerika o cloveku
            "t": iso(t),
            "exp": iso(t + timedelta(hours=hours)),
            "mode": mode,
            "src": src if mode == "lab" else src.split(":")[0],   # venku se neuklada nazev souboru
            "q": round(float(quality), 3),
            "emb": emb_encode(emb),
            "note": note if mode == "lab" else "",
        }
        if thumb_jpg is not None:
            if mode != "lab":
                raise PermissionError("v rezimu field se fotky ani vyrezy neukladaji")
            rec["thumb"] = base64.b64encode(thumb_jpg).decode("ascii")
        self.records.append(rec)
        audit(self.dir, "enroll", rec["id"], mode=mode, src=rec["src"])
        return rec

    def delete(self, ids, reason="manual"):
        ids = set(ids)
        before = len(self.records)
        self.data["records"] = [r for r in self.records if r["id"] not in ids]
        self.data["decisions"] = {k: v for k, v in self.data["decisions"].items()
                                  if not (set(k.split("|")) & ids)}
        for rv in self.data["reviews"]:
            rv["members"] = [m for m in rv["members"] if m not in ids]
            rv["pairs"] = [p for p in rv["pairs"] if p[0] not in ids and p[1] not in ids]
        self.data["reviews"] = [rv for rv in self.data["reviews"] if len(rv["members"]) > 1]
        n = before - len(self.records)
        if n:
            audit(self.dir, "delete", *sorted(ids), reason=reason, count=n)
        return n

    def purge_expired(self):
        t = now()
        old = [r["id"] for r in self.records if parse(r["exp"]) <= t]
        if old:
            self.delete(old, reason="retention")
            self.save()
        return len(old)

    def drop_thumbs(self):
        """Pri prepnuti do field: smazat vsechny ulozene vyrezy."""
        n = 0
        for r in self.records:
            if r.pop("thumb", None) is not None:
                n += 1
        if n:
            audit(self.dir, "drop_thumbs", count=n)
        return n

    def matrix(self):
        ids = [r["id"] for r in self.records]
        if not ids:
            return ids, np.zeros((0, 128), np.float32)
        E = np.stack([emb_decode(r["emb"]) for r in self.records])
        E /= np.linalg.norm(E, axis=1, keepdims=True) + 1e-9
        return ids, E

    def decide(self, a, b, verdict):
        self.data["decisions"][pair_key(a, b)] = {"v": verdict, "t": iso(now())}
        audit(self.dir, "decide", a, b, verdict=verdict)


def wipe(data_dir: Path):
    """Smaze trezor i klic. Nevratne."""
    data_dir = Path(data_dir)
    keystore.shred(data_dir / VAULT_FILE)
    keystore.shred(data_dir / (Path(VAULT_FILE).stem + ".tmp"))
    keystore.destroy(data_dir)
    audit(data_dir, "wipe")
