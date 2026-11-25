"""Smoke test GUI se zastupnym tkinterem: chytá preklepy, spatna jmena a vyjimky v kodu."""
import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Var:
    def __init__(self, value=0):
        self.v = value

    def get(self):
        return self.v

    def set(self, v):
        self.v = v


class GuiSmoke(unittest.TestCase):
    def test_app_runs_ticks(self):
        tk, ttk = MagicMock(), MagicMock()
        tk.DoubleVar = tk.BooleanVar = tk.StringVar = Var
        tk.Tk.return_value.winfo_pointerxy.return_value = (500, 400)
        tk.TclError = Exception
        sys.modules["tkinter"], sys.modules["tkinter.ttk"] = tk, ttk
        tk.ttk = ttk
        from xreal.config import Config, Runtime
        from xreal.engine import Engine, NullOutput
        from xreal import gui
        cfg, rt = Config(), Runtime()
        eng = Engine(cfg, rt, io=NullOutput(), use_hotkeys=False,
                     monitor_fn=lambda: {"x": 0, "y": 0, "w": 1920, "h": 1080})
        rt.monitors = [{"x": 0, "y": 0, "w": 1920, "h": 1080, "primary": True, "xreal": False}]
        app = gui.App(cfg, rt, eng)
        tick = app.root.after.call_args_list[0].args[1]
        rt.enabled, rt.dwell_active, rt.dwell_progress = True, True, 0.5
        for _ in range(8):
            tick()
        rt.dwell_active = False
        rt.flash_until = time.time() + 1
        for _ in range(6):
            tick()
        rt.toast_text, rt.toast_ok, rt.toast_until = "STŘED\nOK", True, time.time() + 1
        for _ in range(6):
            tick()
        app.mode_combo.current.return_value = 1
        app._on_mode()
        self.assertEqual(cfg.mode, "view")
        rt.quit_request = True
        tick()
        self.assertTrue(app.root.destroy.called)


if __name__ == "__main__":
    unittest.main()


class EyeSmoke(unittest.TestCase):
    def test_eye_window_ticks_with_fake_stream(self):
        import threading
        import numpy as np
        tk, ttk = MagicMock(), MagicMock()
        tk.DoubleVar = tk.BooleanVar = tk.StringVar = Var
        tk.TclError = Exception
        sys.modules["tkinter"], sys.modules["tkinter.ttk"] = tk, ttk
        tk.ttk = ttk
        from xreal import eye
        from xreal.config import Config
        win = eye.EyeWindow(MagicMock(), Config())
        tick = win.win.after.call_args_list[0].args[1]

        class FakeStream:
            lock = threading.Lock()
            status, fps, frame_len, head_hex = "ok", 15.0, 193862, "27 48"
            variant, chosen, framing, raw_tail = "auto", ("8bit", "konec", 1.0), object(), b""
            right = None
            rx_time = 0.0

            def __init__(self):
                rng = np.random.default_rng(0)
                img = rng.normal(12, 3, (eye.H, eye.W))
                img[100:200, 100:260] = 50
                self.left = img.clip(0, 255).astype(np.uint8).tobytes()
        win.stream = FakeStream()
        for key in list(win.mode_keys):
            win.mode_var.set(win.vision.MODES[key])
            for half in (False, True):
                win.half_var.set(half)
                tick()
        self.assertIn("celkem", win.timing_var.get())
        self.assertTrue(win.tk.PhotoImage.called)
        self.assertIsNotNone(win.last_gray)
        win.run_benchmark()                 # spusti vlakno, nesmi spadnout
        time.sleep(1.5)

        # --- barevna UVC kamera (falesna) ---
        class FakeUvc:
            lock = threading.Lock()
            status, fps, running = "UVC kamera 1: 1280×720", 30.0, True
            rx_time = 0.0

            def __init__(self):
                rng = np.random.default_rng(1)
                self.frame = rng.integers(0, 255, (720, 1280, 3), dtype=np.uint8)
        win.stream = None
        win.uvc = FakeUvc()
        for key in ("orig", "canny", "contours"):
            win.mode_var.set(win.vision.MODES[key])
            tick()
        self.assertIn("celkem", win.timing_var.get())
        win._cams_found([(0, 640, 480), (1, 1280, 720)], ["Integrated Camera", "XREAL Eye"])
        self.assertEqual(win.cam_combo.__setitem__.call_args.args[1][1], "kamera 1: 1280×720")
        win.close()
