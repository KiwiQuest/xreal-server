"""YOLO (ultralytics) - volitelne. Spusteni kontroly prostredi:  python -m xreal.yolo"""
import sys
from typing import List, Tuple

Box = Tuple[float, float, float, float, str, float]    # x1, y1, x2, y2, nazev, jistota


def availability() -> Tuple[bool, str]:
    missing = []
    for mod in ("numpy", "cv2", "torch", "ultralytics"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    if missing:
        return False, ("Chybí: " + ", ".join(missing) +
                       "  →  spusť setup_yolo.bat (nebo: pip install -r requirements-yolo.txt)")
    return True, "YOLO prostředí je připravené"


class Detector:
    def __init__(self, weights: str = "yolov8n.pt", conf: float = 0.35):
        self.weights, self.conf = weights, conf
        self.model = None

    def load(self):
        if self.model is None:
            from ultralytics import YOLO          # import az tady, ať aplikace běží i bez nich
            self.model = YOLO(self.weights)       # pri prvnim pouziti se stahne (vyzaduje internet)
        return self

    def detect(self, img) -> List[Box]:
        """img: numpy pole HxW (sedoted) nebo HxWx3 (BGR)."""
        import numpy as np
        self.load()
        if img.ndim == 2:
            img = np.stack([img] * 3, axis=-1)
        res = self.model.predict(img, conf=self.conf, verbose=False)[0]
        names = res.names
        out: List[Box] = []
        for b in res.boxes:
            x1, y1, x2, y2 = (float(v) for v in b.xyxy[0])
            out.append((x1, y1, x2, y2, names[int(b.cls[0])], float(b.conf[0])))
        return out


def _segment(self, img):
    """Obrysy objektu (polygony): [(body [(x, y), ...], nazev, jistota)]"""
    import numpy as np
    self.load()
    if img.ndim == 2:
        img = np.stack([img] * 3, axis=-1)
    res = self.model.predict(img, conf=self.conf, verbose=False)[0]
    out = []
    if res.masks is None:
        return out
    for poly, b in zip(res.masks.xy, res.boxes):
        out.append(([(float(x), float(y)) for x, y in poly], res.names[int(b.cls[0])],
                    float(b.conf[0])))
    return out


Detector.segment = _segment


def main():
    ok, msg = availability()
    print(msg)
    if not ok:
        sys.exit(1)
    import numpy as np
    import torch
    import ultralytics
    print("torch", torch.__version__, "| CUDA:", torch.cuda.is_available(),
          "| ultralytics", ultralytics.__version__)
    det = Detector()
    print("Stahuji / nacitam", det.weights, "...")
    det.load()
    print("Zkusebni inference:", det.detect(np.zeros((378, 512), dtype=np.uint8)))
    print("Hotovo - YOLO funguje.")


if __name__ == "__main__":
    main()
