"""Gesta hlavou a tlačítko brýlí pro kořenové ovládání.

  kývnutí (nahoru-dolů)        = ano / potvrdit
  zavrtění (doleva-doprava)     = ne / zavřít
  podržení tlačítka brýlí       = kořenové menu (funguje vždy, i když modul drží myš)

Detektor pracuje jen s úhlovou rychlostí (°/s), takže nevadí drift gyra.
Gesto = několik rychlých "laloků" rychlosti se střídavým znaménkem v krátkém okně.
"""
from collections import deque
from typing import Optional

NOD, SHAKE = "nod", "shake"


class _Lobes:
    """Úseky, kde |rychlost| přesáhla práh, se znaménkem a špičkou."""

    def __init__(self, enter: float):
        self.enter = enter
        self.sign = 0
        self.peak = 0.0
        self.t0 = 0.0
        self.done = deque(maxlen=12)        # (t_konec, znaménko, špička)

    def feed(self, t: float, r: float):
        s = 1 if r > self.enter else -1 if r < -self.enter else 0
        if s != self.sign:
            if self.sign != 0:
                self.done.append((t, self.sign, self.peak))
            self.sign, self.peak, self.t0 = s, 0.0, t
        if s:
            self.peak = max(self.peak, abs(r))

    def recent(self, t: float, window: float, peak_min: float):
        return [l for l in self.done if t - l[0] <= window and l[2] >= peak_min]

    def max_peak(self, t: float, window: float) -> float:
        ps = [l[2] for l in self.done if t - l[0] <= window]
        if self.sign:
            ps.append(self.peak)
        return max(ps, default=0.0)

    def clear(self):
        self.done.clear()
        self.sign, self.peak = 0, 0.0


def _alternating(lobes) -> int:
    """Délka nejdelší střídavé řady na konci seznamu."""
    n = 0
    prev = 0
    for _, s, _ in lobes:
        n = n + 1 if s != prev and prev != 0 else 1
        prev = s
    return n


class GestureDetector:
    def __init__(self, nod_dps: float = 55.0, shake_dps: float = 70.0,
                 window_s: float = 0.9, cooldown_s: float = 1.0):
        self.nod_dps, self.shake_dps = nod_dps, shake_dps
        self.window, self.cooldown = window_s, cooldown_s
        self.pitch = _Lobes(enter=20.0)
        self.yaw = _Lobes(enter=20.0)
        self.t_last = -1e9

    def feed(self, t: float, yaw_dps: float, pitch_dps: float) -> Optional[str]:
        self.pitch.feed(t, pitch_dps)
        self.yaw.feed(t, yaw_dps)
        if t - self.t_last < self.cooldown:
            return None
        p = self.pitch.recent(t, self.window, self.nod_dps)
        y = self.yaw.recent(t, self.window * 1.4, self.shake_dps)
        # zavrtění: aspoň 3 střídavé laloky vodorovně, svisle klid
        if len(y) >= 3 and _alternating(y) >= 3:
            if self.pitch.max_peak(t, self.window * 1.4) < 0.6 * min(l[2] for l in y[-3:]):
                return self._fire(t, SHAKE)
        # kývnutí: aspoň 2 střídavé laloky svisle, vodorovně klid
        if len(p) >= 2 and _alternating(p) >= 2:
            if self.yaw.max_peak(t, self.window) < 0.6 * min(l[2] for l in p[-2:]):
                return self._fire(t, NOD)
        return None

    def _fire(self, t: float, g: str) -> str:
        self.t_last = t
        self.pitch.clear()
        self.yaw.clear()
        return g


class ButtonTracker:
    """Surové stisky tlačítka brýlí -> krátký stisk / podržení pro kořenové menu."""

    def __init__(self, hold_s: float = 2.0, short_max_s: float = 0.6):
        self.hold_s, self.short_max = hold_s, short_max_s
        self.down_t: Optional[float] = None
        self.hold_fired = False

    def down(self, t: float):
        self.down_t, self.hold_fired = t, False

    def up(self, t: float) -> Optional[str]:
        if self.down_t is None:
            return None
        dur = t - self.down_t
        fired = self.hold_fired
        self.down_t, self.hold_fired = None, False
        if fired:
            return None
        return "press" if dur <= self.short_max else None

    def tick(self, t: float) -> Optional[str]:
        if self.down_t is not None and not self.hold_fired and t - self.down_t >= self.hold_s:
            self.hold_fired = True
            return "hold"
        return None

    def held_for(self, t: float) -> float:
        """Jak dlouho je tlačítko drženo (pro ukazatel v overlay), 0 = není."""
        return 0.0 if self.down_t is None or self.hold_fired else t - self.down_t
