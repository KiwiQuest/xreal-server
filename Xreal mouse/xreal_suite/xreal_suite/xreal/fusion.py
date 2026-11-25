"""Orientace bryli: kvaterniony + Mahony filtr (gyro + gravitace z akcelerometru).

Vystupy pro ukazovani:
  up            smer "nahoru" ve souradnicich senzoru (jednotkovy vektor)
  yaw_rate_dps  rychlost otaceni kolem svisle osy (deg/s, + = doleva), bez mrtve zony
  pitch_rate_dps rychlost naklanení kolem vodorovne osy bryli (deg/s, + = nahoru)
  pitch_deg     uhel naklonu z gravitace (neuplave), stejne znamenko jako pitch_rate
"""
import math
import time
from collections import deque
from typing import Sequence, Tuple

from .protocol import G

Vec = Tuple[float, float, float]


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Sequence[float], b: Sequence[float]) -> Vec:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def q_norm(q):
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    return tuple(c / n for c in q)


def q_mul(a, b):
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (aw * bw - ax * bx - ay * by - az * bz,
            aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw)


def q_to_matrix(q):
    """Rotace telo -> svet."""
    w, x, y, z = q
    return ((1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)),
            (2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)),
            (2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)))


def arc_quat(a: Vec, b: Vec):
    """Nejkratsi rotace jednotkoveho vektoru a na b."""
    d = dot(a, b)
    if d < -0.9999:
        axis = cross(a, (1, 0, 0))
        if dot(axis, axis) < 1e-6:
            axis = cross(a, (0, 1, 0))
        return q_norm((0.0,) + tuple(axis))
    c = cross(a, b)
    return q_norm((1 + d, c[0], c[1], c[2]))


def up_in_body(q) -> Vec:
    """Svisle 'nahoru' vyjadrene v souradnicich tela (treti radek matice rotace)."""
    w, x, y, z = q
    return (2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y))


class Orientation:
    def __init__(self, kp: float = 1.0, axis_map=(0, 1, 2), axis_sign=(1, 1, 1),
                 calib_samples: int = 1000, calib_max_range: float = 0.15):
        self.kp = kp
        self.axis_map = axis_map
        self.axis_sign = axis_sign
        self.calib_samples = calib_samples
        self.calib_max_range = calib_max_range

        self.q = (1.0, 0.0, 0.0, 0.0)
        self.bias = [0.0, 0.0, 0.0]
        self.calibrating = True
        self.status = "Kalibrace - drz bryle v klidu"
        self._calib: list = []
        self._last_ts = None
        self._win: deque = deque()
        self._still_t = 0.0

        self.dt = 0.0
        self.hz = 0.0
        self.up: Vec = (0.0, 0.0, 1.0)
        self.yaw_rate_dps = 0.0
        self.pitch_rate_dps = 0.0
        self.pitch_deg = 0.0

    # ------------------------------------------------------------------
    def start_calibration(self):
        self.calibrating = True
        self._calib = []
        self.status = "Kalibrace - drz bryle v klidu"

    def update(self, ts_ns: int, gyro: Sequence[float], acc: Sequence[float]) -> bool:
        """Zpracuje vzorek. Vraci True, kdyz jsou vystupy platne (po kalibraci)."""
        g = tuple(self.axis_sign[i] * gyro[self.axis_map[i]] for i in range(3))
        a = tuple(self.axis_sign[i] * acc[self.axis_map[i]] for i in range(3))
        an = math.sqrt(dot(a, a))
        if an < 1e-3:
            return False
        ah = tuple(c / an for c in a)

        # frekvence z casovych znacek bryli (okno 1 s)
        self._win.append(ts_ns)
        while self._win and ts_ns - self._win[0] > 1_000_000_000:
            self._win.popleft()
        span = (ts_ns - self._win[0]) * 1e-9
        self.hz = (len(self._win) - 1) / span if span > 0 else 0.0

        self.dt = (ts_ns - self._last_ts) * 1e-9 if self._last_ts else 0.0
        self._last_ts = ts_ns

        if self.calibrating:
            self.q = arc_quat(ah, (0.0, 0.0, 1.0))
            self.up = up_in_body(self.q)
            self._calib.append(g)
            if len(self._calib) >= self.calib_samples:
                rng = [max(c[i] for c in self._calib) - min(c[i] for c in self._calib)
                       for i in range(3)]
                if max(rng) < self.calib_max_range:
                    n = len(self._calib)
                    self.bias = [sum(c[i] for c in self._calib) / n for i in range(3)]
                    self.calibrating = False
                    self.status = "OK"
                else:
                    self._calib = []
                    self.status = "Kalibrace - hybes, drz bryle v klidu"
            return False
        if self.dt <= 0 or self.dt > 0.1:
            return False
        dt = self.dt

        # --- Mahony: korekce naklonu z gravitace ---
        up = up_in_body(self.q)
        e = cross(ah, up)
        k = self.kp if 0.85 < an / G < 1.15 else 0.0
        om = tuple(g[i] - self.bias[i] + k * e[i] for i in range(3))
        dq = q_mul(self.q, (0.0, om[0], om[1], om[2]))
        self.q = q_norm(tuple(self.q[i] + 0.5 * dq[i] * dt for i in range(4)))

        # --- pomalu dolad bias gyra, kdyz je hlava v klidu ---
        if all(abs(g[i] - self.bias[i]) < 0.01 for i in range(3)):
            self._still_t += dt
            if self._still_t > 1.0:
                a_b = 1.0 - math.exp(-dt / 5.0)
                for i in range(3):
                    self.bias[i] += a_b * (g[i] - self.bias[i])
        else:
            self._still_t = 0.0

        # --- vystupy ---
        up = up_in_body(self.q)
        self.up = up
        yaw = dot(om, up)                              # kolem svisle osy
        xh = (1 - up[0] * up[0], -up[0] * up[1], -up[0] * up[2])   # vodorovny prumet osy X
        xn = math.sqrt(dot(xh, xh))
        pitch = dot(om, xh) / xn if xn > 0.2 else 0.0
        self.yaw_rate_dps = math.degrees(yaw)
        self.pitch_rate_dps = math.degrees(pitch)
        self.pitch_deg = math.degrees(math.asin(max(-1.0, min(1.0, up[2]))))
        return True
