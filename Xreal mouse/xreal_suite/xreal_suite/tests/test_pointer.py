import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from synth import Head                                   # noqa: E402
from xreal.config import Config                          # noqa: E402
from xreal.fusion import Orientation                     # noqa: E402
from xreal.pointer import PointerController              # noqa: E402

MON = {"x": 2560, "y": 0, "w": 1920, "h": 1080}
CX, CY = 2560 + 960, 540


def calibrated(head):
    o = Orientation()
    for s in head.hold(1.2):
        o.update(s.ts_ns, s.gyro, s.acc)
    assert not o.calibrating, o.status
    return o


def run(o, head, pc, mode, seconds, yaw=0.0, pitch=0.0):
    tx = ty = None
    dx = dy = 0.0
    for s in head.hold(seconds, yaw, pitch):
        if o.update(s.ts_ns, s.gyro, s.acc):
            out = pc.step(o, o.dt, mode, MON)
            dx += out.dx
            dy += out.dy
            if out.tx is not None:
                tx, ty = out.tx, out.ty
    return dx, dy, tx, ty


class RelativeMode(unittest.TestCase):
    def test_turn_left_moves_cursor_left(self):
        cfg = Config(deadzone=0.0, sens=15.0)
        head = Head(); o = calibrated(head); pc = PointerController(cfg)
        dx, dy, *_ = run(o, head, pc, "relative", 1.0, yaw=10.0)
        self.assertAlmostEqual(dx, -150.0, delta=4.0)
        self.assertAlmostEqual(dy, 0.0, delta=4.0)

    def test_look_up_moves_cursor_up(self):
        cfg = Config(deadzone=0.0, sens=15.0)
        head = Head(); o = calibrated(head); pc = PointerController(cfg)
        dx, dy, *_ = run(o, head, pc, "relative", 1.0, pitch=10.0)
        self.assertAlmostEqual(dy, -150.0, delta=4.0)
        self.assertAlmostEqual(dx, 0.0, delta=4.0)

    def test_flip(self):
        cfg = Config(deadzone=0.0, sens=15.0, flip_h=True)
        head = Head(); o = calibrated(head); pc = PointerController(cfg)
        dx, *_ = run(o, head, pc, "relative", 1.0, yaw=10.0)
        self.assertGreater(dx, 100)


class ViewMode(unittest.TestCase):
    """Kurzor = stred pohledu: nakonec se MUSI hybat spolu s hlavou."""

    def setUp(self):
        self.cfg = Config(fov_h=40.0, smooth_ms=1.0)     # 1920 / 40 = 48 px na stupen
        self.head = Head()
        self.o = calibrated(self.head)
        self.pc = PointerController(self.cfg)

    def test_starts_at_center(self):
        *_, tx, ty = run(self.o, self.head, self.pc, "view", 0.3)
        self.assertAlmostEqual(tx, CX, delta=2)
        self.assertAlmostEqual(ty, CY, delta=2)

    def test_turn_left_moves_left_by_angle(self):
        run(self.o, self.head, self.pc, "view", 0.2)
        *_, tx, ty = run(self.o, self.head, self.pc, "view", 1.0, yaw=10.0)
        self.assertAlmostEqual(tx, CX - 480, delta=8)      # 10 stupnu * 48 px
        self.assertAlmostEqual(ty, CY, delta=8)

    def test_look_up_moves_up(self):
        run(self.o, self.head, self.pc, "view", 0.2)
        *_, tx, ty = run(self.o, self.head, self.pc, "view", 1.0, pitch=10.0)
        self.assertAlmostEqual(ty, CY - 480, delta=10)
        self.assertAlmostEqual(tx, CX, delta=8)

    def test_moves_and_comes_back(self):
        run(self.o, self.head, self.pc, "view", 0.2)
        run(self.o, self.head, self.pc, "view", 1.0, yaw=-10.0)
        *_, tx, _ = run(self.o, self.head, self.pc, "view", 1.0, yaw=10.0)
        self.assertAlmostEqual(tx, CX, delta=10)

    def test_clamped_to_screen(self):
        run(self.o, self.head, self.pc, "view", 0.2)
        *_, tx, ty = run(self.o, self.head, self.pc, "view", 3.0, yaw=30.0)
        self.assertEqual(tx, MON["x"])                     # levy okraj obrazovky

    def test_recenter(self):
        run(self.o, self.head, self.pc, "view", 0.2)
        run(self.o, self.head, self.pc, "view", 1.0, yaw=10.0)
        self.pc.recenter()
        *_, tx, ty = run(self.o, self.head, self.pc, "view", 0.1)
        self.assertAlmostEqual(tx, CX, delta=3)
        self.assertAlmostEqual(ty, CY, delta=3)

    def test_fov_changes_gain(self):
        self.cfg.fov_h = 80.0                              # 24 px na stupen
        run(self.o, self.head, self.pc, "view", 0.2)
        *_, tx, _ = run(self.o, self.head, self.pc, "view", 1.0, yaw=10.0)
        self.assertAlmostEqual(tx, CX - 240, delta=6)


class Fusion(unittest.TestCase):
    def test_pitch_deg_matches_motion(self):
        head = Head(); o = calibrated(head)
        p0 = o.pitch_deg
        for s in head.hold(1.0, pitch_dps=12.0):
            o.update(s.ts_ns, s.gyro, s.acc)
        self.assertAlmostEqual(o.pitch_deg - p0, 12.0, delta=0.6)

    def test_bias_is_learned_when_still(self):
        head = Head()
        o = Orientation()
        for s in head.hold(1.2):
            s.gyro = (s.gyro[0] + 0.03, s.gyro[1] - 0.02, s.gyro[2])   # konstantni bias
            o.update(s.ts_ns, s.gyro, s.acc)
        self.assertFalse(o.calibrating)
        self.assertAlmostEqual(o.bias[0], 0.03, delta=0.002)


if __name__ == "__main__":
    unittest.main()
