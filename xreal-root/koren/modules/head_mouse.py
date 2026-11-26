"""Modul: myš hlavou z hotového XREAL Head Mouse 2.0 (Xreal mouse/xreal_suite).

Bere engine beze změny (ukazování, circle click, tlačítko brýlí jako myš), jen ho krmí
daty z kořene místo vlastního spojení, takže se s kořenem nepřetahuje o porty brýlí.
Nastavení sdílí s Head Mouse (`xreal_config.json` v jeho složce).

V režimu "notebook v tašce" se zapne sám. Když si kořen bere vstup (menu, otázka),
myš se pozastaví a pustí případně držené tlačítko.
"""
import ctypes
import sys
import time

from ..config import use_suite
from ..pointer_curve import PRESET_LABELS, PRESETS, CurvePointer
from ..menu import MenuItem
from . import Module

use_suite()
from xreal.config import Config as MouseConfig, Runtime  # noqa: E402
from xreal.engine import Engine, NullOutput, WinOutput   # noqa: E402


class HeadMouse(Module):
    id = "head_mouse"
    name = "Myš hlavou"
    description = "Kurzor podle pohybu hlavy, klik zastavením pohledu, tlačítko brýlí jako myš."
    z = 200
    needs = ("imu",)

    def start(self, ctx):
        super().start(ctx)
        if getattr(self, "engine", None) is not None:      # znovu zapnutý modul: engine i kroužek už jsou
            self.rt.running = True
            self.paused = False
            return
        self.mcfg = MouseConfig().load()
        self.rt = Runtime()
        self.engine = Engine(self.mcfg, self.rt, io=NullOutput() if ctx.dry else WinOutput(),
                             use_hotkeys=False, monitor_fn=ctx.monitor)   # monitor brýlí určuje kořen
        self.engine.pointer = CurvePointer(self.mcfg, ctx.cfg)   # zrychlení podle rychlosti hlavy
        self.engine.refresh_monitor()
        self._clipped = False
        self.paused = False
        self.user_wants = False          # stav u stolu (zapnul si ji uživatel sám?)
        self._t_mon = 0.0
        self.ring = None
        try:
            from xreal.overlay import RingOverlay
            self.ring = RingOverlay(ctx.tk, self.mcfg, self.rt)
        except Exception as e:
            ctx.log(f"[MOUSE] kroužek circle click nejde zobrazit: {e}")

    def stop(self):
        self._clip(None)
        self.rt.enabled = False
        self.engine.buttons.abort()
        if self.ring and self.ring.visible:
            self.ring.win.withdraw()
            self.ring.visible = False

    @property
    def on(self) -> bool:
        return self.rt.enabled

    def set_on(self, on: bool):
        if on != self.rt.enabled:
            self.engine.buttons.abort()
            self.rt.enabled = on

    # ----------------------------------------------------------------- události
    def on_mode(self, prev, mode):
        if mode == "BAG":
            if prev == "DESK":
                self.user_wants = self.rt.enabled
            self.set_on(True)
        elif mode == "DESK":
            self.set_on(self.user_wants)
        else:
            self.set_on(False)

    def on_imu(self, s):
        if not self.paused:
            self.engine.process(s)

    def on_button(self, down, t):
        if self.paused or not self.rt.enabled:
            return
        now = time.time()
        (self.engine.buttons.down if down else self.engine.buttons.up)(now)

    def on_focus(self, root_has_focus):
        self.paused = root_has_focus
        if root_has_focus:
            self.engine.buttons.abort()
            self.engine.dwell.reset()

    # ------------------------------------------------------------- rychlost a okraje
    def _want_clip(self) -> bool:
        app = self.ctx.app()
        return (self.ctx.cfg.mouse_clip and self.rt.enabled and not self.paused and not self.ctx.dry
                and (self.ctx.mode() == "BAG" or app.profile_eff == "glasses"))

    def _clip(self, m):
        """ClipCursor na monitor brýlí: kurzor neuteče na zhasnutý displej notebooku a okraje
        a rohy obrazovky (Start, zavírací křížky) jsou „nekonečně velké“ cíle."""
        if sys.platform != "win32":
            return
        u = ctypes.WinDLL("user32")
        if m is None:
            if self._clipped:
                u.ClipCursor(None)
                self._clipped = False
            return

        class RECT(ctypes.Structure):
            _fields_ = [("l", ctypes.c_long), ("t", ctypes.c_long), ("r", ctypes.c_long), ("b", ctypes.c_long)]
        u.ClipCursor(ctypes.byref(RECT(m["x"], m["y"], m["x"] + m["w"], m["y"] + m["h"])))
        self._clipped = True

    def set_speed(self, speed=None, precision=None, preset=None):
        c = self.ctx.cfg
        if preset in PRESETS:
            speed, precision = PRESETS[preset]
        if speed is not None:
            c.mouse_speed = round(min(max(speed, 0.3), 3.0), 2)
        if precision is not None:
            c.mouse_precision = round(min(max(precision, 0.0), 1.0), 2)
        c.save()
        self.ctx.toast(f"Myš: rychlost {c.mouse_speed:.1f}×, přesnost {int(c.mouse_precision * 100)} %", 1.5)

    def preset_name(self) -> str:
        c = self.ctx.cfg
        for k, (sp, pr) in PRESETS.items():
            if abs(sp - c.mouse_speed) < 0.01 and abs(pr - c.mouse_precision) < 0.01:
                return k
        return ""

    def tick(self, now):
        self.engine.buttons.tick(time.time())
        # okno se znovu aktivuje = Windows ClipCursor zruší, proto obnovovat
        if now - getattr(self, "_t_clip", 0.0) > 0.5:
            self._t_clip = now
            self._clip(self.ctx.monitor() if self._want_clip() else None)
        if now - self._t_mon > 2.0:
            self._t_mon = now
            self.engine.refresh_monitor()
        if self.ring:
            self.ring.tick()

    def status(self):
        if not self.rt.enabled:
            return "vypnutá"
        return "zapnutá" + (" (pozastavená)" if self.paused else "")

    def ui(self):
        c = self.ctx.cfg
        pre = self.preset_name()
        return {"window": True, "control": True,
                "lines": [f"Rychlost {c.mouse_speed:.1f}×  ·  přesnost {int(c.mouse_precision * 100)} %"
                          + (f"  ({PRESET_LABELS[pre]})" if pre else ""),
                          "Pomalý pohyb hlavy = přesné míření, rychlý = daleko.",
                          "Kurzor drží na brýlích: " + ("ano" if self._clipped else "ne"),
                          "Klik pohledem: " + ("zapnutý" if self.mcfg.dwell_on else "vypnutý"),
                          f"Rychlost hlavy: {self.rt.speed:.0f} °/s"],
                "actions": [{"id": "toggle", "label": "Myš hlavou", "on": self.rt.enabled}]
                + [{"id": "preset_" + k, "label": PRESET_LABELS[k], "on": pre == k} for k in PRESETS]
                + [{"id": "slower", "label": "Pomaleji"}, {"id": "faster", "label": "Rychleji"},
                   {"id": "prec_up", "label": "Přesnost +"}, {"id": "prec_down", "label": "Přesnost −"},
                            {"id": "dwell", "label": "Klik pohledem", "on": self.mcfg.dwell_on},
                            {"id": "recenter", "label": "Vycentrovat"},
                            {"id": "calib", "label": "Kalibrovat"}]}

    def ui_action(self, action, value=None):
        if action == "toggle":
            self.ctx.app().set_head_mouse(not self.rt.enabled)
        elif action == "dwell":
            self._toggle_dwell()
        elif action.startswith("preset_"):
            self.set_speed(preset=action[7:])
        elif action == "slower":
            self.set_speed(speed=self.ctx.cfg.mouse_speed / 1.2)
        elif action == "faster":
            self.set_speed(speed=self.ctx.cfg.mouse_speed * 1.2)
        elif action == "prec_up":
            self.set_speed(precision=self.ctx.cfg.mouse_precision + 0.1)
        elif action == "prec_down":
            self.set_speed(precision=self.ctx.cfg.mouse_precision - 0.1)
        elif action == "recenter":
            self._recenter()
        elif action == "calib":
            self.recalibrate()

    def menu_items(self):
        r = self.rt
        return [
            MenuItem("mouse_toggle", "Vypnout myš hlavou" if r.enabled else "Zapnout myš hlavou",
                     action=lambda: self.set_on(not self.rt.enabled)),
            MenuItem("mouse_recenter", "Vycentrovat kurzor", "střed = kam se díváš",
                     action=self._recenter),
            MenuItem("mouse_dwell", ("Vypnout" if self.mcfg.dwell_on else "Zapnout") + " klik pohledem",
                     action=self._toggle_dwell),
            MenuItem("mouse_preset", "Myš: " + PRESET_LABELS[self._next_preset()].lower(),
                     "přepnout předvolbu rychlosti", action=lambda: self.set_speed(preset=self._next_preset())),
            MenuItem("mouse_slower", "Myš pomaleji", f"teď {self.ctx.cfg.mouse_speed:.1f}×",
                     action=lambda: self.set_speed(speed=self.ctx.cfg.mouse_speed / 1.2)),
            MenuItem("mouse_faster", "Myš rychleji", f"teď {self.ctx.cfg.mouse_speed:.1f}×",
                     action=lambda: self.set_speed(speed=self.ctx.cfg.mouse_speed * 1.2)),
        ]

    def _next_preset(self) -> str:
        keys = list(PRESETS)
        cur = self.preset_name()
        return keys[(keys.index(cur) + 1) % len(keys)] if cur in keys else "presna"

    def recalibrate(self):
        self.rt.calib_request = True

    def _recenter(self):
        self.rt.recenter_request = True
        c = self.engine.center()
        if c and self.rt.enabled and not self.ctx.dry:
            self.engine.io.set_cursor(*c)

    def _toggle_dwell(self):
        self.mcfg.dwell_on = not self.mcfg.dwell_on
        self.mcfg.save()


MODULE = HeadMouse
