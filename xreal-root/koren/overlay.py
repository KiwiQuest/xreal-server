"""Průhledné vrstvy přes displej brýlí (tkinter, bez knihoven navíc).

Každá vrstva je samostatné okno přes celý monitor: průhledné pozadí, nechytá kliky ani
fokus, vždy navrchu. Vrstvy se řadí podle `z`, kořenová nápověda (z = 1000) je nejvýš.
Kreslí se jen při změně obsahu, ať overlay nezatěžuje notebooky s integrovanou grafikou.

Nápověda je nenásilná: po změně stavu ukáže klidný panel s možnostmi, po `hint_seconds`
se stáhne do jednoho řádku dole. Znovu se rozbalí při změně, otázce nebo když rádce pozná,
že ovládání nejde.
"""
import time
from typing import Dict, List, Optional

from .config import use_suite

use_suite()
from xreal import winapi  # noqa: E402

KEY = "#010101"            # průhledná barva
C_PANEL = "#0b1a26"
C_BORDER = "#24475f"
C_TEXT = "#c4dcea"
C_DIM = "#6d8ea3"
C_TITLE = {"calm": "#7fd8be", "info": "#7cc4ff", "warn": "#ffcf6b"}
C_SEL = "#173a52"
C_RING = "#7fd8be"
FONT = "Segoe UI"


class Layer:
    def __init__(self, root, lid: str, z: int, alpha: float):
        import tkinter as tk
        self.id, self.z = lid, z
        w = self.win = tk.Toplevel(root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=KEY)
        try:
            w.attributes("-transparentcolor", KEY)
            w.attributes("-alpha", alpha)
        except tk.TclError:
            pass
        self.cv = tk.Canvas(w, bg=KEY, highlightthickness=0, bd=0)
        self.cv.pack(fill="both", expand=True)
        self.rect = (0, 0, 800, 600)
        w.update_idletasks()
        winapi.make_overlay_window(w)
        self.visible = True
        self.sig = None

    @property
    def W(self):
        return self.rect[2]

    @property
    def H(self):
        return self.rect[3]

    def place(self, m: dict):
        r = (m["x"], m["y"], m["w"], m["h"])
        if r != self.rect:
            self.rect = r
            self.win.geometry(f"{r[2]}x{r[3]}+{r[0]}+{r[1]}")
            self.cv.config(width=r[2], height=r[3])
            self.sig = None

    def show(self, on: bool):
        if on != self.visible:
            self.visible = on
            (self.win.deiconify if on else self.win.withdraw)()
            if on:
                self.sig = None

    def draw(self, sig, fn):
        """Překreslí jen když se změnil podpis obsahu."""
        if sig == self.sig:
            return
        self.sig = sig
        self.cv.delete("all")
        fn(self.cv)

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass


class Overlay:
    def __init__(self, root, cfg, log=print):
        self.root, self.cfg, self.log = root, cfg, log
        self.layers: Dict[str, Layer] = {}
        self.monitor = None
        self.t_change = 0.0
        self._last_key = None
        self._toast = ("", 0.0)
        self.help = self.layer("koren", 1000)

    # ----------------------------------------------------------------- vrstvy
    def layer(self, lid: str, z: int = 100) -> Layer:
        if lid not in self.layers:
            self.layers[lid] = Layer(self.root, lid, z, self.cfg.overlay_alpha)
            if self.monitor:
                self.layers[lid].place(self.monitor)
            self._restack()
        return self.layers[lid]

    def _restack(self):
        for l in sorted(self.layers.values(), key=lambda l: l.z):
            try:
                l.win.lift()
            except Exception:
                pass

    def set_monitor(self, m: Optional[dict]):
        if not m:
            return
        self.monitor = m
        for l in self.layers.values():
            l.place(m)
        self._restack()

    def toast(self, text: str, seconds: float = 2.0):
        self._toast = (text, time.monotonic() + seconds)

    # ------------------------------------------------------------ kořenový panel
    def render(self, g, menu=None, prompt=None, struggle: int = 0, hold: float = 0.0,
               hold_total: float = 2.0, extra_lines: List[str] = ()):
        """g = advisor.Guidance. Volá se ~20x za sekundu, kreslí jen při změně."""
        now = time.monotonic()
        key = (g.title, g.text, tuple(g.options), g.tone, bool(menu), bool(prompt), struggle)
        if key != self._last_key:
            self._last_key, self.t_change = key, now
        expanded = (menu is not None or prompt is not None or struggle >= 1 or g.tone == "warn"
                    or now - self.t_change < self.cfg.hint_seconds)
        toast = self._toast[0] if now < self._toast[1] else ""
        visible = g.show or menu is not None or prompt is not None or bool(toast) or hold > 0
        L = self.help
        L.show(visible)
        if not visible:
            return
        prog = None if g.progress is None else round(g.progress, 3)
        msig = None
        if menu is not None:
            msig = (menu.title, menu.index, round(menu.progress, 2),
                    tuple((i.label, i.enabled) for i in menu.items))
        psig = None if prompt is None else (prompt.question, prompt.repeats)
        sig = (L.rect, key, expanded, prog, msig, psig, toast, round(hold / max(0.1, hold_total), 2),
               tuple(g.warnings), tuple(extra_lines))
        L.draw(sig, lambda cv: self._paint(cv, L.W, L.H, g, expanded, menu, prompt, toast,
                                           hold / max(0.1, hold_total), extra_lines))

    def _paint(self, cv, W, H, g, expanded, menu, prompt, toast, hold, extra):
        s = self.cfg.overlay_scale * max(0.6, min(1.6, H / 1080.0))
        f = lambda n, b=False: (FONT, int(n * s), "bold" if b else "normal")
        title_c = C_TITLE.get(g.tone, C_TITLE["calm"])
        cx = W // 2
        pad = int(26 * s)

        if menu is not None:
            self._paint_menu(cv, W, H, menu, f, s)
        elif prompt is not None:
            pw = int(min(W - 80, 860 * s))
            fl = _Flow(cv, cx - pw // 2, int(H * 0.30), pw, pad)
            fl.text(prompt.question, C_TEXT, f(26, True), center=True)
            fl.gap(10 * s)
            simple = prompt.repeats > 0
            fl.text("Kývni = ANO   ·   Zavrť hlavou = NE" if not simple
                    else "Jen kývni hlavou, když ANO. Když nic neuděláš, znamená to NE.",
                    C_TITLE["info"], f(20), center=True)
            fl.text("krátký stisk tlačítka brýlí = ANO", C_DIM, f(15), center=True)
            fl.panel()

        if expanded and menu is None:
            pw = int(min(W - 80, 1100 * s))
            fl = _Flow(cv, cx - pw // 2, 0, pw, pad)
            fl.text(g.title, title_c, f(22, True))
            fl.text(g.text, C_TEXT, f(18))
            for ln in list(g.warnings) + list(extra):
                fl.text(ln, C_TITLE["warn"], f(16))
            if g.progress is not None:
                fl.bar(g.progress, title_c, 10 * s)
            if g.options:
                fl.gap(6 * s)
                for how, what in g.options:
                    fl.pair(how, what, C_DIM, C_TEXT, f(16))
            fl.panel(bottom=H - int(50 * s))
        elif menu is None:
            # stažený tvar: jeden řádek dole, ať nepřekáží
            txt = g.title if not g.options else f"{g.title}   ·   {g.options[0][0]} → {g.options[0][1]}"
            cv.create_text(cx, H - 34 * s, text=txt, fill=C_DIM, font=f(14))

        if hold > 0:                                   # ukazatel podržení tlačítka
            r = 34 * s
            cv.create_oval(cx - r, 60 * s, cx + r, 60 * s + 2 * r, outline="#2b4256", width=int(6 * s))
            cv.create_arc(cx - r, 60 * s, cx + r, 60 * s + 2 * r, start=90, extent=-359.9 * min(1.0, hold),
                          style="arc", outline=C_RING, width=int(6 * s))
            cv.create_text(cx, 60 * s + 2 * r + 24 * s, text="drž pro menu", fill=C_DIM, font=f(14))
        if toast:
            cv.create_text(cx, int(H * 0.22), text=toast, fill=C_TITLE["calm"], font=f(24, True))

    def _paint_menu(self, cv, W, H, menu, f, s):
        pw = int(min(W - 80, 780 * s))
        pad = int(22 * s)
        cx = W // 2
        fl = _Flow(cv, cx - pw // 2, 0, pw, pad)
        fl.text(menu.title, C_TITLE["calm"], f(24, True), center=True)
        fl.gap(8 * s)
        for i, it in enumerate(menu.items):
            sel = i == menu.index
            y0 = fl.y
            fl.gap(8 * s)
            fl.text(it.label, C_TEXT if it.enabled else C_DIM, f(21 if sel else 19, sel), indent=int(24 * s))
            fl.gap(8 * s)
            if sel:
                fl.under(y0, fl.y, C_SEL)
                cy = (y0 + fl.y) / 2
                if it.hint:
                    fl.cv.create_text(fl.x + pw - 70 * s, cy, text=it.hint, fill=C_DIM, font=f(14), anchor="e", tags=fl.tag)
                if it.enabled:
                    r = 16 * s
                    ccx = fl.x + pw - 38 * s
                    cv.create_oval(ccx - r, cy - r, ccx + r, cy + r, outline="#2b4256", width=int(5 * s), tags=fl.tag)
                    if menu.progress > 0.02:
                        cv.create_arc(ccx - r, cy - r, ccx + r, cy + r, start=90,
                                      extent=-359.9 * min(1.0, menu.progress), style="arc",
                                      outline=C_RING, width=int(5 * s), tags=fl.tag)
        fl.gap(10 * s)
        fl.text("hlava nahoru / dolů = výběr  ·  drž pohled nebo stiskni tlačítko = potvrdit  ·  zavrť = zavřít",
                C_DIM, f(14), center=True)
        fl.panel(center_y=H // 2)

    def destroy(self):
        for l in self.layers.values():
            l.destroy()


class _Flow:
    """Skládá řádky shora dolů podle skutečné velikosti textu (bbox), nakonec pod ně dá panel
    a celý blok posune na požadované místo. Nic se nepřekrývá ani při jiném DPI."""

    def __init__(self, cv, x, y, w, pad):
        self.cv, self.x, self.w, self.pad = cv, x, w, pad
        self.top = y
        self.y = y + pad
        self.tag = f"flow{id(self)}"

    def text(self, t, color, font, center=False, indent=0):
        if center:
            i = self.cv.create_text(self.x + self.w / 2, self.y, text=t, fill=color, font=font, anchor="n",
                                    width=self.w - 2 * self.pad, justify="center", tags=self.tag)
        else:
            i = self.cv.create_text(self.x + self.pad + indent, self.y, text=t, fill=color, font=font,
                                    anchor="nw", width=self.w - 2 * self.pad - indent, tags=self.tag)
        bb = self.cv.bbox(i)
        self.y = (bb[3] if bb else self.y + 20) + 4

    def pair(self, how, what, c_how, c_what, font):
        i = self.cv.create_text(self.x + self.pad, self.y, text=how, fill=c_how, font=font, anchor="nw",
                                tags=self.tag)
        bb = self.cv.bbox(i)
        x2 = max(bb[2] if bb else 0, self.x + self.pad + int(self.w * 0.34))
        j = self.cv.create_text(x2 + 16, self.y, text="→  " + what, fill=c_what, font=font, anchor="nw",
                                width=self.x + self.w - self.pad - x2 - 16, tags=self.tag)
        bb2 = self.cv.bbox(j)
        self.y = max(bb[3] if bb else 0, bb2[3] if bb2 else 0) + 4

    def bar(self, p, color, h):
        x1, x2 = self.x + self.pad, self.x + self.w - self.pad
        self.y += 6
        self.cv.create_rectangle(x1, self.y, x2, self.y + h, fill="#1d3344", outline="", tags=self.tag)
        self.cv.create_rectangle(x1, self.y, x1 + (x2 - x1) * max(0.0, min(1.0, p)), self.y + h,
                                 fill=color, outline="", tags=self.tag)
        self.y += h + 8

    def gap(self, h):
        self.y += h

    def under(self, y1, y2, color):
        r = self.cv.create_rectangle(self.x + 10, y1, self.x + self.w - 10, y2, fill=color, outline=C_BORDER,
                                     tags=self.tag)
        self.cv.tag_lower(r)

    def panel(self, bottom=None, center_y=None):
        h = self.y - self.top + self.pad
        r = self.cv.create_rectangle(self.x, self.top, self.x + self.w, self.top + h, fill=C_PANEL,
                                     outline=C_BORDER, width=2, tags=self.tag)
        self.cv.tag_lower(r)
        dy = 0
        if bottom is not None:
            dy = bottom - (self.top + h)
        elif center_y is not None:
            dy = center_y - (self.top + h / 2)
        if dy:
            self.cv.move(self.tag, 0, dy)
