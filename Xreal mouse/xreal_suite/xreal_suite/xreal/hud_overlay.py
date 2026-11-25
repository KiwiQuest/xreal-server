"""HUD overlay – průhledný panel přes brýle, modulární systém.

Moduly:
  0  MouseControlModule  – stav myši, zkratky, piny
  1  InfoModule          – hodiny, datum, FOV/speed
  2  KeyboardModule      – virtuální klávesnice (gaze dwell)

Přepínání: F5 = předchozí, F6 = další modul.
Skrýt/zobrazit HUD: F7.
"""
import datetime
import time

from . import winapi

# ── barvy ────────────────────────────────────────────────────────────────────
KEY      = "#010101"   # průhledná barva okna
C_PANEL  = "#0a1520"   # pozadí panelu
C_BORDER = "#1a3550"
C_GREEN  = "#00ff80"
C_CYAN   = "#4dc3ff"
C_YELLOW = "#ffe040"
C_RED    = "#ff5050"
C_DIM    = "#2a4458"
C_TEXT   = "#a8c8dc"
C_WHITE  = "#e8f4ff"

# ── VK kódy ──────────────────────────────────────────────────────────────────
VK_F5 = 0x74
VK_F6 = 0x75
VK_F7 = 0x76


# ─────────────────────────── pomocné kreslení ────────────────────────────────

def _panel(cv, x1, y1, x2, y2, r=14):
    """Zaoblený poloprůhledný panel (stipple = ~65% krytí)."""
    kw = dict(fill=C_PANEL, outline=C_BORDER, width=1)
    # rohy
    for ax, ay, start in ((x1, y1, 90), (x2-2*r, y1, 0),
                           (x1, y2-2*r, 180), (x2-2*r, y2-2*r, 270)):
        cv.create_arc(ax, ay, ax+2*r, ay+2*r, start=start, extent=90,
                      style="pieslice", stipple="gray75", **kw)
    cv.create_rectangle(x1+r, y1, x2-r, y2, stipple="gray75", **kw)
    cv.create_rectangle(x1, y1+r, x2, y2-r, stipple="gray75", **kw)
    # vnější rámeček
    cv.create_line(x1+r, y1,   x2-r, y1,   fill=C_BORDER, width=1)
    cv.create_line(x1+r, y2,   x2-r, y2,   fill=C_BORDER, width=1)
    cv.create_line(x1,   y1+r, x1,   y2-r, fill=C_BORDER, width=1)
    cv.create_line(x2,   y1+r, x2,   y2-r, fill=C_BORDER, width=1)


def _title(cv, x, y, text, color=C_CYAN):
    cv.create_text(x, y, text=text, fill=color,
                   font=("Segoe UI", 17, "bold"), anchor="center")


def _sep(cv, y, W, pad=60):
    cv.create_line(pad, y, W-pad, y, fill=C_DIM, width=1)


# ─────────────────────────── MODUL 1 – ovládání myši ─────────────────────────

class MouseControlModule:
    title = "OVLÁDÁNÍ MYŠI"

    SHORTCUTS = [
        ("F1",       "zapnout / vypnout"),
        ("F3",       "přidat AR pin"),
        ("F4",       "smazat piny"),
        ("F5 / F6",  "předchozí / další modul"),
        ("F7",       "skrýt / zobrazit HUD"),
        ("F8/Pause", "zapnout / vypnout"),
        ("F9",       "reset kalibrace"),
        ("F10",      "konec"),
        ("Scroll",   "uvolnit / držet střed"),
    ]

    def draw(self, cv, W, H, st, cfg):
        cx = W // 2
        _panel(cv, 40, 20, W-40, H-20)

        # ── nadpis ──
        _title(cv, cx, 56, "◈  " + self.title + "  ◈")
        _sep(cv, 76, W)

        # ── stav ──
        enabled = getattr(st, "enabled", False)
        col   = C_GREEN if enabled else C_RED
        label = "●  AKTIVNÍ" if enabled else "○  VYPNUTO"
        cv.create_text(cx, 116, text=label, fill=col,
                       font=("Segoe UI", 32, "bold"), anchor="center")

        # ── gaze úhly ──
        ey = getattr(st, "ar_ey", 0.0)
        ep = getattr(st, "ar_ep", 0.0)
        cv.create_text(cx, 160,
                       text=f"H  {ey:+7.2f}°      V  {ep:+7.2f}°",
                       fill=C_TEXT, font=("Consolas", 14), anchor="center")

        # ── piny ──
        pins = getattr(st, "ar_pins", [])
        pin_txt = f"piny:  {len(pins)}"
        if pins:
            pin_txt += "   [ " + "  ".join(p[2] for p in pins) + " ]"
        cv.create_text(cx, 190, text=pin_txt,
                       fill=C_CYAN if pins else C_DIM,
                       font=("Segoe UI", 13), anchor="center")

        _sep(cv, 212, W)

        # ── zkratky ──
        col_k = cx - 30
        col_v = cx + 30
        y = 234
        for key, desc in self.SHORTCUTS:
            cv.create_text(col_k, y, text=key, fill=C_YELLOW,
                           font=("Consolas", 13, "bold"), anchor="e")
            cv.create_text(col_v, y, text=desc, fill=C_TEXT,
                           font=("Segoe UI", 13), anchor="w")
            y += 30

        # ── sens / FOV ──
        sens = getattr(cfg, "sens", "?")
        fov  = getattr(cfg, "ar_disp_fov", "?")
        _sep(cv, y+4, W)
        cv.create_text(cx, y+24, fill=C_DIM, anchor="center",
                       text=f"sens {sens:.1f}   disp FOV {fov}°",
                       font=("Consolas", 12))


# ─────────────────────────── MODUL 2 – info ──────────────────────────────────

class InfoModule:
    title = "INFO"

    def draw(self, cv, W, H, st, cfg):
        cx, cy = W // 2, H // 2
        _panel(cv, 40, 20, W-40, H-20)
        _title(cv, cx, 56, "◈  INFO  ◈")
        _sep(cv, 76, W)

        now = datetime.datetime.now()

        # ── velké hodiny ──
        cv.create_text(cx, cy - 30,
                       text=now.strftime("%H:%M:%S"),
                       fill=C_GREEN, font=("Segoe UI", 80, "bold"), anchor="center")

        # ── datum ──
        DNAMES = ["pondělí","úterý","středa","čtvrtek","pátek","sobota","neděle"]
        day = DNAMES[now.weekday()]
        cv.create_text(cx, cy + 60,
                       text=f"{day}  {now.day}. {now.month}. {now.year}",
                       fill=C_TEXT, font=("Segoe UI", 20), anchor="center")

        _sep(cv, cy + 96, W)

        # ── systémové info ──
        sens = getattr(cfg, "sens", "?")
        fov  = getattr(cfg, "ar_disp_fov", "?")
        hz   = getattr(st, "hz", 0.0)
        pins = len(getattr(st, "ar_pins", []))
        cv.create_text(cx, cy + 122, fill=C_DIM, anchor="center",
                       font=("Consolas", 13),
                       text=f"IMU {hz:.0f} Hz   sens {sens:.1f}   FOV {fov}°   piny {pins}")


# ─────────────────────────── MODUL 3 – klávesnice (stub) ─────────────────────

class KeyboardModule:
    title = "KLÁVESNICE"

    # Rozložení: 5 řádků, dwell = výběr
    ROWS = [
        list("QWERTYUIOP"),
        list("ASDFGHJKL"),
        list("ZXCVBNM") + ["⌫"],
        ["SPACE", ",", ".", "?", "!"],
    ]
    CELL_W = 96
    CELL_H = 72

    def __init__(self):
        self._buf = ""
        self._hover = None
        self._dwell_start = None
        self._dwell_key   = None
        self.DWELL_TIME   = 1.2   # s

    def draw(self, cv, W, H, st, cfg):
        cx = W // 2
        _panel(cv, 40, 20, W-40, H-20)
        _title(cv, cx, 50, "◈  KLÁVESNICE  ◈")

        # ── textový buffer ──
        buf_disp = self._buf[-28:] if len(self._buf) > 28 else self._buf
        cv.create_rectangle(60, 72, W-60, 112,
                            fill=C_DIM, outline=C_BORDER, width=1)
        cv.create_text(W-70, 92, text=buf_disp + "▌",
                       fill=C_WHITE, font=("Consolas", 16), anchor="e")

        # ── klávesy ──
        ey = getattr(st, "ar_ey", 0.0)
        ep = getattr(st, "ar_ep", 0.0)
        ppd = W / max(5.0, getattr(cfg, "ar_disp_fov", 50.0))
        gx = cx + ppd * (-ey)        # gaze pozice na obrazovce (world-locked inverzně)
        gy = H / 2 + ppd * (-ep)

        total_rows = len(self.ROWS)
        kb_h = total_rows * self.CELL_H
        y0 = H // 2 - kb_h // 2 + 30

        hovered = None
        for ri, row in enumerate(self.ROWS):
            row_w = sum(self.CELL_W * (2 if k == "SPACE" else 1) for k in row)
            x0 = cx - row_w // 2
            y  = y0 + ri * self.CELL_H
            for key in row:
                kw = self.CELL_W * 2 if key == "SPACE" else self.CELL_W
                x1, y1, x2, y2 = x0, y, x0+kw-4, y+self.CELL_H-4
                inside = x1 <= gx <= x2 and y1 <= gy <= y2
                if inside:
                    hovered = key
                bg  = C_CYAN if inside else C_DIM
                fg  = C_PANEL if inside else C_TEXT
                cv.create_rectangle(x1, y1, x2, y2, fill=bg,
                                    outline=C_BORDER, width=1)
                cv.create_text((x1+x2)//2, (y1+y2)//2, text=key,
                               fill=fg, font=("Segoe UI", 14, "bold"),
                               anchor="center")
                x0 += kw

        # ── dwell progress ──
        now = time.time()
        if hovered and hovered == self._dwell_key:
            if self._dwell_start is None:
                self._dwell_start = now
            elapsed = now - self._dwell_start
            prog = min(elapsed / self.DWELL_TIME, 1.0)
            bw = int((W - 120) * prog)
            cv.create_rectangle(60, H-50, 60+bw, H-30,
                                fill=C_GREEN, outline="")
            if prog >= 1.0:
                self._commit(hovered)
                self._dwell_start = now   # reset
        else:
            self._dwell_key   = hovered
            self._dwell_start = None

        # ── ukazatel gazel (crosshair) ──
        r = 8
        cv.create_line(gx-r-4, gy, gx+r+4, gy, fill=C_YELLOW, width=1)
        cv.create_line(gx, gy-r-4, gx, gy+r+4, fill=C_YELLOW, width=1)
        cv.create_oval(gx-r, gy-r, gx+r, gy+r,
                       outline=C_YELLOW, width=2)

    def _commit(self, key):
        if key == "⌫":
            self._buf = self._buf[:-1]
        elif key == "SPACE":
            self._buf += " "
        else:
            self._buf += key


# ─────────────────────────── HudOverlay ──────────────────────────────────────

MODULES = [MouseControlModule(), InfoModule(), KeyboardModule()]


class HudOverlay:
    """Průhledné HUD okno přes celý displej brýlí – zobrazuje aktivní modul."""

    def __init__(self, root, cfg, state_getter):
        import tkinter as tk
        self.tk = tk
        self.root = root
        self.cfg = cfg
        self.state_getter = state_getter
        self.alive   = True
        self.visible = True
        self.mod_idx = 0
        self._prev   = {VK_F5: False, VK_F6: False, VK_F7: False}
        self._geom   = None

        win = self.win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=KEY)
        try:
            win.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        self.cv = tk.Canvas(win, bg=KEY, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self._fit()
        win.update_idletasks()
        winapi.make_overlay_window(win)
        self.tick()

    # ── geometrie ─────────────────────────────────────────────────────────────
    def _fit(self):
        w = self.win.winfo_screenwidth()
        h = self.win.winfo_screenheight()
        g = (w, h)
        if g != self._geom:
            self._geom = g
            self.win.geometry(f"{w}x{h}+0+0")
            self.cv.config(width=w, height=h)
        return w, h

    def close(self):
        self.alive = False
        try:
            self.win.destroy()
        except Exception:
            pass

    # ── klávesy ───────────────────────────────────────────────────────────────
    def _keys(self):
        cur5 = winapi.key_state(VK_F5)[0]
        cur6 = winapi.key_state(VK_F6)[0]
        cur7 = winapi.key_state(VK_F7)[0]
        if cur5 and not self._prev[VK_F5]:
            self.mod_idx = (self.mod_idx - 1) % len(MODULES)
        if cur6 and not self._prev[VK_F6]:
            self.mod_idx = (self.mod_idx + 1) % len(MODULES)
        if cur7 and not self._prev[VK_F7]:
            self.visible = not self.visible
        self._prev[VK_F5] = cur5
        self._prev[VK_F6] = cur6
        self._prev[VK_F7] = cur7

    # ── tick ──────────────────────────────────────────────────────────────────
    def tick(self):
        if not self.alive:
            return
        self._keys()
        W, H = self._fit()
        cv = self.cv
        cv.delete("all")

        if self.visible:
            st  = self.state_getter()
            cfg = self.cfg
            mod = MODULES[self.mod_idx]
            mod.draw(cv, W, H, st, cfg)

            # ── spodní lišta – přepínání modulů ──────────────────────────────
            pad    = 40
            n      = len(MODULES)
            slot_w = (W - 2 * pad) // n
            bar_y  = H - 18
            for i, m in enumerate(MODULES):
                x = pad + i * slot_w + slot_w // 2
                if i == self.mod_idx:
                    cv.create_rectangle(x - slot_w//2 + 6, bar_y - 10,
                                        x + slot_w//2 - 6, bar_y + 2,
                                        fill=C_CYAN, outline="")
                    col = C_PANEL
                else:
                    col = C_DIM
                cv.create_text(x, bar_y - 4, text=m.title,
                               fill=col, font=("Segoe UI", 11, "bold"),
                               anchor="center")

            # F5 / F6 nápověda mini
            cv.create_text(W - 8, bar_y - 4,
                           text="◀F5  F6▶",
                           fill=C_DIM, font=("Segoe UI", 10),
                           anchor="e")

        # Klávesnicový modul potřebuje rychlejší refresh (gaze tracking)
        delay = 50 if (self.visible and self.mod_idx == 2) else 200
        self.win.after(delay, self.tick)
