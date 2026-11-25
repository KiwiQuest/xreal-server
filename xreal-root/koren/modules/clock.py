"""Modul: hodiny a baterie v rohu brýlí. Ukázka průhledné vrstvy, která běží nad jinými."""
import ctypes
import datetime
import sys

from . import Module


def battery():
    """(procenta, nabíjí) nebo None."""
    if sys.platform != "win32":
        return None

    class SPS(ctypes.Structure):
        _fields_ = [("AC", ctypes.c_ubyte), ("Flag", ctypes.c_ubyte), ("Pct", ctypes.c_ubyte),
                    ("Saver", ctypes.c_ubyte), ("Life", ctypes.c_ulong), ("Full", ctypes.c_ulong)]
    s = SPS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)) or s.Pct == 255:
        return None
    return s.Pct, s.AC == 1


class Clock(Module):
    id = "clock"
    name = "Hodiny a baterie"
    description = "Čas a stav baterie nenápadně v rohu, jen v režimu bez rukou."
    z = 50
    needs = ("display",)

    def start(self, ctx):
        super().start(ctx)
        self.L = ctx.layer(self.z)
        self.L.show(False)

    def stop(self):
        self.L.show(False)

    def tick(self, now):
        show = self.ctx.mode() in ("BAG", "WAITING")
        self.L.show(show)
        if not show:
            return
        t = datetime.datetime.now().strftime("%H:%M")
        b = battery()
        txt = t if b is None else f"{t}    {b[0]} %{' ⚡' if b[1] else ''}"
        low = b is not None and b[0] <= 15 and not b[1]
        self.L.draw((self.L.rect, txt, low), lambda cv: cv.create_text(
            self.L.W - 40, 34, text=txt, anchor="e", font=("Segoe UI", 16),
            fill="#ffcf6b" if low else "#6d8ea3"))

    def status(self):
        b = battery()
        return "" if b is None else f"baterie {b[0]} %"


MODULE = Clock
