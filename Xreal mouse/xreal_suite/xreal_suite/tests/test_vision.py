import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                          # noqa: E402

from xreal import eye, vision                               # noqa: E402


def dark_scene(noise=3, seed=0):
    rng = np.random.default_rng(seed)
    img = rng.normal(12, noise, (eye.H, eye.W)) + np.linspace(0, 10, eye.W)[None, :]
    img[120:250, 200:330] = rng.normal(42, noise, (130, 130))
    img[250:310, 60:130] = rng.normal(35, noise, (60, 70))
    return img.clip(0, 255).astype(np.uint8)


def near(o, truth, tol=8):
    return all(abs(a - b) <= tol for a, b in zip(o[:4], truth))


class Vision(unittest.TestCase):
    def test_finds_objects_in_dark_noisy_image(self):
        for noise in (2, 3, 5):
            img = dark_scene(noise)
            for mode in ("contours", "otsu"):
                _, _, objs = vision.render(img, mode)
                boxes = [o[:4] for o in objs]
                self.assertTrue(any(near(b, (200, 120, 130, 130)) for b in boxes), (mode, noise, boxes))
                self.assertTrue(any(near(b, (60, 250, 70, 60)) for b in boxes), (mode, noise, boxes))

    def test_stretch_brightens(self):
        img = dark_scene()
        self.assertGreater(vision.stretch(img).mean(), 2 * img.mean())

    def test_half_resolution(self):
        out, t, objs = vision.render(dark_scene(), "contours", half=True)
        self.assertEqual(out.shape[:2], (eye.H // 2, eye.W // 2))
        self.assertTrue(objs)

    def test_all_modes_run_and_report_timing(self):
        for mode in vision.MODES:
            out, t, _ = vision.render(dark_scene(), mode, "CLAHE")
            self.assertIn("celkem", t)
            self.assertEqual(out.dtype, np.uint8)

    def test_fast_enough(self):
        img = dark_scene()
        vision.render(img, "contours")
        import time
        t0 = time.perf_counter()
        for _ in range(50):
            vision.render(img, "contours")
        self.assertLess((time.perf_counter() - t0) / 50 * 1000, 25.0)    # velka rezerva

    def test_benchmark_has_rows(self):
        rows = vision.benchmark(dark_scene(), 3)
        self.assertGreaterEqual(len(rows), 8)
        self.assertTrue(all(ms > 0 for _, ms in rows))


class VariantChoice(unittest.TestCase):
    def photo(self):
        # hladky "obrazek" s detaily: gradienty + kruh
        y, x = np.mgrid[0:eye.H, 0:eye.W]
        img = 40 + 0.25 * x + 0.1 * y + 60 * (((x - 250) ** 2 + (y - 190) ** 2) < 80 ** 2)
        return img.clip(0, 255).astype(np.uint8)

    def test_8bit_frame_with_320B_header_is_detected(self):
        """Presne velikost z hlaseni: ramec 193862 B = 6 B hlavicka + 320 B + 512x378 8bit."""
        img = self.photo()
        body = bytes(320) + img.tobytes()
        self.assertEqual(6 + len(body), 193862)
        bodies = [body] * 4
        name, align, score = eye.choose_variant(bodies)
        self.assertEqual((name, align), ("8bit (1 bajt = 1 pixel)", "konec"))
        left, right = eye.decode_variant(body, name, align)
        self.assertEqual(left, img.tobytes())
        self.assertIsNone(right)

    def test_noisy_low_bits_do_not_flip_choice(self):
        rng = np.random.default_rng(3)
        img = self.photo()
        noisy = (img & 0xF0) | rng.integers(0, 16, img.shape, dtype=np.uint8)   # sum jen v dolnich bitech
        body = bytes(320) + noisy.tobytes()
        name, align, _ = eye.choose_variant([body] * 4)
        self.assertEqual(name, "8bit (1 bajt = 1 pixel)")


if __name__ == "__main__":
    unittest.main()


class ColorRender(unittest.TestCase):
    def test_color_base_is_kept_and_contours_drawn_in_color(self):
        gray = dark_scene()
        rgb = np.zeros((eye.H, eye.W, 3), dtype=np.uint8)
        rgb[..., 0] = 200                                    # cervene pozadi = je videt, ze zustala barva
        out, _, _ = vision.render(gray, "orig", base_rgb=rgb)
        self.assertEqual(out.shape, (eye.H, eye.W, 3))
        self.assertTrue((out[..., 0] == 200).all())
        out, _, objs = vision.render(gray, "contours", base_rgb=rgb)
        self.assertEqual(out.shape, (eye.H, eye.W, 3))
        self.assertTrue(objs)
        self.assertTrue((out[..., 1] > 200).any())            # zelene kontury nakreslene do barvy
        out, _, _ = vision.render(gray, "contours", half=True, base_rgb=rgb)
        self.assertEqual(out.shape, (eye.H // 2, eye.W // 2, 3))
