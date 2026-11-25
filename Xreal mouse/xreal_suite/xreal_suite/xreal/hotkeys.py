"""Globalni klavesy (F-klavesy, Pause, klik kolieckem) + zalozni pismena v konzoli."""
import threading
import time
from collections import deque

from . import winapi

BINDINGS = {                 # akce -> virtualni kody klaves
    "toggle": [0x77, 0x13],  # F8, Pause          zapnout / vypnout ovladani mysi
    "calib": [0x78],         # F9                 kalibrace gyra
    "recenter": [0x74],      # F5                 vycentrovat pohled
    "flipH": [0x75],         # F6
    "flipV": [0x76],         # F7
    "quit": [0x79],          # F10
    "sens_down": [0x7A],     # F11 (drzeni)
    "sens_up": [0x7B],       # F12 (drzeni)
    "center": [0x04],        # klik kolieckem myši: uvolnit / znovu drzet stred
}
CONSOLE_KEYS = {"r": "recenter", "m": "toggle", "c": "calib", "h": "flipH", "v": "flipV",
                "q": "quit", "-": "sens_down_step", "+": "sens_up_step", "=": "sens_up_step"}
HELD_ONLY = ("sens_down", "sens_up")


class Hotkeys(threading.Thread):
    """Vlastni vlakno hlida klavesy kazdych 5 ms; udalosti cte engine z `events`."""

    def __init__(self):
        super().__init__(daemon=True)
        self.events = deque()
        self.held = {a: False for a in BINDINGS}
        self.stop_flag = False

    def run(self):
        prev = {a: False for a in BINDINGS}
        while not self.stop_flag:
            for action, vks in BINDINGS.items():
                states = [winapi.key_state(vk) for vk in vks]
                down = any(d for d, _ in states)
                tapped = any(t for _, t in states)
                self.held[action] = down
                if action not in HELD_ONLY and ((down and not prev[action]) or tapped):
                    self.events.append(action)
                prev[action] = down
            if winapi.msvcrt is not None:
                while winapi.msvcrt.kbhit():
                    ch = winapi.msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):
                        winapi.msvcrt.getwch()
                        continue
                    action = CONSOLE_KEYS.get(ch.lower())
                    if action:
                        self.events.append(action)
            time.sleep(0.005)
