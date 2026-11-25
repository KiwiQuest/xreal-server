"""Generator syntetickych IMU dat s konzistentnim gyrem a akcelerometrem."""
import math

from xreal.fusion import arc_quat, q_mul, q_norm, up_in_body
from xreal.protocol import G, ImuSample

LEVEL_UP = (0.0, -1.0, 0.0)      # bryle drzene rovne: osa Y smeruje dolu


class Head:
    """Simuluje hlavu: X doprava, Y dolu, Z dopredu; rychlosti v deg/s."""

    def __init__(self, t0=0.0):
        self.q = arc_quat(LEVEL_UP, (0.0, 0.0, 1.0))
        self.t = t0          # s
        self.samples = []

    def step(self, yaw_dps=0.0, pitch_dps=0.0, dt=0.001):
        up = up_in_body(self.q)
        # otaceni doleva = kladna rotace kolem svisle osy (up), pitch nahoru = kolem osy X
        w = (math.radians(pitch_dps) + math.radians(yaw_dps) * up[0],
             math.radians(yaw_dps) * up[1],
             math.radians(yaw_dps) * up[2])
        dq = q_mul(self.q, (0.0, w[0], w[1], w[2]))
        self.q = q_norm(tuple(self.q[i] + 0.5 * dq[i] * dt for i in range(4)))
        up2 = up_in_body(self.q)
        acc = tuple(G * c for c in up2)
        self.t += dt
        s = ImuSample(int(self.t * 1e9), w, acc, 40.0)
        self.samples.append(s)
        return s

    def hold(self, seconds, yaw_dps=0.0, pitch_dps=0.0):
        out = []
        for _ in range(int(round(seconds * 1000))):
            out.append(self.step(yaw_dps, pitch_dps))
        return out
