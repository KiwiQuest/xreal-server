"""Modul: myš hlavou z hotového XREAL Head Mouse 2.0 (Xreal mouse/xreal_suite).

Bere engine beze změny (ukazování, circle click, tlačítko brýlí jako myš), jen ho krmí
daty z kořene místo vlastního spojení, takže se s kořenem nepřetahuje o porty brýlí.
Nastavení sdílí s Head Mouse (`xreal_config.json` v jeho složce).

V režimu "notebook v tašce" se zapne sám. Když si kořen bere vstup (menu, otázka),
myš se pozastaví a pustí případně držené tlačítko.
"""
import time

from ..config import use_suite
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
        self.engine.refresh_monitor()
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

    def tick(self, now):
        self.engine.buttons.tick(time.time())
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
        return {"window": True, "control": True,
                "lines": [f"Režim: {self.mcfg.mode}", f"Citlivost: {self.mcfg.sens:.0f}",
                          "Klik pohledem: " + ("zapnutý" if self.mcfg.dwell_on else "vypnutý"),
                          f"Rychlost hlavy: {self.rt.speed:.0f} °/s"],
                "actions": [{"id": "toggle", "label": "Myš hlavou", "on": self.rt.enabled},
                            {"id": "dwell", "label": "Klik pohledem", "on": self.mcfg.dwell_on},
                            {"id": "recenter", "label": "Vycentrovat"},
                            {"id": "calib", "label": "Kalibrovat"}]}

    def ui_action(self, action, value=None):
        if action == "toggle":
            self.ctx.app().set_head_mouse(not self.rt.enabled)
        elif action == "dwell":
            self._toggle_dwell()
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
        ]

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
