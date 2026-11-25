"""Windows API: mys, klavesy, monitory, DPI, okna. Na jinych systemech prazdne nahrady."""
import sys
import threading

IS_WIN = sys.platform == "win32"

try:
    import msvcrt  # type: ignore
except ImportError:   # pragma: no cover
    msvcrt = None

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                  ctypes.c_ubyte, wintypes.DWORD]
    user32.GetForegroundWindow.restype = wintypes.HWND

    def _send(inp):
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))

    def _mouse(flags, dx=0, dy=0):
        inp = INPUT(type=0)
        inp.u.mi = MOUSEINPUT(dx, dy, 0, flags, 0, 0)
        _send(inp)

    def _key(vk=0, scan=0, flags=0):
        inp = INPUT(type=1)
        inp.u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
        _send(inp)


def set_dpi_aware():
    """Fyzicke pixely vsude (i pri ruznem meritku monitoru). Volat pred vytvorenim oken."""
    if not IS_WIN:
        return
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # per-monitor v2
        return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


# ------------------------------ mys ----------------------------------------
def move_mouse(dx: int, dy: int):
    if IS_WIN:
        _mouse(0x0001, int(dx), int(dy))


def set_cursor(x, y):
    if IS_WIN:
        user32.SetCursorPos(int(round(x)), int(round(y)))


def left_down():
    if IS_WIN:
        _mouse(0x0002)


def left_up():
    if IS_WIN:
        _mouse(0x0004)


def left_click():
    left_down()
    left_up()


def right_click():
    if IS_WIN:
        _mouse(0x0008)
        _mouse(0x0010)


# ------------------------------ klavesnice ---------------------------------
def type_text(text: str):
    """Napise text do aktivniho okna (Unicode, funguje i pro diakritiku)."""
    if not IS_WIN:
        return
    for ch in text:
        if ch == "\n":
            _key(vk=0x0D)
            _key(vk=0x0D, flags=0x0002)
            continue
        data = ch.encode("utf-16-le")
        for i in range(0, len(data), 2):
            code = int.from_bytes(data[i:i + 2], "little")
            _key(scan=code, flags=0x0004)            # KEYEVENTF_UNICODE
            _key(scan=code, flags=0x0004 | 0x0002)   # + KEYUP


def press_backspace():
    if IS_WIN:
        _key(vk=0x08)
        _key(vk=0x08, flags=0x0002)


def key_state(vk: int):
    """(je_drzena, byla_stisknuta_od_posledniho_dotazu)"""
    if not IS_WIN:
        return False, False
    st = user32.GetAsyncKeyState(vk)
    return bool(st & 0x8000), bool(st & 0x0001)


# ------------------------------ okna ---------------------------------------
def root_hwnd(tk_window):
    if not IS_WIN:
        return 0
    hwnd = tk_window.winfo_id()
    return user32.GetAncestor(hwnd, 2) or hwnd          # GA_ROOT


def foreground_hwnd():
    return user32.GetForegroundWindow() if IS_WIN else 0


def make_overlay_window(win, key=(1, 1, 1)):
    """Okno propousti kliky, nebere fokus a barva `key` je pruhledna."""
    if not IS_WIN:
        return
    hwnd = root_hwnd(win)
    style = user32.GetWindowLongW(hwnd, -20)
    # LAYERED | TRANSPARENT (click-through) | TOOLWINDOW | NOACTIVATE
    user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80 | 0x08000000)
    colorref = key[0] | (key[1] << 8) | (key[2] << 16)
    user32.SetLayeredWindowAttributes(hwnd, colorref, 0, 0x1)       # LWA_COLORKEY


def set_noactivate(win):
    """Okno nebere fokus (cilova aplikace zustane aktivni)."""
    if not IS_WIN:
        return
    hwnd = root_hwnd(win)
    style = user32.GetWindowLongW(hwnd, -20)
    user32.SetWindowLongW(hwnd, -20, style | 0x08000000)


# ------------------------------ monitory -----------------------------------
def list_monitors():
    """[{x, y, w, h, primary, xreal}] ve fyzickych pixelech."""
    if not IS_WIN:
        return []

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]

    class DISPLAY_DEVICEW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                    ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                    ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]

    mons = []
    proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.POINTER(RECT), wintypes.LPARAM)

    def cb(hmon, hdc, lprect, lparam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
            r = mi.rcMonitor
            ident = ""
            try:    # EDID id monitoru: u XREAL One Pro "MRG4100"
                dd = DISPLAY_DEVICEW()
                dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
                if user32.EnumDisplayDevicesW(mi.szDevice, 0, ctypes.byref(dd), 0):
                    ident = (dd.DeviceID + " " + dd.DeviceString).upper()
            except Exception:
                pass
            mons.append({"x": r.left, "y": r.top, "w": r.right - r.left,
                         "h": r.bottom - r.top, "primary": bool(mi.dwFlags & 1),
                         "xreal": "MRG" in ident or "XREAL" in ident or "NREAL" in ident})
        return True

    try:
        user32.EnumDisplayMonitors(None, None, proc_t(cb), 0)
    except Exception:
        return []
    return mons


def pick_monitor(mons, idx):
    """idx >= 0: pevne zvoleny; jinak auto = XREAL, pak prvni neprimarni, pak prvni."""
    if not mons:
        return None
    if 0 <= idx < len(mons):
        return mons[idx]
    for m in mons:
        if m["xreal"]:
            return m
    for m in mons:
        if not m["primary"]:
            return m
    return mons[0]


# ------------------------------ ruzne --------------------------------------
def get_mouse_vanish():
    """Nastaveni 'Skryt ukazatel behem psani'."""
    if not IS_WIN:
        return None
    val = wintypes.BOOL()
    if user32.SystemParametersInfoW(0x1020, 0, ctypes.byref(val), 0):
        return bool(val.value)
    return None


def set_mouse_vanish(on: bool):
    if IS_WIN:
        user32.SystemParametersInfoW(0x1021, 0, ctypes.c_void_p(1 if on else 0), 0)


def beep_async(freq: int, ms: int):
    if not IS_WIN:
        return

    def run():
        try:
            import winsound
            winsound.Beep(freq, ms)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()
