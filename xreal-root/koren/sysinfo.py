"""Základní informace o systému bez knihoven navíc (ctypes): CPU, paměť, baterie, doba běhu."""
import ctypes
import sys
import time

IS_WIN = sys.platform == "win32"


class _CPU:
    def __init__(self):
        self.prev = None
        self.value = None

    def sample(self):
        if not IS_WIN:
            return None
        from ctypes import wintypes
        idle, kern, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
            return self.value
        f = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime
        cur = (f(idle), f(kern), f(user))
        if self.prev:
            di, dk, du = (cur[i] - self.prev[i] for i in range(3))
            tot = dk + du                                   # kernel obsahuje i idle
            if tot > 0:
                self.value = round(100.0 * (tot - di) / tot)
        self.prev = cur
        return self.value


_cpu = _CPU()


def memory():
    """(procenta, použito GB, celkem GB) nebo None."""
    if not IS_WIN:
        return None

    class MS(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                    ("avail", ctypes.c_ulonglong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                    ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ave", ctypes.c_ulonglong)]
    m = MS()
    m.len = ctypes.sizeof(MS)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return None
    gb = 1024 ** 3
    return m.load, round((m.total - m.avail) / gb, 1), round(m.total / gb, 1)


def battery():
    """(procenta, nabíjí) nebo None."""
    if not IS_WIN:
        return None

    class SPS(ctypes.Structure):
        _fields_ = [("AC", ctypes.c_ubyte), ("Flag", ctypes.c_ubyte), ("Pct", ctypes.c_ubyte),
                    ("Saver", ctypes.c_ubyte), ("Life", ctypes.c_ulong), ("Full", ctypes.c_ulong)]
    s = SPS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)) or s.Pct == 255:
        return None
    return s.Pct, s.AC == 1


def uptime_s() -> float:
    if not IS_WIN:
        return time.monotonic()
    ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    return ctypes.windll.kernel32.GetTickCount64() / 1000.0


def snapshot() -> dict:
    mem = memory()
    bat = battery()
    return {
        "cpu": _cpu.sample(),
        "mem": None if mem is None else {"pct": mem[0], "used": mem[1], "total": mem[2]},
        "bat": None if bat is None else {"pct": bat[0], "ac": bat[1]},
        "uptime": int(uptime_s()),
    }
