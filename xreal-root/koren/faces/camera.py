"""Kamera pro tváře: XREAL Eye (po zapnutí přes xreal-eye-rgb je to obyčejná webkamera) nebo jiná.

DirectShow neříká jména zařízení přes OpenCV a obě kamery Eye se jmenují „UVC Camera 1“, proto se
kamera vybírá podle obrazu: přeskočí se šedé (SLAM, IR), malé (512×378) a černé (zakrytá kamera
notebooku se zavřeným víkem). Poslední dobrá volba se pamatuje a jde přepnout tlačítkem „Další kamera“.
"""
import threading
import time

import cv2
import numpy as np

MAX_INDEX = 6


def _grab(idx: int, w: int, h: int, frames: int = 6):
    cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap.release()
        return None, None
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    f = None
    for _ in range(frames):
        ok, fr = cap.read()
        if ok:
            f = fr
    return cap, f


def judge(f) -> str:
    """'' = použitelná barevná kamera, jinak důvod, proč ne."""
    if f is None:
        return "nedává obraz"
    if f.shape[1] < 800:
        return f"malé rozlišení {f.shape[1]}×{f.shape[0]} (SLAM / IR)"
    small = f[::8, ::8].astype(np.int16)
    if float(small.mean()) < 3:
        return "černý obraz (zakrytá)"
    if float(np.abs(small[:, :, 0] - small[:, :, 2]).mean()) < 0.8 and float(np.abs(small[:, :, 1] - small[:, :, 2]).mean()) < 0.8:
        return "šedý obraz (SLAM / IR)"
    return ""


class Camera:
    """Čte snímky ve vlastním vlákně, `latest()` vrací vždy ten poslední (žádné zpoždění fronty)."""

    def __init__(self, index=None, width: int = 1280, height: int = 720, log=print):
        self.want = index               # None = automaticky
        self.w, self.h, self.log = width, height, log
        self.index = None
        self.cap = None
        self._frame, self._t, self._n = None, 0.0, 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._th = None
        self.error = ""

    def open(self, skip=()) -> bool:
        order = [self.want] if isinstance(self.want, int) else []
        order += [i for i in range(MAX_INDEX) if i not in order]
        why = []
        for i in order:
            if i in skip:
                continue
            cap, f = _grab(i, self.w, self.h)
            if cap is None:
                continue
            reason = judge(f)
            if reason and i != self.want:
                why.append(f"{i}: {reason}")
                cap.release()
                continue
            self.cap, self.index = cap, i
            self.error = ""
            self.log(f"[FACES] kamera {i}: {f.shape[1]}×{f.shape[0]}" + (f" (přeskočeno {'; '.join(why)})" if why else ""))
            self._stop.clear()
            self._th = threading.Thread(target=self._run, name="faces-cam", daemon=True)
            self._th.start()
            return True
        self.error = "žádná barevná kamera" + (f" ({'; '.join(why)})" if why else "")
        self.log(f"[FACES] {self.error}")
        return False

    def _run(self):
        fails = 0
        while not self._stop.is_set():
            ok, f = self.cap.read()
            if not ok:
                fails += 1
                if fails > 30:
                    self.error = "kamera přestala posílat obraz"
                    break
                time.sleep(0.05)
                continue
            fails = 0
            with self._lock:
                self._frame, self._t, self._n = f, time.monotonic(), self._n + 1

    def latest(self):
        """(snímek, čas, pořadí) nebo (None, 0, 0)."""
        with self._lock:
            return self._frame, self._t, self._n

    @property
    def alive(self) -> bool:
        return self._th is not None and self._th.is_alive()

    def close(self):
        self._stop.set()
        if self._th is not None:
            self._th.join(timeout=2.0)
        if self.cap is not None:
            self.cap.release()
        self.cap, self._th, self._frame = None, None, None
