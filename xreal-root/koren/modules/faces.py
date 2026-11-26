"""Modul Tváře: živá detekce, uložení vybrané tváře, fronta trénování, databáze osob, rozpoznání
a osobní karta nad živým videem. Ovládá se jen hlavou: myš hlavou (klik pohledem nebo tlačítkem
brýlí) v okně modulu, nebo kořenové menu (podržet tlačítko brýlí) a kývnutí / zavrtění.

Okno modulu je vlastní stránka (`faces_view.html`) v okně kořenové plochy (iframe): černé pozadí =
v brýlích průhledné, takže jde skládat přes jiná okna a v režimu „jen popisky“ visí jména přímo
nad lidmi. Kamera běží jen když se okno dívá (nebo při ukládání), jinak je vypnutá.

Logika je v balíku `koren.faces` (pipeline, recog, store), tady je jen napojení na kořen.
"""
import os
import time

from . import Module

VIEW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faces_view.html")


class Faces(Module):
    id = "faces"
    name = "Tváře"
    description = "Pozná lidi kolem: vyber tvář, uloží se, natrénuje a pak se nad člověkem ukáže jméno."
    z = 120
    needs = ("display",)

    def start(self, ctx):
        super().start(ctx)
        self.svc = None
        self.err = ""
        self.show_n = 0
        self.last_msg = ""
        self._asked = set()
        from ..faces import missing
        miss = missing()
        if miss:
            self.err = "chybí: " + ", ".join(miss) + " → spusť xreal-root\\tvare_install.bat"
            ctx.log(f"[FACES] {self.err}")
            return
        from ..faces.pipeline import FacesService
        cfg = dict(ctx.cfg.faces or {})
        try:
            self.svc = FacesService(cfg, log=ctx.log)
        except Exception as e:
            self.err = f"databázi tváří nejde otevřít: {e}"
            ctx.log(f"[FACES] {self.err}")
            return
        self.svc.start()

    def stop(self):
        if self.svc is not None:
            self.svc.stop()
            self.svc = None

    # ------------------------------------------------------------------ hlavní vlákno
    def tick(self, now):
        if self.svc is None:
            return
        for _ in range(20):
            try:
                ev = self.svc.events.get_nowait()
            except Exception:
                break
            if ev[0] == "toast":
                self.last_msg = ev[1]
                self.ctx.toast(ev[1], 2.5)
            elif ev[0] == "ask":
                self._ask(ev[1])
            elif ev[0] == "new":
                self.svc.card = ev[1]          # nová osoba: rovnou karta k pojmenování a popisu
                self.show_n += 1
        # odpovědi ze starší relace (otázka čekala při restartu)
        for j in list(self.svc.store.queue):
            if j["status"] == "ask" and j["id"] not in self._asked:
                self._ask(j["id"])

    def _ask(self, jid):
        if self.svc is None or jid in self._asked:
            return
        j = self.svc.store.job(jid)
        if j is None or j["status"] != "ask":
            return
        self._asked.add(jid)
        p = self.svc.store.person(j.get("cand") or "")
        name = p["name"] if p else "někdo známý"
        self.ctx.ask(f"Nová tvář se podobá: {name}. Je to {name}?",
                     on_yes=lambda: self.svc and self.svc.resolve(jid, True),
                     on_no=lambda: self.svc and self.svc.resolve(jid, False))

    def _front(self, known=True):
        """Největší (nejbližší) tvář v záběru, volitelně jen poznaná."""
        if self.svc is None:
            return None
        ts = [t for t in self.svc.tracks_snapshot() if (t["pid"] and t["level"] == "known") or not known]
        return max(ts, key=lambda t: t["w"] * t["h"]) if ts else None

    def open_window(self):
        self.show_n += 1

    def card_front(self):
        t = self._front(True)
        if t is None:
            self.svc.keep(30)
            on = self.svc.state == "on"
            self.ctx.toast("Před tebou není nikdo známý" if on else "Zapínám kameru, zkus to za chvíli znovu", 2.5)
            return
        self.svc.card = t["pid"]
        self.open_window()

    def enroll_front(self):
        if self.svc is None:
            return
        self.svc.enroll(None)                   # kamera se zapne i bez okna (na dobu ukládání)
        self.open_window()

    def on_gesture(self, g):
        if g == "shake" and self.svc is not None and self.svc.card:
            self.svc.card = None                # zavrtění hlavou zavře osobní kartu

    # ------------------------------------------------------------------ kořen
    def status(self):
        if self.err:
            return "nepřipraveno"
        if self.svc is None:
            return ""
        # bez živých čísel (počet tváří, fps): karta v panelu se pak nepřekresluje pod kurzorem myši hlavou
        s = self.svc.summary()
        q = sum(1 for j in s["queue"] if j["status"] in ("queued", "training", "ask"))
        cam = {"on": "kamera zapnutá", "opening": "zapínám kameru", "error": "kamera: chyba"}.get(s["state"], "kamera vypnutá")
        return cam + (f" · {q} ve frontě" if q else "")

    def ui(self):
        if self.err or self.svc is None:
            return {"window": False, "control": True, "lines": [self.err or "nenačteno"]}
        s = self.svc.summary()
        lines = [f"Osob v databázi: {len(s['people'])}",
                 {"on": f"Kamera {s['cam']} zapnutá", "opening": "Hledám kameru…",
                  "error": f"Kamera: {s['error']}"}.get(s["state"], "Kamera vypnutá (zapne se s oknem)")]
        if self.last_msg:
            lines.append(self.last_msg)
        return {"window": True, "control": True, "frame": "/mod/faces/", "show": self.show_n, "lines": lines,
                "actions": [{"id": "open", "label": "Otevřít okno"},
                            {"id": "enroll", "label": "Uložit tvář před sebou"},
                            {"id": "card_front", "label": "Karta osoby před sebou"},
                            {"id": "hold", "label": "Kamera stále", "on": s["hold"]},
                            {"id": "nextcam", "label": "Další kamera"}]}

    def ui_action(self, action, value=None):
        svc = self.svc
        if svc is None:
            return
        v = value if isinstance(value, dict) else {}
        if action == "open":
            self.open_window()
        elif action == "enroll":
            tid = value if isinstance(value, int) else v.get("tid")
            svc.enroll(tid, v.get("pid"))
        elif action == "add_samples":
            pid = v.get("pid") or value
            t = next((t for t in svc.tracks_snapshot() if t["pid"] == pid), None)
            svc.enroll(t["id"] if t else None, pid)
        elif action == "card_front":
            self.card_front()
        elif action == "card":
            svc.card = value if isinstance(value, str) and svc.store.person(value) else None
        elif action == "card_close":
            svc.card = None
        elif action == "edit":
            if svc.store.edit(v.get("pid", ""), name=v.get("name"), relation=v.get("relation"), note=v.get("note")):
                svc.store.save()
        elif action == "delete":
            pid = value if isinstance(value, str) else v.get("pid")
            p = svc.store.person(pid or "")
            if p is not None:
                self.ctx.ask(f"Smazat osobu {p['name']} i všechny její snímky?",
                             on_yes=lambda: self._delete(pid))
        elif action == "delete_now":            # potvrzeno dvojím klikem přímo v kartě
            self._delete(v.get("pid") or value)
        elif action == "resolve":
            self._asked.add(v.get("jid"))
            svc.resolve(v.get("jid", ""), bool(v.get("same")))
        elif action == "keyboard":
            kb = self.ctx.app().mods.get("keyboard")
            if kb is not None:
                self.ctx.app().mods.call(kb, "show" if not v.get("close") else "close")
        elif action == "hold":
            svc.hold(not svc._hold)
        elif action == "nextcam":
            svc.next_camera()

    def _delete(self, pid):
        if self.svc and self.svc.store.delete(pid):
            if self.svc.card == pid:
                self.svc.card = None
            self.svc.store.save()
            self.ctx.toast("Osoba smazána", 2.0)

    def menu_items(self):
        from ..menu import MenuItem
        if self.svc is None:
            return []
        items = [MenuItem("faces_open", "Tváře: okno s kamerou", action=self.open_window),
                 MenuItem("faces_enroll", "Tváře: uložit tvář před sebou", action=self.enroll_front)]
        if self.svc.card:
            items.append(MenuItem("faces_close", "Tváře: zavřít kartu osoby",
                                  action=lambda: setattr(self.svc, "card", None)))
        else:
            items.append(MenuItem("faces_card", "Tváře: kdo je přede mnou", action=self.card_front))
        return items

    def home_tiles(self):
        from .home import Tile
        return [Tile("faces", "Tváře", "kdo je kolem", "☺", self.open_window, enabled=self.svc is not None)]

    # ------------------------------------------------------------------ vlákno serveru
    def http_get(self, path, query):
        svc = self.svc
        if path in ("", "index.html"):
            with open(VIEW, "rb") as f:
                return "text/html; charset=utf-8", f.read()
        if svc is None:
            return "application/json; charset=utf-8", {"state": "error", "error": self.err, "tracks": [],
                                                        "people": [], "queue": []}
        if path == "tracks":
            return "application/json; charset=utf-8", svc.tracks()
        if path == "video.mjpg":
            return "stream", "image/jpeg", svc.mjpeg()
        if path == "person":
            p = svc.person_json(query.get("pid", ""))
            return ("application/json; charset=utf-8", p) if p else None
        if path == "thumb":
            b = svc.thumb(query.get("k", ""))
            return ("image/jpeg", b) if b else None
        return None


MODULE = Faces
