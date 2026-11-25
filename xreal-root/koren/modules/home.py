"""Modul: Domov, prostorové prostředí v brýlích ovládané pohledem.

Dlaždice visí v prostoru kolem uživatele (ukotvené vůči místnosti podle IMU): otočením
hlavy se mezi nimi rozhlížíš, uprostřed pohledu je zaměřovač. Dlaždici vybereš tak, že
na ni chvíli koukáš (kroužek se naplní), nebo krátkým stiskem tlačítka brýlí.

Pozadí je černé: v brýlích XREAL je černá průhledná, takže Domov visí ve skutečném
prostoru a nezakrývá okolí. Zavřené víko Domov otevře samo; dlaždice „Windows“ ho zavře
a vrátí na plochu s myší hlavou. Zpět se dostaneš přes menu (podržet tlačítko brýlí).

Další moduly přidávají vlastní dlaždice přes `Module.home_tiles()`.
"""
import math
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, List, Optional

from . import Module
from ..menu import MenuItem

BG = "#000000"                      # v brýlích průhledné
C_TILE = "#0d2233"
C_TILE_SEL = "#174866"
C_EDGE = "#2a5674"
C_EDGE_SEL = "#7fd8be"
C_TEXT = "#d4e8f4"
C_DIM = "#7395aa"
C_RING = "#7fd8be"
C_OFF = "#3c5566"
FONT = "Segoe UI"


@dataclass
class Tile:
    id: str
    title: str
    subtitle: str = ""
    icon: str = "●"
    action: Optional[Callable[[], None]] = None
    close_home: bool = True          # po spuštění zavřít Domov (aplikace se má ukázat)
    confirm: str = ""                # neprázdné = nejdřív otázka ano/ne
    enabled: bool = True


def layout(n: int, cols: int, spacing: float, rows_pitch=(6.0, -16.0)):
    """Rozmístí n dlaždic do řádků kolem středu: [(yaw°, pitch°)], yaw + = doprava."""
    out = []
    for i in range(n):
        r, c = divmod(i, cols)
        in_row = min(cols, n - r * cols)
        yaw = (c - (in_row - 1) / 2.0) * spacing
        pitch = rows_pitch[r] if r < len(rows_pitch) else rows_pitch[-1] - 22.0 * (r - len(rows_pitch) + 1)
        out.append((yaw, pitch))
    return out


def hit(tiles_pos, w_deg: float, h_deg: float, yaw: float, pitch: float) -> int:
    """Index dlaždice pod středem pohledu, nebo -1."""
    for i, (ty, tp) in enumerate(tiles_pos):
        if abs(ty - yaw) <= w_deg / 2 and abs(tp - pitch) <= h_deg / 2:
            return i
    return -1


class Home(Module):
    id = "home"
    name = "Domov v brýlích"
    description = "Prostorová domovská obrazovka: dlaždice kolem hlavy, výběr pohledem."
    z = 10                           # pod ostatními moduly, kořenové menu a nápověda jsou nad ním
    needs = ("imu", "display")

    FOV = 50.0                       # vodorovné zorné pole displeje brýlí (°), One Pro ~57° úhlopříčka
    TILE_W, TILE_H, SPACING, COLS = 10.5, 8.5, 12.5, 5     # ° (dlaždice ~ 1/5 šířky displeje)
    ROWS = (5.5, -5.5)
    DWELL_S = 1.6

    def start(self, ctx):
        super().start(ctx)
        self.open = False
        self.L = None
        self.items = {}
        self.tiles: List[Tile] = []
        self.pos = []
        self.yaw0 = self.pitch0 = 0.0
        self.t_lost = None           # od kdy není vidět žádná dlaždice
        self.sel = -1
        self.progress = 0.0
        self._t = time.monotonic()
        self._sig = None
        self.world_locked = True     # brýle v režimu „sleduj hlavu“: kotví software

    def stop(self):
        self.close()

    # ------------------------------------------------------------------ otevření
    def captures_input(self):
        return self.open

    def on_mode(self, prev, mode):
        if mode == "BAG":
            self.show()
        elif mode in ("DESK", "SUSPEND"):
            self.close()

    def show(self):
        if self.open:
            return
        if self.L is None:
            self.L = self.ctx.layer(self.z)
            try:
                self.L.win.attributes("-alpha", 1.0)
            except Exception:
                pass
        self.open = True
        self._build_tiles()
        self.recenter()
        self._sig = None
        self.L.show(True)
        self.ctx.log("[HOME] otevřen")

    def close(self):
        if not self.open:
            return
        self.open = False
        if self.L is not None:
            self.L.show(False)
            self.L.cv.delete("all")
            self.items = {}
        self.ctx.log("[HOME] zavřen")

    def recenter(self):
        """Domov před sebe: střed = kam se teď díváš (vodorovně i svisle)."""
        h = self.ctx.head()
        self.yaw0, self.pitch0 = (h[0], h[1]) if h else (0.0, 0.0)
        self.t_lost = None

    # ------------------------------------------------------------------ dlaždice
    def _build_tiles(self):
        app = self.ctx.app()
        base = [
            Tile("windows", "Windows", "plocha, myš hlavou", "⊞", self._to_windows),
            Tile("osk", "Klávesnice", "na obrazovce", "⌨", lambda: self._start("osk.exe")),
            Tile("browser", "Prohlížeč", "web", "◎", lambda: self._open("https://www.google.com")),
            Tile("camera", "Kamera Eye", "zapnout a ukázat", "◉", self._camera,
                 enabled=os.path.isfile(self._eye_script())),
            Tile("files", "Soubory", "Průzkumník", "▤", lambda: self._start("explorer.exe")),
        ]
        extra = []
        for t in app.mods.home_tiles():
            if isinstance(t, Tile):
                extra.append(t)
        tail = [
            Tile("center", "Vycentrovat", "Domov před sebe", "◇", self.recenter, close_home=False),
            Tile("settings", "Nastavení", "kořen a monitor", "⚙", app.settings.show),
            Tile("tray", "Režim Tray", "vše vypnout", "▾", lambda: app.set_profile("tray")),
            Tile("hib", "Hibernovat", "uložit a vypnout", "☾", lambda: app.q.put(("hibernate_now",)),
                 confirm="Opravdu hibernovat notebook?"),
        ]
        self.tiles = base + extra + tail
        self.pos = layout(len(self.tiles), self.COLS, self.SPACING, self.ROWS)

    def home_tiles(self):
        return []

    def menu_items(self):
        if self.open:
            return [MenuItem("home_close", "Zavřít Domov", "zpět na Windows", action=self._to_windows)]
        return [MenuItem("home_open", "Domov (prostředí v brýlích)", "dlaždice pohledem", action=self.show)]

    def _activate(self, i: int):
        t = self.tiles[i]
        if not t.enabled:
            return
        self.ctx.log(f"[HOME] {t.id}")
        if t.confirm:
            self.ctx.ask(t.confirm, on_yes=t.action)
            return
        if t.close_home:
            self.close()
        try:
            if t.action:
                t.action()
        except Exception as e:
            self.ctx.log(f"[HOME] {t.id}: {type(e).__name__}: {e}")
            self.ctx.toast(f"{t.title}: nepovedlo se")
            return
        self.ctx.toast("✓ " + t.title, 1.2)

    def _to_windows(self):
        self.close()
        app = self.ctx.app()
        hm = app.mods.get("head_mouse")
        if hm is not None and self.ctx.mode() == "BAG":
            app.mods.call(hm, "set_on", True)

    def _start(self, exe: str):
        if self.ctx.dry:
            self.ctx.log(f"[HOME] (dry) spustil bych {exe}")
            return
        os.startfile(exe)

    def _open(self, url: str):
        if self.ctx.dry:
            self.ctx.log(f"[HOME] (dry) otevřel bych {url}")
            return
        os.startfile(url)

    @staticmethod
    def _eye_script():
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.normpath(os.path.join(here, "..", "..", "..", "xreal-eye-rgb", "eye_rgb_enable.py"))

    def _camera(self):
        """Zapne kameru Eye (xreal-eye-rgb) a otevře aplikaci Kamera ve Windows."""
        if self.ctx.dry:
            self.ctx.log("[HOME] (dry) zapnul bych kameru Eye")
            return
        import sys
        subprocess.Popen([sys.executable, self._eye_script()], cwd=os.path.dirname(self._eye_script()),
                         creationflags=0x08000000)
        os.startfile("microsoft.windows.camera:")

    # ------------------------------------------------------------------ vstup
    def on_press(self, t):
        if self.open and self.sel >= 0:
            self._activate(self.sel)

    def on_gesture(self, g):
        if self.open and g == "shake" and self.sel >= 0:
            self.progress = 0.0              # zavrtění = zrušit rozběhnutý výběr

    # ------------------------------------------------------------------ kreslení
    def tick(self, now):
        dt, self._t = now - self._t, now
        if not self.open or self.L is None:
            return
        h = self.ctx.head()
        W, H = self.L.W, self.L.H
        ppd = W / self.FOV
        if h is None:
            yaw, pitch, live = 0.0, 0.0, False
        else:
            yaw = (h[0] - self.yaw0) if self.world_locked else 0.0
            pitch, live = h[1] - self.pitch0, True
        # výběr pohledem
        sel = hit(self.pos, self.TILE_W, self.TILE_H, yaw, pitch) if live else -1
        if sel != self.sel:
            self.sel, self.progress = sel, 0.0
        elif sel >= 0 and self.tiles[sel].enabled:
            self.progress += dt / self.DWELL_S
            if self.progress >= 1.0:
                self.progress = 0.0
                self._activate(sel)
                if not self.open:
                    return
        visible = self._draw(W, H, ppd, yaw, pitch, live)
        # ztracený Domov (brýle se nasadily až po otevření, drift gyra): po 3 s ho přivést před sebe
        if live and not visible:
            if self.t_lost is None:
                self.t_lost = now
            elif now - self.t_lost > 3.0:
                self.recenter()
                self.ctx.toast("Domov je před tebou", 1.5)
        else:
            self.t_lost = None

    def _font(self, px, bold=False):
        return (FONT, -max(8, int(px)), "bold" if bold else "normal")

    def _draw(self, W, H, ppd, yaw, pitch, live):
        cv = self.L.cv
        sig = (W, H, len(self.tiles))
        if sig != self._sig:
            self._sig = sig
            cv.delete("all")
            self.items = {}
            cv.create_rectangle(0, 0, W, H, fill=BG, outline="")
            for i, t in enumerate(self.tiles):
                tw, th = self.TILE_W * ppd, self.TILE_H * ppd
                self.items[i] = (
                    cv.create_rectangle(0, 0, tw, th, fill=C_TILE, outline=C_EDGE, width=3),
                    cv.create_text(0, 0, text=t.icon, fill=C_TEXT if t.enabled else C_OFF,
                                   font=self._font(th * 0.30)),
                    cv.create_text(0, 0, text=t.title, fill=C_TEXT if t.enabled else C_OFF,
                                   font=self._font(th * 0.14, True)),
                    cv.create_text(0, 0, text=t.subtitle if t.enabled else "nedostupné", fill=C_DIM,
                                   font=self._font(th * 0.09)),
                    cv.create_arc(0, 0, 1, 1, start=90, extent=0, style="arc", outline=C_RING,
                                  width=max(3, int(th * 0.03))),
                )
            self.r_ret = cv.create_oval(0, 0, 1, 1, outline="#9fdcff", width=2)
            self.r_dot = cv.create_oval(0, 0, 1, 1, fill="#9fdcff", outline="")
            self.r_hint = cv.create_text(W / 2, H - 0.05 * H, text="", fill=C_DIM, font=self._font(H * 0.022))
            self.r_top = cv.create_text(W / 2, 0.045 * H, text="", fill=C_DIM, font=self._font(H * 0.024))
            self.r_arrow = cv.create_text(0, 0, text="", fill=C_EDGE_SEL, font=self._font(H * 0.06, True))
        cx, cy = W / 2, H / 2
        tw, th = self.TILE_W * ppd, self.TILE_H * ppd
        any_visible = False
        for i, (ty, tp) in enumerate(self.pos):
            x = cx + (ty - yaw) * ppd
            y = cy - (tp - pitch) * ppd
            rect, icon, title, sub, arc = self.items[i]
            sel = i == self.sel
            cv.coords(rect, x - tw / 2, y - th / 2, x + tw / 2, y + th / 2)
            cv.itemconfig(rect, fill=C_TILE_SEL if sel else C_TILE, outline=C_EDGE_SEL if sel else C_EDGE)
            cv.coords(icon, x, y - th * 0.16)
            cv.coords(title, x, y + th * 0.13)
            cv.coords(sub, x, y + th * 0.30)
            r = th * 0.11
            ax, ay = x + tw / 2 - r * 1.6, y - th / 2 + r * 1.6
            cv.coords(arc, ax - r, ay - r, ax + r, ay + r)
            cv.itemconfig(arc, extent=-359.9 * self.progress if sel and self.progress > 0.02 else 0)
            if -tw / 2 < x < W + tw / 2 and -th / 2 < y < H + th / 2:
                any_visible = True
        # zaměřovač uprostřed pohledu
        rr = H * 0.018
        cv.coords(self.r_ret, cx - rr, cy - rr, cx + rr, cy + rr)
        cv.coords(self.r_dot, cx - 3, cy - 3, cx + 3, cy + 3)
        # šipka k Domovu, když není nic vidět
        if not any_visible and self.pos:
            ty = sum(p[0] for p in self.pos) / len(self.pos)
            tp = sum(p[1] for p in self.pos) / len(self.pos)
            ang = math.atan2(-(tp - pitch), ty - yaw)
            ax = cx + math.cos(ang) * W * 0.40
            ay = cy + math.sin(ang) * H * 0.38
            cv.coords(self.r_arrow, ax, ay)
            cv.itemconfig(self.r_arrow, text="➜" if abs(math.cos(ang)) > 0.5 and math.cos(ang) > 0 else
                          "⬅" if abs(math.cos(ang)) > 0.5 else ("⬇" if math.sin(ang) > 0 else "⬆"))
        else:
            cv.itemconfig(self.r_arrow, text="")
        if not live:
            hint = "Brýle neposílají pohyb hlavy. Chvíli počkej, nebo podrž tlačítko brýlí pro menu."
        elif self.sel >= 0:
            t = self.tiles[self.sel]
            hint = (f"{t.title}: drž pohled nebo stiskni tlačítko" if t.enabled else f"{t.title}: zatím nedostupné")
        else:
            hint = "Otoč hlavu na dlaždici  ·  podrž tlačítko brýlí = menu"
        cv.itemconfig(self.r_hint, text=hint)
        cv.itemconfig(self.r_top, text=f"Domov  ·  {time.strftime('%H:%M')}")
        return any_visible


MODULE = Home
