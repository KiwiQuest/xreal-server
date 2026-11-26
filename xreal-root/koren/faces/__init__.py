"""Tváře pro kořen: detekce živě, uložení vybrané tváře, fronta trénování, databáze osob, rozpoznání.

Vychází z `ariel/faces` (YuNet + SFace přes OpenCV, šifrování AES-256-GCM s klíčem v DPAPI),
jen místo jednotlivých záznamů pracuje s **osobami** (jméno, vztah, popis, více vzorků) a běží živě.

  engine.py    YuNet (detekce) + SFace (otisk 128 čísel), jen CPU
  recog.py     čistá logika: galerie osob a porovnání, sledování tváří mezi snímky, hlasování o identitě
  store.py     šifrovaná databáze osob a fronta trénování (data/faces/people.vault)
  camera.py    kamera (XREAL Eye jako webkamera, nebo jiná), automatický výběr
  pipeline.py  vlákno kamery a vlákno trénování, výstup pro okno modulu

Knihovny navíc: opencv-python, numpy, cryptography (`tvare_install.bat`). Když chybí, modul to
klidně řekne a kořen běží dál.
"""
import os

from ..config import APP_DIR, REPO_DIR

DATA_DIR = os.path.join(APP_DIR, "data", "faces")
# modely: nejdřív vlastní složka kořene, pak ty, které už stáhl Ariel (tools/tvare_install.bat)
MODEL_DIRS = (os.path.join(APP_DIR, "models", "faces"), os.path.join(REPO_DIR, "ariel", "models", "faces"))
YUNET = "face_detection_yunet_2023mar.onnx"
SFACE = "face_recognition_sface_2021dec.onnx"


def model_path(name: str):
    for d in MODEL_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.path.getsize(p) > 100_000:
            return p
    return None


def missing() -> list:
    """Co chybí ke spuštění (prázdné = vše je)."""
    out = []
    for mod in ("cv2", "numpy", "cryptography"):
        try:
            __import__(mod)
        except Exception:
            out.append({"cv2": "opencv-python"}.get(mod, mod))
    for m in (YUNET, SFACE):
        if model_path(m) is None:
            out.append(m)
    return out
