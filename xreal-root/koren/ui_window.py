"""Okno plochy: prohlížeč (Chrome / Edge) v režimu aplikace, bez rámečku přes celý monitor brýlí.

Stejný postup jako `ariel/tools/oko.ps1`: vlastní profil prohlížeče, `--app`. Po startu kořen
okno najde podle titulku, sundá mu rámeček a přesně ho položí na monitor brýlí (fyzické pixely),
takže nezáleží na měřítku Windows ani na tom, kolikátý monitor brýle jsou.
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
from typing import Optional

TITLE = "XREAL Kořen – plocha"
PROFILE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "xreal-koren-ui-profile")


def find_browser() -> Optional[str]:
    pf, pf86, la = (os.environ.get(k, "") for k in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"))
    for p in (rf"{pf}\Google\Chrome\Application\chrome.exe", rf"{pf86}\Google\Chrome\Application\chrome.exe",
              rf"{la}\Google\Chrome\Application\chrome.exe", rf"{pf86}\Microsoft\Edge\Application\msedge.exe",
              rf"{pf}\Microsoft\Edge\Application\msedge.exe"):
        if p and os.path.isfile(p):
            return p
    return None


def _find_window(title: str):
    if sys.platform != "win32":
        return None
    u = ctypes.WinDLL("user32")
    found = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def cb(h, _):
        n = u.GetWindowTextLengthW(h)
        if n and u.IsWindowVisible(h):
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, buf, n + 1)
            if buf.value.startswith(title):
                found.append(h)
        return True
    u.EnumWindows(proto(cb), None)
    return found[0] if found else None


class UiWindow:
    def __init__(self, log=print):
        self.log = log
        self.proc: Optional[subprocess.Popen] = None
        self.hwnd = None
        self.monitor = None

    @property
    def open(self) -> bool:
        if self.proc is not None and self.proc.poll() is None:
            return True
        return _find_window(TITLE) is not None      # prohlížeč mohl start předat jinému procesu

    def show(self, url: str, monitor: dict, dry: bool = False):
        self.monitor = monitor
        if self.open:
            self.place(monitor)
            return
        exe = find_browser()
        if not exe:
            self.log("[UI] Chrome ani Edge nenalezen, otevírám v výchozím prohlížeči")
            os.startfile(url)
            return
        m = monitor or {"x": 0, "y": 0, "w": 1280, "h": 720}
        args = [exe, f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check",
                "--disable-features=Translate", f"--app={url}",
                f"--window-position={m['x'] + 20},{m['y'] + 20}", f"--window-size={max(400, m['w'] - 40)},{max(300, m['h'] - 40)}"]
        self.proc = subprocess.Popen(args, creationflags=0x08000000)
        self.log(f"[UI] plocha otevřena ({os.path.basename(exe)})")
        threading.Thread(target=self._grab, args=(m,), daemon=True).start()

    def _grab(self, m):
        for _ in range(60):                         # až 15 s na naběhnutí prohlížeče
            h = _find_window(TITLE)
            if h:
                self.hwnd = h
                time.sleep(0.4)
                self.place(m)
                return
            time.sleep(0.25)
        self.log("[UI] okno plochy jsem nenašel")

    def place(self, m: dict):
        """Bez rámečku, přesně přes monitor (fyzické pixely)."""
        if not m or sys.platform != "win32":
            return
        self.monitor = m
        h = self.hwnd or _find_window(TITLE)
        if not h:
            return
        self.hwnd = h
        u = ctypes.WinDLL("user32")
        u.GetWindowLongW.restype = ctypes.c_long
        style = u.GetWindowLongW(h, -16)
        style &= ~(0x00C00000 | 0x00040000)          # WS_CAPTION | WS_THICKFRAME
        u.SetWindowLongW(h, -16, style)
        # SWP_FRAMECHANGED | SWP_NOZORDER | SWP_NOACTIVATE? -> aktivovat ano, ať jde psát
        u.SetWindowPos(h, None, m["x"], m["y"], m["w"], m["h"], 0x0020 | 0x0004)

    def close(self):
        h = self.hwnd or _find_window(TITLE)
        if h and sys.platform == "win32":
            ctypes.WinDLL("user32").PostMessageW(h, 0x0010, 0, 0)      # WM_CLOSE
            self.log("[UI] plocha zavřena")
        if self.proc is not None and self.proc.poll() is None:
            try:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"], capture_output=True,
                               timeout=10, creationflags=0x08000000)
            except Exception as e:
                self.log(f"[UI] zavření: {e}")
            self.log("[UI] plocha zavřena")
        self.proc = None
        self.hwnd = None
