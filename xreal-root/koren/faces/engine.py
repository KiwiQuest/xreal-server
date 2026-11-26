"""Detekce a otisk tváře: OpenCV YuNet + SFace (stejné modely jako ariel/faces/engine.py).

Detekce běží na zmenšeném snímku (rychlé), otisk se počítá jen pro tváře, které ho potřebují
(nová stopa, obnova rozpoznání, ukládání), z plného rozlišení. Snímek zůstává jen v paměti.
"""
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import SFACE, YUNET, model_path


@dataclass
class Det:
    bbox: tuple                       # x, y, w, h v pixelech plného snímku
    score: float
    row: np.ndarray = field(repr=False, default=None)   # řádek YuNet (body obličeje) v plném měřítku


class FaceEngine:
    def __init__(self, det_score: float = 0.8, det_width: int = 640):
        det, rec = model_path(YUNET), model_path(SFACE)
        if not det or not rec:
            raise FileNotFoundError("chybí modely tváří, spusť xreal-root\\tvare_install.bat")
        if not hasattr(cv2, "FaceDetectorYN"):
            raise RuntimeError(f"OpenCV {cv2.__version__} nemá FaceDetectorYN (potřeba >= 4.8)")
        self.det_width = det_width
        self.det = cv2.FaceDetectorYN.create(det, "", (320, 320), float(det_score), 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(rec, "")
        self._size = None

    def detect(self, img) -> list:
        h, w = img.shape[:2]
        s = min(1.0, self.det_width / float(w))
        small = cv2.resize(img, (int(w * s), int(h * s))) if s < 1.0 else img
        sz = (small.shape[1], small.shape[0])
        if sz != self._size:
            self.det.setInputSize(sz)
            self._size = sz
        _, dets = self.det.detect(small)
        out = []
        if dets is None:
            return out
        for d in dets:
            row = d.copy()
            row[:14] /= s                     # bbox + 5 bodů zpět do plného měřítka
            x, y, fw, fh = (int(v) for v in row[:4])
            out.append(Det((x, y, fw, fh), float(row[-1]), row))
        out.sort(key=lambda f: -f.bbox[2] * f.bbox[3])
        return out

    def embed(self, img, det: Det):
        """(otisk float32[128] normalizovaný, ostrost, zarovnaný výřez 112x112 BGR)."""
        crop = self.rec.alignCrop(img, det.row)
        sharp = float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
        e = self.rec.feature(crop).flatten().astype(np.float32)
        e /= np.linalg.norm(e) + 1e-9
        return e, sharp, crop


def jpg(img, q: int = 80):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return buf.tobytes() if ok else None
