"""Pruhledny kruzek kolem kurzoru (circle click) + kratke hlasky ZAP / VYP."""
import math
import time

from . import winapi

RING = 128          # velikost okna s kruzkem (px)
R0 = 34             # zakladni polomer kruzku (px)
SPIN_DPS = 220.0    # rychlost otaceni (stupne/s), 0 = neotaci se
KEY = "#010101"     # pruhledna barva


class RingOverlay:
    def __init__(self, root, cfg, rt):
        import tkinter as tk
        self.root, self.cfg, self.rt = root, cfg, rt
        ov = self.win = tk.Toplevel(root)
        ov.overrideredirect(True)
        ov.attributes("-topmost", True)
        ov.configure(bg=KEY)
        try:
            ov.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        self.cv = tk.Canvas(ov, width=RING, height=RING, bg=KEY, highlightthickness=0)
        self.cv.pack()
        ov.geometry(f"{RING}x{RING}+0+0")
        ov.update_idletasks()
        winapi.make_overlay_window(ov)
        ov.withdraw()
        self.visible = False
        self.mid = RING // 2
        self.anim = {"prev": False, "t_in": 0.0, "mode": None, "t0": 0.0, "p": 0.0}

    def _circle(self, r):
        m = self.mid
        return (m - r, m - r, m + r, m + r)

    def _draw_active(self, now, p):
        a = self.anim
        k = min(1.0, (now - a["t_in"]) / 0.18)
        r = R0 * (0.55 + 0.45 * (1 - (1 - k) ** 3))
        cv = self.cv
        cv.create_oval(*self._circle(r), outline="#3a4250", width=8)
        extent = -max(40.0, 359.9 * min(p, 1.0))
        start = 90.0 - (now * SPIN_DPS) % 360.0
        cv.create_arc(*self._circle(r), start=start, extent=extent, style="arc",
                      outline="#4de1a1", width=8)
        head = math.radians(start + extent)
        hx, hy = self.mid + r * math.cos(head), self.mid - r * math.sin(head)
        cv.create_oval(hx - 6, hy - 6, hx + 6, hy + 6, fill="#eafff5", outline="")

    def _draw_end(self, now):
        a, cv = self.anim, self.cv
        click = a["mode"] == "click"
        t = (now - a["t0"]) / (0.30 if click else 0.16)
        if t >= 1.0:
            a["mode"] = None
            return False
        if click:
            cv.create_oval(*self._circle(R0 * (1.0 + 0.6 * t)), outline="#9dffd6",
                           width=max(1.5, 10 * (1 - t)))
        else:
            r, w = R0 * (1.0 - 0.5 * t), max(1.5, 8 * (1 - t))
            cv.create_oval(*self._circle(r), outline="#5a2f28", width=w)
            cv.create_arc(*self._circle(r), start=90, extent=-max(40.0, 359.9 * a["p"]),
                          style="arc", outline="#e1704d", width=w)
        return True

    def _draw_toast(self):
        rt = self.rt
        col = "#4de1a1" if rt.toast_ok else "#e1704d"
        self.cv.create_oval(*self._circle(R0 + 4), outline=col, width=6)
        size = 11 if "\n" in rt.toast_text else 15
        self.cv.create_text(self.mid, self.mid, text=rt.toast_text, fill=col,
                            justify="center", font=("Segoe UI", size, "bold"))

    def tick(self):
        rt, a = self.rt, self.anim
        now = time.time()
        active = rt.enabled and rt.dwell_active
        if active and not a["prev"]:
            a["t_in"], a["mode"] = now, None
        if a["prev"] and not active:
            a["mode"] = "click" if now < rt.flash_until else "cancel"
            a["t0"] = now
        if not rt.enabled:
            a["mode"] = None
        if active:
            a["p"] = rt.dwell_progress
        a["prev"] = active

        toast = now < rt.toast_until
        show = active or a["mode"] is not None or toast
        if show:
            x, y = self.root.winfo_pointerxy()
            self.win.geometry(f"+{x - RING // 2}+{y - RING // 2}")
            self.cv.delete("all")
            drew = False
            if active:
                self._draw_active(now, rt.dwell_progress)
                drew = True
            elif a["mode"] is not None:
                drew = self._draw_end(now)
            if toast:
                self._draw_toast()
                drew = True
            show = drew
        if show:
            if not self.visible:
                self.win.deiconify()
                self.win.attributes("-topmost", True)
                winapi.make_overlay_window(self.win)
                self.visible = True
        elif self.visible:
            self.win.withdraw()
            self.visible = False
