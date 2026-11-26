"""Služba tváří: vlákno kamery a rozpoznávání + vlákno trénování. Kořen (Tk) se jí jen ptá na stav
a posílá příkazy; nic z toho neblokuje hlavní vlákno.

Tok:
  kamera -> detekce (YuNet) -> stopy (Tracker) -> otisk (SFace) jen když je potřeba -> hlasování -> jméno
  výběr tváře -> sběr 8 různých vzorků ze stopy (pár sekund) -> fronta -> trénink:
     podobá se někomu v databázi?  ano -> otázka „Je to X?“ (kývnutí / zavrtění / tlačítko)
                                    ne  -> nová osoba „Osoba N“ s popisem
  -> přestavba galerie -> tvář se od teď poznává živě

„Trénink“ tady znamená spočítat otisky vzorků, zkontrolovat duplicity a přestavět galerii. SFace se
nepřeučuje, takže nová osoba se pozná hned po zpracování fronty (zlomek sekundy na CPU).
"""
import base64
import queue
import threading
import time
from typing import Optional

import cv2

from . import DATA_DIR
from .engine import FaceEngine, jpg
from .recog import MAYBE, THRESHOLD, Gallery, Tracker, diverse, mean_unit
from .store import Store, emb_dec

COLLECT_WANT = 8          # vzorků na uložení
COLLECT_S = 8.0           # nejdéle tolik sekund sbírat
COLLECT_MIN = 3           # méně vzorků = nepovedlo se
MIN_FACE = 48             # menší tváře se jen ukážou, nerozpoznávají
RECOG_EVERY = 0.6         # s, obnova rozpoznání stopy
VISIT_GAP = 600           # s, po této době bez vidění se počítá nová návštěva
AUTO_LEARN_SIM = 0.55     # jistě poznaná tvář smí přidat vzorek (galerie se zlepšuje sama)
AUTO_LEARN_EVERY = 90     # s, nejvýš jeden automatický vzorek na osobu za tuto dobu
OUT_W = 960               # šířka videa pro okno


class FacesService:
    def __init__(self, cfg: dict, log=print, data_dir: str = DATA_DIR, plain_key: bool = False):
        self.cfg, self.log = cfg, log
        self.threshold = float(cfg.get("threshold", THRESHOLD))
        self.maybe = float(cfg.get("maybe", MAYBE))
        self.store = Store(data_dir, plain_key=plain_key, log=log)
        self.gallery = Gallery(self.store.gallery_input())
        self._gver = self.store.version
        self.engine: Optional[FaceEngine] = None
        self.cam = None
        self.tracker = Tracker()
        self.events: "queue.Queue[tuple]" = queue.Queue()   # pro hlavní vlákno: ("ask", job) ("toast", text)
        self.card: Optional[str] = None                     # otevřená osobní karta (pid)
        self.cam_index = cfg.get("camera")                  # None = automaticky
        self.state = "off"                                  # off / opening / on / error
        self.error = ""
        self.fps = 0.0
        self._viewer = 0.0
        self._hold = False                                  # kamera zapnutá ručně (bez okna)
        self._keep_until = 0.0                              # kamera dočasně zapnutá (příkaz z menu)
        self._pending = None                                # (tid, pid, do kdy) ukládání čeká na kameru / tvář
        self._skip = set()
        self._cmd: "queue.Queue[tuple]" = queue.Queue()
        self._jpg, self._jn = None, 0
        self._jcond = threading.Condition()
        self._tracks_json = {"tracks": [], "w": 16, "h": 9}
        self._learned = {}
        self._wake_train = threading.Event()
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._loop, name="faces", daemon=True)
        self._tt = threading.Thread(target=self._trainer, name="faces-train", daemon=True)

    # ================================================================ život
    def start(self):
        self._th.start()
        self._tt.start()
        self._wake_train.set()

    def stop(self):
        self._stop.set()
        self._wake_train.set()
        with self._jcond:
            self._jcond.notify_all()
        self._th.join(timeout=3)
        self._tt.join(timeout=3)
        self._close_cam()
        try:
            if self.store.dirty:
                self.store.save()
        except Exception as e:
            self.log(f"[FACES] uložení při ukončení: {e}")

    def viewer(self):
        """Okno se dívá (volá se z HTTP vlákna při každém dotazu)."""
        self._viewer = time.monotonic()

    def hold(self, on: bool):
        self._hold = on
        if on:
            self._viewer = time.monotonic()

    def keep(self, seconds: float):
        """Zapnout kameru aspoň na tolik sekund (i bez otevřeného okna)."""
        self._keep_until = max(self._keep_until, time.monotonic() + seconds)

    @property
    def watching(self) -> bool:
        idle = float(self.cfg.get("idle_off_s", 20))
        now = time.monotonic()
        return (self._hold or now < self._keep_until or now - self._viewer < idle or self._pending is not None
                or any(tr.collect for tr in list(self.tracker.tracks.values())))

    # ================================================================ příkazy (z hlavního vlákna)
    def enroll(self, tid: Optional[int] = None, pid: Optional[str] = None):
        """Uložit tvář ze stopy tid (None = největší tvář v záběru). pid = doplnit snímky známé osobě."""
        self.keep(COLLECT_S + 8)
        self._cmd.put(("enroll", tid, pid))

    def next_camera(self):
        self._cmd.put(("nextcam",))

    def resolve(self, jid: str, same: bool):
        self._cmd.put(("resolve", jid, same))
        self._wake_train.set()

    # ================================================================ vlákno kamery
    def _open_cam(self):
        from .camera import Camera
        self.state, self.error = "opening", ""
        if self.engine is None:
            self.engine = FaceEngine(float(self.cfg.get("det_score", 0.8)))
        cam = Camera(self.cam_index, int(self.cfg.get("cam_w", 1280)), int(self.cfg.get("cam_h", 720)), log=self.log)
        if cam.open(skip=self._skip):
            self.cam, self.state = cam, "on"
            self.cam_index = cam.index
        else:
            self.state, self.error = "error", cam.error

    def _close_cam(self):
        if self.cam is not None:
            self.cam.close()
            self.cam = None
            self.log("[FACES] kamera vypnutá")
        self.tracker = Tracker()
        self._tracks_json = {"tracks": [], "w": 16, "h": 9}
        if self.state != "error":
            self.state = "off"

    def _loop(self):
        last_n, t_err, frames, t_fps = -1, 0.0, 0, time.monotonic()
        while not self._stop.is_set():
            try:
                self._commands()
                now = time.monotonic()
                if not self.watching:
                    if self.cam is not None:
                        self._close_cam()
                    time.sleep(0.2)
                    continue
                if self.cam is None:
                    if self.state == "error" and now - t_err < 10:
                        time.sleep(0.2)
                        continue
                    self._open_cam()
                    t_err = now
                    continue
                if not self.cam.alive:
                    self.error = self.cam.error or "kamera spadla"
                    self._close_cam()
                    self.state = "error"
                    t_err = now
                    continue
                f, t, n = self.cam.latest()
                if f is None or n == last_n:
                    time.sleep(0.01)
                    continue
                last_n = n
                self._process(f, now)
                frames += 1
                if now - t_fps >= 2.0:
                    self.fps, frames, t_fps = frames / (now - t_fps), 0, now
            except Exception as e:
                import traceback
                self.log(f"[FACES] chyba: {type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}")
                self.error, self.state = f"{type(e).__name__}: {e}", "error"
                self._close_cam()
                self.state = "error"
                time.sleep(2.0)

    def _commands(self):
        while True:
            try:
                c = self._cmd.get_nowait()
            except queue.Empty:
                return
            if c[0] == "enroll":
                self._pending = (c[1], c[2], time.monotonic() + 8.0)
            elif c[0] == "nextcam":
                if self.cam_index is not None:
                    self._skip.add(self.cam_index)
                self.cam_index = None
                self._close_cam()
                self.state = "off"
                if len(self._skip) > 5:
                    self._skip.clear()
            elif c[0] == "resolve":
                self._resolve(c[1], c[2])

    def _try_pending(self, now: float):
        """Ukládání čeká, než se zapne kamera a objeví tvář (příkaz z menu bez okna)."""
        tid, pid, until = self._pending
        vis = self.tracker.visible(now)
        tr = self.tracker.tracks.get(tid) if tid is not None else None
        if tr is None and vis:
            tr = max(vis, key=lambda x: x.bbox[2] * x.bbox[3])
        if tr is None:
            if now > until:
                self._pending = None
                self.events.put(("toast", "Žádná tvář v záběru, nic neukládám"))
            return
        self._pending = None
        self._start_collect(tr, pid)

    def _start_collect(self, tr, pid):
        if pid is None and tr.level == "known" and tr.pid:
            pid = tr.pid               # už ji známe: jen doplnit snímky
        tr.collect = {"samples": [], "best": None, "until": time.monotonic() + COLLECT_S, "pid": pid}
        name = self.store.person(pid)["name"] if pid and self.store.person(pid) else ""
        self.events.put(("toast", f"Ukládám tvář{' – ' + name if name else ''}, chvíli se dívej na ni"))

    def _process(self, f, now: float):
        H, W = f.shape[:2]
        if self.store.version != self._gver:            # změna z hlavního vlákna (smazání, úprava)
            self.gallery = Gallery(self.store.gallery_input())
            self._gver = self.store.version
        dets = self.engine.detect(f)
        pairs = self.tracker.update([d.bbox for d in dets], now)
        for tr, i in pairs:
            d = dets[i]
            tr.score = d.score
            small = min(d.bbox[2], d.bbox[3]) < MIN_FACE
            need = tr.collect is not None or (not small and now - tr.t_recog >= (0.15 if len(tr.votes) < 3 else RECOG_EVERY))
            if not need:
                continue
            try:
                e, sharp, crop = self.engine.embed(f, d)
            except cv2.error:
                continue
            tr.t_recog = now
            if not tr.collect:
                pid, sim = self.gallery.best(e)
                tr.vote(pid, sim, self.threshold, self.maybe)
                if tr.level == "known":
                    self._known(tr, e, sim, sharp, now)
            else:
                self._collect(tr, e, sharp, crop, now)
        if self._pending is not None:
            self._try_pending(now)
        for tr in list(self.tracker.tracks.values()):     # sběr u stopy, která zmizela / vypršela
            if tr.collect and (now > tr.collect["until"] or now - tr.t_seen > 1.0):
                self._finish_collect(tr)
        self._publish(f, W, H, now)

    def _known(self, tr, e, sim, sharp, now):
        if not tr.counted:
            p = self.store.person(tr.pid)
            if p is not None:
                gap = time.time() - (p.get("last_seen") or 0)
                self.store.seen(tr.pid, time.time(), new_visit=gap > VISIT_GAP)
                tr.counted = True
        elif int(now) % 5 == 0:
            self.store.seen(tr.pid, time.time(), new_visit=False)
        # samoučení: jen velmi jistá, ostrá a jiná než dosavadní vzorky
        if sim >= AUTO_LEARN_SIM and sharp >= 60 and now - self._learned.get(tr.pid, -1e9) > AUTO_LEARN_EVERY:
            p = self.store.person(tr.pid)
            if p is not None and diverse([emb_dec(s) for s in p["samples"]], e, 0.85):
                self.store.add_samples(tr.pid, [e], auto=True)
                self._learned[tr.pid] = now
                self.gallery = Gallery(self.store.gallery_input())
                self._gver = self.store.version

    def _collect(self, tr, e, sharp, crop, now):
        c = tr.collect
        if sharp < 25 or tr.score < 0.8:
            return
        if diverse(c["samples"], e, 0.96):
            c["samples"].append(e)
            q = tr.score * min(1.0, sharp / 200.0)
            if c["best"] is None or q > c["best"][0]:
                c["best"] = (q, crop.copy())
        if len(c["samples"]) >= COLLECT_WANT:
            self._finish_collect(tr)

    def _finish_collect(self, tr):
        c, tr.collect = tr.collect, None
        if c is None:
            return
        if len(c["samples"]) < COLLECT_MIN:
            self.events.put(("toast", "Tvář se nepodařilo uložit, málo ostrých snímků. Zkus to znovu blíž."))
            return
        thumb = None
        if c["best"] is not None:
            b = jpg(cv2.resize(c["best"][1], (112, 112)), 85)
            thumb = base64.b64encode(b).decode("ascii") if b else None
        kind = "add" if c["pid"] else "new"
        j = self.store.enqueue(c["samples"], thumb, kind=kind, pid=c["pid"], seen_t=time.time())
        tr.votes.clear()
        tr.t_recog = -1e9
        self.events.put(("toast", f"Tvář uložena ({len(c['samples'])} snímků), zařazena k trénování"))
        self._wake_train.set()
        return j

    def _publish(self, f, W, H, now):
        out = []
        for tr in self.tracker.visible(now):
            x, y, w, h = tr.bbox
            p = self.store.person(tr.pid) if tr.pid else None
            out.append({
                "id": tr.id, "x": x / W, "y": y / H, "w": w / W, "h": h / H,
                "level": tr.level if p else "unknown", "pid": tr.pid if p else None,
                "name": p["name"] if p else "", "relation": p.get("relation", "") if p else "",
                "note": (p.get("note") or "")[:60] if p else "",
                "sim": round(tr.sim, 2), "small": min(w, h) < MIN_FACE,
                "collect": (len(tr.collect["samples"]), COLLECT_WANT) if tr.collect else None,
            })
        self._tracks_json = {"tracks": out, "w": W, "h": H}
        s = OUT_W / float(W)
        small = cv2.resize(f, (OUT_W, int(H * s))) if s < 1 else f
        b = jpg(small, 75)
        with self._jcond:
            self._jpg, self._jn = b, self._jn + 1
            self._jcond.notify_all()

    # ================================================================ vlákno trénování
    def _trainer(self):
        t_save = time.monotonic()
        while not self._stop.is_set():
            self._wake_train.wait(5.0)
            self._wake_train.clear()
            try:
                while not self._stop.is_set():
                    j = self.store.next_job()
                    if j is None:
                        break
                    self._train(j)
                if self.store.dirty and time.monotonic() - t_save > 20:
                    self.store.save()
                    t_save = time.monotonic()
            except Exception as e:
                import traceback
                self.log(f"[FACES] trénink: {type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}")
                time.sleep(2.0)

    def _train(self, j: dict):
        with self.store.lock:
            j["status"], j["msg"] = "training", "trénuji: otisky a kontrola duplicit"
        samples = [emb_dec(s) for s in j["samples"]]
        if not samples:
            self.store.finish(j, "failed", "bez vzorků")
            return
        if j["kind"] == "add" and self.store.person(j["pid"]):
            self.store.add_samples(j["pid"], samples, thumb=j["thumb"] or None)
            self._rebuilt(j, j["pid"], f"doplněno {len(samples)} snímků")
            return
        cand, sim = self.gallery.best(mean_unit(samples))
        if cand and sim >= self.maybe and self.store.person(cand):
            with self.store.lock:
                j["status"], j["cand"], j["cand_sim"] = "ask", cand, round(sim, 2)
                j["msg"] = f"podobá se: {self.store.person(cand)['name']} ({int(sim * 100)} %)"
                self.store.touch()
            self.store.save()
            self.events.put(("ask", j["id"]))
            return
        self._new_person(j, samples)

    def _new_person(self, j, samples):
        pid = self.store.new_person(samples, j["thumb"] or None, j.get("seen_t") or time.time())
        p = self.store.person(pid)
        self._rebuilt(j, pid, f"nová osoba: {p['name']}")
        self.events.put(("new", pid))

    def _rebuilt(self, j, pid, msg):
        self.gallery = Gallery(self.store.gallery_input())
        self._gver = self.store.version
        self.store.finish(j, "done", msg, pid=pid)
        self.store.save()
        self.events.put(("toast", f"Hotovo, {msg}. Rozpoznávám."))

    def _resolve(self, jid: str, same: bool):
        j = self.store.job(jid)
        if j is None or j["status"] != "ask":
            return
        samples = [emb_dec(s) for s in j["samples"]]
        if same and self.store.person(j.get("cand")):
            self.store.add_samples(j["cand"], samples, thumb=j["thumb"] or None)
            self._rebuilt(j, j["cand"], f"přidáno k osobě {self.store.person(j['cand'])['name']}")
        else:
            self._new_person(j, samples)

    # ================================================================ výstup pro okno
    def tracks_snapshot(self) -> list:
        return list(self._tracks_json["tracks"])

    def tracks(self) -> dict:
        self.viewer()
        d = dict(self._tracks_json)
        d.update(self.summary())
        return d

    def summary(self) -> dict:
        with self.store.lock:
            q = [{"id": j["id"], "status": j["status"], "kind": j["kind"], "msg": j["msg"],
                  "pid": j.get("pid"), "cand": j.get("cand"), "t": j["t"]} for j in self.store.queue]
            people = sorted(({"pid": pid, "name": p["name"], "relation": p.get("relation", ""),
                              "last_seen": p.get("last_seen"), "seen": p.get("seen", 0),
                              "samples": len(p["samples"])}
                             for pid, p in self.store.persons.items()), key=lambda x: -(x["last_seen"] or 0))
        return {"state": self.state, "error": self.error, "fps": round(self.fps, 1), "cam": self.cam_index,
                "pending": self._pending is not None, "hold": self._hold,
                "queue": q, "people": people, "card": self.card}

    def person_json(self, pid: str) -> Optional[dict]:
        with self.store.lock:
            p = self.store.person(pid)
            if p is None:
                return None
            d = {k: p.get(k) for k in ("name", "relation", "note", "created", "updated", "first_seen",
                                       "last_seen", "seen", "auto")}
            d["pid"], d["samples"], d["has_thumb"] = pid, len(p["samples"]), bool(p.get("thumb"))
            return d

    def thumb(self, key: str) -> Optional[bytes]:
        """Výřez osoby (pid) nebo úlohy ve frontě (j:<id>)."""
        with self.store.lock:
            if key.startswith("j:"):
                j = self.store.job(key[2:])
                s = j.get("thumb") if j else None
            else:
                p = self.store.person(key)
                s = p.get("thumb") if p else None
        return base64.b64decode(s) if s else None

    def mjpeg(self):
        """Generátor snímků pro /video.mjpg (HTTP vlákno)."""
        n = -1
        while not self._stop.is_set():
            self.viewer()
            with self._jcond:
                if self._jn == n:
                    self._jcond.wait(1.0)
                if self._jn == n or self._jpg is None:
                    b = None
                else:
                    b, n = self._jpg, self._jn
            if b is None:
                continue
            yield b
