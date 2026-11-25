"""Tlacitko na bryli -> mys.

  kratky stisk                     levy klik
  stisk a drzeni                   drzene levé tlacitko (tazeni)
  dvojstisk                        pravy klik
  dvojstisk, druhy stisk drzet     circle click zapnout / vypnout
"""
import socket
import threading
import time

from . import protocol


class ButtonMouse:
    def __init__(self, cfg, left_click, left_down, left_up, right_click, toggle):
        self.cfg = cfg
        self.left_click, self.left_down, self.left_up = left_click, left_down, left_up
        self.right_click, self.toggle = right_click, toggle
        self.lock = threading.RLock()
        self.state = "IDLE"
        self.t = 0.0

    @property
    def busy(self) -> bool:
        return self.state != "IDLE"

    def down(self, now: float):
        with self.lock:
            if self.state == "IDLE":
                self.state, self.t = "DOWN1", now
            elif self.state == "WAIT2":
                self.state, self.t = "DOWN2", now

    def up(self, now: float):
        with self.lock:
            st = self.state
            if st == "DOWN1":
                self.state, self.t = "WAIT2", now
            elif st == "DRAG":
                self.left_up()
                self.state = "IDLE"
            elif st == "DOWN2":
                self.right_click()
                self.state = "IDLE"
            elif st == "CONSUMED":
                self.state = "IDLE"

    def tick(self, now: float):
        with self.lock:
            hold = self.cfg.hold_ms / 1000.0
            dbl = self.cfg.double_ms / 1000.0
            if self.state == "DOWN1" and now - self.t >= hold:
                self.left_down()
                self.state = "DRAG"
            elif self.state == "WAIT2" and now - self.t >= dbl:
                self.left_click()
                self.state = "IDLE"
            elif self.state == "DOWN2" and now - self.t >= hold:
                self.toggle()
                self.state = "CONSUMED"

    def abort(self):
        """Zrusi rozdelanou sekvenci a pusti pripadne drzene tlacitko mysi."""
        with self.lock:
            if self.state == "DRAG":
                self.left_up()
            self.state = "IDLE"


class ControlClient(threading.Thread):
    """Ridici kanal bryli (port 52999): udalosti tlacitka + casovace automatu."""

    def __init__(self, cfg, rt, buttons: ButtonMouse, host=protocol.HOST,
                 port=protocol.PORT_CONTROL):
        super().__init__(daemon=True)
        self.cfg, self.rt, self.buttons = cfg, rt, buttons
        self.host, self.port = host, port

    def run(self):
        cfg, rt = self.cfg, self.rt
        while rt.running:
            try:
                sock = socket.create_connection((self.host, self.port), timeout=5.0)
                sock.settimeout(0.01)
                rt.ctrl_status = "Tlačítko: připojeno"
                buf = bytearray()
                while rt.running:
                    try:
                        data = sock.recv(4096)
                        if not data:
                            raise OSError("spojení ukončeno")
                        buf.extend(data)
                    except socket.timeout:
                        pass
                    now = time.time()
                    for mid, body in protocol.control_frames(buf):
                        if mid != protocol.MSG_KEY_RAW:
                            continue
                        ev = protocol.parse_key_event(body)
                        if ev is None:
                            continue
                        key_id, state = ev
                        rt.last_key_id = key_id
                        if cfg.button_id and key_id != cfg.button_id:
                            continue
                        if not (rt.enabled and cfg.button_on):
                            continue
                        if state == 1:
                            self.buttons.down(now)
                        elif state == 2:
                            self.buttons.up(now)
                    self.buttons.tick(time.time())
                sock.close()
            except OSError as e:
                self.buttons.abort()
                rt.ctrl_status = f"Tlačítko: {e} – zkouším znovu"
            time.sleep(2.0)
