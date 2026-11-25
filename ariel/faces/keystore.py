"""Klic k trezoru tvari.

Windows: 32 nahodnych bajtu chranenych DPAPI (CurrentUser) v data/faces/vault.key.
         Desifrovat je umi jen stejny uzivatel Windows na stejnem PC. Zkopirovany soubor jinde nefunguje.
Jinde:   klic z hesla v promenne ARIEL_FACES_PASSPHRASE (scrypt), jinak nechraneny klic s varovanim (jen vyvoj).

Smazani klice = kryptograficke skartovani: trezor uz nikdo neprecte.
"""
import ctypes
import os
import secrets
import sys
from pathlib import Path

KEY_FILE = "vault.key"
SALT_FILE = "vault.salt"


# ── DPAPI pres ctypes (bez dalsich knihoven) ────────────────────────────────
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    src, _keep = _blob(data)
    entropy, _keep2 = _blob(b"ariel-faces-v1")
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(out))  # 0x1 = UI_FORBIDDEN
    if not ok:
        raise OSError(f"DPAPI selhalo (kod {kernel32.GetLastError()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def load_or_create(data_dir: Path) -> bytes:
    data_dir.mkdir(parents=True, exist_ok=True)
    kf = data_dir / KEY_FILE
    if sys.platform == "win32":
        if kf.exists():
            return _dpapi(kf.read_bytes(), protect=False)
        key = secrets.token_bytes(32)
        kf.write_bytes(_dpapi(key, protect=True))
        return key

    pw = os.environ.get("ARIEL_FACES_PASSPHRASE")
    if pw:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        sf = data_dir / SALT_FILE
        if not sf.exists():
            sf.write_bytes(secrets.token_bytes(16))
        return Scrypt(salt=sf.read_bytes(), length=32, n=2 ** 15, r=8, p=1).derive(pw.encode("utf-8"))

    print("[faces] VAROVANI: klic neni chraneny (neni Windows DPAPI ani ARIEL_FACES_PASSPHRASE). Jen pro vyvoj.")
    if kf.exists():
        return kf.read_bytes()
    key = secrets.token_bytes(32)
    kf.write_bytes(key)
    try:
        os.chmod(kf, 0o600)
    except OSError:
        pass
    return key


def shred(path: Path):
    """Prepise soubor nahodnymi daty a smaze ho (na SSD to neni zaruka, proto hlavni ochranou je sifrovani)."""
    if not path.exists():
        return
    try:
        n = path.stat().st_size
        with open(path, "r+b") as f:
            f.write(secrets.token_bytes(max(n, 1)))
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    path.unlink(missing_ok=True)


def destroy(data_dir: Path):
    for name in (KEY_FILE, SALT_FILE):
        shred(data_dir / name)
