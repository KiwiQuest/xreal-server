"""Testy modulu Tváře bez kamery a bez modelů:  python -m unittest discover tests

Otisky tváří jsou tu náhodné vektory: „stejná osoba“ = stejný základ + šum.
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import numpy as np
except ImportError:            # bez numpy se modul Tváře nespustí, testy se přeskočí
    np = None

needs_np = unittest.skipIf(np is None, "chybí numpy")


def _crypto():
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def _cv2():
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        return False


class Faces:
    def __init__(self, seed=3):
        self.rng = np.random.default_rng(seed)
        self.base = [self.rng.normal(size=128).astype(np.float32) for _ in range(5)]

    def of(self, i, noise=0.25):
        x = self.base[i] + self.rng.normal(scale=noise, size=128).astype(np.float32) * np.linalg.norm(self.base[i]) / 11.3
        return x / np.linalg.norm(x)


@needs_np
class GalleryTest(unittest.TestCase):
    def test_match_picks_right_person(self):
        from koren.faces.recog import THRESHOLD, Gallery
        F = Faces()
        g = Gallery({"a": [F.of(0), F.of(0)], "b": [F.of(1)]})
        self.assertEqual(len(g), 2)
        pid, sim = g.best(F.of(0))
        self.assertEqual(pid, "a")
        self.assertGreaterEqual(sim, THRESHOLD)
        pid, sim = g.best(F.of(3))
        self.assertLess(sim, THRESHOLD)          # neznámý člověk

    def test_empty_gallery(self):
        from koren.faces.recog import Gallery
        self.assertEqual(Gallery().best(np.ones(128)), (None, 0.0))

    def test_diverse(self):
        from koren.faces.recog import diverse
        F = Faces()
        e = F.of(0, 0.0)
        self.assertTrue(diverse([], e))
        self.assertFalse(diverse([e], e))
        self.assertTrue(diverse([e], F.of(2)))


@needs_np
class TrackerTest(unittest.TestCase):
    def test_same_face_keeps_id_and_lost_expires(self):
        from koren.faces.recog import Tracker
        tr = Tracker(ttl=1.0)
        a = tr.update([(100, 100, 80, 80)], 0.0)[0][0]
        b = tr.update([(110, 104, 82, 80)], 0.1)[0][0]
        self.assertEqual(a.id, b.id)
        tr.update([], 2.0)
        self.assertEqual(tr.tracks, {})

    def test_fast_head_turn_still_same_track(self):
        from koren.faces.recog import Tracker
        tr = Tracker()
        a = tr.update([(100, 100, 80, 80)], 0.0)[0][0]
        b = tr.update([(150, 100, 80, 80)], 0.05)[0][0]     # IoU malé, střed blízko
        self.assertEqual(a.id, b.id)

    def test_two_faces_two_tracks(self):
        from koren.faces.recog import Tracker
        tr = Tracker()
        p = tr.update([(0, 0, 50, 50), (400, 0, 50, 50)], 0.0)
        self.assertEqual(len({t.id for t, _ in p}), 2)
        p2 = tr.update([(402, 2, 50, 50), (2, 1, 50, 50)], 0.1)
        self.assertEqual({(t.id, i) for t, i in p2}, {(p[1][0].id, 0), (p[0][0].id, 1)})

    def test_vote_is_stable(self):
        from koren.faces.recog import Track
        t = Track(1, (0, 0, 10, 10), 0)
        for _ in range(4):
            t.vote("a", 0.6)
        t.vote("b", 0.4)                          # jedno špatné rozpoznání jméno nepřepne
        self.assertEqual((t.pid, t.level), ("a", "known"))
        t2 = Track(2, (0, 0, 10, 10), 0)
        t2.vote(None, 0.1)
        self.assertEqual(t2.level, "unknown")
        t2.vote("c", 0.33)
        self.assertEqual(t2.level, "maybe")


@needs_np
@unittest.skipUnless(_crypto(), "chybí cryptography")
class StoreTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_encrypted_roundtrip_and_delete(self):
        from koren.faces.store import Store, emb_enc
        F = Faces()
        s = Store(self.d, plain_key=True)
        pid = s.new_person([F.of(0)], None, 1.0)
        s.edit(pid, name="Babička", relation="rodina", note="má ráda kávu")
        s.save()
        with open(os.path.join(self.d, "people.vault"), "rb") as f:
            raw = f.read()
        self.assertNotIn("Babička".encode(), raw)
        self.assertNotIn(emb_enc(F.of(0, 0)).encode()[:20], raw)
        s2 = Store(self.d, plain_key=True)
        self.assertEqual(s2.person(pid)["name"], "Babička")
        self.assertEqual(s2.person(pid)["relation"], "rodina")
        self.assertTrue(s2.delete(pid))
        self.assertIsNone(s2.person(pid))

    def test_wipe_makes_old_file_unreadable(self):
        from koren.faces.store import Store
        F = Faces()
        s = Store(self.d, plain_key=True)
        s.new_person([F.of(0)], None, 1.0)
        s.save()
        s.wipe()
        s.save()
        self.assertEqual(Store(self.d, plain_key=True).persons, {})

    def test_interrupted_training_requeued(self):
        from koren.faces.store import Store
        F = Faces()
        s = Store(self.d, plain_key=True)
        j = s.enqueue([F.of(0)], None)
        j["status"] = "training"
        s.save()
        self.assertEqual(Store(self.d, plain_key=True).queue[0]["status"], "queued")


@needs_np
@unittest.skipUnless(_crypto() and _cv2(), "chybí cryptography nebo opencv")
class TrainingQueueTest(unittest.TestCase):
    """Fronta trénování bez kamery: nová osoba, podobná tvář -> otázka, odpověď ano / ne."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        from koren.faces.pipeline import FacesService
        self.svc = FacesService({}, log=lambda *_: None, data_dir=self.d, plain_key=True)
        self.F = Faces()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def run_queue(self):
        while True:
            j = self.svc.store.next_job()
            if j is None:
                return
            self.svc._train(j)

    def events(self):
        out = []
        while not self.svc.events.empty():
            out.append(self.svc.events.get())
        return out

    def test_new_person_then_recognized(self):
        self.svc.store.enqueue([self.F.of(0) for _ in range(5)], None)
        self.run_queue()
        self.assertEqual(len(self.svc.store.persons), 1)
        pid = next(iter(self.svc.store.persons))
        self.assertEqual(self.svc.gallery.best(self.F.of(0))[0], pid)
        self.assertIn(("new", pid), self.events())

    def test_similar_face_asks_then_merges_or_splits(self):
        self.svc.store.enqueue([self.F.of(0) for _ in range(5)], None)
        self.run_queue()
        pid = next(iter(self.svc.store.persons))
        n0 = len(self.svc.store.person(pid)["samples"])
        j = self.svc.store.enqueue([self.F.of(0) for _ in range(4)], None)
        self.run_queue()
        self.assertEqual(j["status"], "ask")
        self.assertEqual(j["cand"], pid)
        self.svc._resolve(j["id"], True)
        self.assertEqual(len(self.svc.store.persons), 1)
        self.assertEqual(len(self.svc.store.person(pid)["samples"]), n0 + 4)
        j2 = self.svc.store.enqueue([self.F.of(0) for _ in range(3)], None)
        self.run_queue()
        self.svc._resolve(j2["id"], False)                   # „ne, jiný člověk“ (třeba dvojče)
        self.assertEqual(len(self.svc.store.persons), 2)
        self.assertEqual(j2["status"], "done")
        self.assertEqual(j2["samples"], [])                  # hotová úloha už otisky nedrží

    def test_add_samples_to_known(self):
        self.svc.store.enqueue([self.F.of(1) for _ in range(3)], None)
        self.run_queue()
        pid = next(iter(self.svc.store.persons))
        self.svc.store.enqueue([self.F.of(1) for _ in range(2)], None, kind="add", pid=pid)
        self.run_queue()
        self.assertEqual(len(self.svc.store.person(pid)["samples"]), 5)


if __name__ == "__main__":
    unittest.main()
