import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from synth import Head                                   # noqa: E402
from xreal.config import Config, Runtime                 # noqa: E402
from xreal.engine import Engine, NullOutput              # noqa: E402

MON = {"x": 2560, "y": 0, "w": 1920, "h": 1080}
CENTER = (3520.0, 540.0)


def make(mode="relative", **cfg_kw):
    cfg = Config(mode=mode, smooth_ms=1.0, **cfg_kw)
    rt = Runtime()
    io = NullOutput()
    eng = Engine(cfg, rt, io=io, use_hotkeys=False, monitor_fn=lambda: MON)
    eng.refresh_monitor()
    return eng, cfg, rt, io


def feed(eng, samples):
    for s in samples:
        eng.process(s, now=s.ts_ns * 1e-9)


def boot(eng, head):
    feed(eng, head.hold(1.3))
    assert not eng.ori.calibrating


def calls(io, name):
    return [c for c in io.calls if c[0] == name]


class ViewModeEngine(unittest.TestCase):
    def test_cursor_follows_head_in_view_mode(self):
        """Presne scenar z hlaseni: kurzor je na stredu a MUSI se hybat s bryli."""
        eng, cfg, rt, io = make("view", fov_h=40.0)
        head = Head(); boot(eng, head)
        rt.enabled = True
        feed(eng, head.hold(0.3))
        self.assertEqual(calls(io, "set_cursor")[0][1:], CENTER)      # skok na stred
        io.calls.clear()
        feed(eng, head.hold(1.0, yaw_dps=10.0))                        # otoceni doleva
        xs = [c[1] for c in calls(io, "set_cursor")]
        self.assertTrue(xs, "kurzor se vubec nenastavoval")
        self.assertLess(min(xs), CENTER[0] - 400)                      # ujel doleva
        self.assertAlmostEqual(xs[-1], CENTER[0] - 480, delta=10)

    def test_disabled_does_nothing(self):
        eng, cfg, rt, io = make("view")
        head = Head(); boot(eng, head)
        feed(eng, head.hold(1.0, yaw_dps=10.0))
        self.assertEqual(io.calls, [])

    def test_lock_off_switches_to_relative(self):
        eng, cfg, rt, io = make("view", sens=15.0, deadzone=0.0)
        head = Head(); boot(eng, head); rt.enabled = True
        feed(eng, head.hold(0.3))
        eng.handle_event("center")                                     # klik kolieckem
        self.assertTrue(rt.lock_off)
        io.calls.clear()
        feed(eng, head.hold(1.0, yaw_dps=10.0))
        moves = calls(io, "move")
        self.assertTrue(moves)
        self.assertLess(sum(c[1] for c in moves), -100)                # relativni pohyb doleva
        eng.handle_event("center")                                     # znovu chytit
        self.assertFalse(rt.lock_off)


class RelativeAndLook(unittest.TestCase):
    def test_relative_sends_deltas(self):
        eng, cfg, rt, io = make("relative", sens=15.0, deadzone=0.0)
        head = Head(); boot(eng, head); rt.enabled = True
        feed(eng, head.hold(1.0, yaw_dps=10.0))
        self.assertAlmostEqual(sum(c[1] for c in calls(io, "move")), -150, delta=8)
        self.assertEqual(calls(io, "set_cursor"), [])

    def test_look_pins_cursor_to_center_but_sends_motion(self):
        eng, cfg, rt, io = make("look", sens=15.0, deadzone=0.0)
        head = Head(); boot(eng, head); rt.enabled = True
        feed(eng, head.hold(1.0, yaw_dps=10.0))
        self.assertAlmostEqual(sum(c[1] for c in calls(io, "move")), -150, delta=8)
        self.assertTrue(all(c[1:] == CENTER for c in calls(io, "set_cursor")))


class DwellClick(unittest.TestCase):
    def test_click_after_holding_still(self):
        eng, cfg, rt, io = make("relative", dwell_time=1.0)
        head = Head(); boot(eng, head); rt.enabled = True
        feed(eng, head.hold(0.1, yaw_dps=40.0))
        feed(eng, head.hold(2.0))
        self.assertEqual(len(calls(io, "left_click")), 1)
        feed(eng, head.hold(2.0))                                      # cooldown: bez dalsiho kliku
        self.assertEqual(len(calls(io, "left_click")), 1)

    def test_fast_motion_cancels(self):
        eng, cfg, rt, io = make("relative", dwell_time=1.0)
        head = Head(); boot(eng, head); rt.enabled = True
        for _ in range(6):
            feed(eng, head.hold(0.5))
            feed(eng, head.hold(0.1, yaw_dps=60.0))
        self.assertEqual(calls(io, "left_click"), [])


class Buttons(unittest.TestCase):
    def test_toggle_dwell_and_actions(self):
        eng, cfg, rt, io = make("relative")
        self.assertTrue(cfg.dwell_on)
        eng._act_toggle_dwell()
        self.assertFalse(cfg.dwell_on)
        self.assertEqual(rt.toast_text, "VYP")
        self.assertTrue(calls(io, "beep"))
        eng.buttons.down(0.0); eng.buttons.up(0.1)
        eng.buttons.tick(1.0)
        self.assertEqual(len(calls(io, "left_click")), 1)
        eng.buttons.down(2.0); eng.buttons.up(2.1); eng.buttons.down(2.2); eng.buttons.up(2.3)
        self.assertEqual(len(calls(io, "right_click")), 1)
        eng.buttons.down(4.0); eng.buttons.tick(4.5)
        self.assertEqual(len(calls(io, "left_down")), 1)
        eng.buttons.up(6.0)
        self.assertEqual(len(calls(io, "left_up")), 1)


if __name__ == "__main__":
    unittest.main()
