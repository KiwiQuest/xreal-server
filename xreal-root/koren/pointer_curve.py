"""Rychlost myši hlavou: zrychlení podle rychlosti hlavy a vyhlazení třesu.

Původní Head Mouse má v relativním režimu lineární zisk (px na stupeň). To znamená buď
rychlý kurzor, který malé cíle (tlačítko Start) přeletí, nebo pomalý, se kterým se přes
obrazovku musí točit hlava daleko. Tady je zisk závislý na rychlosti hlavy (jako zrychlení
myši ve Windows):
  pomalý pohyb hlavy  -> málo pixelů na stupeň  = přesné míření,
  rychlý pohyb        -> hodně pixelů na stupeň = přes celou obrazovku malým otočením.
Rychlost (`speed`) posouvá celou křivku, přesnost (`precision`) určuje, jak moc se pomalý
pohyb zpomalí a rychlý zrychlí. Navíc dolní propust na úhlové rychlosti potlačí třes hlavy.

Engine z Head Mouse 2.0 se nemění, kořen mu jen podstrčí tento ukazovač.
"""
import math

from .config import use_suite

use_suite()
from xreal.pointer import PointerController, PointerOut, deadzone  # noqa: E402

SLOW_DPS, FAST_DPS = 6.0, 70.0          # pod tím = přesné míření, nad tím = plný zisk
TREMOR_TAU = 0.025                      # s, vyhlazení úhlové rychlosti

PRESETS = {                             # (rychlost, přesnost)
    "presna": (0.8, 0.85),
    "stredni": (1.0, 0.6),
    "rychla": (1.4, 0.4),
}
PRESET_LABELS = {"presna": "Přesná", "stredni": "Střední", "rychla": "Rychlá"}


def gain(w_dps: float, base: float, speed: float, precision: float) -> float:
    """Zisk v px na stupeň pro rychlost hlavy w (°/s)."""
    precision = min(max(precision, 0.0), 1.0)
    g_slow = base * speed * (1.0 - 0.75 * precision)
    g_fast = base * speed * (1.0 + 1.2 * precision)
    t = min(max((w_dps - SLOW_DPS) / (FAST_DPS - SLOW_DPS), 0.0), 1.0)
    s = t * t * (3.0 - 2.0 * t)                          # plynulý přechod (smoothstep)
    return g_slow + (g_fast - g_slow) * s


class CurvePointer(PointerController):
    def __init__(self, cfg, params):
        super().__init__(cfg)
        self.params = params             # objekt s .mouse_speed a .mouse_precision (nastavení kořene)
        self.wy = self.wp = 0.0

    def step(self, o, dt: float, mode: str, mon) -> PointerOut:
        if mode not in ("relative", "look"):
            return super().step(o, dt, mode, mon)
        if mode != self.mode:
            self.reset(mode)
        self.tracker.update(o, dt)
        a = 1.0 - math.exp(-dt / TREMOR_TAU) if dt > 0 else 1.0
        self.wy += a * (o.yaw_rate_dps - self.wy)
        self.wp += a * (o.pitch_rate_dps - self.wp)
        g = gain(math.hypot(self.wy, self.wp), self.cfg.sens, self.params.mouse_speed,
                 self.params.mouse_precision)
        yaw = deadzone(self.wy, self.cfg.deadzone)
        pitch = deadzone(self.wp, self.cfg.deadzone)
        return PointerOut(self.sign_yaw * yaw * dt * g, self.sign_pitch * pitch * dt * g)
