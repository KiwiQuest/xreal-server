"""Jadro: cte IMU z bryli a ridi kurzor, klikani po zastaveni a tlacitko na bryli.

Vsechny vedlejsi efekty (mys, zvuk) jdou pres objekt `io`, takze engine jde testovat
bez Windows i bez bryli (viz tests/).
"""
import math
import threading
import time
from typing import Optional

from . import protocol, winapi
from .buttons import ButtonMouse, ControlClient
from .config import Config, Runtime
from .dwell import Dwell
from .fusion import Orientation
from .hotkeys import Hotkeys
from .pointer import MODE_LABELS, PointerController

SEND_INTERVAL = 0.004     # jak casto posilat pohyb mysi (s)
UI_INTERVAL = 0.05        # jak casto obsluhovat klavesy a diagnostiku (s)
SPEED_TAU = 0.08          # vyhlazeni rychlosti hlavy (s)


class WinOutput:
    """Skutecna mys a zvuk (Windows)."""
    move = staticmethod(winapi.move_mouse)
    set_cursor = staticmethod(winapi.set_cursor)
    left_click = staticmethod(winapi.left_click)
    left_down = staticmethod(winapi.left_down)
    left_up = staticmethod(winapi.left_up)
    right_click = staticmethod(winapi.right_click)
    beep = staticmethod(winapi.beep_async)


class NullOutput:
    """Nic nedela (--dry), jen si pamatuje volani."""

    def __init__(self):
        self.calls = []

    def _rec(self, name):
        return lambda *a: self.calls.append((name,) + a)

    def __getattr__(self, name):
        if name in ("move", "set_cursor", "left_click", "left_down", "left_up",
                    "right_click", "beep"):
            return self._rec(name)
        raise AttributeError(name)


class Engine:
    def __init__(self, cfg: Config, rt: Runtime, io=None, replay: Optional[str] = None,
                 realtime: bool = True, use_hotkeys: bool = True, monitor_fn=None):
        self.cfg, self.rt = cfg, rt
        self.io = io or WinOutput()
        self.replay = replay
        self.realtime = realtime
        self.ori = Orientation()
        self.pointer = PointerController(cfg)
        self.dwell = Dwell()
        self.buttons = ButtonMouse(cfg, self._act_left_click, self._act_left_down,
                                   self._act_left_up, self._act_right_click,
                                   self._act_toggle_dwell)
        self.keys = Hotkeys() if use_hotkeys else None
        self.monitor: Optional[dict] = None
        self._monitor_fn = monitor_fn or (lambda: winapi.pick_monitor(
            winapi.list_monitors(), self.cfg.screen_idx))
        self._threads = []

        self.speed = 0.0
        self._acc_x = self._acc_y = 0.0     # zlomky pixelu
        self._send_dx = self._send_dy = 0
        self._target = None
        self._last_send = 0.0
        self._last_ui = 0.0
        self._prev_key = (False, "relative")

    # ------------------------------------------------------------------ start
    def start(self):
        self.refresh_monitor()
        if self.keys:
            self.keys.start()
        targets = [self._monitor_loop, self._imu_loop]
        for t in targets:
            th = threading.Thread(target=t, daemon=True)
            th.start()
            self._threads.append(th)
        if not self.replay:
            ControlClient(self.cfg, self.rt, self.buttons).start()
        else:
            self.rt.ctrl_status = "Tlačítko: při přehrávání souboru vypnuto"

    def stop(self):
        self.rt.running = False
        self.buttons.abort()
        if self.keys:
            self.keys.stop_flag = True

    # ---------------------------------------------------------------- monitory
    def refresh_monitor(self):
        try:
            m = self._monitor_fn()
        except Exception:
            m = None
        self.monitor = m
        if m is None:
            self.rt.screen_text = "Střed obrazovky: monitor nenalezen"
        else:
            self.rt.screen_text = (f"Střed obrazovky: {m['x'] + m['w'] // 2}, "
                                   f"{m['y'] + m['h'] // 2}  ({m['w']}×{m['h']})")
        return m

    def _monitor_loop(self):
        while self.rt.running:
            try:
                self.rt.monitors = winapi.list_monitors()
            except Exception:
                pass
            self.refresh_monitor()
            for _ in range(20):                      # 2 s, ale reaguje na ukonceni
                if not self.rt.running:
                    return
                time.sleep(0.1)

    def center(self):
        m = self.monitor
        if not m:
            return None
        return (m["x"] + m["w"] / 2.0, m["y"] + m["h"] / 2.0)

    # ------------------------------------------------------------- IMU vlakno
    def _imu_loop(self):
        rt = self.rt
        wall0 = ts0 = None
        while rt.running:
            try:
                chunks = (protocol.file_chunks(self.replay) if self.replay
                          else protocol.socket_chunks(protocol.PORT_IMU))
                rt.status = "Připojeno"
                for s in protocol.imu_samples(chunks):
                    if not rt.running:
                        return
                    if not isinstance(s, protocol.ImuSample):
                        continue
                    if self.replay and self.realtime:
                        if wall0 is None:
                            wall0, ts0 = time.time(), s.ts_ns
                        delay = wall0 + (s.ts_ns - ts0) * 1e-9 - time.time()
                        if delay > 0:
                            time.sleep(delay)
                    self.process(s)
                rt.status = "Přehrávání dokončeno" if self.replay else "Spojení ukončeno"
            except OSError as e:
                rt.status = f"Chyba připojení: {e} – zkouším znovu"
            if self.replay:
                return
            time.sleep(2.0)

    # ------------------------------------------------------ zpracovani vzorku
    def process(self, s: "protocol.ImuSample", now: Optional[float] = None):
        now = time.time() if now is None else now
        cfg, rt, o = self.cfg, self.rt, self.ori
        valid = o.update(s.ts_ns, s.gyro, s.acc)
        rt.hz = o.hz
        if now - self._last_ui >= UI_INTERVAL:
            self._last_ui = now
            self._ui_tick(now)
        if not valid:
            return
        dt = o.dt

        raw = math.hypot(o.yaw_rate_dps, o.pitch_rate_dps)
        self.speed += (1.0 - math.exp(-dt / SPEED_TAU)) * (raw - self.speed)
        rt.speed = self.speed

        mode = "relative" if rt.lock_off else cfg.mode
        key = (rt.enabled, mode)
        if key != self._prev_key:
            self._on_mode_change(rt.enabled, mode)
            self._prev_key = key
        if not rt.enabled or not cfg.button_on:
            self.buttons.abort()

        out = self.pointer.step(o, dt, mode, self.monitor)     # tracker natoceni bezi vzdy (AR)
        rt.view_h, rt.view_v = self.pointer.view_angles()
        rt.view_hist.append((now, rt.view_h, rt.view_v))

        px = py = 0.0
        if rt.enabled:
            px, py = out.dx, out.dy
            if mode == "view":
                self._target = (out.tx, out.ty) if out.tx is not None else None
            else:
                self._acc_x += out.dx
                self._acc_y += out.dy
                ix, iy = int(self._acc_x), int(self._acc_y)
                self._acc_x -= ix
                self._acc_y -= iy
                self._send_dx += ix
                self._send_dy += iy
                self._target = None
            if self.buttons.busy:
                self.dwell.block()
            if cfg.dwell_on:
                if self.dwell.update(cfg, dt, self.speed, px, py):
                    self._dwell_click(mode, now)
            else:
                self.dwell.reset()
        else:
            self.dwell.reset()
            self._target = None
        rt.dwell_active = rt.enabled and self.dwell.active
        rt.dwell_progress = self.dwell.progress

        if now - self._last_send >= SEND_INTERVAL:
            self._last_send = now
            self._flush(mode)

    def _flush(self, mode: str):
        if not self.rt.enabled:
            self._send_dx = self._send_dy = 0
            return
        if mode in ("relative", "look"):
            if self._send_dx or self._send_dy:
                self.io.move(self._send_dx, self._send_dy)
            self._send_dx = self._send_dy = 0
            if mode == "look":
                c = self.center()
                if c:
                    self.io.set_cursor(*c)
        elif mode == "view" and self._target:
            self.io.set_cursor(*self._target)
            self.rt.pointer_xy = self._target

    def _on_mode_change(self, enabled: bool, mode: str):
        self.pointer.reset(mode)
        self.dwell.reset()
        self._send_dx = self._send_dy = 0
        self._acc_x = self._acc_y = 0.0
        self._target = None
        if enabled and (self.cfg.center_on_enable or mode in ("look", "view")):
            c = self.center()
            if c:
                self.io.set_cursor(*c)      # vychozi bod je vzdy stred obrazovky
        if mode == "view" and enabled:
            self.pointer.recenter_pending = False

    def _dwell_click(self, mode: str, now: float):
        if mode == "view" and self._target:
            self.io.set_cursor(*self._target)
        if self._send_dx or self._send_dy:
            self.io.move(self._send_dx, self._send_dy)
            self._send_dx = self._send_dy = 0
        self.io.left_click()
        self.rt.flash_until = now + 0.25
        self.rt.last_action = "circle click: levý klik"

    # ---------------------------------------------------- klavesy a diagnostika
    def _ui_tick(self, now: float):
        cfg, rt, o = self.cfg, self.rt, self.ori
        if rt.calib_request:
            rt.calib_request = False
            o.start_calibration()
            self.dwell.reset()
        if rt.recenter_request:
            rt.recenter_request = False
            self.pointer.recenter()
        if self.keys:
            while self.keys.events:
                self.handle_event(self.keys.events.popleft(), now)
            if self.keys.held["sens_down"]:
                cfg.sens = max(1.0, cfg.sens * 0.97)
            if self.keys.held["sens_up"]:
                cfg.sens = min(200.0, cfg.sens * 1.03)
        rt.status = o.status if o.calibrating else "Připojeno – OK"
        mode = "relative" if rt.lock_off else cfg.mode
        if o.calibrating:
            rt.diag_text = "Kalibrace gyra – drž brýle v klidu (asi 1 s)"
        elif mode == "view":
            p = self.pointer
            tgt = "-" if self._target is None else f"({self._target[0]:.0f}, {self._target[1]:.0f})"
            rt.diag_text = (f"Střed pohledu | yaw {p.yaw_offset_deg:+6.1f}°  "
                            f"pitch {p.pitch_offset_deg:+6.1f}°  cíl {tgt}")
        else:
            rt.diag_text = (f"{MODE_LABELS[mode].split(' (')[0]} | rychlost "
                            f"yaw {o.yaw_rate_dps:+6.1f}  pitch {o.pitch_rate_dps:+6.1f} °/s")

    def handle_event(self, ev: str, now: Optional[float] = None):
        cfg, rt = self.cfg, self.rt
        now = time.time() if now is None else now
        if ev == "toggle":
            rt.enabled = not rt.enabled
        elif ev == "calib":
            rt.calib_request = True
        elif ev == "recenter":
            rt.recenter_request = True
            self._toast("STŘED\nOK", True, 0.8, "pohled vycentrován (střed = tam, kam se díváš)", 900, 80)
        elif ev == "flipH":
            cfg.flip_h = not cfg.flip_h
        elif ev == "flipV":
            cfg.flip_v = not cfg.flip_v
        elif ev == "sens_down_step":
            cfg.sens = max(1.0, cfg.sens * 0.9)
        elif ev == "sens_up_step":
            cfg.sens = min(200.0, cfg.sens * 1.1)
        elif ev == "center":            # klik kolieckem: uvolnit / znovu drzet stred
            rt.lock_off = not rt.lock_off
            held = not rt.lock_off
            self._toast("STŘED\nZAP" if held else "STŘED\nVYP", held, 1.0,
                        "kurzor držen podle režimu" if held else "kurzor uvolněn (klik kolečkem)",
                        1300 if held else 500, 140)
        elif ev == "quit":
            rt.quit_request = True

    def _toast(self, text, ok, secs, action, freq, ms):
        rt = self.rt
        rt.toast_text, rt.toast_ok = text, ok
        rt.toast_until = time.time() + secs
        rt.last_action = action
        if self.cfg.sound_on:
            self.io.beep(freq, ms)

    # ----------------------------------------------- akce tlacitka na bryli
    def _act_left_click(self):
        self.rt.last_action = "levý klik"
        self.io.left_click()

    def _act_left_down(self):
        self.rt.last_action = "levé tlačítko drženo"
        self.io.left_down()

    def _act_left_up(self):
        self.rt.last_action = "levé tlačítko puštěno"
        self.io.left_up()

    def _act_right_click(self):
        self.rt.last_action = "pravý klik"
        self.io.right_click()

    def _act_toggle_dwell(self):
        cfg = self.cfg
        cfg.dwell_on = not cfg.dwell_on
        self._toast("ZAP" if cfg.dwell_on else "VYP", cfg.dwell_on, 1.0,
                    f"circle click {'ZAPNUT' if cfg.dwell_on else 'VYPNUT'}",
                    1300 if cfg.dwell_on else 500, 140)
