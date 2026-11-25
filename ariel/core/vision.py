"""Ariel Core / vision - YOLO, textury, barvy trid, nahravani.

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

from .config import DATA_DIR, MODELS_DIR, STATIC_DIR
from .state import _lock, state
from .camera import EYE_IDLE_S, _NO_WINDOW, _bind_to_server, _find_ffmpeg, eye
from .ocr import ocr


# ── YOLO nad Eye streamem → /eye_yolo.mjpg + /yolo.json + state["yolo"] ──────
# Modely: yolo11{n,s,m}[-seg], beh: PyTorch (CPU) nebo OpenVINO (CPU / GPU Arc / NPU).
YOLO_IMGSZ = int(os.environ.get("YOLO_IMGSZ", "640"))
YOLO_CONF  = float(os.environ.get("YOLO_CONF", "0.35"))
YOLO_DIR   = MODELS_DIR                                 # vahy a OpenVINO exporty
YOLO_CFG_FILE = DATA_DIR / "yolo_config.json"
YOLO_SIZES, YOLO_TASKS = ("n", "s", "m"), ("det", "seg")
YOLO_BACKENDS = ("torch:cpu", "openvino:cpu", "openvino:gpu", "openvino:npu")
DEFAULT_YOLO_CFG = {"size": "n", "task": "det", "backend": "torch:cpu"}


def _ov_devices():
    try:
        import openvino as ov
        return [d.split(".")[0] for d in ov.Core().available_devices]
    except Exception:
        return []


def _clean_cfg(c):
    c = {**DEFAULT_YOLO_CFG, **(c or {})}
    if c["size"] not in YOLO_SIZES:
        c["size"] = "n"
    if c["task"] not in YOLO_TASKS:
        c["task"] = "det"
    if c["backend"] not in YOLO_BACKENDS:
        c["backend"] = "torch:cpu"
    return {k: c[k] for k in DEFAULT_YOLO_CFG}


def _load_cfg():
    try:
        return _clean_cfg(json.loads(YOLO_CFG_FILE.read_text(encoding="utf-8")))
    except Exception:
        return dict(DEFAULT_YOLO_CFG)


def _cfg_label(c):
    return f"yolo11{c['size']}{'-seg' if c['task'] == 'seg' else ''} · {c['backend']}"


def build_model(cfg, log=print):
    """-> (model, kwargs pro predict). OpenVINO export se udela jen poprve."""
    from ultralytics import YOLO
    stem = f"yolo11{cfg['size']}{'-seg' if cfg['task'] == 'seg' else ''}"
    pt = YOLO_DIR / f"{stem}.pt"
    kind, dev = cfg["backend"].split(":")
    if kind == "torch":
        return YOLO(str(pt) if pt.exists() else pt.name), {"device": "cpu"}
    # ultralytics pozna OpenVINO model jen podle koncovky slozky "_openvino_model"
    ovdir = YOLO_DIR / f"{stem}_{YOLO_IMGSZ}_openvino_model"
    old = YOLO_DIR / f"{stem}_ov{YOLO_IMGSZ}"                 # nazev z predchozi verze
    if old.exists() and not ovdir.exists():
        shutil.move(str(old), str(ovdir))
    if not (ovdir / f"{stem}.xml").exists():
        log(f"[YOLO] prevadim {stem} do OpenVINO (jen poprve, chvili to trva) …")
        src = YOLO(str(pt) if pt.exists() else pt.name)
        try:                                    # novejsi ultralytics: quantize=16 (FP16)
            out = Path(src.export(format="openvino", quantize=16, imgsz=YOLO_IMGSZ))
        except Exception:                       # starsi verze: half=True
            out = Path(src.export(format="openvino", half=True, imgsz=YOLO_IMGSZ))
        if out.resolve() != ovdir.resolve():
            if ovdir.exists():
                shutil.rmtree(ovdir)
            shutil.move(str(out), str(ovdir))
    model = YOLO(str(ovdir), task="segment" if cfg["task"] == "seg" else "detect")
    return model, {"device": f"intel:{dev}"}


def _predict(model, kw, img):
    try:
        return model.predict(img, imgsz=YOLO_IMGSZ, conf=YOLO_CONF, verbose=False, **kw)[0]
    except Exception as e:
        if kw.get("device", "cpu") != "cpu" and "device" in str(e).lower():
            print(f"[YOLO] zarizeni {kw['device']} nepodporovane touto verzi ultralytics ({e}) – zkousim AUTO")
            kw.pop("device", None)
            return model.predict(img, imgsz=YOLO_IMGSZ, conf=YOLO_CONF, verbose=False, **kw)[0]
        raise


def _poly(xy, max_pts=96):
    """Zjednodusi obrys masky na <= max_pts bodu tak, aby zustal tvar (Douglas-Peucker),
    normalizovane 0..1, + plocha (podil snimku). Pro AR vyplne pres realny predmet."""
    pts = [(float(x), float(y)) for x, y in xy]
    if len(pts) < 3:
        return [], 0.0
    area = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        area += x1 * y2 - x2 * y1
    try:
        import cv2
        import numpy as np
        c = np.float32(pts).reshape(-1, 1, 2)
        eps, out = 0.0015, c
        while len(out) > max_pts and eps < 0.05:
            out = cv2.approxPolyDP(c, eps, True)
            eps *= 1.5
        if len(out) < 3:
            out = c
        pts = [tuple(p) for p in out.reshape(-1, 2)]
    except Exception:
        step = max(1, len(pts) // max_pts)
        pts = pts[::step]
    return [[round(float(x), 4), round(float(y), 4)] for x, y in pts], round(min(1.0, abs(area) / 2), 4)


# ── textury pres objekty (AR "pretexturovani") ────────────────────────────────
TEXTURE_DIR = STATIC_DIR / "textures"   # vlastni obrazky .jpg/.png
_tex_cache: dict = {}


def _proc_texture(name, n=512):
    """Proceduralni textury bez souboru: drevo, mramor, cihly, sachovnice."""
    import cv2
    import numpy as np
    rng = np.random.default_rng(7)

    def noise(octaves=5):
        acc = np.zeros((n, n), np.float32)
        for o in range(octaves):
            k = 4 * 2 ** o
            acc += cv2.resize(rng.random((k, k)).astype(np.float32), (n, n),
                              interpolation=cv2.INTER_CUBIC) / 2 ** o
        return acc / acc.max()

    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32) / n
    if name == "mramor":
        v = np.sin((xx * 6 + yy * 3 + noise() * 5) * np.pi) * 0.5 + 0.5
        v = v ** 0.35
        img = np.dstack([215 + 35 * v, 210 + 40 * v, 200 + 50 * v])
        img -= (1 - v)[..., None] * np.array([120, 115, 105])
    elif name == "cihly":
        img = np.zeros((n, n, 3), np.float32)
        img[:] = (160, 165, 170)                            # malta (BGR)
        bh, bw = n // 8, n // 4
        for r in range(8):
            off = (bw // 2) * (r % 2)
            for c in range(-1, 5):
                x0, y0 = c * bw + off + 3, r * bh + 3
                shade = 0.8 + 0.4 * rng.random()
                cv2.rectangle(img, (x0, y0), (x0 + bw - 6, y0 + bh - 6),
                              (40 * shade, 60 * shade, 150 * shade), -1)
        img *= (0.85 + 0.3 * noise(4))[..., None]
    elif name == "sachovnice":
        v = ((np.floor(xx * 8) + np.floor(yy * 8)) % 2).astype(np.float32)
        img = np.dstack([v * 230 + 15] * 3)
    else:                                                    # drevo
        r = np.sqrt((xx - 0.5) ** 2 * 0.15 + (yy - 0.2) ** 2) * 28 + noise() * 3
        v = (np.sin(r * np.pi) * 0.5 + 0.5) ** 0.6
        img = np.dstack([40 + 30 * v, 85 + 50 * v, 140 + 70 * v])
        img *= (0.9 + 0.2 * noise(3))[..., None]
    return np.clip(img, 0, 255).astype(np.uint8)


def texture_names():
    names = ["drevo", "mramor", "cihly", "sachovnice"]
    if TEXTURE_DIR.exists():
        names += sorted(p.stem for p in TEXTURE_DIR.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    return names


def get_texture(name):
    if name not in _tex_cache:
        import cv2
        tex = None
        for ext in (".jpg", ".jpeg", ".png"):
            f = TEXTURE_DIR / f"{name}{ext}"
            if f.exists():
                tex = cv2.imread(str(f), cv2.IMREAD_COLOR)
                break
        _tex_cache[name] = tex if tex is not None else _proc_texture(name)
    return _tex_cache[name]


def apply_textures(out, raw, r, dets, tex_cfg):
    """Vyplni masky vybranych objektu texturou; stiny a svetlo bere z realneho obrazu."""
    import cv2
    import numpy as np
    H, W = out.shape[:2]
    tex = get_texture(tex_cfg.get("name", "drevo"))
    wanted = set(tex_cfg.get("classes") or [])
    polys = list(r.masks.xy) if getattr(r, "masks", None) is not None else []
    for i, d in enumerate(dets):
        if wanted and d["name"] not in wanted:
            continue
        x1, y1, x2, y2 = (int(round(v)) for v in (d["box"][0] * W, d["box"][1] * H, d["box"][2] * W, d["box"][3] * H))
        x1, y1, x2, y2 = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
        if x2 - x1 < 4 or y2 - y1 < 4:
            continue
        mask = np.zeros((y2 - y1, x2 - x1), np.uint8)
        if i < len(polys) and len(polys[i]) >= 3:
            pts = (np.asarray(polys[i], np.float32) - [x1, y1]).astype(np.int32)
            cv2.fillPoly(mask, [pts], 255)
        else:
            mask[:] = 255                                   # bez segmentace = cely ramecek
        mask = cv2.GaussianBlur(mask, (5, 5), 0).astype(np.float32) / 255 * 0.92
        t = cv2.resize(tex, (x2 - x1, y2 - y1), interpolation=cv2.INTER_LINEAR).astype(np.float32)
        luma = cv2.cvtColor(raw[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY).astype(np.float32)
        shade = np.clip(0.35 + 0.75 * luma / max(float(luma.mean()), 1.0), 0.2, 1.6)[..., None]
        region = out[y1:y2, x1:x2].astype(np.float32)
        m = mask[..., None]
        out[y1:y2, x1:x2] = np.clip(region * (1 - m) + np.clip(t * shade, 0, 255) * m, 0, 255).astype(np.uint8)
        if i < len(polys) and len(polys[i]) >= 3:
            c = d["color"].lstrip("#")
            bgr = (int(c[4:6], 16), int(c[2:4], 16), int(c[0:2], 16))
            cv2.polylines(out, [np.asarray(polys[i], np.int32)], True, bgr, 2)
    return out


class YoloWorker:
    """Bere vzdy posledni snimek z Eye, pusti YOLO, nakresli vysledky a posle dal.
    Pomale snimky se preskakuji, takze zpozdeni nenarusta. Model jde prepnout za behu."""

    def __init__(self, src):
        self.src = src
        self.cond = threading.Condition()
        self.start_lock = threading.Lock()
        self.frame, self.seq = None, 0
        self.dets, self.ms, self.fps = [], 0.0, 0.0
        self.clients, self.last_seen = 0, 0.0
        self.video_viewers, self.nseq = 0, 0          # divaci videa s ramecky; poradi detekci
        self.status, self.model, self.thread, self.stop_flag = "idle", None, None, False
        self.cfg, self.kw, self.pending = _load_cfg(), {}, None
        self.bench = {"running": False, "results": [], "current": ""}
        self.tex = {"on": False, "name": "drevo", "classes": []}
        threading.Thread(target=self._watchdog, daemon=True).start()

    def running(self):
        return self.thread is not None and self.thread.is_alive()

    def _load(self):
        if self.model is not None:
            return True
        try:
            import cv2      # noqa: F401
            import numpy    # noqa: F401
            import ultralytics  # noqa: F401
        except Exception as e:
            self.status = "chybi ultralytics - spust yolo_install.bat"
            print(f"[YOLO] {self.status} ({e})")
            return False
        self.status = "nacitam model"
        print(f"[YOLO] nacitam {_cfg_label(self.cfg)} …")
        try:
            self.model, self.kw = build_model(self.cfg)
        except Exception as e:
            print(f"[YOLO] {_cfg_label(self.cfg)} nejde nacist: {e} – zkousim PyTorch CPU")
            try:
                self.cfg = {**self.cfg, "backend": "torch:cpu"}
                self.model, self.kw = build_model(self.cfg)
            except Exception as e2:
                self.status = f"model nejde nacist: {e2}"
                print(f"[YOLO] {self.status}")
                return False
        return True

    def set_config(self, cfg):
        cfg = _clean_cfg({**self.cfg, **(cfg or {})})
        YOLO_CFG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        if self.running():
            self.pending = cfg                      # prepne vlakno pred dalsim snimkem
        else:
            self.cfg, self.model = cfg, None        # nacte se pri dalsim startu
        print(f"[YOLO] nove nastaveni: {_cfg_label(cfg)}")
        return cfg

    def _apply_pending(self):
        cfg, self.pending = self.pending, None
        self.status = f"prepinam na {_cfg_label(cfg)}"
        print(f"[YOLO] {self.status} …")
        try:
            t = time.time()
            self.model, self.kw = build_model(cfg)
            self.cfg = cfg
            self.status = "bezi"
            print(f"[YOLO] prepnuto za {time.time() - t:.1f} s")
        except Exception as e:
            self.status = "bezi"
            print(f"[YOLO] prepnuti selhalo ({type(e).__name__}: {e}) – zustava {_cfg_label(self.cfg)}")

    def start(self) -> bool:
        with self.start_lock:
            if self.running():
                return True
            if not self._load():
                return False
            self.src.add_client()                   # drzi Eye stream naziva (uz behem spousteni)
            if not self.src.start():
                self.src.remove_client()
                self.status = "Eye: " + self.src.status
                return False
            self.stop_flag = False
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            self.status = "bezi"
            print(f"[YOLO] bezi ({_cfg_label(self.cfg)}, imgsz {YOLO_IMGSZ}, conf {YOLO_CONF})")
            return True

    def _loop(self):
        import cv2
        import numpy as np
        last = self.src.seq - 1 if self.src.frame is not None else self.src.seq
        n, t0 = 0, time.time()
        try:
            while not self.stop_flag:
                if self.pending:
                    self._apply_pending()
                jpg, seq = self.src.wait_frame(last, timeout=5)
                if jpg is None or seq == last:
                    if not self.src.running() and not getattr(self.src, "restarting", False):
                        if not self.src.start():            # kamera spadla -> zkusit znovu, YOLO bezi dal
                            self.status = "Eye: " + self.src.status
                            time.sleep(2)
                            continue
                        self.status = "bezi"
                    continue
                last = seq
                img, vmap = self.src.view(seq)          # uz dekodovany pohled (setri cas)
                if img is None:
                    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                    vmap = (0.0, 0.0, 1.0, 1.0) if not self.src.raw_mode else self.src.vmap
                if img is None:
                    continue
                zx, zy, zw, zh = vmap
                t = time.time()
                r = _predict(self.model, self.kw, img)
                ms = round((time.time() - t) * 1000, 1)
                ts = time.time()
                dets = []
                polys = list(r.masks.xyn) if getattr(r, "masks", None) is not None else []
                for i, b in enumerate(r.boxes):
                    x1, y1, x2, y2 = (round(float(v), 4) for v in b.xyxyn[0])
                    c = int(b.cls[0])
                    det = {"name": r.names[c], "cls": c, "color": _cls_color(c),
                           "conf": round(float(b.conf[0]), 2), "box": [x1, y1, x2, y2]}
                    if i < len(polys):
                        det["poly"], det["area"] = _poly(polys[i])
                    if zw < 0.999:                      # zoom: box i v souradnicich celeho snimku
                        det["sbox"] = [round(zx + x1 * zw, 4), round(zy + y1 * zh, 4),
                                       round(zx + x2 * zw, 4), round(zy + y2 * zh, 4)]
                    dets.append(det)
                ok, enc = False, None
                if self.video_viewers > 0 or recorder.active:      # AR rezim videa nepotrebuje
                    out = r.plot()
                    if self.tex["on"] and dets:
                        try:
                            out = apply_textures(out, img, r, dets, self.tex)
                        except Exception as e:
                            print(f"[TEX] chyba: {type(e).__name__}: {e}")
                            self.tex["on"] = False
                    ok, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 85])
                n += 1
                if time.time() - t0 >= 2:
                    self.fps, n, t0 = round(n / (time.time() - t0), 1), 0, time.time()
                with self.cond:
                    if ok:
                        self.frame = enc.tobytes()
                        self.seq += 1
                    self.dets, self.ms, self.dt = dets, ms, ts
                    self.raw = jpg
                    self.cond.notify_all()
                if ok:
                    recorder.on_frame(self.frame, dets, ts, self.seq)
                self.nseq += 1
                with _lock:
                    state["yolo"] = {"dets": dets, "ms": ms, "fps": self.fps,
                                     "t": round(ts, 3), "seq": self.nseq, "model": _cfg_label(self.cfg),
                                     "zoom": self.src.zoom_info(),
                                     "head": [state.get("yaw", 0.0), state.get("pitch", 0.0)],
                                     "task": self.cfg.get("task", "det"),
                                     "video": ok}
        except Exception as e:
            self.status = f"chyba: {e}"
            print(f"[YOLO] chyba: {type(e).__name__}: {e}")
        finally:
            self.src.remove_client()
            with self.cond:
                self.cond.notify_all()
            with _lock:
                state.pop("yolo", None)
            if self.status == "bezi":
                self.status = "zastaveno"
            print("[YOLO] zastaveno")

    # -- benchmark: stejne snimky, ruzne modely a zarizeni
    def start_bench(self, sizes=None, backends=None, task=None):
        if self.bench["running"]:
            return False
        devs = _ov_devices()
        sizes = [s for s in (sizes or ["n", "s"]) if s in YOLO_SIZES]
        backends = [b for b in (backends or YOLO_BACKENDS) if b in YOLO_BACKENDS
                    and (b.startswith("torch") or b.split(":")[1].upper() in devs)]
        task = task if task in YOLO_TASKS else self.cfg["task"]
        self.bench = {"running": True, "results": [], "current": "", "task": task}
        threading.Thread(target=self._bench, args=(sizes, backends, task), daemon=True).start()
        return True

    def _bench(self, sizes, backends, task):
        import cv2
        import numpy as np
        img = None
        if self.src.frame is not None:
            img = cv2.imdecode(np.frombuffer(self.src.frame, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            img = np.full((540, 960, 3), 90, np.uint8)
        print(f"[BENCH] start: {sizes} × {backends} ({task})")
        for s in sizes:
            for b in backends:
                cfg = {"size": s, "task": task, "backend": b}
                self.bench["current"] = _cfg_label(cfg)
                row = {"model": f"yolo11{s}{'-seg' if task == 'seg' else ''}", "backend": b}
                try:
                    t = time.time()
                    m, kw = build_model(cfg)
                    for _ in range(3):                      # zahrati (u GPU/NPU kompilace)
                        _predict(m, kw, img)
                    row["load_s"] = round(time.time() - t, 1)
                    times = []
                    for _ in range(15):
                        t1 = time.time()
                        _predict(m, kw, img)
                        times.append((time.time() - t1) * 1000)
                    times.sort()
                    row["ms"] = round(sum(times[2:-2]) / len(times[2:-2]), 1)
                    row["fps"] = round(1000 / row["ms"], 1)
                    del m
                except Exception as e:
                    row["error"] = f"{type(e).__name__}: {e}"[:160]
                print(f"[BENCH] {row}")
                self.bench["results"].append(row)
        self.bench["running"], self.bench["current"] = False, ""
        print("[BENCH] hotovo")

    def _watchdog(self):
        while True:
            time.sleep(2)
            if (self.running() and self.clients == 0 and not recorder.active
                    and time.time() - self.last_seen > EYE_IDLE_S):
                self.stop_flag = True

    def add_client(self):
        with self.cond:
            self.clients += 1
            self.last_seen = time.time()

    def remove_client(self):
        with self.cond:
            self.clients = max(0, self.clients - 1)
            self.last_seen = time.time()

    def keepalive(self):
        """AR stranka: YOLO ma bezet i bez video divaku (staci detekce pres WebSocket)."""
        self.last_seen = time.time()
        if not self.running():
            self.start()

    def wait_frame(self, last_seq, timeout=5.0):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq or not self.running(), timeout=timeout)
            return self.frame, self.seq

    def info(self):
        return {"status": self.status, "running": self.running(), "model": _cfg_label(self.cfg),
                "cfg": self.cfg, "pending": bool(self.pending), "devices": _ov_devices_cached(),
                "ms": self.ms, "fps": self.fps, "dets": self.dets,
                "t": round(getattr(self, "dt", 0.0), 3), "seq": self.seq, "bench": self.bench,
                "texture": self.tex, "textures": texture_names(), "zoom": self.src.zoom_info()}

    def set_texture(self, b):
        t = dict(self.tex)
        if "on" in b:
            t["on"] = bool(b["on"])
        if b.get("name") in texture_names():
            t["name"] = b["name"]
        if isinstance(b.get("classes"), list):
            t["classes"] = [str(c) for c in b["classes"]][:20]
        if b.get("toggle_class"):
            c = str(b["toggle_class"])
            t["classes"] = [x for x in t["classes"] if x != c] if c in t["classes"] else t["classes"] + [c]
            t["on"] = True
        self.tex = t
        print(f"[TEX] {t}")
        return t


_ov_cache = {"t": 0.0, "v": []}


def _ov_devices_cached():
    if time.time() - _ov_cache["t"] > 30:
        _ov_cache["v"], _ov_cache["t"] = _ov_devices(), time.time()
    return _ov_cache["v"]


# ── barvy trid stejne jako kresli YOLO (ultralytics), zaloha = jejich paleta ──
_PALETTE = ["042AFF", "0BDBEB", "F3F3F3", "00DFB7", "111F68", "FF6FDD", "FF444F", "CCED00",
            "00F344", "BD00FF", "00B4FF", "DD00BA", "00FFFF", "26C000", "01FFB3", "7D24FF",
            "7B0068", "FF1B6C", "FC6D2F", "A2FF0B"]
_color_cache: dict = {}


def _cls_color(c: int) -> str:
    if c not in _color_cache:
        try:
            from ultralytics.utils.plotting import colors as _uc
            r, g, b = _uc(c, False)
            _color_cache[c] = "#%02x%02x%02x" % (int(r), int(g), int(b))
        except Exception:
            _color_cache[c] = "#" + _PALETTE[c % len(_PALETTE)]
    return _color_cache[c]


# ── nahravani (overlay video + data zvlast) a snimky → captures/ ─────────────
CAPTURE_DIR = DATA_DIR / "captures"
SRC_W, SRC_H = 1920, 1080                      # rozliseni RGB kamery Eye (pro souradnice v px)


def _tstr(ts):
    return time.strftime("%H:%M:%S", time.localtime(ts)) + f".{int(ts * 1000) % 1000:03d}"


def _px(box):
    x1, y1, x2, y2 = box
    return round(x1 * SRC_W), round(y1 * SRC_H), round((x2 - x1) * SRC_W), round((y2 - y1) * SRC_H)


class Recorder:
    """Video s YOLO overlayem (MJPEG v .mkv, skutecne casy) a CSV s detekcemi - kazde zvlast."""

    def __init__(self):
        self.lock = threading.Lock()
        self.active, self.video, self.csv, self.dir = False, None, None, None
        self.t0, self.frames, self.rows = 0.0, 0, 0

    def start(self, video=True, data=True):
        with self.lock:
            if self.active:
                return self.info()
            d = CAPTURE_DIR / time.strftime("rec_%Y%m%d_%H%M%S")
            d.mkdir(parents=True, exist_ok=True)
            self.video = self.csv = None
            ff = _find_ffmpeg() if video else None
            if ff:
                self.video = subprocess.Popen(
                    [ff, "-hide_banner", "-loglevel", "error", "-use_wallclock_as_timestamps", "1",
                     "-f", "mjpeg", "-i", "pipe:0", "-c:v", "copy", "-y", str(d / "yolo_overlay.mkv")],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=_NO_WINDOW)
                _bind_to_server(self.video)
            if data:
                self.csv = open(d / "yolo_data.csv", "w", encoding="utf-8", newline="")
                self.csv.write("time,epoch,frame,class,cls,conf,x,y,w,h,x_norm,y_norm,w_norm,h_norm,mask_area\n")
            self.dir, self.active, self.t0, self.frames, self.rows = d, True, time.time(), 0, 0
            print(f"[REC] start {d} (video={self.video is not None}, data={self.csv is not None})")
            return self.info()

    def on_frame(self, jpg, dets, ts, seq):
        if not self.active:
            return
        with self.lock:
            if not self.active:
                return
            if self.video is not None:
                try:
                    self.video.stdin.write(jpg)
                    self.frames += 1
                except Exception as e:
                    print(f"[REC] video chyba: {e}")
                    self.video = None
            if self.csv is not None:
                for det in dets:
                    sb = det.get("sbox", det["box"])        # pri zoomu souradnice v celem snimku
                    x, y, w, h = _px(sb)
                    x1, y1, x2, y2 = sb
                    self.csv.write(f"{_tstr(ts)},{ts:.3f},{seq},{det['name']},{det['cls']},{det['conf']},"
                                   f"{x},{y},{w},{h},{x1},{y1},{round(x2 - x1, 4)},{round(y2 - y1, 4)},{det.get('area', '')}\n")
                    self.rows += 1

    def stop(self):
        with self.lock:
            if not self.active:
                return self.info()
            self.active = False
            if self.video is not None:
                try:
                    self.video.stdin.close()
                    self.video.wait(8)
                except Exception:
                    self.video.kill()
                self.video = None
            if self.csv is not None:
                self.csv.close()
                self.csv = None
            print(f"[REC] stop {self.dir} ({self.frames} snimku, {self.rows} detekci)")
            return self.info()

    def info(self):
        return {"active": self.active, "dir": str(self.dir) if self.dir else None,
                "frames": self.frames, "rows": self.rows,
                "secs": round(time.time() - self.t0, 1) if self.active else 0}


recorder = Recorder()


def save_snapshot(image_b64: str, dets, ts, live: bool):
    """Ulozi presne to, co je videt (overlay), k tomu cisty snimek a detekce jako JSON."""
    import base64
    ts = ts or time.time()
    d = CAPTURE_DIR / (time.strftime("snap_%Y%m%d_%H%M%S", time.localtime(ts)) + f"_{int(ts * 1000) % 1000:03d}")
    d.mkdir(parents=True, exist_ok=True)
    if image_b64:
        data = image_b64.split(",", 1)[-1]
        (d / "yolo_overlay.jpg").write_bytes(base64.b64decode(data))
    if live and getattr(yolo, "raw", None):
        (d / "raw.jpg").write_bytes(yolo.raw)
    if live and eye.raw_mode and eye.raw:            # plne 1080p z kamery (i pri zoomu cely zaber)
        (d / "full_1080p.jpg").write_bytes(eye.raw)
    rows = []
    for det in dets or []:
        x, y, w, h = _px(det.get("sbox", det["box"]))
        rows.append({**det, "x": x, "y": y, "w": w, "h": h})
    (d / "detections.json").write_text(json.dumps({"time": _tstr(ts), "epoch": ts, "source": [SRC_W, SRC_H],
                                                   "zoom": eye.zoom_info(),
                                                   "detections": rows}, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    r = getattr(ocr, "result", None) if "ocr" in globals() else None
    if r and time.time() - r.get("t", 0) < 15:      # cerstve precteny text k snimku
        (d / "ocr.json").write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "ocr.txt").write_text(r.get("text", ""), encoding="utf-8")
    print(f"[SNAP] {d}")
    return str(d)


LAYOUT_FILE = DATA_DIR / "hud_layout.json"

yolo = YoloWorker(eye)
