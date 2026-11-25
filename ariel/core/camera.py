"""Ariel Core / camera - Kamera XREAL Eye -> MJPEG.

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

from .config import DATA_DIR, LOGS_DIR, ROOT


# ── XREAL Eye RGB kamera → MJPEG stream (/eye.mjpg) ───────────────────────────
EYE_NAME   = "UVC Camera 1"          # obe kamery Eye se ve Windows jmenuji takhle
EYE_WIDTH  = int(os.environ.get("EYE_WIDTH", "960"))   # zpracovani (YOLO, AR); video v okne: set EYE_WIDTH=1920
TABLET_W   = int(os.environ.get("TABLET_W", "960"))    # tablet dostava zmensenou kopii (WiFi), /eye.mjpg?w=960
EYE_FPS    = int(os.environ.get("EYE_FPS", "15"))
EYE_Q      = int(os.environ.get("EYE_Q", "6"))         # kvalita JPEG 2 (nejlepsi) .. 31
EYE_IDLE_S = int(os.environ.get("EYE_IDLE_S", "60"))  # bez divaku -> po 60 s zastavit ffmpeg
# 1 = ffmpeg jen predava plne 1080p snimky z kamery (bez prekodovani) a vyrez/zmenseni dela server
#     -> ostry digitalni zoom a plne rozliseni pro OCR;  0 = stary rezim (ffmpeg rovnou zmensuje, bez zoomu)
EYE_RAW    = os.environ.get("EYE_RAW", "1") != "0"
ZOOM_MAX   = float(os.environ.get("EYE_ZOOM_MAX", "8"))
EYE_JPEG_Q = int(os.environ.get("EYE_JPEG_Q", "82"))   # kvalita JPEG po zpracovani (rezim EYE_RAW)
EYE_ENABLER = ROOT.parent / "xreal-eye-rgb" / "eye_rgb_enable.py"   # krok 3: prevzit do Ariel
EYE_FFLOG   = LOGS_DIR / "eye_ffmpeg.log"
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# Windows "job": vse, co do nej dame, Windows zabije, jakmile skonci server
# (i pri zavreni okna nebo Ctrl+C) -> ffmpeg uz nikdy nezustane drzet kameru.
_JOB = None
if sys.platform == "win32":
    try:
        import ctypes as _ct
        from ctypes import wintypes as _w

        class _IOC(_ct.Structure):
            _fields_ = [(n, _ct.c_ulonglong) for n in ("r", "w", "o", "rt", "wt", "ot")]

        class _BASIC(_ct.Structure):
            _fields_ = [("PerProcessUserTimeLimit", _ct.c_longlong), ("PerJobUserTimeLimit", _ct.c_longlong),
                        ("LimitFlags", _w.DWORD), ("MinimumWorkingSetSize", _ct.c_size_t),
                        ("MaximumWorkingSetSize", _ct.c_size_t), ("ActiveProcessLimit", _w.DWORD),
                        ("Affinity", _ct.c_size_t), ("PriorityClass", _w.DWORD), ("SchedulingClass", _w.DWORD)]

        class _EXT(_ct.Structure):
            _fields_ = [("BasicLimitInformation", _BASIC), ("IoInfo", _IOC),
                        ("ProcessMemoryLimit", _ct.c_size_t), ("JobMemoryLimit", _ct.c_size_t),
                        ("PeakProcessMemoryUsed", _ct.c_size_t), ("PeakJobMemoryUsed", _ct.c_size_t)]

        _k32 = _ct.WinDLL("kernel32", use_last_error=True)
        _k32.CreateJobObjectW.restype = _w.HANDLE
        _k32.SetInformationJobObject.argtypes = [_w.HANDLE, _ct.c_int, _ct.c_void_p, _w.DWORD]
        _k32.AssignProcessToJobObject.argtypes = [_w.HANDLE, _w.HANDLE]
        _JOB = _k32.CreateJobObjectW(None, None)
        _info = _EXT()
        _info.BasicLimitInformation.LimitFlags = 0x2000          # KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(_JOB, 9, _ct.byref(_info), _ct.sizeof(_info)):
            _JOB = None
    except Exception as _e:
        print(f"[EYE] job object nejde vytvorit: {_e}")
        _JOB = None


def _bind_to_server(proc):
    if _JOB is not None:
        try:
            _k32.AssignProcessToJobObject(_JOB, int(proc._handle))
        except Exception as e:
            print(f"[EYE] nelze svazat ffmpeg se serverem: {e}")


def _kill_orphan_ffmpeg():
    """Ukonci ffmpeg, ktery po minulem serveru zustal drzet kameru Eye."""
    if sys.platform != "win32":
        return
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe'\" | "
          "Where-Object { $_.CommandLine -like '*-f dshow*' -and $_.CommandLine -like '*pipe:1*' } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                           text=True, timeout=20, creationflags=_NO_WINDOW)
        pids = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
        if pids:
            print(f"[EYE] ukoncen osirely ffmpeg z minula (PID {', '.join(pids)}) – kamera uvolnena")
    except Exception as e:
        print(f"[EYE] kontrola osirelych ffmpeg selhala: {e}")


CAM_FILE = DATA_DIR / "camera.json"     # posledni fungujici kamera -> rychly start


def _load_last_cam():
    try:
        d = json.loads(CAM_FILE.read_text(encoding="utf-8"))
        return [str(d["name"]), int(d["num"])]
    except Exception:
        return None


def _save_last_cam(name, num):
    try:
        CAM_FILE.write_text(json.dumps({"name": name, "num": num, "t": time.ctime()}), encoding="utf-8")
    except Exception as e:
        print(f"[EYE] nelze ulozit {CAM_FILE.name}: {e}")


def _find_ffmpeg():
    for p in (r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\ffmpeg.exe"):
        if os.path.exists(p):
            return p
    return shutil.which("ffmpeg")


def split_jpegs(buf: bytearray):
    """Vytahne z bufferu cele JPEG snimky (SOI ffd8 .. EOI ffd9), zbytek necha."""
    out = []
    while True:
        s = buf.find(b"\xff\xd8")
        if s < 0:
            buf.clear()
            break
        e = buf.find(b"\xff\xd9", s + 2)
        if e < 0:
            if s > 0:
                del buf[:s]
            break
        out.append(bytes(buf[s:e + 2]))
        del buf[:e + 2]
    return out


class EyeStream:
    """Jeden ffmpeg proces cte RGB kameru (MJPEG) a vsichni divaci dostavaji posledni snimek."""

    def __init__(self):
        self.cond = threading.Condition()
        self.start_lock = threading.Lock()
        self.frame, self.seq = None, 0
        self.proc = None
        self.clients, self.last_seen = 0, 0.0
        self.status, self.devnum, self.devname = "idle", None, EYE_NAME
        self.frames, self.errlog = 0, None
        # plny snimek z kamery (1080p JPEG) a zpracovany pohled (zoom, 960 px) pro vsechny divaky
        self.raw_mode = EYE_RAW
        self.raw, self.raw_seq, self.raw_cond = None, 0, threading.Condition()
        self.img, self.vmap = None, (0.0, 0.0, 1.0, 1.0)   # vmap = vyrez v plnem snimku (x0, y0, w, h) 0..1
        self.zoom = {"z": 1.0, "cx": 0.5, "cy": 0.5}
        self.proc_ms = 0.0
        threading.Thread(target=self._watchdog, daemon=True).start()
        threading.Thread(target=self._process, daemon=True).start()

    # -- detekce kamery
    def _run(self, args, timeout=15):
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
            return (r.stdout or "") + (r.stderr or "")
        except Exception as e:
            return str(e)

    def _find_rgb(self, ff):
        """Najde RGB kameru Eye podle schopnosti (MJPEG 1920x1080), ne podle nazvu.
        -> (cislo zarizeni nebo None, pocet kandidatu); jmeno ulozi do self.devname."""
        out = self._run([ff, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
        # "(video)" = kamera jde otevrit, "(none)" = Windows ji desktopovym aplikacim prave neotevre
        listed = re.findall(r'"([^"]+)"\s*\((video|none)\)', out)
        names = [n for n, _ in listed]
        self.unopenable = [n for n, kind in listed if kind == "none"]
        skip = ("integrated", "meta quest", " ir ", "virtual", "obs ")
        cands, seen = [], {}
        for n in names:
            num = seen.get(n, 0)
            seen[n] = num + 1
            if not any(k in f" {n.lower()} " for k in skip):
                cands.append((n, num))
        cands.sort(key=lambda c: 0 if any(k in c[0].lower() for k in ("uvc", "xreal", "eye")) else 1)
        virtual = ("meta quest", "virtual", "obs ")
        # zadna fyzicka kamera (ani vestavena) = problem Windows (opravneni / Frame Server), ne bryli
        self.no_physical = not any(not any(k in f" {n.lower()} " for k in virtual)
                                   for n in names if n not in self.unopenable)
        self.rgb_cands = []
        for n, num in cands:
            opts = self._run([ff, "-hide_banner", "-list_options", "true", "-f", "dshow",
                              "-video_device_number", str(num), "-i", f"video={n}"])
            if "vcodec=mjpeg" in opts and "1920x1080" in opts:
                self.rgb_cands.append((n, num))
        if self.rgb_cands:
            good = getattr(self, "good", None)              # naposledy fungujici ma prednost
            self.rgb_cands.sort(key=lambda c: 0 if c == good else 1)
            n, num = self.rgb_cands[0]
            self.devname = n
            return num, len(cands)
        if time.time() - getattr(self, "last_names_log", 0) > 60:
            self.last_names_log = time.time()
            print(f"[EYE] kamery ve Windows: {names or '(zadne)'}; kandidati: {[c[0] for c in cands]}")
            if self.unopenable and cands:
                n, num = cands[0]
                err = self._run([ff, "-hide_banner", "-list_options", "true", "-f", "dshow",
                                 "-video_device_number", str(num), "-i", f"video={n}"])
                last = [l.split("]", 1)[-1].strip() for l in err.splitlines() if l.strip()][-3:]
                print(f"[EYE] Windows kamery nabizi jen jmenem, otevrit nejdou ({self.unopenable}); "
                      f"ffmpeg u \"{n}\": {' | '.join(last)}")
        return None, len(cands)

    def _enable_cameras(self) -> bool:
        """Zapne kamery pres HID - nejvys jednou za 2 minuty (kazde zapnuti bryle znovu pripoji)."""
        if not EYE_ENABLER.exists():
            print(f"[EYE] zapinac nenalezen: {EYE_ENABLER}")
            return False
        if time.time() - getattr(self, "last_enable", 0) < 120:
            return False
        self.last_enable = time.time()
        print("[EYE] zapinam kamery Eye pres HID …")
        out = self._run([sys.executable, str(EYE_ENABLER), "--no-probe"], timeout=90)
        for line in out.splitlines():
            if any(k in line for k in ("USB funkce", "HID", "selhal", "Chyba", "nenalezeno", "Error", "error")):
                print("[EYE]   " + line.strip()[:160])
        return True

    # -- beh
    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> bool:
        self.last_seen = time.time()               # zadost o start = nekdo se chce divat
        with self.start_lock:
            if self.running():
                return True
            ff = _find_ffmpeg()
            if not ff:
                self.status = "ffmpeg nenalezen"
                return False
            # rychly start: posledni fungujici kamera (data/camera.json) bez hledani pres dshow
            last = _load_last_cam()
            if last and self._launch(ff, last[0], last[1], wait_s=6.0):
                self.good = tuple(last)
                print(f"[EYE] rychly start z posledni kamery za {time.time() - self.last_seen:.1f} s")
                return True
            if last:
                self.stop()
            self.status = "hledam kameru"
            num, count = None, 0
            for attempt in range(3):                # kamera muze byt chvili "busy" po predchozim ffmpeg
                num, count = self._find_rgb(ff)
                if num is not None:
                    break
                if attempt == 0 and count:
                    _kill_orphan_ffmpeg()           # kamera je v systemu, ale neodpovida -> drzi ji nekdo
                time.sleep(2)
            if num is None and getattr(self, "no_physical", False):
                self.status = ("Windows kamery ukazuje, ale desktopovym aplikacim je neotevre (i vestavenou) - "
                               "Nastaveni > Ochrana osobnich udaju > Fotoaparat > pristup pro desktopove "
                               "aplikace, pripadne restart Windows")
                if time.time() - getattr(self, "last_np_log", 0) > 60:
                    self.last_np_log = time.time()
                    print(f"[EYE] {self.status}")
                return False
            if num is None and count == 0 and self._enable_cameras():   # kamery v systemu nejsou
                self.status = "zapinam kameru"
                for _ in range(5):
                    time.sleep(3)
                    num, count = self._find_rgb(ff)
                    if num is not None:
                        break
            if num is None:
                self.status = ("kamera obsazena jinou aplikaci" if count
                               else "kamera Eye neni v systemu - odpoj a pripoj bryle")
                print(f"[EYE] {self.status} (kamer Eye v systemu: {count})")
                return False
            # kandidati (stejne jmeno "UVC Camera 1", poradi se muze menit): ten, co do 8 s posle snimek
            cands = getattr(self, "rgb_cands", None) or [(self.devname, num)]
            for n, dnum in cands:
                if self._launch(ff, n, dnum):
                    self.good = (n, dnum)
                    _save_last_cam(n, dnum)
                    print(f"[EYE] kamera bezi za {time.time() - self.last_seen:.1f} s od zadosti")
                    return True
                print(f"[EYE] \"{n}\" #{dnum}: do 8 s zadny snimek – zkousim dalsi")
                self.stop()
            self.status = "kamera neposila obraz - mas bryle nasazene (Eye sviti)?"
            print(f"[EYE] {self.status}")
            return False

    def _launch(self, ff, name, num, wait_s=8.0) -> bool:
        """Spusti ffmpeg pro jednu kameru a pocka na prvni snimek."""
        self.devnum, self.devname = num, name
        # stejne vstupni volby jako overeny test (eye_cam_map.py), ktery snimky dal
        cmd = [ff, "-hide_banner", "-loglevel", "warning",
               "-f", "dshow", "-rtbufsize", "256M", "-video_device_number", str(num),
               "-video_size", "1920x1080", "-vcodec", "mjpeg",
               "-i", f"video={name}"]
        if self.raw_mode:       # snimky z kamery beze zmeny (zadne dekodovani v ffmpeg)
            cmd += ["-c:v", "copy", "-f", "mjpeg", "-flush_packets", "1", "pipe:1"]
        else:
            cmd += ["-vf", f"scale={EYE_WIDTH}:-2", "-r", str(EYE_FPS), "-q:v", str(EYE_Q),
                    "-f", "mjpeg", "-flush_packets", "1", "pipe:1"]
        try:
            self.errlog = open(EYE_FFLOG, "a", encoding="utf-8", errors="replace")
            self.errlog.write(f"\n=== {time.ctime()}\n{' '.join(cmd)}\n")
            self.errlog.flush()
        except Exception:
            self.errlog = subprocess.DEVNULL
        self.frames = 0
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.errlog, creationflags=_NO_WINDOW)
        _bind_to_server(self.proc)
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self.status = "spoustim kameru"
        print(f"[EYE] spoustim \"{name}\" #{num} ({EYE_WIDTH}px, {EYE_FPS} fps, "
              f"{'plne 1080p + zoom' if self.raw_mode else 'zmensuje ffmpeg, bez zoomu'})")
        t0 = time.time()
        while time.time() - t0 < wait_s:
            if self.frames > 0:
                self.status = "bezi"
                return True
            if not self.running():
                return False
            time.sleep(0.1)
        return False

    def stop(self, fast=False):
        """Setrne zastaveni: ffmpeg dostane 'q', uzavre DirectShow a kameru uvolni.
        fast=True: zaseknuty ffmpeg (na 'q' nereaguje) rovnou ukoncit."""
        p = self.proc
        if p and p.poll() is None:
            try:
                p.stdin.write(b"q")
                p.stdin.flush()
                p.wait(1 if fast else 4)
            except Exception:
                pass
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(3)
                except Exception:
                    p.kill()
        self.status = "zastaveno"

    def _reader(self, proc):
        buf = bytearray()
        read = getattr(proc.stdout, "read1", proc.stdout.read)
        while True:
            chunk = read(65536)
            if not chunk:
                break
            buf += chunk
            for jpg in split_jpegs(buf):
                if self.frames == 0:
                    print(f"[EYE] prvni snimek ({len(jpg)} B)")
                self.frames += 1
                self.last_frame_t = time.time()
                if self.raw_mode:                   # zpracuje _process (zoom, zmenseni)
                    with self.raw_cond:
                        self.raw = jpg
                        self.raw_seq += 1
                        self.raw_cond.notify_all()
                    continue
                with self.cond:
                    self.frame = jpg
                    self.seq += 1
                    self.cond.notify_all()
        with self.cond:
            self.cond.notify_all()
        rc = proc.wait()
        tail = ""
        try:
            tail = " | ".join(l.strip() for l in open(EYE_FFLOG, encoding="utf-8",
                                                      errors="replace").read().splitlines()[-4:])
        except Exception:
            pass
        print(f"[EYE] ffmpeg skoncil (kod {rc}, snimku {self.frames}) {tail}")
        if self.proc is proc:
            self.status = "zastaveno"

    def _watchdog(self):
        while True:
            time.sleep(2)
            if self.start_lock.locked():
                continue                            # prave se spousti -> nechat byt
            if self.running() and self.clients == 0 and time.time() - self.last_seen > EYE_IDLE_S:
                print("[EYE] zadny divak – zastavuji stream")
                self.stop()
            elif (self.running() and self.clients > 0 and self.frames > 0
                  and time.time() - getattr(self, "last_frame_t", time.time()) > 6):
                # kamera prestala posilat bez chyby (napr. bryle sundane/uspane) -> restart ffmpeg
                print("[EYE] kamera 6 s neposlala snimek – restartuji stream")
                self.restarting = True
                try:
                    self.stop(fast=True)
                    self.start()
                finally:
                    self.restarting = False

    def add_client(self):
        with self.cond:
            self.clients += 1
            self.last_seen = time.time()

    def remove_client(self):
        with self.cond:
            self.clients = max(0, self.clients - 1)
            self.last_seen = time.time()

    def wait_frame(self, last_seq, timeout=5.0):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq or not self.running(), timeout=timeout)
            return self.frame, self.seq

    def view(self, seq):
        """Dekodovany pohled (numpy) a jeho vyrez v plnem snimku pro dany seq, jinak (None, None)."""
        with self.cond:
            if seq == self.seq and self.img is not None:
                return self.img, self.vmap
        return None, None

    # -- zoom: vyrez z plneho 1080p snimku, vystup vzdy EYE_WIDTH px
    def _crop_rect(self, z, cx, cy):
        cw = ch = 1.0 / z
        x0 = min(max(cx - cw / 2, 0.0), 1.0 - cw)
        y0 = min(max(cy - ch / 2, 0.0), 1.0 - ch)
        return x0, y0, cw, ch

    def set_zoom(self, b: dict):
        """z = absolutni zoom, dz = nasobek (kolecko), at = [u, v] bod v obrazu, ktery zustane na miste,
        dx/dy = posun v obrazovkach, cx/cy = stred 0..1, reset = 1x."""
        z0, cx, cy = self.zoom["z"], self.zoom["cx"], self.zoom["cy"]
        x0, y0, cw, ch = self._crop_rect(z0, cx, cy)
        if b.get("reset"):
            z, cx, cy = 1.0, 0.5, 0.5
        else:
            z = float(b["z"]) if "z" in b else z0 * float(b.get("dz", 1.0))
            z = min(max(z, 1.0), ZOOM_MAX)
            if "cx" in b or "cy" in b:
                cx, cy = float(b.get("cx", cx)), float(b.get("cy", cy))
            elif isinstance(b.get("at"), (list, tuple)) and len(b["at"]) == 2:
                u, v = (min(max(float(t), 0.0), 1.0) for t in b["at"])
                px, py = x0 + u * cw, y0 + v * ch          # bod v plnem snimku
                cx, cy = px - u / z + 0.5 / z, py - v / z + 0.5 / z
            else:
                cx, cy = x0 + cw / 2, y0 + ch / 2         # z orezaneho (realneho) stredu
            cx += float(b.get("dx", 0.0)) / z
            cy += float(b.get("dy", 0.0)) / z
        nx0, ny0, ncw, nch = self._crop_rect(z, cx, cy)
        self.zoom = {"z": round(z, 3), "cx": round(nx0 + ncw / 2, 4), "cy": round(ny0 + nch / 2, 4)}
        return self.zoom_info()

    def zoom_info(self):
        return {**self.zoom, "max": ZOOM_MAX, "available": self.raw_mode}

    def _render(self, raw):
        import cv2
        import numpy as np
        z = self.zoom["z"]
        if z <= 1.001:      # bez zoomu: JPEG rovnou dekodovat v polovicnim rozliseni (rychle)
            flag = cv2.IMREAD_REDUCED_COLOR_2 if EYE_WIDTH <= 960 else cv2.IMREAD_COLOR
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), flag)
            rect = (0.0, 0.0, 1.0, 1.0)
        else:
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                return None, None
            h, w = img.shape[:2]
            rect = self._crop_rect(z, self.zoom["cx"], self.zoom["cy"])
            x0, y0, cw, ch = rect
            img = img[int(y0 * h):int((y0 + ch) * h), int(x0 * w):int((x0 + cw) * w)]
        if img is None or img.size == 0:
            return None, None
        h, w = img.shape[:2]
        ow = EYE_WIDTH
        oh = int(round(ow * h / w / 2)) * 2
        if (w, h) != (ow, oh):
            interp = cv2.INTER_AREA if w > ow else cv2.INTER_CUBIC
            img = cv2.resize(img, (ow, oh), interpolation=interp)
        return img, rect

    def _process(self):
        """Z plnych snimku kamery dela pohled pro divaky: zoom, zmenseni, JPEG; max EYE_FPS."""
        try:
            import cv2
        except Exception as e:
            print(f"[EYE] OpenCV chybi ({e}) – zoom nepujde, prepinam na stary rezim")
            self.raw_mode = False
            return
        last, tnext, fails = 0, 0.0, 0
        period = 1.0 / max(1, EYE_FPS)
        while True:
            with self.raw_cond:
                self.raw_cond.wait_for(lambda: self.raw_seq != last, timeout=2)
                raw, rs = self.raw, self.raw_seq
            if raw is None or rs == last or not self.raw_mode:
                continue
            last = rs
            now = time.time()
            if now < tnext:                         # kamera posila az 60 fps, divakum staci EYE_FPS
                continue
            tnext = max(tnext + period, now - period)
            t = time.time()
            try:
                img, rect = self._render(raw)
            except Exception as e:
                img, rect = None, None
                if fails == 0:
                    print(f"[EYE] zpracovani snimku: {type(e).__name__}: {e}")
            if img is None:
                fails += 1
                if fails == 30:                     # snimky z kamery nejdou dekodovat -> stary rezim
                    print("[EYE] snimky z kamery nejdou dekodovat – prepinam na stary rezim (bez zoomu)")
                    self.raw_mode = False
                    threading.Thread(target=self._restart, daemon=True).start()
                continue
            fails = 0
            if rect == (0.0, 0.0, 1.0, 1.0) and img.shape[1] == EYE_WIDTH and EYE_WIDTH >= 1920:
                jpg = raw                           # bez zoomu v plnem rozliseni: originalni JPEG z kamery
            else:
                ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, EYE_JPEG_Q])
                if not ok:
                    continue
                jpg = enc.tobytes()
            self.proc_ms = round((time.time() - t) * 1000, 1)
            with self.cond:
                self.frame, self.img, self.vmap = jpg, img, rect
                self.seq += 1
                self.cond.notify_all()

    def _restart(self):
        self.restarting = True
        try:
            self.stop(fast=True)
            self.start()
        finally:
            self.restarting = False


eye = EyeStream()
