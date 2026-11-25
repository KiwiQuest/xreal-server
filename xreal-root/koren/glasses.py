"""Spojení s brýlemi: jediný vlastník IMU (port 52998) a řídicího kanálu (52999).

Kořen data rozdává modulům (`sinks`), takže moduly se o brýle nepřetahují.
Protokol a fúzi IMU bere z hotového XREAL Head Mouse 2.0 (balík `xreal`).

Přítomnost brýlí = USB zařízení VID 3318 NEBO živý proud IMU. Výpadek se hlásí až po
`lost_after_s` souvislé nepřítomnosti, ať krátké zakolísání kabelu nespustí odpočet.
"""
import socket
import threading
import time
from typing import Callable, List, Optional

from .config import use_suite
from .gestures import GestureDetector

use_suite()
from xreal import protocol            # noqa: E402
from xreal.fusion import Orientation  # noqa: E402


class GlassesLink:
    def __init__(self, post: Callable[[tuple], None], usb_probe: Callable[[], bool],
                 log=print, lost_after_s: float = 3.0, host: str = protocol.HOST):
        self.post, self.usb_probe, self.log = post, usb_probe, log
        self.host = host
        self.lost_after = lost_after_s
        self.sinks: List[Callable] = []          # f(ImuSample), volá se z vlákna IMU
        self.ori = Orientation()
        self.gestures = GestureDetector()
        self.running = True
        self.usb = False
        self.present = False
        self.imu_t = 0.0                          # monotonic posledního platného vzorku
        self.btn_connected = False
        self.imu_status = "nepřipojeno"
        self._absent_since: Optional[float] = None
        self._wake = threading.Event()
        self.streams = threading.Event()          # IMU + tlačítko jen když je potřeba (režim Tray = ne)
        self._recal = False

    # ----------------------------------------------------------------- dotazy
    @property
    def imu_alive(self) -> bool:
        return time.monotonic() - self.imu_t < 1.5

    @property
    def calibrating(self) -> bool:
        return self.ori.calibrating

    @property
    def pitch_deg(self) -> Optional[float]:
        return None if (not self.imu_alive or self.ori.calibrating) else self.ori.pitch_deg

    def recalibrate(self):
        self._recal = True

    def set_streams(self, on: bool):
        (self.streams.set if on else self.streams.clear)()
        if not on:
            self.imu_t = 0.0

    def check_now(self):
        """Změna USB (WM_DEVICECHANGE): zkontrolovat přítomnost hned."""
        self._wake.set()

    # ----------------------------------------------------------------- vlákna
    def start(self):
        for fn, name in ((self._imu_loop, "glasses-imu"), (self._ctrl_loop, "glasses-ctrl"),
                         (self._presence_loop, "glasses-presence")):
            threading.Thread(target=fn, name=name, daemon=True).start()

    def stop(self):
        self.running = False
        self._wake.set()

    def _presence_loop(self):
        while self.running:
            try:
                self.usb = bool(self.usb_probe())
            except Exception:
                self.usb = False
            now = time.monotonic()
            seen = self.usb or self.imu_alive
            if seen:
                self._absent_since = None
                if not self.present:
                    self.present = True
                    self.post(("glasses", True))
            elif self.present:
                if self._absent_since is None:
                    self._absent_since = now
                elif now - self._absent_since >= self.lost_after:
                    self.present = False
                    self.post(("glasses", False))
            self._wake.wait(1.0)
            self._wake.clear()

    def _imu_loop(self):
        while self.running:
            if not self.streams.wait(1.0):
                continue
            sock = None
            try:
                sock = socket.create_connection((self.host, protocol.PORT_IMU), timeout=3.0)
                sock.settimeout(2.0)                 # 2 s bez dat = zaseknuté spojení -> znovu
                self.imu_status = "připojeno"
                self.ori.start_calibration()
                buf = bytearray()
                while self.running and self.streams.is_set():
                    data = sock.recv(65536)
                    if not data:
                        raise OSError("spojení ukončeno")
                    buf.extend(data)
                    for fr in protocol.split_imu_frames(buf):
                        s = protocol.parse_imu_frame(fr)
                        if isinstance(s, protocol.ImuSample):
                            self._sample(s)
            except OSError as e:
                self.imu_status = f"čekám ({e.__class__.__name__})"
            except Exception as e:                 # chyba v parsování nesmí ukončit vlákno
                self.log(f"[IMU] {type(e).__name__}: {e}")
            finally:
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if self.running and self.streams.is_set():
                time.sleep(2.0)

    def _sample(self, s):
        if self._recal:
            self._recal = False
            self.ori.start_calibration()
        valid = self.ori.update(s.ts_ns, s.gyro, s.acc)
        self.imu_t = time.monotonic()
        if valid:
            g = self.gestures.feed(s.ts_ns * 1e-9, self.ori.yaw_rate_dps, self.ori.pitch_rate_dps)
            if g:
                self.post(("gesture", g))
        for fn in list(self.sinks):
            fn(s)                                  # ModuleManager.call chrání před výjimkou

    def _ctrl_loop(self):
        while self.running:
            if not self.streams.wait(1.0):
                continue
            sock = None
            try:
                sock = socket.create_connection((self.host, protocol.PORT_CONTROL), timeout=3.0)
                sock.settimeout(1.0)
                self.btn_connected = True
                buf = bytearray()
                while self.running and self.streams.is_set():
                    try:
                        data = sock.recv(4096)
                    except socket.timeout:
                        continue
                    if not data:
                        raise OSError("spojení ukončeno")
                    buf.extend(data)
                    for mid, body in protocol.control_frames(buf):
                        if mid != protocol.MSG_KEY_RAW:
                            continue
                        ev = protocol.parse_key_event(body)
                        if ev:
                            self.post(("button", ev[1] == 1, time.monotonic(), ev[0]))
            except OSError:
                pass
            except Exception as e:
                self.log(f"[BTN] {type(e).__name__}: {e}")
            finally:
                self.btn_connected = False
                if sock:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if self.running and self.streams.is_set():
                time.sleep(2.0)
