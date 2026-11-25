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

from . import profiles, win
from .advisor import Snapshot, Struggle, capabilities, guidance
from .config import LOG_DIR, Config
from .gestures import NOD, SHAKE, ButtonTracker
from .glasses import GlassesLink
from .menu import Menu, MenuItem, Prompt
from .modules import _FAIL, Module, ModuleContext, ModuleManager
from . import displays
from .overlay import Overlay
from .settings import SettingsWindow
from .states import BAG, DESK, MODE_LABELS, SUSPEND, WAITING, Machine

TICK_MS = 30          # první tik; dál podle režimu (profiles.Plan.tick_ms)
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
        self.displays = []                  # aktivní monitory se jménem (displays.py)
        self.gdisp = None                   # monitor brýlí (potvrzený nebo odhad)
        self.gconfirmed = False
        self._asked_display = False         # okno s potvrzením monitoru už bylo nabídnuto
        self._focus = False
        self.profile_eff = profiles.TRAY
        self.plan = profiles.plan(profiles.TRAY, DESK)
        self._plan_key = None
        self._plan_logged = False
        self.head_forced = False            # ovládání hlavou zapnuté ručně v režimech 1-3
        self._screen_off = False
        self.settings = SettingsWindow(self)
        self._t_gui = 0.0

    # ================================================================ start / stop
    def run(self):
        self.log(f"[KOREN] start, víko: {'ano' if self.power['lid'] else 'ne'}, "
                 f"hibernace: {'ano' if self.power['hibernate'] else 'ne'}, akce víka: {self.lid_action}, "
                 f"dry: {self.cfg.dry_run}")
        self.native.start()
        self.native.ready.wait(3.0)
        self.mods.load(ALL_MODULES)            # načíst; spustí je až plán podle režimu
        self.glasses.sinks.append(self._imu_to_modules)
        self.glasses.start()
        self._refresh_displays()
        if not self.cfg.dry_run:
            self._sync_autostart()
        self._sync_plan(time.monotonic())
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
        self._laptop_screen(False)
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
                self.root.after(self.plan.tick_ms, self._tick)

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

        self._sync_plan(now)
        hold = self.btn.tick(now)
        if hold == "hold" and self.plan.overlay:
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
            self._refresh_displays()
        if now - self._t_slow > 30.0:
            self._t_slow = now
            self.lid_action = win.lid_action()

        self._focus_update()
        self.mods.each("tick", now)
        self._render(now)
        if now - self._t_gui > 1.0:
            self._t_gui = now
            self.settings.refresh()

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
            self.settings.show()
        elif kind == "hotkey":
            self.log("[KOREN] Ctrl+Alt+T: režim Tray")
            self.set_profile(profiles.TRAY)
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
        if self.profile_eff != profiles.TRAY and m.facts.lid != "closed":
            items.append(MenuItem("tray", "Přepnout do režimu Tray", "nic dalšího neběží",
                                  action=lambda: self.set_profile(profiles.TRAY)))
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

    # ============================================================ provozní režimy
    def set_profile(self, p: str):
        if p not in profiles.PROFILES:
            return
        if p == profiles.TRAY:
            self.head_forced = False           # Tray = opravdu nic neběží
        self.cfg.profile = p
        self.cfg.save()
        self._plan_key = None
        self.log(f"[PROFILE] {p}")

    def set_head_mouse(self, on: bool):
        """Ruční zapnutí ovládání hlavou (v režimech 1-3 se samo nezapíná, ale jde zapnout)."""
        self.head_forced = on
        if on and not self.cfg.modules.get("head_mouse"):
            self.cfg.modules["head_mouse"] = True
        self._plan_key = None
        self._sync_plan(time.monotonic())
        hm = self.mods.get("head_mouse")
        if hm is not None:
            self.mods.call(hm, "set_on", on)
            if self.machine.mode == DESK:
                hm.user_wants = on

    def set_autostart(self, on: bool):
        self.cfg.autostart = on
        self.cfg.save()
        if not self.cfg.dry_run:
            self._sync_autostart()

    def _sync_autostart(self):
        script = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "koren.py"))
        want = win.autostart_command(script) if self.cfg.autostart else None
        if win.autostart_get() != want:
            ok = win.autostart_set(self.cfg.autostart, script, self.log)
            self.log(f"[AUTOSTART] {'zapnuto' if self.cfg.autostart else 'vypnuto'}: {'OK' if ok else 'chyba'}")

    def _sync_plan(self, now: float):
        """Srovná, co běží, s provozním režimem a stavem víka (volá se každý tik, levné)."""
        eff = profiles.effective(self.cfg.profile, self.machine.facts.glasses)
        pl = profiles.plan(eff, self.machine.mode)
        key = (eff, pl, self.head_forced, tuple(sorted(self.cfg.modules.items())))
        if key == self._plan_key:
            return
        if not self._plan_logged or eff != self.profile_eff or pl != self.plan:
            self.log(f"[PLAN] {eff}/{self.machine.mode}: {pl}")
        self._plan_logged = True
        self._plan_key = key
        self.profile_eff, self.plan = eff, pl
        self.glasses.set_streams(pl.streams or self.head_forced)
        for mid, mod in self.mods.mods.items():
            want = self.cfg.modules.get(mid, False) and (pl.modules or (mid == "head_mouse" and self.head_forced))
            if want and not mod.active:
                self._start_module(mod)
                if mid == "head_mouse" and self.head_forced and mod.active:
                    self.mods.call(mod, "set_on", True)
            elif not want and mod.active:
                self._stop_module(mod)
        if not pl.overlay:
            self.close_menu(abandoned=False)
            self.prompt = None
        if pl.gui:
            self.settings.show(monitor=self._second_monitor(), pinned=True)
        elif self.settings.pinned:
            self.settings.pinned = False
            self.settings.hide()
        self._laptop_screen(pl.laptop_screen_off)

    def _second_monitor(self):
        """Druhý monitor pro okno nastavení: brýle, jinak jiný než hlavní, jinak hlavní."""
        if self.gdisp:
            return self.gdisp
        for m in self.displays:
            if not m.get("primary"):
                return m
        return self.displays[0] if self.displays else None

    # ============================================================ monitor brýlí
    def _refresh_displays(self):
        """Najde monitor brýlí podle uloženého výběru uživatele (ne podle pořadí ani názvu XREAL)."""
        try:
            self.displays = displays.list_displays()
        except Exception as e:
            self.log(f"[DISP] {type(e).__name__}: {e}")
            self.displays = []
        g, ok = displays.find_glasses(self.displays, self.cfg.glasses_display)
        if (g or {}).get("gdi") != (self.gdisp or {}).get("gdi") or ok != self.gconfirmed:
            self.log(f"[DISP] brýle: {g['label'] if g else 'nenalezeny'} ({'potvrzeno' if ok else 'odhad'})")
        if ok != self.gconfirmed:
            self._plan_key = None                       # po potvrzení znovu zkusit zhasnout notebook
        self.gdisp, self.gconfirmed = g, ok
        self.overlay.set_monitor(g or self._fallback_display())
        # nepotvrzený monitor: jednou nabídnout potvrzení, ale jen když má uživatel ruce (víko otevřené)
        need = not ok and self.glasses.usb and len(self.displays) > 0
        if need and not self._asked_display and self.machine.mode == DESK and self.machine.facts.lid != "closed":
            self._asked_display = True
            self.settings.show(monitor=g or self._fallback_display())
            self.settings.focus_display()

    def _fallback_display(self):
        """Bez potvrzených brýlí: jiný než vestavěný displej, jinak hlavní."""
        for m in self.displays:
            if not m.get("internal") and not m.get("primary"):
                return m
        for m in self.displays:
            if m.get("primary"):
                return m
        return self.displays[0] if self.displays else None

    def confirm_glasses_display(self, d: dict):
        self.cfg.glasses_display = displays.remember(d)
        self.cfg.save()
        self.log(f"[DISP] uživatel potvrdil brýle: {d.get('label')}")
        self._refresh_displays()
        self.overlay_toast("Monitor brýlí uložen", 2.0)

    def _laptop_screen(self, off: bool):
        """Režim 3: displej notebooku zhasnout (jako Win+P „Jen druhá obrazovka“), pak vrátit."""
        if off and not self._screen_off:
            if len(self.displays) < 2 or self.gdisp is None or not self.gconfirmed:
                return      # notebook už nic neukazuje, brýle chybí, nebo monitor brýlí není potvrzený
            topo = win.display_topology() or win.TOPO_EXTEND
            self._screen_off = True
            if self.cfg.dry_run:
                self.log(f"[DISP] (dry) displej notebooku by se zhasnul (teď {topo})")
                return
            self.cfg.saved_topology = topo
            self.cfg.save()
            win.set_display_topology(win.TOPO_EXTERNAL, self.log)
            self.log("[DISP] displej notebooku zhasnut")
        elif not off and (self._screen_off or self.cfg.saved_topology):
            was = self._screen_off
            self._screen_off = False
            if self.cfg.dry_run:
                if was:
                    self.log("[DISP] (dry) displej notebooku by se rozsvítil")
                return
            topo = self.cfg.saved_topology or win.TOPO_EXTEND
            self.cfg.saved_topology = 0
            self.cfg.save()
            if topo != win.TOPO_EXTERNAL:
                win.set_display_topology(topo, self.log)
                self.log(f"[DISP] displej notebooku vrácen ({topo})")

    # ================================================================== stav
    def snapshot(self) -> Snapshot:
        m = self.machine
        hm = self.mods.get("head_mouse")
        la = self.lid_action
        lid_ok = None if not la or not self.power["lid"] else all(v == 0 for v in la.values())
        return Snapshot(
            mode=m.mode, lid=m.facts.lid, glasses_usb=self.glasses.usb, imu_alive=self.glasses.imu_alive,
            imu_calibrating=self.glasses.calibrating, button_alive=self.glasses.btn_connected,
            glasses_display=self.gdisp is not None, glasses_confirmed=self.gconfirmed,
            physical_input_age=self.native.physical_input_age(),
            lid_action_ok=lid_ok, remaining=m.remaining, glasses_lost=m.lost_since is not None,
            menu_open=self.menu is not None, prompt=self.prompt.question if self.prompt else None,
            head_mouse_on=bool(hm and hm.on), modules_failed=tuple(self.mods.failed))

    def _render(self, now: float):
        tip = f"XREAL Kořen: {MODE_LABELS[self.machine.mode]} · {profiles.SHORT[self.profile_eff]}"
        if not self.plan.overlay:
            self.overlay.help.show(False)
            self.native.set_tray(tip, TRAY_ICON.get(self.machine.mode, "desk"))
            return
        s = self.snapshot()
        c = capabilities(s, self.cfg.physical_input_fresh_s)
        st = self.struggle.level(now)
        g = guidance(s, c, st, self.machine.wait_s)
        self.overlay.render(g, self.menu, self.prompt, st, self.btn.held_for(now), self.cfg.menu_hold_s)
        self.native.set_tray(tip, TRAY_ICON.get(s.mode, "desk"))

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
        if self.glasses.usb and not self.gconfirmed:
            e.append((12, "Potvrď monitor brýlí v nastavení…", 0))
        if self.lid_action and self.power["lid"] and any(v != 0 for v in self.lid_action.values()):
            e.append((2, "Pozor: zavření víka není „Nedělat nic“", 1))
        e.append((0, "", -1))
        for i, p in enumerate(profiles.PROFILES):
            e.append((50 + i, profiles.LABELS[p] + ("   (Ctrl+Alt+T)" if p == profiles.TRAY else ""),
                      8 if self.cfg.profile == p else 0))
        e.append((0, "", -1))
        e.append((12, "Nastavení a stav…", 0))
        hm = self.mods.mods.get("head_mouse")
        e.append((14, "Ovládání hlavou zapnuté", 8 if (hm and hm.active and hm.on) else 0))
        e.append((10, "Režim bez rukou i s otevřeným víkem", 8 if self.machine.facts.manual else 0))
        e.append((13, "Spouštět s Windows", 8 if self.cfg.autostart else 0))
        e.append((0, "", -1))
        for i, mid in enumerate(ALL_MODULES):
            mod = self.mods.mods.get(mid)
            if mod:
                e.append((20 + i, f"Modul: {mod.name}", 8 if self.cfg.modules.get(mid) else 0))
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
        elif cid == 12:
            self.settings.show()
        elif cid == 13:
            self.set_autostart(not self.cfg.autostart)
        elif cid == 14:
            hm = self.mods.mods.get("head_mouse")
            self.set_head_mouse(not (hm and hm.active and hm.on))
        elif 50 <= cid < 50 + len(profiles.PROFILES):
            self.set_profile(profiles.PROFILES[cid - 50])
        elif 20 <= cid < 20 + len(ALL_MODULES):
            mid = ALL_MODULES[cid - 20]
            mod = self.mods.mods.get(mid)
            if mod:
                self.cfg.modules[mid] = not self.cfg.modules.get(mid, False)
                if mod.name in self.mods.failed:
                    self.mods.failed.remove(mod.name)
                self.mods.errors[mid] = 0
                self.cfg.save()
                self._plan_key = None             # srovnat moduly podle režimu
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
