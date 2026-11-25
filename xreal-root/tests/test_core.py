"""Testy čisté logiky kořene (bez Windows a bez brýlí):  python -m unittest discover tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from koren.advisor import FULL, HEAD, NONE, Snapshot, Struggle, capabilities, guidance  # noqa: E402
from koren.gestures import NOD, SHAKE, ButtonTracker, GestureDetector  # noqa: E402
from koren.menu import Menu, MenuItem  # noqa: E402
from koren.modules import Module, ModuleManager  # noqa: E402
from koren.states import BAG, DESK, SUSPEND, WAITING, Machine  # noqa: E402


def kinds(acts):
    return [a[0] for a in acts]


class StatesTest(unittest.TestCase):
    def m(self):
        return Machine(wait_s=300, lost_grace_s=20)

    def test_lid_closed_with_glasses_goes_to_bag(self):
        m = self.m()
        m.handle(("glasses", True), 0)
        self.assertEqual(m.mode, DESK)
        acts = m.handle(("lid", "closed"), 1)
        self.assertEqual(m.mode, BAG)
        self.assertIn("mode", kinds(acts))

    def test_lid_closed_without_glasses_waits_then_hibernates(self):
        m = self.m()
        m.handle(("lid", "closed"), 0)
        self.assertEqual(m.mode, WAITING)
        self.assertAlmostEqual(m.deadline, 300)
        self.assertEqual(kinds(m.handle(("tick",), 100)), [])
        warn = m.handle(("tick",), 245)
        self.assertIn("beep", kinds(warn))
        acts = m.handle(("tick",), 300)
        self.assertEqual(m.mode, SUSPEND)
        self.assertIn("hibernate", kinds(acts))

    def test_glasses_arrive_cancel_countdown(self):
        m = self.m()
        m.handle(("lid", "closed"), 0)
        m.handle(("glasses", True), 50)
        self.assertEqual(m.mode, BAG)
        self.assertIsNone(m.deadline)
        self.assertNotIn("hibernate", kinds(m.handle(("tick",), 1000)))

    def test_lid_open_cancels_everything(self):
        m = self.m()
        m.handle(("lid", "closed"), 0)
        m.handle(("lid", "open"), 10)
        self.assertEqual(m.mode, DESK)
        self.assertNotIn("hibernate", kinds(m.handle(("tick",), 1000)))

    def test_glasses_lost_in_bag_has_grace_then_waits(self):
        m = self.m()
        m.handle(("glasses", True), 0)
        m.handle(("lid", "closed"), 0)
        m.handle(("glasses", False), 10)
        self.assertEqual(m.mode, BAG)
        m.handle(("tick",), 25)
        self.assertEqual(m.mode, BAG)
        m.handle(("tick",), 31)
        self.assertEqual(m.mode, WAITING)
        self.assertAlmostEqual(m.deadline, 331)

    def test_glasses_flicker_is_ignored(self):
        m = self.m()
        m.handle(("glasses", True), 0)
        m.handle(("lid", "closed"), 0)
        m.handle(("glasses", False), 10)
        m.handle(("glasses", True), 15)
        m.handle(("tick",), 60)
        self.assertEqual(m.mode, BAG)
        self.assertIsNone(m.lost_since)

    def test_wait_longer_and_hibernate_now(self):
        m = self.m()
        m.handle(("lid", "closed"), 0)
        m.handle(("wait_longer",), 100)
        self.assertAlmostEqual(m.deadline, 600)
        acts = m.handle(("hibernate_now",), 120)
        self.assertEqual(m.mode, SUSPEND)
        self.assertIn("hibernate", kinds(acts))

    def test_resume_reevaluates(self):
        m = self.m()
        m.handle(("lid", "closed"), 0)
        m.handle(("tick",), 301)
        self.assertEqual(m.mode, SUSPEND)
        m.handle(("lid", "open"), 302)          # během hibernace se ignoruje, ale fakt se uloží
        self.assertEqual(m.mode, SUSPEND)
        m.handle(("resumed",), 400)
        self.assertEqual(m.mode, DESK)

    def test_resume_with_lid_still_closed_waits_again(self):
        m = self.m()
        m.handle(("lid", "closed"), 0)
        m.handle(("tick",), 301)
        m.handle(("resumed",), 500)
        self.assertEqual(m.mode, WAITING)
        self.assertAlmostEqual(m.deadline, 800)

    def test_suspend_failed_returns_to_waiting(self):
        m = self.m()
        m.handle(("lid", "closed"), 0)
        m.handle(("tick",), 301)
        m.handle(("suspend_failed", "x"), 302)
        self.assertEqual(m.mode, WAITING)

    def test_manual_hands_free_on_desktop(self):
        m = self.m()
        m.handle(("glasses", True), 0)
        m.handle(("manual", True), 1)
        self.assertEqual(m.mode, BAG)
        m.handle(("manual", False), 2)
        self.assertEqual(m.mode, DESK)

    def test_no_lid_sensor_never_hibernates(self):
        m = self.m()
        m.handle(("lid", None), 0)
        for t in range(0, 2000, 100):
            self.assertNotIn("hibernate", kinds(m.handle(("tick",), t)))
        self.assertEqual(m.mode, DESK)


def feed_wave(det, t0, axis, amps, half=0.12, hz=500):
    """Pošle řadu půlvln rychlosti (°/s) na osu 'yaw' / 'pitch'. Vrací první gesto."""
    import math
    t, out = t0, None
    for a in amps:
        n = int(half * hz)
        for i in range(n):
            v = a * math.sin(math.pi * i / n)
            g = det.feed(t, v if axis == "yaw" else 0.0, v if axis == "pitch" else 0.0)
            out = out or g
            t += 1.0 / hz
    for _ in range(50):
        g = det.feed(t, 0.0, 0.0)
        out = out or g
        t += 1.0 / hz
    return out


class GesturesTest(unittest.TestCase):
    def test_nod(self):
        self.assertEqual(feed_wave(GestureDetector(), 0, "pitch", [-90, 90]), NOD)

    def test_shake(self):
        self.assertEqual(feed_wave(GestureDetector(), 0, "yaw", [120, -120, 120]), SHAKE)

    def test_slow_movement_is_not_gesture(self):
        self.assertIsNone(feed_wave(GestureDetector(), 0, "pitch", [-30, 30, -30], half=0.4))
        self.assertIsNone(feed_wave(GestureDetector(), 0, "yaw", [40, -40, 40], half=0.4))

    def test_single_turn_is_not_shake(self):
        self.assertIsNone(feed_wave(GestureDetector(), 0, "yaw", [150]))

    def test_button_hold_and_press(self):
        b = ButtonTracker(hold_s=2.0)
        b.down(0)
        self.assertEqual(b.up(0.2), "press")
        b.down(1)
        self.assertIsNone(b.tick(2.0))
        self.assertEqual(b.tick(3.1), "hold")
        self.assertIsNone(b.up(3.5))             # podržení nespustí ještě "press"


class MenuTest(unittest.TestCase):
    def items(self, n=4):
        return [MenuItem(str(i), f"p{i}") for i in range(n)]

    def test_pitch_selects(self):
        m = Menu("x", self.items(), step_deg=7, dwell_s=2.5)
        m.update(10.0, 0.03)
        self.assertEqual(m.index, 0)
        m.update(10.0 - 7.5, 0.03)
        self.assertEqual(m.index, 1)
        m.update(10.0 - 15, 0.03)
        self.assertEqual(m.index, 2)

    def test_edge_rebases_reference(self):
        m = Menu("x", self.items(3), step_deg=7)
        m.update(0.0, 0.03)
        m.update(30.0, 0.03)                     # hodně nahoru: zůstane na 0
        self.assertEqual(m.index, 0)
        m.update(30.0 - 7.5, 0.03)               # hned o krok zpět dolů = položka 1
        self.assertEqual(m.index, 1)

    def test_dwell_confirms_only_after_moving(self):
        m = Menu("x", self.items(), step_deg=7, dwell_s=1.0)
        got = None
        for _ in range(60):                      # hned po otevření se nic nepotvrdí samo
            got = got or m.update(0.0, 0.03)
        self.assertIsNone(got)
        for _ in range(60):
            got = got or m.update(-7.5, 0.03)
        self.assertEqual(got.id, "1")
        self.assertEqual(m.press().id, "1")


class AdvisorTest(unittest.TestCase):
    def test_capabilities(self):
        self.assertEqual(capabilities(Snapshot(mode=DESK, lid="open")).level, FULL)
        s = Snapshot(mode=BAG, lid="closed", imu_alive=True, button_alive=True)
        self.assertEqual(capabilities(s).level, HEAD)
        s = Snapshot(mode=BAG, lid="closed")
        self.assertEqual(capabilities(s).level, NONE)
        s = Snapshot(mode=BAG, lid="closed", physical_input_age=3.0)   # externí klávesnice
        self.assertEqual(capabilities(s).level, FULL)

    def test_guidance_bag_lists_how_to_control(self):
        s = Snapshot(mode=BAG, lid="closed", imu_alive=True, button_alive=True)
        g = guidance(s, capabilities(s))
        self.assertTrue(g.show)
        self.assertTrue(any("tlačítko" in how for how, _ in g.options))

    def test_guidance_waiting_has_countdown(self):
        s = Snapshot(mode=WAITING, lid="closed", remaining=90)
        g = guidance(s, capabilities(s), wait_total=300)
        self.assertIn("1:30", g.text)
        self.assertAlmostEqual(g.progress, 0.7)

    def test_desk_overlay_hidden_but_lid_warning_kept(self):
        s = Snapshot(mode=DESK, lid="open", lid_action_ok=False)
        g = guidance(s, capabilities(s))
        self.assertFalse(g.show)
        self.assertTrue(g.warnings)

    def test_struggle_levels(self):
        st = Struggle()
        self.assertEqual(st.level(0), 0)
        st.note(1, "timeout")
        self.assertEqual(st.level(2), 1)
        st.note(3, "timeout")
        st.note(4, "menu_abandoned")
        self.assertEqual(st.level(5), 2)
        s = Snapshot(mode=BAG, lid="closed", imu_alive=True, button_alive=True)
        self.assertEqual(guidance(s, capabilities(s), 2).title, "Jednodušší ovládání")
        st.note(6, "success")
        self.assertEqual(st.level(7), 0)
        st.note(8, "timeout")
        self.assertEqual(st.level(8 + Struggle.WINDOW + 1), 0)


class Boom(Module):
    id, name = "boom", "Boom"

    def tick(self, now):
        raise RuntimeError("x")


class Fine(Module):
    id, name = "fine", "Fine"
    ticks = 0

    def tick(self, now):
        self.ticks += 1


class ModulesTest(unittest.TestCase):
    def test_failing_module_is_isolated_and_disabled(self):
        mm = ModuleManager(log=lambda *_: None)
        mm.load(["boom", "fine"], factory=lambda i: {"boom": Boom, "fine": Fine}[i]())
        mm.start_all(lambda mid: None)
        for _ in range(5):
            mm.each("tick", 0)
        self.assertFalse(mm.mods["boom"].active)
        self.assertIn("Boom", mm.failed)
        self.assertEqual(mm.mods["fine"].ticks, 5)

    def test_missing_module_does_not_break_loading(self):
        mm = ModuleManager(log=lambda *_: None)
        mm.load(["neexistuje_xyz"])
        self.assertEqual(mm.failed, ["neexistuje_xyz"])


from koren import profiles  # noqa: E402


class ProfilesTest(unittest.TestCase):
    def test_auto_follows_glasses(self):
        self.assertEqual(profiles.effective("auto", True), profiles.GLASSES)
        self.assertEqual(profiles.effective("auto", False), profiles.TRAY)
        self.assertEqual(profiles.effective("monitor", False), profiles.MONITOR)
        self.assertEqual(profiles.effective("nesmysl", True), profiles.TRAY)

    def test_tray_runs_nothing(self):
        p = profiles.plan(profiles.TRAY, DESK)
        self.assertFalse(p.streams or p.modules or p.overlay or p.gui or p.laptop_screen_off)

    def test_monitor_shows_gui_without_modules(self):
        p = profiles.plan(profiles.MONITOR, DESK)
        self.assertTrue(p.gui and p.streams)
        self.assertFalse(p.modules or p.overlay)

    def test_glasses_runs_modules_and_hides_laptop_screen(self):
        p = profiles.plan(profiles.GLASSES, DESK)
        self.assertTrue(p.modules and p.overlay and p.laptop_screen_off)

    def test_closed_lid_overrides_any_profile(self):
        for eff in (profiles.TRAY, profiles.MONITOR, profiles.GLASSES):
            for mode in (BAG, WAITING):
                p = profiles.plan(eff, mode)
                self.assertTrue(p.streams and p.modules and p.overlay)
                self.assertFalse(p.gui or p.laptop_screen_off)


if __name__ == "__main__":
    unittest.main()
