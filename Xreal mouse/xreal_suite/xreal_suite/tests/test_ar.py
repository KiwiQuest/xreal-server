import math
import os
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np                                          # noqa: E402

from synth import Head                                      # noqa: E402
from xreal import ar                                        # noqa: E402
from xreal.config import Config, Runtime                    # noqa: E402
from xreal.engine import Engine, NullOutput                 # noqa: E402

MON = {"x": 2560, "y": 0, "w": 1920, "h": 1080}
CAM_W, CAM_H, FOV = 1280, 720, 70.0
F = (CAM_W / 2) / math.tan(math.radians(FOV / 2))


def project(alpha_h, alpha_v):
    """Presna kamera: uhel od osy (doprava/dolu) -> pixel."""
    return CAM_W / 2 + F * math.tan(math.radians(alpha_h)), CAM_H / 2 + F * math.tan(math.radians(alpha_v))


class Math(unittest.TestCase):
    def test_cam_angle_inverts_projection(self):
        for ah, av in ((0, 0), (10, -5), (-25, 12), (30, 15)):
            px, py = project(ah, av)
            got = ar.cam_angle(px, py, CAM_W, CAM_H, FOV)
            self.assertAlmostEqual(got[0], ah, places=6)
            self.assertAlmostEqual(got[1], av, places=6)

    def test_flip(self):
        px, py = project(10, 0)
        self.assertAlmostEqual(ar.cam_angle(px, py, CAM_W, CAM_H, FOV, flip_x=True)[0], -10, places=6)

    def test_lookup_interpolates(self):
        hist = [(0.0, 0.0, 0.0), (1.0, 10.0, -4.0)]
        self.assertEqual(ar.lookup(hist, 0.5), (5.0, -2.0))
        self.assertEqual(ar.lookup(hist, -1), (0.0, 0.0))
        self.assertEqual(ar.lookup(hist, 9), (10.0, -4.0))
        self.assertEqual(ar.lookup([], 1.0), (0.0, 0.0))

    def test_screen_mapping(self):
        cx, cy = 2560 + 960, 540
        self.assertEqual(ar.to_screen(0, 0, MON, 40, True), (cx, cy))
        x, y = ar.to_screen(5, -2, MON, 40, True)             # 48 px/stupen
        self.assertAlmostEqual(x, cx + 240); self.assertAlmostEqual(y, cy - 96)
        # pevna obrazovka: odecte se aktualni natoceni hlavy
        x, y = ar.to_screen(5, -2, MON, 40, False, head_h=2, head_v=1)
        self.assertAlmostEqual(x, cx + 3 * 48); self.assertAlmostEqual(y, cy - 3 * 48)


class Registration(unittest.TestCase):
    """Cely retezec: IMU -> natoceni hlavy -> kamera vidi objekt -> poloha v bryli sedi."""

    def setUp(self):
        self.cfg = Config(smooth_ms=1.0)
        self.rt = Runtime()
        self.eng = Engine(self.cfg, self.rt, io=NullOutput(), use_hotkeys=False, monitor_fn=lambda: MON)
        self.eng.refresh_monitor()
        self.head = Head()
        for s in self.head.hold(1.3):
            self.eng.process(s, now=s.ts_ns * 1e-9)
        self.rt.enabled = True
        self.cal = {"fov": FOV, "ox": 0.0, "oy": 0.0, "flip_x": False, "flip_y": False}

    def move(self, secs, yaw=0.0, pitch=0.0, yaw_dps=None, pitch_dps=None):
        yaw = yaw if yaw_dps is None else yaw_dps
        pitch = pitch if pitch_dps is None else pitch_dps
        for s in self.head.hold(secs, yaw, pitch):
            self.eng.process(s, now=s.ts_ns * 1e-9)

    def world_of_object_seen_at(self, obj_world, t_frame_back=0.0):
        """Kamera vidi objekt (svetove uhly obj_world) z aktualni polohy hlavy -> pipeline -> svetove uhly."""
        rt = self.rt
        t_now = self.head.t
        # skutecny uhel od osy kamery v okamziku snimku = objekt - natoceni hlavy v ten okamzik
        hh, vv = ar.lookup(rt.view_hist, t_now - t_frame_back)
        px, py = project(obj_world[0] - hh, obj_world[1] - vv)
        head_h, head_v = ar.lookup(rt.view_hist, t_now - t_frame_back)
        return ar.world_points([(px, py)], CAM_W, CAM_H, self.cal, head_h, head_v)[0]

    def test_anchored_screen_object_does_not_move_with_head(self):
        obj = (-4.0, 3.0)                                  # 4 stupne vlevo, 3 dolu od reference
        positions = []
        for yaw, pitch in ((0, 0), (10, 0), (0, 5), (-6, -3)):
            self.move(1.0, yaw, pitch)
            w = self.world_of_object_seen_at(obj)
            positions.append(ar.to_screen(w[0], w[1], MON, 40, True))
        truth = ar.to_screen(obj[0], obj[1], MON, 40, True)
        for p in positions:
            self.assertAlmostEqual(p[0], truth[0], delta=0.4 * 48)      # < 0,4 stupne
            self.assertAlmostEqual(p[1], truth[1], delta=0.4 * 48)

    def test_head_locked_follows_head(self):
        obj = (3.0, -2.0)
        self.move(1.0, yaw_dps=-8.0)                      # otoceni doprava o ~8 stupnu
        w = self.world_of_object_seen_at(obj)
        h, v = self.rt.view_h, self.rt.view_v
        x, y = ar.to_screen(w[0], w[1], MON, 50, False, h, v)
        # co ma byt: aktualni uhel od osy kamery (obj - hlava) na displeji bryli
        ex, ey = ar.to_screen(obj[0] - h, obj[1] - v, MON, 50, True)
        self.assertAlmostEqual(x, ex, delta=0.4 * 38.4)
        self.assertAlmostEqual(y, ey, delta=0.4 * 38.4)

    def test_latency_compensation_matters(self):
        """Hlava se otaci 10 st/s, snimek je 100 ms stary: s historii sedi, s aktualnim natocenim ne."""
        obj = (2.0, 1.0)
        self.move(0.5, yaw_dps=10.0)
        w_ok = self.world_of_object_seen_at(obj, t_frame_back=0.1)
        # bez kompenzace: pouzije se aktualni natoceni misto natoceni v case snimku
        hh, vv = ar.lookup(self.rt.view_hist, self.head.t - 0.1)
        px, py = project(obj[0] - hh, obj[1] - vv)
        w_bad = ar.world_points([(px, py)], CAM_W, CAM_H, self.cal, self.rt.view_h, self.rt.view_v)[0]
        self.assertAlmostEqual(w_ok[0], obj[0], delta=0.3)
        self.assertGreater(abs(w_bad[0] - obj[0]), 0.7)       # ~1 stupen chyby

    def test_calibration_offset_shifts_overlay(self):
        self.cal["ox"], self.cal["oy"] = 1.5, -0.5
        w = self.world_of_object_seen_at((0.0, 0.0))
        self.assertAlmostEqual(w[0], 1.5, delta=0.05)
        self.assertAlmostEqual(w[1], -0.5, delta=0.05)


def project_fisheye(alpha_h, alpha_v, w=512, h=378, fov=100.0):
    """Presna ekvidistantni fisheye kamera: uhly paprsku -> pixel (r = f * theta)."""
    x, y = math.tan(math.radians(alpha_h)), math.tan(math.radians(alpha_v))
    rr = math.hypot(x, y)
    f = (w / 2) / math.radians(fov / 2)
    if rr < 1e-12:
        return w / 2, h / 2
    theta = math.atan(rr)
    return w / 2 + f * theta * x / rr, h / 2 + f * theta * y / rr


class Fisheye(unittest.TestCase):
    """Sirokouhla kamera Eye (SLAM): pixel -> uhel musi sedet i u okraju a v rozich."""

    def test_fisheye_inverts_projection(self):
        for ah, av in ((0, 0), (10, -5), (-40, 20), (45, 0), (0, -35), (38, 30)):
            px, py = project_fisheye(ah, av)
            got = ar.cam_angle(px, py, 512, 378, 100.0, lens="fisheye")
            self.assertAlmostEqual(got[0], ah, places=5)
            self.assertAlmostEqual(got[1], av, places=5)

    def test_rectilinear_model_is_wrong_for_fisheye_edges(self):
        """Proc to cele: bezny model zkalibrovany ve stredu (zorny uhel doladeny tak, aby stred
        sedel) se u fisheye kamery k okraji splete o stupne - presne to, co bylo videt v bryli."""
        f_fish = 256 / math.radians(50)
        fov_rect = math.degrees(2 * math.atan(256 / f_fish))      # stejne meritko ve stredu
        px, py = project_fisheye(2, 0)
        self.assertAlmostEqual(ar.cam_angle(px, py, 512, 378, fov_rect)[0], 2, delta=0.05)
        px, py = project_fisheye(40, 0)
        err = abs(ar.cam_angle(px, py, 512, 378, fov_rect)[0] - 40)
        self.assertGreater(err, 3.0)

    def test_fisheye_center_and_flip(self):
        self.assertEqual(ar.cam_angle(256, 189, 512, 378, 100.0, lens="fisheye"), (0.0, 0.0))
        px, py = project_fisheye(20, 0)
        got = ar.cam_angle(px, py, 512, 378, 100.0, flip_x=True, lens="fisheye")
        self.assertAlmostEqual(got[0], -20, places=5)

    def test_world_points_uses_lens_from_calibration(self):
        px, py = project_fisheye(35, 10)
        cal = {"fov": 100.0, "ox": 0.0, "oy": 0.0, "lens": "fisheye"}
        wh, wv = ar.world_points([(px, py)], 512, 378, cal, 5.0, -2.0)[0]
        self.assertAlmostEqual(wh, 40.0, places=4)
        self.assertAlmostEqual(wv, 8.0, places=4)


class EdgeMarkers(unittest.TestCase):
    """Kamera vidi vic nez displej: objekty mimo obrazovku jako sipka na okraji."""

    def test_on_screen_has_no_marker(self):
        self.assertIsNone(ar.edge_marker(500, 400, 1920, 1080))

    def test_right_and_corner(self):
        mx, my, ux, uy = ar.edge_marker(5000, 540, 1920, 1080, margin=60)
        self.assertAlmostEqual(mx, 1920 - 60); self.assertAlmostEqual(my, 540)
        self.assertAlmostEqual(ux, 1.0); self.assertAlmostEqual(uy, 0.0)
        mx, my, ux, uy = ar.edge_marker(-3000, -3000, 1920, 1080, margin=60)
        self.assertTrue(0 <= mx <= 1920 and 0 <= my <= 1080)
        self.assertTrue(ux < 0 and uy < 0)
        # znacka lezi na primce stred -> objekt
        self.assertAlmostEqual((my - 540) / (mx - 960), (-3000 - 540) / (-3000 - 960), places=6)


class ConfigLens(unittest.TestCase):
    def test_old_config_gets_lens_defaults(self):
        import json
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "cfg.json")
        with open(path, "w", encoding="utf-8") as f:     # stary config bez 'lens' a bez 'rgb'
            json.dump({"ar_cal": {"slam": {"fov": 90.0, "ox": 1.0, "oy": 0.0,
                                           "flip_x": False, "flip_y": True}}}, f)
        cal = Config().load(path).ar_cal
        self.assertEqual(cal["slam"]["lens"], "fisheye")
        self.assertEqual(cal["slam"]["fov"], 90.0)          # kalibrace uzivatele zustane
        self.assertTrue(cal["slam"]["flip_y"])
        self.assertEqual(cal["rgb"]["lens"], "rectilinear")


class Pipeline(unittest.TestCase):
    def test_items_from_cv_polygon_and_box(self):
        import cv2
        cnt = np.array([[[10, 10]], [[100, 10]], [[100, 80]], [[10, 80]]], dtype=np.int32)
        cal = {"fov": 70.0, "ox": 0.0, "oy": 0.0, "flip_x": False, "flip_y": False}
        items = ar.items_from_cv([(10, 10, 90, 70, cnt)], 128, 96, cal, 0.0, 0.0, cv2=cv2)
        self.assertEqual(len(items), 1)
        self.assertEqual(len(items[0].pts), 4)
        boxes = ar.items_from_boxes([(10, 10, 100, 80, "cup", 0.8)], 128, 96, cal, 1.0, 2.0)
        self.assertEqual(boxes[0].label, "cup 80%")

    def test_overlay_draws_with_mock_tk(self):
        tk = MagicMock()
        tk.TclError = Exception
        sys.modules["tkinter"] = tk
        from xreal import ar_overlay
        cfg, rt = Config(), Runtime()
        eng = MagicMock()
        eng.monitor = MON
        state = ar.ArState()
        state.set_layer("x", [ar.ArItem("poly", [(0, 0), (5, 0), (5, 3)], "kniha 90%"),
                              ar.ArItem("box", [(-100, -100), (-99, -100)], "mimo")])
        ov = ar_overlay.ArOverlay(MagicMock(), cfg, rt, eng, state, lambda: "rgb")
        ov.cv.reset_mock()                                # __init__ uz jednou kreslil
        ov.tick()
        self.assertTrue(ov.cv.create_line.called)
        self.assertTrue(ov.cv.create_text.called)
        arrows = [c for c in ov.cv.create_line.call_args_list if c.kwargs.get("arrow") == "last"]
        self.assertEqual(len(arrows), 1)                  # "mimo" -> sipka na okraji
        cfg.ar_edge_markers = False
        ov.cv.reset_mock()
        ov.tick()
        arrows = [c for c in ov.cv.create_line.call_args_list if c.kwargs.get("arrow") == "last"]
        self.assertEqual(arrows, [])
        ov.set_calibration(True)
        ov.cv.reset_mock()
        ov.tick()
        texts = " ".join(str(c) for c in ov.cv.create_text.call_args_list)
        self.assertIn("KALIBRACE", texts)
        ov.close()


if __name__ == "__main__":
    unittest.main()
