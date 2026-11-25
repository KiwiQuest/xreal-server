import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np                                          # noqa: E402

from xreal import dasher, eye                               # noqa: E402


class Dasher(unittest.TestCase):
    def test_model_is_distribution(self):
        m = dasher.load_model("cs")
        for ctx in ("", "a", "ah", "dě", "xyz"):
            ch = m.children(ctx)
            self.assertAlmostEqual(ch[-1][2], 1.0, places=9)
            self.assertTrue(all(b > a for _, a, b in ch))

    def test_likely_letter_gets_big_box(self):
        m = dasher.load_model("cs")
        ch = {c: b - a for c, a, b in m.children("ahoj")}
        self.assertGreater(ch[" "], 0.3)                 # po "ahoj" tesne nasleduje mezera

    def steer(self, target, max_s=40.0):
        eng = dasher.DasherEngine(dasher.load_model("cs"), speed=1.6)
        t = 0.0
        dt = 1 / 60
        while eng.text != target and t < max_s * len(target):
            want = target[len(eng.text)]
            ch = next((c, a, b) for c, a, b in eng.children() if c == want)
            mid = (ch[1] + ch[2]) / 2
            py = (mid - eng.v0) / (eng.v1 - eng.v0)
            eng.step(0.9, min(1.0, max(0.0, py)), dt)
            t += dt
        return eng.text, t

    def test_types_text_by_pointing(self):
        text, t = self.steer("ahoj svete")
        self.assertEqual(text, "ahoj svete")
        self.assertLess(t, 60)

    def test_pointing_left_deletes(self):
        eng = dasher.DasherEngine(dasher.load_model("cs"))
        eng.text, eng.stack = "", []
        # napsat "a" ...
        while eng.text != "a":
            ch = next((c, a, b) for c, a, b in eng.children() if c == "a")
            eng.step(0.9, min(1, max(0, ((ch[1] + ch[2]) / 2 - eng.v0) / (eng.v1 - eng.v0))), 1 / 60)
        # ... a vratit se doleva
        for _ in range(600):
            eng.step(0.0, 0.5, 1 / 60)
            if eng.text == "":
                break
        self.assertEqual(eng.text, "")


def make_stream(n_frames=6, hdr=6, hi_first=True):
    """Syntetickym streamem overime detekci ramcu a dekodovani (format je vymyslen!)."""
    frames = []
    for k in range(n_frames):
        img = np.full((eye.H, eye.W), 17 * (k % 16), dtype=np.uint8)
        img[:, : eye.W // 2] = 255 - 17 * (k % 16)
        packed = bytearray()
        flat = img.tobytes()
        n = (flat[0::2], flat[1::2])
        a = bytes(v // 17 for v in n[0]); b = bytes(v // 17 for v in n[1])
        packed = bytes(((x << 4) | y) if hi_first else ((y << 4) | x) for x, y in zip(a, b))
        body = packed + packed
        frames.append(b"\xAB\xCD" + len(body).to_bytes(4, "big") + body)
    return b"".join(frames), img


class Eye(unittest.TestCase):
    def test_nibbles(self):
        self.assertEqual(eye.unpack_nibbles(bytes([0x0F, 0xA5])), bytes([0, 255, 170, 85]))
        self.assertEqual(eye.unpack_nibbles(bytes([0x0F]), hi_first=False), bytes([255, 0]))

    def test_framing_and_decode(self):
        data, last = make_stream()
        fr = eye.detect_framing(data)
        self.assertIsNotNone(fr)
        self.assertEqual((fr.hdr_len, fr.off, fr.endian, fr.inclusive), (6, 2, "big", False))
        buf = bytearray(data)
        frames = eye.split_frames(buf, fr)
        self.assertEqual(len(frames), 6)
        left, right = eye.decode_payload(frames[-1], fr)
        self.assertEqual(len(left), eye.W * eye.H)
        self.assertEqual(left, last.tobytes())
        self.assertEqual(right, last.tobytes())

    def test_garbage_is_not_framing(self):
        rng = np.random.default_rng(1)
        self.assertIsNone(eye.detect_framing(bytes(rng.integers(0, 256, 300000, dtype=np.uint8))))

    def test_pgm_header(self):
        self.assertTrue(eye.pgm(b"\x00" * 6, 3, 2).startswith(b"P5 3 2 255\n"))


if __name__ == "__main__":
    unittest.main()
