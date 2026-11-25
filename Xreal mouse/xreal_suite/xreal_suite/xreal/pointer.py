"""Rezimy ovladani kurzoru + sledovani natoceni hlavy (pro kurzor i pro AR overlay).

  relative  pohyb hlavy = relativni pohyb mysi (klasicke ukazovani hlavou)
  view      KURZOR = STRED POHLEDU: obrazovka je ukotvena v prostoru (3DoF/6DoF),
            otoceni hlavy posouva kurzor po obrazovce tak, aby byl vzdy uprostred
            toho, na co se divas (absolutni poloha)
  look      kurzor je pevne uprostred obrazovky, pohyb hlavy se posila jako
            relativni pohyb mysi (rozhlizeni ve hrach / 3D)

ViewTracker bezi VZDY (nezavisle na rezimu): zná natoceni hlavy vzhledem k reference
(F5). Pouziva ho kurzor v rezimu "view" i AR overlay.
"""
import math
from dataclasses import dataclass
from typing import Optional

MODES = ("relative", "view", "look")
MODE_LABELS = {
    "relative": "Relativní myš (klasické ukazování hlavou)",
    "view": "Střed pohledu (absolutní, ukotvená obrazovka)",
    "look": "Rozhlížení (kurzor pevně ve středu, pohyb pro hry)",
}


def deadzone(x: float, dz: float) -> float:
    return 0.0 if abs(x) <= dz else x - math.copysign(dz, x)


def wrap180(a: float) -> float:
    return (a + 180.0) % 360.0 - 180.0


@dataclass
class PointerOut:
    dx: float = 0.0            # pohyb kurzoru v px od minuleho vzorku (pro dwell)
    dy: float = 0.0
    tx: Optional[float] = None  # cil v absolutnich souradnicich (jen rezim view)
    ty: Optional[float] = None


class ViewTracker:
    """Natoceni hlavy oproti reference: yaw (integrace gyra) + pitch (z gravitace)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.reset()

    def reset(self):
        self.started = False
        self.yaw_int = 0.0
        self.ys = self.ps = 0.0          # vyhlazene (pro kurzor)
        self.y0 = self.p0 = 0.0          # reference
        self.pitch_raw = 0.0
        self.recenter_pending = False
        self.dyaw = self.dpitch = 0.0            # vyhlazene odchylky (deg)
        self.dyaw_raw = self.dpitch_raw = 0.0    # nevyhlazene (pro AR)

    def recenter(self):
        self.recenter_pending = True

    def rebase(self):
        """Aktualni natoceni prohlasi za stred (okamzite)."""
        self.y0, self.p0 = self.yaw_int, self.pitch_raw
        self.ys, self.ps = self.yaw_int, self.pitch_raw
        self.dyaw = self.dpitch = self.dyaw_raw = self.dpitch_raw = 0.0

    def update(self, o, dt: float):
        self.pitch_raw = o.pitch_deg
        if not self.started:
            self.started = True
            self.yaw_int = 0.0
            self.rebase()
        self.yaw_int += o.yaw_rate_dps * dt
        a = 1.0 - math.exp(-dt / max(0.001, self.cfg.smooth_ms / 1000.0))
        self.ys += a * (self.yaw_int - self.ys)
        self.ps += a * (o.pitch_deg - self.ps)
        if self.recenter_pending:
            self.recenter_pending = False
            self.rebase()
        self.dyaw = wrap180(self.ys - self.y0)
        self.dpitch = self.ps - self.p0
        self.dyaw_raw = wrap180(self.yaw_int - self.y0)
        self.dpitch_raw = self.pitch_raw - self.p0


class PointerController:
    def __init__(self, cfg):
        self.cfg = cfg
        self.mode = "relative"
        self.tracker = ViewTracker(cfg)
        self.tx: Optional[float] = None
        self.ty: Optional[float] = None
        self.yaw_offset_deg = 0.0      # diagnostika: odchylka od stredu
        self.pitch_offset_deg = 0.0

    # znamenka: vychozi chovani odpovida overenemu relativnimu rezimu
    @property
    def sign_yaw(self) -> int:
        return 1 if self.cfg.flip_h else -1

    @property
    def sign_pitch(self) -> int:
        return 1 if self.cfg.flip_v else -1

    @property
    def recenter_pending(self) -> bool:
        return self.tracker.recenter_pending

    @recenter_pending.setter
    def recenter_pending(self, v: bool):
        self.tracker.recenter_pending = v

    def reset(self, mode: str):
        """Zmena rezimu. Tracker natoceni se NERESETUJE (potrebuje ho AR overlay)."""
        self.mode = mode
        self.tx = self.ty = None
        self.yaw_offset_deg = self.pitch_offset_deg = 0.0

    def recenter(self):
        self.tracker.recenter()

    def view_angles(self):
        """(H, V) ve stupnich: o kolik se pohled posunul DOPRAVA / DOLU na ukotvene obrazovce."""
        t = self.tracker
        return self.sign_yaw * t.dyaw_raw, self.sign_pitch * t.dpitch_raw

    def step(self, o, dt: float, mode: str, mon: Optional[dict]) -> PointerOut:
        if mode != self.mode:
            self.reset(mode)
        cfg = self.cfg
        self.tracker.update(o, dt)
        if mode in ("relative", "look"):
            yaw = deadzone(o.yaw_rate_dps, cfg.deadzone)
            pitch = deadzone(o.pitch_rate_dps, cfg.deadzone)
            return PointerOut(self.sign_yaw * yaw * dt * cfg.sens,
                              self.sign_pitch * pitch * dt * cfg.sens)

        # ---------------- view ----------------
        if not mon:
            return PointerOut()
        t = self.tracker
        if self.tx is None:                       # nabeh: aktualni pohled = stred
            t.rebase()
        ppd = mon["w"] / max(5.0, cfg.fov_h)                 # pixelu na stupen
        self.yaw_offset_deg, self.pitch_offset_deg = t.dyaw, t.dpitch
        cx = mon["x"] + mon["w"] / 2.0
        cy = mon["y"] + mon["h"] / 2.0
        tx = min(mon["x"] + mon["w"] - 1, max(mon["x"], cx + self.sign_yaw * t.dyaw * ppd))
        ty = min(mon["y"] + mon["h"] - 1, max(mon["y"], cy + self.sign_pitch * t.dpitch * ppd))
        ox, oy = self.tx, self.ty
        self.tx, self.ty = tx, ty
        if ox is None:
            return PointerOut(0.0, 0.0, tx, ty)
        return PointerOut(tx - ox, ty - oy, tx, ty)
