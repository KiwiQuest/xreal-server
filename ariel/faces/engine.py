"""Detekce a embedding tvari: OpenCV YuNet (detektor) + SFace (128-D embedding).

Proc tohle: bezi na CPU, staci opencv-python (uz ho mame kvuli YOLO), modely maji ~37 MB dohromady
a licence Apache-2.0 / MIT, takze se daji pouzit i dal. Pozdeji jde vymenit za ArcFace (InsightFace)
pres OpenVINO/NPU, rozhrani FaceEngine.faces() zustane stejne.

Obraz se zpracuje jen v pameti; na disk nikdy nejde ani snimek, ani vyrez (vyjimka: lab + --thumb, sifrovane v trezoru).
"""
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import FACES_MODELS

YUNET = "face_detection_yunet_2023mar.onnx"
SFACE = "face_recognition_sface_2021dec.onnx"


@dataclass
class Face:
    bbox: tuple            # x, y, w, h v pixelech
    score: float           # jistota detektoru
    sharp: float           # ostrost vyrezu
    emb: np.ndarray        # float32[128], normalizovany
    crop: np.ndarray = field(repr=False, default=None)   # zarovnany vyrez 112x112, jen v pameti

    @property
    def quality(self):
        return self.score * min(1.0, self.sharp / 200.0)


class FaceEngine:
    def __init__(self, cfg):
        det, rec = FACES_MODELS / YUNET, FACES_MODELS / SFACE
        missing = [p.name for p in (det, rec) if not p.exists()]
        if missing:
            raise FileNotFoundError("chybi modely " + ", ".join(missing) +
                                    f" v {FACES_MODELS}  ->  spust tools\\tvare_install.bat")
        if not hasattr(cv2, "FaceDetectorYN"):
            raise RuntimeError(f"OpenCV {cv2.__version__} nema FaceDetectorYN, je potreba opencv-python >= 4.8")
        self.cfg = cfg
        self.det = cv2.FaceDetectorYN.create(str(det), "", (320, 320), float(cfg["det_score"]), 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(str(rec), "")

    def faces(self, img):
        """Vsechny dost velke a ostre tvare v obrazku BGR, serazene od nejvetsi."""
        if img is None or img.size == 0:
            return []
        h, w = img.shape[:2]
        self.det.setInputSize((w, h))
        _, dets = self.det.detect(img)
        out = []
        if dets is None:
            return out
        for d in dets:
            x, y, fw, fh = (int(v) for v in d[:4])
            if min(fw, fh) < self.cfg["min_face_px"]:
                continue
            crop = self.rec.alignCrop(img, d)
            sharp = float(cv2.Laplacian(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())
            if sharp < self.cfg["min_sharpness"]:
                continue
            e = self.rec.feature(crop).flatten().astype(np.float32)
            e /= np.linalg.norm(e) + 1e-9
            out.append(Face((x, y, fw, fh), float(d[-1]), sharp, e, crop))
        out.sort(key=lambda f: -f.bbox[2] * f.bbox[3])
        return out


def mean_embedding(embs):
    E = np.stack(embs)
    m = E.mean(axis=0)
    return (m / (np.linalg.norm(m) + 1e-9)).astype(np.float32)


def thumb_jpg(crop):
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return buf.tobytes() if ok else None
