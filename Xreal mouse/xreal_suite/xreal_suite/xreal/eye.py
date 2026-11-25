"""Kamera XREAL Eye: zivy obraz + volitelne YOLO.

Co je zname (komunitni projekty):
  * TCP 52997 na bryli = video SLAM kamer Eye: 512x378, 4 bity na pixel, stereo dvojice,
    vlastni format. Funguje jen s Eye pripojenym a zapnutym Spatial Anchor
    (u nekterych verzi je potreba Nebula beta).
  * RGB kamera (2048x1512 HEVC) jde pres USB UVC az po odeslani HID prikazu - to zatim
    neumime. Pokud ji Windows vidi jako normalni kameru, zkus tlacitko "UVC kamera".

Format ramcu na 52997 NENI zdokumentovany, proto ho zkousim zjistit z dat:
detect_framing() hleda v prvnich bajtech hlavicku s delkou ramce (jako u IMU/ridiciho
kanalu). Kdyz se nepodari, viewer to rekne a ulozi surova data (data/eye_dump.bin),
ze kterych se format da dopocitat.
"""
import os
import socket
import struct
import threading
import time
from typing import List, Optional, Tuple

from . import protocol
from .ar import (ArState, items_from_boxes, items_from_cv, items_from_polys, lookup)
from .config import APP_DIR

W, H = 512, 378
IMG_BYTES = W * H // 2                 # 96 768 B jeden obraz (4 bity na pixel)
PAIR_BYTES = 2 * IMG_BYTES
DUMP_FILE = os.path.join(APP_DIR, "data", "eye_dump.bin")

_HI = bytes((i >> 4) * 17 for i in range(256))
_LO = bytes((i & 15) * 17 for i in range(256))


def unpack_nibbles(data: bytes, hi_first: bool = True) -> bytes:
    """4 bity na pixel -> 8 bitu (0..255)."""
    a, b = data.translate(_HI), data.translate(_LO)
    if not hi_first:
        a, b = b, a
    out = bytearray(len(data) * 2)
    out[0::2] = a
    out[1::2] = b
    return bytes(out)


def pgm(pixels: bytes, w: int, h: int) -> bytes:
    return b"P5 %d %d 255\n" % (w, h) + pixels


# ------------------------------ detekce ramcu ------------------------------
class Framing:
    def __init__(self, hdr_len, off, endian, inclusive, magic):
        self.hdr_len, self.off, self.endian = hdr_len, off, endian
        self.inclusive, self.magic = inclusive, magic

    def __repr__(self):
        return (f"Framing(hlavicka {self.hdr_len} B, delka na offsetu {self.off} "
                f"{'big' if self.endian == 'big' else 'little'}-endian, "
                f"{'vcetne hlavicky' if self.inclusive else 'jen telo'}, magic {self.magic.hex(' ')})")

    def total(self, buf, p) -> Optional[int]:
        if p + self.hdr_len > len(buf):
            return None
        n = int.from_bytes(buf[p + self.off:p + self.off + 4], self.endian)
        t = n if self.inclusive else n + self.hdr_len
        return t if self.hdr_len <= t <= (1 << 23) else -1


def detect_framing(buf: bytes, min_frames: int = 4) -> Optional[Framing]:
    """Zkusi najit 'hlavicka + delka' tak, aby po sobe jdouci ramce navazovaly."""
    if len(buf) < 4096:
        return None
    for hdr_len in (6, 8, 4, 12, 16, 20, 24, 32, 10, 14):
        for off in range(0, hdr_len - 3):
            for endian in ("big", "little"):
                for inclusive in (False, True):
                    fr = Framing(hdr_len, off, endian, inclusive, b"")
                    p, n, ok = 0, 0, True
                    magic = bytes(buf[:min(2, off) or 2]) if off else bytes(buf[:2])
                    while n < min_frames:
                        t = fr.total(buf, p)
                        if t is None:       # nedostatek dat
                            break
                        if t < 0 or p + t + hdr_len > len(buf):
                            ok = False
                            break
                        if buf[p:p + len(magic)] != magic or buf[p + t:p + t + len(magic)] != magic:
                            ok = False
                            break
                        p += t
                        n += 1
                    if ok and n >= min_frames and p > 0:
                        fr.magic = magic
                        return fr
    return None


def split_frames(buf: bytearray, fr: Framing) -> List[bytes]:
    out = []
    while True:
        i = buf.find(fr.magic) if fr.magic else 0
        if i == -1:
            del buf[:-len(fr.magic)]
            return out
        if i > 0:
            del buf[:i]
        t = fr.total(buf, 0)
        if t is None:
            return out
        if t < 0:
            del buf[:1]
            continue
        if len(buf) < t:
            return out
        out.append(bytes(buf[:t]))
        del buf[:t]


def decode_payload(frame: bytes, fr: Framing, hi_first: bool = True):
    """Z ramce vyrobi (levy, pravy) obraz 512x378 v 8 bitech; pravy muze byt None."""
    body = frame[fr.hdr_len:]
    if len(body) >= PAIR_BYTES:
        body = body[-PAIR_BYTES:]
        return (unpack_nibbles(body[:IMG_BYTES], hi_first),
                unpack_nibbles(body[IMG_BYTES:], hi_first))
    if len(body) >= IMG_BYTES:
        return unpack_nibbles(body[-IMG_BYTES:], hi_first), None
    return None, None


# ------------------------------ varianty dekodovani ------------------------
# Presny format pixelu neni zdokumentovany. Zkusime vic hypotez a vybereme tu, ktera da
# nejhladsi obraz (spravne dekodovana fotka ma sousedni pixely podobne, spatne "sum").
IMG8 = W * H


def _id(b):
    return b


VARIANTS = {   # nazev: (bajtu na obraz, dekoder) - poradi = priorita pri shode
    "8bit (1 bajt = 1 pixel)": (IMG8, _id),
    "4bit horní půlbajt první": (IMG_BYTES, lambda b: unpack_nibbles(b, True)),
    "4bit dolní půlbajt první": (IMG_BYTES, lambda b: unpack_nibbles(b, False)),
    "8bit, jen dolní 4 bity": (IMG8, lambda b: b.translate(_LO)),
    "8bit, jen horní 4 bity": (IMG8, lambda b: b.translate(_HI)),
}
ALIGNS = ("konec", "začátek")      # kde v tele ramce lezi obrazova data


def decode_variant(body: bytes, name: str, align: str = "konec"):
    """body = telo ramce bez hlavicky -> (levy, pravy) 8bit obraz W*H; pravy muze byt None."""
    size, dec = VARIANTS[name]
    n = len(body)
    if n >= 2 * size:
        seg = body[n - 2 * size:] if align == "konec" else body[:2 * size]
        return dec(seg[:size]), dec(seg[size:])
    if n >= size:
        seg = body[n - size:] if align == "konec" else body[:size]
        return dec(seg), None
    return None, None


def roughness(img: bytes, w: int = W, h: int = H) -> float:
    """Prumerny rozdil sousednich pixelu na 4bitove urovni (mensi = hladsi = spravnejsi).

    Merime po zahrubeni na 4 bity, aby 8bit a "jen horni 4 bity" byly ve shode a
    sum v dolnich bitech nerozhodoval; spatne dekodovane (zdvojene/posunute) obrazy
    maji i tak vyrazne vyssi hodnotu.
    """
    if not img or len(img) < w * h:
        return 1e9
    tot = cnt = 0
    for y in range(2, h - 2, 6):
        row = img[y * w:(y + 1) * w]
        for x in range(0, w - 1, 2):
            tot += abs((row[x] >> 4) - (row[x + 1] >> 4))
            cnt += 1
    return tot / max(cnt, 1)


def choose_variant(bodies: List[bytes]):
    """Vybere (nazev, zarovnani) s nejnizsi pruměrnou drsnosti. Vraci (nazev, align, skore)."""
    best = None
    for name in VARIANTS:
        for align in ALIGNS:
            scores = []
            for body in bodies:
                left, _ = decode_variant(body, name, align)
                scores.append(roughness(left) if left else 1e9)
            sc = sum(scores) / len(scores)
            if best is None or sc < best[2] - 0.05:      # tolerance: pri shode vyhrava drivejsi
                best = (name, align, sc)
    return best


# ------------------------------ cteni streamu ------------------------------
class VideoStream(threading.Thread):
    def __init__(self, host=protocol.HOST, port=protocol.PORT_VIDEO, save_dump=True):
        super().__init__(daemon=True)
        self.host, self.port, self.save_dump = host, port, save_dump
        self.running = True
        self.status = "Připojuji k portu 52997..."
        self.framing: Optional[Framing] = None
        self.left: Optional[bytes] = None
        self.right: Optional[bytes] = None
        self.raw_tail = b""
        self.frames = 0
        self.fps = 0.0
        self.hi_first = True
        self.variant = "auto"            # "auto" nebo nazev z VARIANTS
        self.align = "konec"
        self.chosen = None               # (nazev, zarovnani, skore) pri auto
        self.sample_bodies: List[bytes] = []
        self.frame_len = 0
        self.head_hex = ""
        self.rx_time = 0.0               # cas prijmu posledniho snimku
        self.lock = threading.Lock()

    def run(self):
        while self.running:
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5.0)
            except OSError as e:
                self.status = (f"Port 52997 nedostupný: {e}. Je připojené Eye a zapnutý "
                               f"Spatial Anchor?")
                time.sleep(2.0)
                continue
            self.status = "Připojeno, čekám na data..."
            self._read(sock)
            sock.close()
            time.sleep(1.0)

    def _read(self, sock):
        buf = bytearray()
        probe = bytearray()
        dumped = 0
        t0, n0 = time.time(), 0
        sock.settimeout(1.0)
        while self.running:
            try:
                data = sock.recv(262144)
            except socket.timeout:
                self.status = "Připojeno, ale žádná data (zapni Spatial Anchor / Eye)"
                continue
            except OSError as e:
                self.status = f"Chyba: {e}"
                return
            if not data:
                self.status = "Spojení ukončeno brýlemi"
                return
            self.raw_tail = (self.raw_tail + data)[-(W * 512 // 2):]
            if self.save_dump and dumped < 8_000_000:
                try:
                    os.makedirs(os.path.dirname(DUMP_FILE), exist_ok=True)
                    with open(DUMP_FILE, "ab" if dumped else "wb") as f:
                        f.write(data)
                    dumped += len(data)
                except OSError:
                    self.save_dump = False
            buf.extend(data)
            if self.framing is None:
                probe.extend(data)
                self.framing = detect_framing(bytes(probe[:2_000_000]))
                if self.framing is None:
                    self.status = (f"Přijato {len(probe)//1024} kB, formát ramců nerozpoznán "
                                   f"(data uložena do data/eye_dump.bin)")
                    if len(probe) > 3_000_000:
                        del probe[:-1_000_000]
                    del buf[:-1_000_000]
                    continue
                buf = bytearray(probe)
                self.status = f"Formát rozpoznán: {self.framing}"
            for fr in split_frames(buf, self.framing):
                body = fr[self.framing.hdr_len:]
                self.frame_len = len(fr)
                self.head_hex = fr[:self.framing.hdr_len + 10].hex(" ")
                name, align = self.variant, self.align
                if name == "auto":
                    if self.chosen is None:
                        self.sample_bodies.append(body)
                        if len(self.sample_bodies) < 4:
                            continue
                        self.chosen = choose_variant(self.sample_bodies)
                        self.sample_bodies = []
                    name, align = self.chosen[0], self.chosen[1]
                left, right = decode_variant(body, name, align)
                if left is None:
                    continue
                with self.lock:
                    self.left, self.right = left, right
                    self.rx_time = time.time()
                self.frames += 1
            now = time.time()
            if now - t0 >= 1.0:
                self.fps = (self.frames - n0) / (now - t0)
                t0, n0 = now, self.frames


# ------------------------------ barevna kamera (UVC) -----------------------
def pnp_camera_names() -> List[str]:
    """Nazvy kamer podle Windows (pro poznani, ktera je od bryli)."""
    import subprocess
    import sys
    if sys.platform != "win32":
        return []
    ps = ("Get-PnpDevice -PresentOnly -Class Camera,Image | "
          "Select-Object -ExpandProperty FriendlyName")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


class UvcStream(threading.Thread):
    """Cte barevnou kameru pres OpenCV (DirectShow) v samostatnem vlakne."""

    def __init__(self, idx: int, width: int = 1280, height: int = 720):
        super().__init__(daemon=True)
        self.idx, self.width, self.height = idx, width, height
        self.running = True
        self.frame = None            # posledni BGR snimek (numpy)
        self.rx_time = 0.0
        self.fps = 0.0
        self.status = "Otevírám kameru..."
        self.lock = threading.Lock()

    def run(self):
        try:
            import cv2
        except Exception:
            self.status = "OpenCV není nainstalované: pip install opencv-python"
            return
        cap = cv2.VideoCapture(self.idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            self.status = f"Kameru {self.idx} se nepodařilo otevřít (používá ji jiná aplikace?)"
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        n, t0 = 0, time.time()
        while self.running:
            ok, fr = cap.read()
            if not ok:
                self.status = "Kamera nevrací obraz"
                time.sleep(0.1)
                continue
            with self.lock:
                self.frame = fr
                self.rx_time = time.time()
            n += 1
            if time.time() - t0 >= 1.0:
                self.fps, n, t0 = n / (time.time() - t0), 0, time.time()
            self.status = f"UVC kamera {self.idx}: {fr.shape[1]}×{fr.shape[0]}"
        cap.release()


# ------------------------------ okno --------------------------------------
class EyeWindow:
    def __init__(self, root, cfg, rt=None, engine=None):
        import tkinter as tk
        from tkinter import ttk
        from . import vision
        self.tk, self.cfg, self.vision = tk, cfg, vision
        self.root, self.rt, self.engine = root, rt, engine
        self.ar_state = ArState()
        self.ar_overlay = None
        self.source = "slam"
        self._last_rx = 0.0
        win = self.win = tk.Toplevel(root)
        win.title("XREAL Eye – kamera, hrany a YOLO")
        top = ttk.Frame(win, padding=(8, 8, 8, 0))
        top.pack(fill="x")
        self.status_var = tk.StringVar(value="Zastaveno")
        ttk.Label(top, textvariable=self.status_var, wraplength=1000, justify="left").pack(anchor="w")

        bar = ttk.Frame(win, padding=(8, 4))
        bar.pack(fill="x")
        ttk.Button(bar, text="Start stream (52997)", command=self.start_stream).pack(side="left")
        ttk.Button(bar, text="Najít barevné kamery", command=self.scan_uvc).pack(side="left", padx=6)
        self.cam_var = tk.StringVar(value="")
        self.cam_combo = ttk.Combobox(bar, textvariable=self.cam_var, state="readonly", width=22,
                                      values=[])
        self.cam_combo.pack(side="left")
        self.cam_combo.bind("<<ComboboxSelected>>", self._start_uvc_sel)
        ttk.Button(bar, text="Stop", command=self.stop).pack(side="left")
        ttk.Button(bar, text="Benchmark", command=self.run_benchmark).pack(side="left", padx=6)
        ttk.Label(bar, text="Dekódování:").pack(side="left", padx=(10, 2))
        self.var_var = tk.StringVar(value="auto")
        self.var_combo = ttk.Combobox(
            bar, textvariable=self.var_var, state="readonly", width=34,
            values=["auto"] + [f"{n} | {a}" for n in VARIANTS for a in ALIGNS])
        self.var_combo.pack(side="left")
        self.var_combo.bind("<<ComboboxSelected>>", self._variant)

        bar2 = ttk.Frame(win, padding=(8, 0))
        bar2.pack(fill="x")
        ttk.Label(bar2, text="Zobrazení:").pack(side="left")
        self.mode_keys = list(vision.MODES)
        self.mode_var = tk.StringVar(value=vision.MODES["contours"])
        ttk.Combobox(bar2, textvariable=self.mode_var, state="readonly", width=40,
                     values=[vision.MODES[k] for k in self.mode_keys]).pack(side="left", padx=4)
        ttk.Label(bar2, text="Kontrast:").pack(side="left", padx=(8, 2))
        self.contrast_var = tk.StringVar(value="roztažení")
        ttk.Combobox(bar2, textvariable=self.contrast_var, state="readonly", width=10,
                     values=list(vision.CONTRASTS)).pack(side="left")
        self.half_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar2, text="½ rozlišení (rychlejší)", variable=self.half_var
                        ).pack(side="left", padx=8)
        self.yolo_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar2, text="YOLO boxy", variable=self.yolo_var,
                        command=lambda: self._yolo_toggle(False)).pack(side="left")
        self.seg_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar2, text="YOLO obrysy (seg)", variable=self.seg_var,
                        command=lambda: self._yolo_toggle(True)).pack(side="left", padx=6)

        bar3 = ttk.Frame(win, padding=(8, 2))
        bar3.pack(fill="x")
        self.ar_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar3, text="AR overlay v brýlích (obrazovka 2)", variable=self.ar_var,
                        command=self._ar_toggle).pack(side="left")
        self.anch_var = tk.BooleanVar(value=cfg.ar_anchored)
        ttk.Checkbutton(bar3, text="kotvená obrazovka", variable=self.anch_var,
                        command=lambda: setattr(cfg, "ar_anchored", self.anch_var.get())
                        ).pack(side="left", padx=8)
        self.calib_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar3, text="Kalibrace AR (F2)", variable=self.calib_var,
                        command=self._calib_toggle).pack(side="left")
        ttk.Label(bar3, text="  zorný úhel kamery").pack(side="left")
        self.fov_var = tk.DoubleVar(value=cfg.ar_cal[self.source]["fov"])
        ttk.Scale(bar3, from_=20, to=160, variable=self.fov_var, length=120,
                  command=lambda _=None: self.cfg.ar_cal[self.source].__setitem__(
                      "fov", float(self.fov_var.get()))).pack(side="left")
        self.lens_var = tk.BooleanVar(value=cfg.ar_cal[self.source].get("lens") == "fisheye")
        ttk.Checkbutton(bar3, text="fisheye", variable=self.lens_var,
                        command=lambda: self.cfg.ar_cal[self.source].__setitem__(
                            "lens", "fisheye" if self.lens_var.get() else "rectilinear")
                        ).pack(side="left", padx=(6, 0))
        self.edge_var = tk.BooleanVar(value=cfg.ar_edge_markers)
        ttk.Checkbutton(bar3, text="šipky mimo obraz", variable=self.edge_var,
                        command=lambda: setattr(cfg, "ar_edge_markers", self.edge_var.get())
                        ).pack(side="left", padx=(6, 0))
        ttk.Label(bar3, text="  latence").pack(side="left")
        self.lat_var = tk.DoubleVar(value=cfg.ar_latency_ms)
        ttk.Scale(bar3, from_=0, to=300, variable=self.lat_var, length=100,
                  command=lambda _=None: setattr(cfg, "ar_latency_ms", float(self.lat_var.get()))
                  ).pack(side="left")
        self.ar_info = tk.StringVar(value="")
        ttk.Label(bar3, textvariable=self.ar_info, foreground="#3a6fb0").pack(side="left", padx=8)

        self.timing_var = tk.StringVar(value="")
        ttk.Label(win, textvariable=self.timing_var, foreground="#3a6fb0",
                  padding=(8, 4)).pack(anchor="w")
        self.cv = tk.Canvas(win, width=2 * W + 12, height=H + 8, bg="#111", highlightthickness=0)
        self.cv.pack(padx=8, pady=(0, 6))
        self.bench = tk.Text(win, height=12, width=100, font=("Consolas", 10))
        self.bench.pack(padx=8, pady=(0, 8))

        self.stream: Optional[VideoStream] = None
        self.uvc = None
        self.detector = None
        self.seg_detector = None
        self.boxes: list = []
        self.polys: list = []
        self._yolo_busy = False
        self._yolo_ms = 0.0
        self._img_refs = []
        self.last_gray = None
        self.alive = True
        win.protocol("WM_DELETE_WINDOW", self.close)
        self.tick()

    # --- zdroje ---
    def start_stream(self):
        self.stop()
        self.stream = VideoStream()
        self.stream.start()

    def scan_uvc(self):
        """Najde vsechny kamery, ktere Windows nabizi (RGB Eye by se mela objevit jako dalsi)."""
        self.stop()
        if not self.vision.available():
            self.status_var.set("Pro barevnou kameru nainstaluj: pip install numpy opencv-python")
            return
        self.status_var.set("Hledám kamery… (chvíli to trvá)")

        def job():
            found = []
            try:
                import cv2
                for idx in range(8):
                    cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
                    if cap.isOpened():
                        ok, fr = cap.read()
                        if ok:
                            found.append((idx, fr.shape[1], fr.shape[0]))
                    cap.release()
            except Exception:
                pass
            names = pnp_camera_names()
            self.win.after(0, lambda: self._cams_found(found, names))

        threading.Thread(target=job, daemon=True).start()

    def _cams_found(self, found, names):
        self.cam_combo["values"] = [f"kamera {i}: {w}×{h}" for i, w, h in found]
        self.bench.delete("1.0", "end")
        self.bench.insert("end", "Kamery podle Windows: " + (", ".join(names) or "(nelze zjistit)") + "\n")
        if found:
            self.bench.insert("end", "Vyber ze seznamu kameru od brýlí (zkus postupně).\n")
            self.status_var.set(f"Nalezeno kamer: {len(found)} – vyber ji v rozbalovátku")
        else:
            self.status_var.set("Žádná kamera nenalezena")
            self.bench.insert("end", (
                "Nic nenalezeno. Podle vývojářské FAQ XREAL se kamera brýlí na Windows objeví mezi "
                "kamerami počítače (zkus appku Kamera). Vypni v Nebule 'Power Saving Mode' "
                "a zavři programy, které kameru používají.\n"))

    def _start_uvc_sel(self, _=None):
        txt = self.cam_var.get()
        if not txt.startswith("kamera "):
            return
        idx = int(txt.split()[1].rstrip(":"))
        self.stop()
        self.uvc = UvcStream(idx)
        self.uvc.start()

    def stop(self):
        if self.stream:
            self.stream.running = False
            self.stream = None
        if self.uvc:
            self.uvc.running = False
            self.uvc = None

    def close(self):
        self.alive = False
        self.stop()
        if self.ar_overlay:
            self.ar_overlay.close()
        self.win.destroy()

    # --- AR overlay ---
    def _ar_toggle(self):
        if self.ar_var.get():
            if not self.vision.available():
                self.status_var.set("AR potřebuje numpy a opencv: pip install numpy opencv-python")
                self.ar_var.set(False)
                return
            from .ar_overlay import ArOverlay
            self.ar_overlay = ArOverlay(self.root, self.cfg, self.rt, self.engine, self.ar_state,
                                        lambda: self.source, self._calib_changed)
        elif self.ar_overlay:
            self.ar_overlay.close()
            self.ar_overlay = None
            self.ar_state.clear()

    def _calib_toggle(self):
        if self.calib_var.get() and not self.ar_overlay:
            self.ar_var.set(True)
            self._ar_toggle()
        if self.ar_overlay:
            self.ar_overlay.set_calibration(self.calib_var.get())

    def _calib_changed(self, on):          # F2 v overlayi
        self.calib_var.set(on)

    def _head_at(self, t):
        if self.rt is None:
            return 0.0, 0.0
        return lookup(self.rt.view_hist, t)

    def _publish_cv(self, objs, size, t_cap):
        if not self.ar_overlay:
            return
        h, v = self._head_at(t_cap)
        self.ar_state.set_layer("obrysy", items_from_cv(
            objs, size[0], size[1], self.cfg.ar_cal[self.source], h, v, cv2=self.vision.cv2))

    def _variant(self, _=None):
        v = self.var_var.get()
        if not self.stream:
            return
        if v == "auto":
            self.stream.variant, self.stream.chosen = "auto", None
        else:
            name, align = v.split(" | ")
            self.stream.variant, self.stream.align = name, align

    # --- YOLO ---
    def _yolo_toggle(self, seg: bool):
        var = self.seg_var if seg else self.yolo_var
        if not var.get():
            (self.polys if seg else self.boxes).clear()
            return
        from .yolo import availability, Detector
        ok, msg = availability()
        if not ok:
            self.status_var.set(msg)
            var.set(False)
            return
        if seg:
            self.seg_detector = Detector(self.cfg.yolo_seg_weights, self.cfg.yolo_conf)
        else:
            self.detector = Detector(self.cfg.yolo_weights, self.cfg.yolo_conf)

    def _run_yolo(self, arr, t_cap=0.0, source="slam"):
        """arr: HxW sedotonovy (stretch) nebo HxWx3 BGR (barevna kamera); souradnice 1:1 s platnem."""
        try:
            if arr.ndim == 2:
                arr = self.vision.stretch(arr)           # tmavy obraz: YOLO vidi lip po roztazeni
            t = time.perf_counter()
            if self.yolo_var.get() and self.detector:
                self.boxes = self.detector.detect(arr)
            if self.seg_var.get() and self.seg_detector:
                self.polys = self.seg_detector.segment(arr)
            self._yolo_ms = (time.perf_counter() - t) * 1000
            if self.ar_overlay:                       # svetove uhly z natoceni v case SNIMKU
                hh, vv = self._head_at(t_cap)
                cal = self.cfg.ar_cal[source]
                sz = (arr.shape[1], arr.shape[0])
                if self.yolo_var.get():
                    self.ar_state.set_layer("yolo", items_from_boxes(
                        self.boxes, sz[0], sz[1], cal, hh, vv))
                if self.seg_var.get():
                    self.ar_state.set_layer("yolo-seg", items_from_polys(
                        self.polys, sz[0], sz[1], cal, hh, vv))
        except Exception as e:
            self.status_var.set(f"YOLO chyba: {e}")
            self.yolo_var.set(False)
            self.seg_var.set(False)
        finally:
            self._yolo_busy = False

    # --- benchmark ---
    def run_benchmark(self):
        if not self.vision.available():
            self.bench.insert("end", "Chybí numpy/opencv: pip install numpy opencv-python\n")
            return
        if self.last_gray is None:
            self.bench.insert("end", "Nejdřív spusť stream (potřebuju reálný snímek).\n")
            return
        gray = self.last_gray.copy()
        self.bench.delete("1.0", "end")
        self.bench.insert("end", "Měřím na posledním snímku (100 opakování)…\n")

        def job():
            lines = ["Metoda                                      ms/snímek     max fps",
                     "-" * 62]
            for name, ms in self.vision.benchmark(gray, 100):
                lines.append(f"{name:42s} {ms:8.2f} {1000 / ms:11.0f}")
            from .yolo import availability, Detector
            if availability()[0]:
                for label, w in (("YOLO boxy", self.cfg.yolo_weights),
                                 ("YOLO obrysy (seg)", self.cfg.yolo_seg_weights)):
                    try:
                        det = Detector(w, self.cfg.yolo_conf).load()
                        img = self.vision.stretch(gray)
                        (det.segment if "seg" in label else det.detect)(img)     # zahrati
                        t = time.perf_counter()
                        for _ in range(10):
                            (det.segment if "seg" in label else det.detect)(img)
                        ms = (time.perf_counter() - t) * 100
                        lines.append(f"{label:42s} {ms:8.2f} {1000 / ms:11.1f}")
                    except Exception as e:
                        lines.append(f"{label:42s} chyba: {e}")
            else:
                lines.append("YOLO: nenainstalováno (setup_yolo.bat)")
            text = "\n".join(lines) + "\n"
            self.win.after(0, lambda: (self.bench.delete("1.0", "end"), self.bench.insert("end", text)))

        threading.Thread(target=job, daemon=True).start()

    # --- vykreslovani ---
    def _photo(self, arr):
        """numpy HxW (sedotonovy) nebo HxWx3 (RGB) -> PhotoImage."""
        h, w = arr.shape[:2]
        head = (b"P5 %d %d 255\n" if arr.ndim == 2 else b"P6 %d %d 255\n") % (w, h)
        ph = self.tk.PhotoImage(data=head + arr.tobytes())
        if w * 2 <= W + 8 and h * 2 <= H + 8:
            ph = ph.zoom(2)                  # polovicni rozliseni -> zobrazit na puvodni velikost
        return ph

    def tick(self):
        if not self.alive:
            return
        tk, cv, vision = self.tk, self.cv, self.vision
        left = right = None
        color = None
        t_rx = 0.0
        if self.stream:
            s = self.stream
            self.source = "slam"
            with s.lock:
                left, right, t_rx = s.left, s.right, s.rx_time
            var = (f"{s.chosen[0]} / {s.chosen[1]} (drsnost {s.chosen[2]:.1f})"
                   if s.variant == "auto" and s.chosen else s.variant)
            self.status_var.set(f"{s.status}   |   {s.fps:.0f} fps   |   rámec {s.frame_len} B   |   "
                                f"dekódování: {var}\nhlavička: {s.head_hex}")
            if left is None and s.raw_tail and s.framing is None:
                left = unpack_nibbles(s.raw_tail[: (W // 2) * H], True)
                left = left[: W * (len(left) // W)]
        elif self.uvc:
            u = self.uvc
            self.source = "rgb"
            with u.lock:
                color, t_rx = u.frame, u.rx_time
            self.status_var.set(f"{u.status}   |   {u.fps:.0f} fps")
        cv.delete("all")
        self._img_refs = []
        t_cap = (t_rx or time.time()) - self.cfg.ar_latency_ms / 1000.0
        new_frame = t_rx != self._last_rx
        self._last_rx = t_rx
        cal = self.cfg.ar_cal[self.source]
        if abs(self.fov_var.get() - cal["fov"]) > 0.5:
            self.fov_var.set(cal["fov"])
        fisheye = cal.get("lens") == "fisheye"          # zmena zdroje nebo klavesa L v kalibraci
        if self.lens_var.get() != fisheye:
            self.lens_var.set(fisheye)
        if self.ar_overlay:
            self.ar_info.set(f"AR: {self.source}, {'fisheye' if fisheye else 'běžný'} "
                             f"{cal['fov']:.0f}°, posun {cal['ox']:+.1f}°/{cal['oy']:+.1f}°")
        mode = self.mode_keys[[vision.MODES[k] for k in self.mode_keys].index(self.mode_var.get())]
        yolo_in = None

        def report(t, objs):
            fps = 1000.0 / max(t["celkem"], 0.01)
            self.timing_var.set(
                f"kontrast {t.get('kontrast', 0):.2f} ms | hrany {t.get('hrany', 0):.2f} ms | "
                f"kontury {t.get('kontury', 0):.2f} ms | celkem {t['celkem']:.2f} ms "
                f"(≈ {fps:.0f} fps zpracování) | objektů: {len(objs)}"
                + (f" | YOLO {self._yolo_ms:.0f} ms" if self._yolo_ms else ""))

        if color is not None and vision.available():              # --- barevna kamera (RGB) ---
            cv2 = vision.cv2
            h0, w0 = color.shape[:2]
            sc = min((2 * W + 4) / w0, H / h0)
            disp = cv2.resize(color, (int(w0 * sc), int(h0 * sc)), interpolation=cv2.INTER_AREA)
            rgb = cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)
            gray = cv2.cvtColor(disp, cv2.COLOR_BGR2GRAY)
            self.last_gray = gray
            out, t, objs = vision.render(gray, mode, self.contrast_var.get(),
                                         self.half_var.get(), base_rgb=rgb)
            report(t, objs)
            if new_frame:
                self._publish_cv(objs, (out.shape[1], out.shape[0]), t_cap)
            ph = self._photo(out)
            self._img_refs.append(ph)
            cv.create_image(4, 4, anchor="nw", image=ph)
            yolo_in = disp
        else:                                                      # --- sedotonovy stream 52997 ---
            for i, img in enumerate((left, right)):
                if not img or len(img) < W * H:
                    continue
                if vision.available():
                    arr = vision.np.frombuffer(img[: W * H], dtype=vision.np.uint8).reshape(H, W)
                    if i == 0:
                        self.last_gray = arr
                        yolo_in = arr
                    out, t, objs = vision.render(arr, mode, self.contrast_var.get(), self.half_var.get())
                    if i == 0:
                        report(t, objs)
                        if new_frame:
                            self._publish_cv(objs, (out.shape[1], out.shape[0]), t_cap)
                    ph = self._photo(out)
                else:
                    h = len(img) // W
                    ph = tk.PhotoImage(data=pgm(img[: W * h], W, h))
                    self.timing_var.set("Pro hrany a obrysy nainstaluj: pip install numpy opencv-python")
                self._img_refs.append(ph)
                cv.create_image(4 + i * (W + 4), 4, anchor="nw", image=ph)
        if yolo_in is not None and (self.yolo_var.get() or self.seg_var.get()) \
                and not self._yolo_busy:
            self._yolo_busy = True
            threading.Thread(target=self._run_yolo, args=(yolo_in.copy(), t_cap, self.source),
                             daemon=True).start()
        for (x1, y1, x2, y2, name, conf) in self.boxes if self.yolo_var.get() else []:
            cv.create_rectangle(4 + x1, 4 + y1, 4 + x2, 4 + y2, outline="#4de1a1", width=2)
            cv.create_text(4 + x1 + 3, 4 + y1 + 3, anchor="nw", fill="#4de1a1",
                           text=f"{name} {conf:.2f}", font=("Segoe UI", 10, "bold"))
        for pts, name, conf in self.polys if self.seg_var.get() else []:
            flat = [c + 4 for p in pts for c in p]
            if len(flat) >= 6:
                cv.create_polygon(flat, outline="#ffd24d", fill="", width=2)
                cv.create_text(flat[0], flat[1], anchor="sw", fill="#ffd24d",
                               text=f"{name} {conf:.2f}", font=("Segoe UI", 10, "bold"))
        self.win.after(33, self.tick)
