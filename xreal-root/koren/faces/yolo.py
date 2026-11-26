"""YOLO v kořeni: převzato z ariel/core/vision.py (build_model, _predict, obrysy masek), jen bez Arielu.

Běží ve vlastním vlákně nad stejnou kamerou jako tváře, takže pomalejší model nezdrží rozpoznávání
obličejů. Modely a OpenVINO exporty bere ze složky Arielu (`ariel/models`, už stažené a převedené),
nastavení `ariel/data/yolo_config.json` (velikost n/s/m, det / seg, torch:cpu / openvino:cpu|gpu|npu).

YOLO (COCO, 80 tříd) najde lidi a věci kolem. Kdo ten člověk je, řeší rozpoznání tváře (SFace):
YOLO nemá třídu „obličej“ a identitu neumí, proto se tvář přiřadí k postavě a jméno se kreslí nad ní.
"""
import json
import os
import shutil
import threading
import time
from pathlib import Path

from ..config import REPO_DIR

MODELS = Path(REPO_DIR) / "ariel" / "models"
CFG_FILE = Path(REPO_DIR) / "ariel" / "data" / "yolo_config.json"
IMGSZ = 640
CONF = 0.35
SIZES, TASKS = ("n", "s", "m"), ("det", "seg")
BACKENDS = ("torch:cpu", "openvino:cpu", "openvino:gpu", "openvino:npu")
DEFAULT = {"size": "n", "task": "det", "backend": "openvino:cpu"}
PERSON = 0

CS = ("osoba", "kolo", "auto", "motorka", "letadlo", "autobus", "vlak", "náklaďák", "loď", "semafor",
      "hydrant", "stopka", "parkovací automat", "lavička", "pták", "kočka", "pes", "kůň", "ovce", "kráva",
      "slon", "medvěd", "zebra", "žirafa", "batoh", "deštník", "kabelka", "kravata", "kufr", "frisbee",
      "lyže", "snowboard", "míč", "drak", "baseballová pálka", "baseballová rukavice", "skateboard",
      "surf", "tenisová raketa", "láhev", "sklenice na víno", "hrnek", "vidlička", "nůž", "lžíce", "miska",
      "banán", "jablko", "sendvič", "pomeranč", "brokolice", "mrkev", "párek v rohlíku", "pizza", "kobliha",
      "dort", "židle", "gauč", "květina", "postel", "jídelní stůl", "záchod", "televize", "notebook", "myš",
      "ovladač", "klávesnice", "mobil", "mikrovlnka", "trouba", "toustovač", "dřez", "lednice", "kniha",
      "hodiny", "váza", "nůžky", "plyšák", "fén", "kartáček na zuby")
_PALETTE = ["042AFF", "0BDBEB", "F3F3F3", "00DFB7", "111F68", "FF6FDD", "FF444F", "CCED00",
            "00F344", "BD00FF", "00B4FF", "DD00BA", "00FFFF", "26C000", "01FFB3", "7D24FF",
            "7B0068", "FF1B6C", "FC6D2F", "A2FF0B"]


def color(c: int) -> str:
    return "#" + _PALETTE[c % len(_PALETTE)]


def clean(c) -> dict:
    c = {**DEFAULT, **(c or {})}
    if c["size"] not in SIZES:
        c["size"] = "n"
    if c["task"] not in TASKS:
        c["task"] = "det"
    if c["backend"] not in BACKENDS:
        c["backend"] = "openvino:cpu"
    return {k: c[k] for k in DEFAULT}


def load_cfg() -> dict:
    try:
        return clean(json.loads(CFG_FILE.read_text(encoding="utf-8")))
    except Exception:
        return dict(DEFAULT)


def save_cfg(c: dict):
    try:
        CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CFG_FILE.write_text(json.dumps(c, indent=2), encoding="utf-8")
    except OSError:
        pass


def label(c: dict) -> str:
    return f"yolo11{c['size']}{'-seg' if c['task'] == 'seg' else ''} · {c['backend']}"


def available() -> str:
    """'' = jde spustit, jinak co chybí."""
    try:
        import ultralytics  # noqa: F401
    except Exception:
        return "chybí ultralytics (ariel\\tools\\yolo_install.bat)"
    return ""


def build_model(cfg, log=print):
    """-> (model, kwargs pro predict). OpenVINO export se udělá jen poprvé (stejně jako Ariel)."""
    from ultralytics import YOLO
    stem = f"yolo11{cfg['size']}{'-seg' if cfg['task'] == 'seg' else ''}"
    pt = MODELS / f"{stem}.pt"
    kind, dev = cfg["backend"].split(":")
    if kind == "torch":
        return YOLO(str(pt) if pt.exists() else pt.name), {"device": "cpu"}
    ovdir = MODELS / f"{stem}_{IMGSZ}_openvino_model"
    if not (ovdir / f"{stem}.xml").exists():
        log(f"[YOLO] převádím {stem} do OpenVINO (jen poprvé, chvíli to trvá) …")
        src = YOLO(str(pt) if pt.exists() else pt.name)
        try:
            out = Path(src.export(format="openvino", quantize=16, imgsz=IMGSZ))
        except Exception:
            out = Path(src.export(format="openvino", half=True, imgsz=IMGSZ))
        if out.resolve() != ovdir.resolve():
            if ovdir.exists():
                shutil.rmtree(ovdir)
            shutil.move(str(out), str(ovdir))
    model = YOLO(str(ovdir), task="segment" if cfg["task"] == "seg" else "detect")
    return model, {"device": f"intel:{dev}"}


def poly(xy, max_pts=64):
    """Obrys masky zjednodušený na <= max_pts bodů, normalizovaný 0..1 (z ariel/core/vision._poly)."""
    import cv2
    import numpy as np
    if len(xy) < 3:
        return []
    c = np.float32(xy).reshape(-1, 1, 2)
    eps, out = 0.0015, c
    while len(out) > max_pts and eps < 0.05:
        out = cv2.approxPolyDP(c, eps, True)
        eps *= 1.5
    if len(out) < 3:
        out = c
    return [[round(float(x), 4), round(float(y), 4)] for x, y in out.reshape(-1, 2)]


class YoloWorker:
    """Bere vždy poslední snímek kamery, pustí YOLO a drží poslední výsledek (pomalé snímky přeskakuje)."""

    def __init__(self, get_frame, log=print):
        self.get_frame, self.log = get_frame, log
        self.cfg = load_cfg()
        self.on = False
        self.model, self.kw = None, {}
        self.pending = None
        self.dets, self.ms, self.fps, self.t = [], 0.0, 0.0, 0.0
        self.status = "vypnuto"
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._loop, name="faces-yolo", daemon=True)

    def start(self):
        self._th.start()

    def stop(self):
        self._stop.set()
        self._th.join(timeout=3)

    def set(self, **kw):
        cfg = clean({**self.cfg, **kw})
        save_cfg(cfg)
        self.pending = cfg

    def _load(self, cfg):
        self.status = f"načítám {label(cfg)}"
        try:
            self.model, self.kw = build_model(cfg, self.log)
        except Exception as e:
            self.log(f"[YOLO] {label(cfg)} nejde načíst: {e} – zkouším PyTorch CPU")
            cfg = {**cfg, "backend": "torch:cpu"}
            self.model, self.kw = build_model(cfg, self.log)
        self.cfg = cfg
        self.log(f"[YOLO] běží {label(cfg)}")

    def _predict(self, img):
        try:
            return self.model.predict(img, imgsz=IMGSZ, conf=CONF, verbose=False, **self.kw)[0]
        except Exception as e:
            if self.kw.get("device", "cpu") != "cpu" and "device" in str(e).lower():
                self.kw.pop("device", None)
                return self.model.predict(img, imgsz=IMGSZ, conf=CONF, verbose=False, **self.kw)[0]
            raise

    def _loop(self):
        last, n, t0 = -1, 0, time.monotonic()
        while not self._stop.is_set():
            try:
                if not self.on:
                    if self.dets:
                        self.dets = []
                    self.status = "vypnuto"
                    time.sleep(0.2)
                    continue
                if self.pending is not None:
                    cfg, self.pending = self.pending, None
                    self.model = None
                    self._load(cfg)
                if self.model is None:
                    miss = available()
                    if miss:
                        self.status = miss
                        time.sleep(2.0)
                        continue
                    self._load(self.cfg)
                f, _, seq = self.get_frame()
                if f is None or seq == last:
                    time.sleep(0.01)
                    if f is None:
                        self.status = "čekám na kameru"
                        self.dets = []
                    continue
                last = seq
                t = time.monotonic()
                r = self._predict(f)
                self.ms = round((time.monotonic() - t) * 1000, 1)
                polys = list(r.masks.xyn) if getattr(r, "masks", None) is not None else []
                dets = []
                for i, b in enumerate(r.boxes):
                    x1, y1, x2, y2 = (round(float(v), 4) for v in b.xyxyn[0])
                    c = int(b.cls[0])
                    d = {"cls": c, "name": CS[c] if c < len(CS) else r.names[c], "conf": round(float(b.conf[0]), 2),
                         "box": [x1, y1, x2, y2], "color": color(c)}
                    if i < len(polys):
                        d["poly"] = poly(polys[i])
                    dets.append(d)
                self.dets, self.t = dets, time.monotonic()
                self.status = f"běží {label(self.cfg)}"
                n += 1
                if time.monotonic() - t0 >= 2.0:
                    self.fps, n, t0 = round(n / (time.monotonic() - t0), 1), 0, time.monotonic()
            except Exception as e:
                self.status = f"chyba: {type(e).__name__}: {e}"
                self.log(f"[YOLO] {self.status}")
                self.model = None
                time.sleep(3.0)

    def fresh(self, max_age: float = 1.0) -> list:
        return self.dets if self.on and time.monotonic() - self.t < max_age else []


def attach(faces: list, dets: list) -> dict:
    """Přiřadí tváře k postavám YOLO: {index tváře: index detekce osoby}. Tvář (střed) musí ležet
    v horní polovině postavy; když se hodí víc postav, vyhraje ta nejmenší (nejtěsnější)."""
    out, used = {}, set()
    persons = [(i, d["box"]) for i, d in enumerate(dets) if d["cls"] == PERSON]
    for fi, f in enumerate(faces):
        cx, cy = f["x"] + f["w"] / 2, f["y"] + f["h"] / 2
        best = None
        for di, (x1, y1, x2, y2) in persons:
            if di in used or not (x1 <= cx <= x2 and y1 <= cy <= y1 + (y2 - y1) * 0.6):
                continue
            area = (x2 - x1) * (y2 - y1)
            if best is None or area < best[1]:
                best = (di, area)
        if best is not None:
            out[fi] = best[0]
            used.add(best[0])
    return out
