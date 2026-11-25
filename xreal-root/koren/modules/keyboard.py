"""Modul: vlastní klávesnice na obrazovce, ovládaná myší hlavou.

Proč ne klávesnice Windows (osk.exe): běží s ochranou UIAccess, takže na ni nefunguje
klik poslaný programem (SendInput z myši hlavou) a nejde ani zavřít ani ukončit z kořene.
Tahle klávesnice je obyčejné okno kořene:
  - nebere fokus (WS_EX_NOACTIVATE), takže píše do okna, které bylo aktivní,
  - klávesy jsou popisky, ne tlačítka, aby Tk nepřesouvalo fokus,
  - píše přes SendInput (Unicode), takže funguje i česká diakritika,
  - zavírá se klávesou ✕, je vždy navrchu, ale pod kořenovým menu.
"""
import ctypes
import sys
import time

from . import Module
from ..menu import MenuItem

ROWS = [
    ["+", "ě", "š", "č", "ř", "ž", "ý", "á", "í", "é", "=", "⌫"],
    ["q", "w", "e", "r", "t", "z", "u", "i", "o", "p", "ú", ")"],
    ["a", "s", "d", "f", "g", "h", "j", "k", "l", "ů", "§", "⏎"],
    ["⇧", "y", "x", "c", "v", "b", "n", "m", ",", ".", "-", "?"],
    ["123", "←", "→", "␣", "@", "!", "✕"],
]
NUM_ROW = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "%", "⌫"]
WIDE = {"␣": 4, "⏎": 1, "⇧": 1, "123": 1}

BG, KEY_BG, KEY_HL, KEY_FG, KEY_SPECIAL = "#0b1a26", "#14324a", "#1f5a80", "#e4f1fa", "#7fd8be"

if sys.platform == "win32":
    from ctypes import wintypes

    class _KI(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _MI(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        _fields_ = [("ki", _KI), ("mi", _MI)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    _send = ctypes.WinDLL("user32").SendInput         # vlastní instance: argtypes nekoliduje s myší hlavou
    _send.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]

    def _key(vk=0, scan=0, flags=0):
        i = _INPUT(type=1)
        i.u.ki = _KI(vk, scan, flags, 0, 0)
        _send(1, ctypes.byref(i), ctypes.sizeof(_INPUT))

    def send_char(ch: str):
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):
            code = data[i] | (data[i + 1] << 8)
            _key(scan=code, flags=0x4)              # KEYEVENTF_UNICODE
            _key(scan=code, flags=0x4 | 0x2)

    def send_vk(vk: int):
        _key(vk=vk)
        _key(vk=vk, flags=0x2)
else:                                               # pragma: no cover
    def send_char(ch: str):
        pass

    def send_vk(vk: int):
        pass

VK = {"⌫": 0x08, "⏎": 0x0D, "←": 0x25, "→": 0x27}


class Keyboard(Module):
    id = "keyboard"
    name = "Klávesnice"
    description = "Klávesnice na obrazovce pro myš hlavou (píše do aktivního okna)."
    z = 300

    def start(self, ctx):
        super().start(ctx)
        self.win = None
        self.shift = False
        self.num = False
        self.labels = []

    def stop(self):
        self.close()

    @property
    def open(self):
        return self.win is not None

    # ------------------------------------------------------------------ okno
    def show(self):
        if self.win is not None:
            return
        import tkinter as tk
        m = self.ctx.monitor() or {"x": 0, "y": 0, "w": 1280, "h": 720}
        w = self.win = tk.Toplevel(self.ctx.tk)
        w.withdraw()                                # ukázat až s NOACTIVATE, jinak by se aktivovalo
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        try:
            w.attributes("-alpha", 0.93)
        except tk.TclError:
            pass
        w.configure(bg=BG)
        W, H = int(m["w"] * 0.86), int(m["h"] * 0.40)
        x, y = m["x"] + (m["w"] - W) // 2, m["y"] + m["h"] - H - int(m["h"] * 0.03)
        w.geometry(f"{W}x{H}+{x}+{y}")
        self.font_px = max(12, int(H / len(ROWS) * 0.42))
        self.frame = tk.Frame(w, bg=BG)
        self.frame.pack(fill="both", expand=True, padx=6, pady=6)
        self._build()
        w.update_idletasks()
        try:
            from xreal import winapi
            winapi.set_noactivate(w)                # klik nepřesune fokus z psané aplikace
            if sys.platform == "win32":
                u32 = ctypes.WinDLL("user32")
                hwnd = winapi.root_hwnd(w)
                u32.ShowWindow(hwnd, 4)             # SW_SHOWNOACTIVATE
                # HWND_TOPMOST, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
                u32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x2 | 0x1 | 0x10)
            else:
                w.deiconify()
        except Exception as e:
            self.ctx.log(f"[KBD] noactivate: {e}")
            w.deiconify()
        self.ctx.log("[KBD] otevřena")

    def close(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
            self.win = None
            self.ctx.log("[KBD] zavřena")

    def toggle(self):
        self.close() if self.open else self.show()

    def _build(self):
        import tkinter as tk
        for c in self.frame.winfo_children():
            c.destroy()
        self.labels = []
        rows = [NUM_ROW] + ROWS[1:] if self.num else ROWS
        for r, row in enumerate(rows):
            rf = tk.Frame(self.frame, bg=BG)
            rf.pack(fill="both", expand=True)
            for k in row:
                txt = k.upper() if (self.shift and len(k) == 1 and k.isalpha()) else k
                if k == "123":
                    txt = "abc" if self.num else "123"
                special = k in VK or k in ("⇧", "123", "␣", "✕")
                on = (k == "⇧" and self.shift) or (k == "123" and self.num)
                lab = tk.Label(rf, text=txt, bg=KEY_HL if on else KEY_BG,
                               fg=KEY_SPECIAL if special else KEY_FG,
                               font=("Segoe UI", -self.font_px, "bold" if special else "normal"),
                               bd=0, relief="flat", takefocus=0)
                lab.pack(side="left", fill="both", expand=True, padx=3, pady=3, ipadx=2)
                if WIDE.get(k, 1) > 1:
                    lab.pack_configure(ipadx=self.font_px * WIDE[k])
                lab.bind("<ButtonRelease-1>", lambda e, k=k: self._press(k))
                lab.bind("<Enter>", lambda e, l=lab: l.configure(bg=KEY_HL))
                lab.bind("<Leave>", lambda e, l=lab, on=on: l.configure(bg=KEY_HL if on else KEY_BG))
                self.labels.append(lab)

    def _press(self, k: str):
        if k == "✕":
            self.close()
            return
        if k == "⇧":
            self.shift = not self.shift
            self._build()
            return
        if k == "123":
            self.num = not self.num
            self._build()
            return
        if self.ctx.dry:
            self.ctx.log(f"[KBD] (dry) {k}")
            return
        if k in VK:
            send_vk(VK[k])
        elif k == "␣":
            send_char(" ")
        else:
            send_char(k.upper() if self.shift and k.isalpha() else k)
            if self.shift:
                self.shift = False
                self._build()

    # ------------------------------------------------------------------ napojení
    def on_mode(self, prev, mode):
        if mode in ("SUSPEND",):
            self.close()

    def menu_items(self):
        return [MenuItem("kbd", "Zavřít klávesnici" if self.open else "Klávesnice na obrazovce",
                         action=self.toggle)]

    def home_tiles(self):
        from .home import Tile
        return [Tile("keyboard", "Klávesnice", "zavřít" if self.open else "psaní myší hlavou", "⌨", self.toggle)]

    def status(self):
        return "otevřená" if self.open else ""


MODULE = Keyboard
