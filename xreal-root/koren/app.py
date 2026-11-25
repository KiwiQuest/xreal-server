"""Hlavní smyčka kořenové aplikace.

Vlákna:
  hlavní (Tk)        overlay, stavový automat, menu, moduly (tick)
  native-window      systray, víko, probuzení, USB, displeje, fyzický vstup  -> fronta
  glasses-*          IMU, tlačítko, přítomnost brýlí                          -> fronta / sinks
Všechno se potkává ve frontě `q`, hlavní vlákno ji vybírá každých 30 ms. Chyba v jednom
kroku se zapíše do logu a smyčka běží dál; když by se hlavní vlákno zaseklo, přestane
psát heartbeat a supervizor (koren.py) proces restartuje.
"""
import os
import queue
import threading
import time
from typing import Optional

from . import win
from .advisor import Snapshot, Struggle, capabilities, guidance
from .config import LOG_DIR, Config
from .gestures import NOD, SHAKE, ButtonTracker
from .glasses import GlassesLink
from .menu import Menu, MenuItem, Prompt
from .modules import _FAIL, Module, ModuleContext, ModuleManager
from .overlay import Overlay, glasses_display_present, pick_display
from .states import BAG, DESK, MODE_LABELS, SUSPEND, WAITING, Machine

TICK_MS = 30
ALL_MODULES = ("head_mouse", "clock", "voice")
TRAY_ICON = {DESK: "desk", BAG: "bag", WAITING: "wait", SUSPEND: "wait"}
BEEPS = {"bag": ((880, 110), (1320, 150)), "wait": ((660, 220),), "warn": ((520, 140), (520, 140)),
         "lost": ((440, 260),)}


class Log:
    def __init__(self, path: str, max_bytes: int = 2_000_000):
        self.path, self.max = path, max_bytes
        self.lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def __call__(self, text: str):
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n"
        with self.lock:
            try:
                if os.path.exists(self.path) and os.path.getsize(self.path) > self.max:
                    os.replace(self.path, self.path + ".1")
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                pass
        try:
            print(line, end="", flush=True)
        except Exception:
            pass


class App:
    def __init__(self, cfg: Config, heartbeat: Optional[str] = None):
        import tkinter as tk
        self.cfg = cfg
        self.log = Log(os.path.join(LOG_DIR, "koren.log"))
        self.heartbeat = heartbeat
        self.q: "queue.Queue[tuple]" = queue.Queue()
        try:
            from xreal import winapi
            winapi.set_dpi_aware()
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.withdraw()
        self.machine = Machine(wait_s=cfg.wait_minutes * 60.0, lost_grace_s=cfg.bag_lost_grace_s)
        self.struggle = Struggle()
        self.overlay = Overlay(self.root, cfg, self.log)
        self.native = win.NativeWindow(self.q.put, self.tray_menu, log=self.log)
        self.glasses = GlassesLink(self.q.put, win.glasses_usb, log=self.log)
        self.mods = ModuleManager(self.log)
        self.btn = ButtonTracker(cfg.menu_hold_s)
        self.menu: Optional[Menu] = None
        self.prompt: Optional[Prompt] = None
        self.power = win.power_caps()
        self.lid_action = win.lid_action()
        self.running = True
        self._t = time.monotonic()
        self._t_hb = self._t_disp = self._t_slow = 0.0
        self._glasses_display = False
        self._focus = False

    # ================================================================ start / stop
    def run(self):
        self.log(f"[KOREN] start, víko: {'ano' if self.power['lid'] else 'ne'}, "
                 f"hibernace: {'ano' if self.power['hibernate'] else 'ne'}, akce víka: {self.lid_action}, "
                 f"dry: {self.cfg.dry_run}")
        self.native.start()
        self.native.ready.wait(3.0)
        self.mods.load(ALL_MODULES)
        for mid, mod in self.mods.mods.items():
            if self.cfg.modules.get(mid, False):
                self._start_module(mod)
        self.glasses.sinks.append(self._imu_to_modules)
        self.glasses.start()
        self.overlay.set_monitor(pick_display(None))
        self.root.after(TICK_MS, self._tick)
        try:
            self.root.mainloop()
        finally:
            self.shutdown()

    def shutdown(self):
        if not self.running:
            return
        self.running = False
        self.log("[KOREN] konec")
        self.mods.stop_all()
        self.glasses.stop()
        self.native.close()
        try:
            self.overlay.destroy()
            self.root.destroy()
        except Exception:
            pass

    def quit(self):
        self.root.after(0, self.root.quit)

    def _start_module(self, mod: Module):
        mod.active = self.mods.call(mod, "start", ModuleContext(self, mod.id)) is not _FAIL
        if not mod.active and mod.name not in self.mods.failed:
            self.mods.failed.append(mod.name)
        if mod.active:
            self.mods.call(mod, "on_mode", DESK, self.machine.mode)

    def _stop_module(self, mod: Module):
        if mod.active:
            self.mods.call(mod, "stop")
            mod.active = False

    def _imu_to_modules(self, s):
        for mod in list(self.mods.mods.values()):
            if mod.active and type(mod).on_imu is not Module.on_imu:
                self.mods.call(mod, "on_imu", s)

    # ===================================================================== smyčka
    def _tick(self):
        try:
            self._step()
        except Exception as e:
            import traceback
            self.log(f"[KOREN] chyba v hlavní smyčce: {type(e).__name__}: {e}\n{traceback.format_exc(limit=5)}")
        finally:
            if self.running:
                self.root.after(TICK_MS, self._tick)

    def _step(self):
        now = time.monotonic()
        dt, self._t = now - self._t, now
        if self.heartbeat and now - self._t_hb > 2.0:
            self._t_hb = now
            try:
                with open(self.heartbeat, "w") as f:
                    f.write(str(time.time()))
            except OSError:
                pass

        for _ in range(300):
            try:
                ev = self.q.get_nowait()
            except queue.Empty:
                break
            self._event(ev, now)

        hold = self.btn.tick(now)
        if hold == "hold":
            if self.menu:
                self.close_menu(abandoned=False)
            elif self.prompt is None:
                self.open_menu()

        self._apply(self.machine.handle(("tick",), now))

        if self.menu:
            item = self.menu.update(self.glasses.pitch_deg, dt)
            if item:
                self._activate(item)
        if self.prompt and self.prompt.expired(now):
            p = self.prompt
            self.struggle.note(now, "timeout")
            if p.repeats < p.max_repeats:
                p.repeats += 1
                p.started = now
                self.say("Zopakuji otázku. " + p.question)
            else:
                self._answer(p.default_yes, auto=True)

        if now - self._t_disp > 3.0:
            self._t_disp = now
            self._glasses_display = glasses_display_present()
            self.overlay.set_monitor(pick_display(self.machine.facts.lid))
        if now - self._t_slow > 30.0:
            self._t_slow = now
            self.lid_action = win.lid_action()

        self._focus_update()
        self.mods.each("tick", now)
        self._render(now)

    # =================================================================== události
    def _event(self, ev: tuple, now: float):
        kind = ev[0]
        if kind in ("lid", "glasses"):
            self.log(f"[EV] {ev}")
            self._apply(self.machine.handle(ev, now))
        elif kind == "power":
            self.log(f"[POWER] {ev[1]}")
            if ev[1] == "resume":
                self._apply(self.machine.handle(("resumed",), now))
                self.glasses.check_now()
                self.glasses.recalibrate()
        elif kind == "devchange":
            self.glasses.check_now()
        elif kind == "display":
            self._t_disp = 0.0
        elif kind == "gesture":
            self._gesture(ev[1], now)
        elif kind == "button":
            down, t = ev[1], ev[2]
            if down:
                self.btn.down(now)
                if not self._focus:
                    self.mods.each("on_button", True, t)
            else:
                r = self.btn.up(now)
                if self._focus:
                    if r == "press":
                        self._confirm_press(now)
                else:
                    self.mods.each("on_button", False, t)
        elif kind == "tray":
            self._tray_cmd(ev[1])
        elif kind == "tray_open":
            self.overlay.t_change = now           # ukázat celou nápovědu znovu
            self.native.balloon("XREAL Kořen", self._status_line())
        elif kind in ("resumed", "suspend_failed", "hibernate_now", "wait_longer", "manual"):
            self._apply(self.machine.handle(ev, now))
        elif kind == "endsession":
            self.quit()

    def _gesture(self, g: str, now: float):
        if self.prompt:
            if g == NOD:
                self._answer(True)
            elif g == SHAKE and self.prompt.repeats == 0:
                self._answer(False)
            return
        if self.menu:
            if g == SHAKE:
                self.close_menu(abandoned=True)
            return
        if g == SHAKE:
            self.struggle.note(now, "shake")
        self.mods.each("on_gesture", g)

    def _confirm_press(self, now: float):
        if self.prompt:
            self._answer(True)
        elif self.menu:
            item = self.menu.press()
            if item:
                self._activate(item)

    # =================================================================== akce
    def _apply(self, actions):
        for a in actions:
            kind = a[0]
            if kind == "mode":
                _, prev, mode, reason = a
                self.log(f"[MODE] {prev} -> {mode} ({reason})")
                if mode not in (BAG, WAITING):
                    self.close_menu(abandoned=False)
                    self.prompt = None
                self.overlay.t_change = time.monotonic()
                for mod in list(self.mods.mods.values()):
                    if mod.active:
                        self.mods.call(mod, "on_mode", prev, mode)
                if mode == BAG:
                    self.recalibrate()
            elif kind == "say":
                self.say(a[1])
            elif kind == "beep":
                self.beep(a[1])
            elif kind == "hibernate":
                self._hibernate()

    def _hibernate(self):
        self.close_menu(abandoned=False)
        self.prompt = None
        self.log("[POWER] hibernace…")

        def run():
            time.sleep(1.5)                           # ať se stihne vykreslit "Hibernuji"
            ok, what = win.hibernate(self.cfg.dry_run, self.cfg.hibernate_fallback_sleep, self.log)
            self.log(f"[POWER] {what}: {'OK' if ok else 'chyba'}")
            if not ok:
                self.q.put(("suspend_failed", what))
            elif self.cfg.dry_run:
                time.sleep(4.0)
                self.q.put(("resumed",))
        threading.Thread(target=run, name="hibernate", daemon=True).start()

    def say(self, text: str, source: str = "koren"):
        self.log(f"[SAY:{source}] {text}")
        v = self.mods.get("voice")
        if v is not None:
            self.mods.call(v, "speak", text)

    def beep(self, kind: str):
        if not self.cfg.sound_on or kind not in BEEPS:
            return

        def run():
            try:
                import winsound
                for f, ms in BEEPS[kind]:
                    winsound.Beep(f, ms)
            except Exception:
                pass
        threading.Thread(target=run, daemon=True).start()

    def overlay_toast(self, text: str, seconds: float = 2.0):
        self.overlay.toast(text, seconds)

    def glasses_monitor(self):
        return self.overlay.monitor

    # ================================================================ menu / otázky
    def _focus_update(self):
        f = self.menu is not None or self.prompt is not None
        if f != self._focus:
            self._focus = f
            self.mods.each("on_focus", f)

    def open_menu(self):
        m = self.machine
        items = [MenuItem("close", "Pokračovat", "zavřít menu", action=lambda: None)]
        items += self.mods.menu_items()
        if m.mode == WAITING:
            items.append(MenuItem("wait", f"Čekat déle ({self.cfg.wait_minutes:g} min)",
                                  action=lambda: self.q.put(("wait_longer",))))
        if m.mode in (BAG, WAITING) and (self.power["hibernate"] or self.power["sleep"]):
            items.append(MenuItem("hib", "Hibernovat notebook", action=lambda: None,
                                  confirm="Opravdu hibernovat notebook?"))
        if m.facts.manual:
            items.append(MenuItem("manual_off", "Ukončit režim bez rukou",
                                  action=lambda: self.q.put(("manual", False))))
        items.append(MenuItem("help", "Co teď můžu dělat?", action=self._explain))
        items.append(MenuItem("calib", "Znovu kalibrovat brýle", "drž hlavu 1 s v klidu",
                              action=self.recalibrate))
        self.menu = Menu("Menu", items, step_deg=self.cfg.menu_step_deg, dwell_s=self.cfg.menu_dwell_s)
        self.log("[MENU] otevřeno")

    def close_menu(self, abandoned: bool):
        if self.menu is None:
            return
        if abandoned:
            self.struggle.note(time.monotonic(), "menu_abandoned")
        self.menu = None

    def _activate(self, item: MenuItem):
        self.log(f"[MENU] {item.id}")
        self.menu = None
        if item.confirm:
            if item.id == "hib":
                self.ask(item.confirm, on_yes=lambda: self.q.put(("hibernate_now",)))
            else:
                self.ask(item.confirm, on_yes=item.action)
            return
        self.struggle.note(time.monotonic(), "success")
        if item.action:
            try:
                item.action()
            except Exception as e:
                self.log(f"[MENU] {item.id}: {type(e).__name__}: {e}")
        self.overlay.toast("✓ " + item.label, 1.5)

    def ask(self, question: str, on_yes=None, on_no=None, default_yes: bool = False):
        self.prompt = Prompt(question, on_yes, on_no, timeout_s=self.cfg.prompt_timeout_s,
                             default_yes=default_yes, started=time.monotonic())
        self.say(question + " Kývni pro ano, zavrť hlavou pro ne.")

    def _answer(self, yes: bool, auto: bool = False):
        p, self.prompt = self.prompt, None
        if p is None:
            return
        self.log(f"[ASK] {p.question} -> {'ANO' if yes else 'NE'}{' (bez odpovědi)' if auto else ''}")
        if not auto:
            self.struggle.note(time.monotonic(), "success")
        fn = p.on_yes if yes else p.on_no
        if fn:
            try:
                fn()
            except Exception as e:
                self.log(f"[ASK] {type(e).__name__}: {e}")
        if auto:
            self.overlay.toast("Bez odpovědi, nic jsem neudělal.", 2.5)

    def recalibrate(self):
        self.glasses.recalibrate()
        hm = self.mods.get("head_mouse")
        if hm is not None:
            self.mods.call(hm, "recalibrate")

    def _explain(self):
        s = self.snapshot()
        g = guidance(s, capabilities(s, self.cfg.physical_input_fresh_s),
                     self.struggle.level(time.monotonic()), self.machine.wait_s)
        opts = ", ".join(f"{a}: {b}" for a, b in g.options)
        self.overlay.t_change = time.monotonic()
        self.say(f"{g.title}. {g.text} {opts}")

    # ================================================================== stav
    def snapshot(self) -> Snapshot:
        m = self.machine
        hm = self.mods.get("head_mouse")
        la = self.lid_action
        lid_ok = None if not la or not self.power["lid"] else all(v == 0 for v in la.values())
        return Snapshot(
            mode=m.mode, lid=m.facts.lid, glasses_usb=self.glasses.usb, imu_alive=self.glasses.imu_alive,
            imu_calibrating=self.glasses.calibrating, button_alive=self.glasses.btn_connected,
            glasses_display=self._glasses_display, physical_input_age=self.native.physical_input_age(),
            lid_action_ok=lid_ok, remaining=m.remaining, glasses_lost=m.lost_since is not None,
            menu_open=self.menu is not None, prompt=self.prompt.question if self.prompt else None,
            head_mouse_on=bool(hm and hm.on), modules_failed=tuple(self.mods.failed))

    def _render(self, now: float):
        s = self.snapshot()
        c = capabilities(s, self.cfg.physical_input_fresh_s)
        st = self.struggle.level(now)
        g = guidance(s, c, st, self.machine.wait_s)
        self.overlay.render(g, self.menu, self.prompt, st, self.btn.held_for(now), self.cfg.menu_hold_s)
        self.native.set_tray(f"XREAL Kořen: {MODE_LABELS[s.mode]}", TRAY_ICON.get(s.mode, "desk"))

    def _status_line(self) -> str:
        s = self.snapshot()
        parts = [MODE_LABELS[s.mode]]
        parts.append("brýle: " + ("IMU OK" if s.imu_alive else "USB" if s.glasses_usb else "nepřipojené"))
        for mod in self.mods.mods.values():
            if mod.active:
                st = self.mods.call(mod, "status")
                if st:
                    parts.append(f"{mod.name}: {st}")
        return " · ".join(parts)

    # ==================================================================== systray
    def tray_menu(self):
        """Volá se z vlákna okna: jen číst stav, nic neměnit."""
        e = [(1, self._status_line()[:120], 1)]
        if self.lid_action and self.power["lid"] and any(v != 0 for v in self.lid_action.values()):
            e.append((2, "Pozor: zavření víka není „Nedělat nic“", 1))
        e.append((0, "", -1))
        e.append((10, "Režim bez rukou (ručně)", 8 if self.machine.facts.manual else 0))
        e.append((11, "Ukázat nápovědu v brýlích", 0))
        e.append((0, "", -1))
        for i, mid in enumerate(ALL_MODULES):
            mod = self.mods.mods.get(mid)
            if mod:
                e.append((20 + i, f"Modul: {mod.name}", 8 if mod.active else 0))
        if self.cfg.dry_run:
            e.append((0, "", -1))
            e.append((30, "Zkouška: zavřít víko", 0))
            e.append((31, "Zkouška: otevřít víko", 0))
            e.append((32, "Zkouška: brýle připojeny / odpojeny", 0))
        e.append((0, "", -1))
        e.append((40, "Otevřít log", 0))
        e.append((99, "Ukončit", 0))
        return e

    def _tray_cmd(self, cid: int):
        now = time.monotonic()
        if cid == 10:
            self._apply(self.machine.handle(("manual", not self.machine.facts.manual), now))
        elif cid == 11:
            self.overlay.t_change = now
        elif 20 <= cid < 20 + len(ALL_MODULES):
            mid = ALL_MODULES[cid - 20]
            mod = self.mods.mods.get(mid)
            if mod:
                if mod.active:
                    self._stop_module(mod)
                else:
                    if mod.name in self.mods.failed:
                        self.mods.failed.remove(mod.name)
                    self.mods.errors[mid] = 0
                    self._start_module(mod)
                self.cfg.modules[mid] = mod.active
                self.cfg.save()
        elif cid == 30:
            self._apply(self.machine.handle(("lid", "closed"), now))
        elif cid == 31:
            self._apply(self.machine.handle(("lid", "open"), now))
        elif cid == 32:
            self._apply(self.machine.handle(("glasses", not self.machine.facts.glasses), now))
        elif cid == 40:
            try:
                os.startfile(self.log.path)
            except Exception:
                pass
        elif cid == 99:
            self.quit()
