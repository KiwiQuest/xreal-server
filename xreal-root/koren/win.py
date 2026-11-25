"""Tenká vrstva nad Windows (jen ctypes, žádné knihovny navíc).

  NativeWindow   skryté okno ve vlastním vlákně: ikona v systray s menu, víko notebooku
                 (power setting notification), probuzení / uspání, změny USB a displejů,
                 fyzický vstup klávesnice a myši (Raw Input; SendInput z myši hlavou se nepočítá)
  power_caps()   má notebook víko, jde hibernovat
  lid_action()   co Windows dělá při zavření víka (0 = nedělat nic)
  glasses_usb()  jsou brýle XREAL na USB (VID 3318)
  hibernate()    hibernace, případně uspání
  single_instance()  jen jedna kopie aplikace
"""
import subprocess
import sys
import threading
import time
from typing import Callable, List, Optional, Tuple

IS_WIN = sys.platform == "win32"

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    powrprof = ctypes.WinDLL("powrprof", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    cfgmgr32 = ctypes.WinDLL("cfgmgr32", use_last_error=True)

    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    HICON = wintypes.HANDLE

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

        @classmethod
        def parse(cls, s: str) -> "GUID":
            s = s.strip("{}").replace("-", "")
            b = bytes.fromhex(s[16:])
            return cls(int(s[0:8], 16), int(s[8:12], 16), int(s[12:16], 16), (ctypes.c_ubyte * 8)(*b))

        def __eq__(self, o):
            return bytes(self) == bytes(o)

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE), ("hIcon", HICON), ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", HICON)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                    ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", HICON),
                    ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                    ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                    ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                    ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID), ("hBalloonIcon", HICON)]

    class POWERBROADCAST_SETTING(ctypes.Structure):
        _fields_ = [("PowerSetting", GUID), ("DataLength", wintypes.DWORD), ("Data", wintypes.DWORD)]

    class RAWINPUTDEVICE(ctypes.Structure):
        _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                    ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]

    class RAWINPUTHEADER(ctypes.Structure):
        _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                    ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]

    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT), ("wParam", wintypes.WPARAM),
                    ("lParam", wintypes.LPARAM), ("time", wintypes.DWORD), ("pt", wintypes.POINT),
                    ("lPrivate", wintypes.DWORD)]

    user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.DefWindowProcW.restype = LRESULT
    user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
    user32.RegisterClassExW.restype = wintypes.ATOM
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.restype = LRESULT
    user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
    user32.LoadIconW.restype = HICON
    user32.RegisterPowerSettingNotification.argtypes = [wintypes.HANDLE, ctypes.POINTER(GUID), wintypes.DWORD]
    user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
    user32.UnregisterPowerSettingNotification.argtypes = [wintypes.HANDLE]
    user32.RegisterRawInputDevices.argtypes = [ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]
    user32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
                                       ctypes.POINTER(wintypes.UINT), wintypes.UINT]
    user32.GetRawInputData.restype = wintypes.UINT
    user32.CreatePopupMenu.restype = wintypes.HMENU
    user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, wintypes.HWND, wintypes.LPVOID]
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.DestroyMenu.argtypes = [wintypes.HMENU]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterWindowMessageW.restype = wintypes.UINT
    shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
    shell32.Shell_NotifyIconW.restype = wintypes.BOOL
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]

    GUID_LIDSWITCH_STATE_CHANGE = GUID.parse("BA3E0F4D-B817-4094-A2D1-D56379E6A0F3")
    GUID_SUB_BUTTONS = GUID.parse("4F971E89-EEBD-4455-A8DE-9E59040E7347")
    GUID_LIDACTION = GUID.parse("5CA83367-6E45-459F-A27B-476B1D01C936")

WM_CLOSE, WM_DESTROY, WM_NULL = 0x0010, 0x0002, 0x0000
WM_POWERBROADCAST, WM_DEVICECHANGE, WM_DISPLAYCHANGE, WM_INPUT = 0x0218, 0x0219, 0x007E, 0x00FF
WM_QUERYENDSESSION, WM_ENDSESSION = 0x0011, 0x0016
WM_HOTKEY = 0x0312
WM_APP = 0x8000
WM_TRAY, WM_REFRESH = WM_APP + 1, WM_APP + 2
WM_LBUTTONUP, WM_RBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0205, 0x0203
PBT_APMSUSPEND, PBT_APMRESUMESUSPEND, PBT_APMRESUMEAUTOMATIC, PBT_POWERSETTINGCHANGE = 0x4, 0x7, 0x12, 0x8013

ICONS = {"desk": 32512, "bag": 32516, "wait": 32515, "error": 32513}   # IDI_APPLICATION / INFORMATION / WARNING / ERROR

MenuEntry = Tuple[int, str, int]     # (id, text, příznaky: 1 = zašedlé, 8 = zaškrtnuté, -1 = oddělovač)


class NativeWindow(threading.Thread):
    """Skryté okno Windows. `post(event)` dostává n-tice, volá se z vlákna okna, má být rychlé
    (typicky queue.put). Události:
        ("lid", "open"|"closed")  ("power", "suspend"|"resume")  ("devchange",)  ("display",)
        ("tray", id)  ("tray_open",)  ("endsession",)
    """

    def __init__(self, post: Callable[[tuple], None], menu: Callable[[], List[MenuEntry]],
                 title: str = "XREAL Kořen", log=print):
        super().__init__(name="native-window", daemon=True)
        self.post, self.menu, self.title, self.log = post, menu, title, log
        self.hwnd = None
        self.ready = threading.Event()
        self.last_physical = None          # time.monotonic() posledního fyzického vstupu
        self._tip, self._icon = title, "desk"
        self._balloon: Optional[Tuple[str, str]] = None
        self._icon_added = False
        self._proc = None
        self._hpower = None

    # ------------------------------------------------------------- z jiných vláken
    def set_tray(self, tip: str, icon: str = "desk"):
        if (tip, icon) == (self._tip, self._icon):
            return
        self._tip, self._icon = tip[:127], icon
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)

    def balloon(self, title: str, text: str):
        self._balloon = (title[:63], text[:255])
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_REFRESH, 0, 0)

    def physical_input_age(self) -> Optional[float]:
        t = self.last_physical
        return None if t is None else time.monotonic() - t

    def close(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)

    # ------------------------------------------------------------------ vlákno okna
    def run(self):
        if not IS_WIN:
            self.ready.set()
            return
        try:
            self._create()
        except Exception as e:
            self.log(f"[WIN] okno nelze vytvořit: {e}")
            self.ready.set()
            return
        self.ready.set()
        msg = MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _create(self):
        hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)              # držet referenci, jinak GC = pád
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = "XrealKorenHidden"
        user32.RegisterClassExW(ctypes.byref(wc))
        # obyčejné (ne message-only) okno: jen takové dostává WM_DEVICECHANGE a WM_DISPLAYCHANGE
        self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, self.title, 0, 0, 0, 0, 0,
                                           None, None, hinst, None)
        if not self.hwnd:
            raise OSError(ctypes.get_last_error())
        self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
        # víko: Windows hned po registraci pošle aktuální stav
        self._hpower = user32.RegisterPowerSettingNotification(
            self.hwnd, ctypes.byref(GUID_LIDSWITCH_STATE_CHANGE), 0)
        # fyzický vstup (i na pozadí): myš 1/2, klávesnice 1/6
        devs = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(1, 2, 0x100, self.hwnd),
                                    RAWINPUTDEVICE(1, 6, 0x100, self.hwnd))
        if not user32.RegisterRawInputDevices(devs, 2, ctypes.sizeof(RAWINPUTDEVICE)):
            self.log("[WIN] Raw Input nejde zaregistrovat, fyzický vstup nepoznám")
        # Ctrl+Alt+T = kdykoliv zpět do režimu Tray (MOD_ALT | MOD_CONTROL | MOD_NOREPEAT)
        if not user32.RegisterHotKey(self.hwnd, 1, 0x1 | 0x2 | 0x4000, ord("T")):
            self.log("[WIN] Ctrl+Alt+T už používá jiný program")
        self._tray(0)

    def _nid(self) -> "NOTIFYICONDATAW":
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd, nid.uID = self.hwnd, 1
        return nid

    def _tray(self, op: int):
        nid = self._nid()
        nid.uFlags = 0x1 | 0x2 | 0x4                    # MESSAGE | ICON | TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = user32.LoadIconW(None, ctypes.c_void_p(ICONS.get(self._icon, 32512)))
        nid.szTip = self._tip
        if self._balloon:
            nid.uFlags |= 0x10                          # INFO
            nid.szInfoTitle, nid.szInfo = self._balloon
            nid.dwInfoFlags = 0x1                       # NIIF_INFO
            self._balloon = None
        ok = shell32.Shell_NotifyIconW(op, ctypes.byref(nid))
        if op == 0:
            self._icon_added = bool(ok)
        elif not ok and not self._icon_added:
            self._icon_added = bool(shell32.Shell_NotifyIconW(0, ctypes.byref(nid)))

    def _show_menu(self):
        entries = self.menu()
        hmenu = user32.CreatePopupMenu()
        for mid, text, flags in entries:
            if flags == -1:
                user32.AppendMenuW(hmenu, 0x800, 0, None)
            else:
                user32.AppendMenuW(hmenu, flags & 0x9, mid, text)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(hmenu, 0x100 | 0x2, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(hmenu)
        if cmd:
            self.post(("tray", int(cmd)))

    def _wndproc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_INPUT:
                self._raw_input(lp)
            elif msg == WM_POWERBROADCAST:
                if wp == PBT_POWERSETTINGCHANGE and lp:
                    s = ctypes.cast(lp, ctypes.POINTER(POWERBROADCAST_SETTING)).contents
                    if s.PowerSetting == GUID_LIDSWITCH_STATE_CHANGE:
                        self.post(("lid", "open" if s.Data else "closed"))
                elif wp == PBT_APMSUSPEND:
                    self.post(("power", "suspend"))
                elif wp in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
                    self.post(("power", "resume"))
                return 1
            elif msg == WM_HOTKEY:
                self.post(("hotkey", int(wp)))
                return 0
            elif msg == WM_DEVICECHANGE:
                self.post(("devchange",))
            elif msg == WM_DISPLAYCHANGE:
                self.post(("display",))
            elif msg == WM_TRAY:
                ev = lp & 0xFFFF
                if ev == WM_RBUTTONUP:
                    self._show_menu()
                elif ev in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self.post(("tray_open",))
                return 0
            elif msg == WM_REFRESH:
                self._tray(1)
                return 0
            elif msg == getattr(self, "_taskbar_created", -1):
                self._tray(0)                            # Průzkumník se restartoval
                return 0
            elif msg == WM_ENDSESSION and wp:
                self.post(("endsession",))
            elif msg == WM_CLOSE:
                shell32.Shell_NotifyIconW(2, ctypes.byref(self._nid()))
                if self._hpower:
                    user32.UnregisterPowerSettingNotification(self._hpower)
                user32.DestroyWindow(hwnd)
                return 0
            elif msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:                           # výjimka v callbacku nesmí shodit okno
            self.log(f"[WIN] {type(e).__name__}: {e}")
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _raw_input(self, lp):
        hdr = RAWINPUTHEADER()
        size = wintypes.UINT(ctypes.sizeof(RAWINPUTHEADER))
        r = user32.GetRawInputData(lp, 0x10000005, ctypes.byref(hdr), ctypes.byref(size),
                                   ctypes.sizeof(RAWINPUTHEADER))
        if r != 0xFFFFFFFF and hdr.hDevice:             # hDevice 0 = SendInput (myš hlavou)
            self.last_physical = time.monotonic()


# ---------------------------------------------------------------- jednorázové dotazy
def power_caps() -> dict:
    """{'lid': bool, 'hibernate': bool, 'sleep': bool} z GetPwrCapabilities."""
    if not IS_WIN:
        return {"lid": False, "hibernate": False, "sleep": False}
    buf = (ctypes.c_ubyte * 128)()
    if not powrprof.GetPwrCapabilities(ctypes.byref(buf)):
        return {"lid": False, "hibernate": False, "sleep": False}
    # PowerButton, SleepButton, LidPresent, S1, S2, S3, S4, S5, HiberFilePresent
    return {"lid": bool(buf[2]), "sleep": bool(buf[3] or buf[4] or buf[5]),
            "hibernate": bool(buf[6] and buf[8])}


def lid_action() -> Optional[dict]:
    """Akce zavření víka v aktivním schématu napájení: {'ac': n, 'dc': n}, 0 = nedělat nic."""
    if not IS_WIN:
        return None
    p = ctypes.POINTER(GUID)()
    if powrprof.PowerGetActiveScheme(None, ctypes.byref(p)) != 0:
        return None
    try:
        out = {}
        for key, fn in (("ac", powrprof.PowerReadACValueIndex), ("dc", powrprof.PowerReadDCValueIndex)):
            v = wintypes.DWORD()
            if fn(None, p, ctypes.byref(GUID_SUB_BUTTONS), ctypes.byref(GUID_LIDACTION), ctypes.byref(v)) == 0:
                out[key] = v.value
        return out or None
    finally:
        kernel32.LocalFree(p)


XREAL_VID = "VID_3318"


def usb_ids() -> List[str]:
    if not IS_WIN:
        return []
    flags = 0x1 | 0x100              # CM_GETIDLIST_FILTER_ENUMERATOR | FILTER_PRESENT
    n = wintypes.ULONG()
    if cfgmgr32.CM_Get_Device_ID_List_SizeW(ctypes.byref(n), "USB", flags) != 0 or not n.value:
        return []
    buf = ctypes.create_unicode_buffer(n.value)
    if cfgmgr32.CM_Get_Device_ID_ListW("USB", buf, n.value, flags) != 0:
        return []
    return [s for s in ctypes.wstring_at(ctypes.addressof(buf), n.value).split("\x00") if s]


def glasses_usb() -> bool:
    try:
        return any(XREAL_VID in s.upper() for s in usb_ids())
    except Exception:
        return False


def _enable_shutdown_privilege():
    class LUID(ctypes.Structure):
        _fields_ = [("Low", wintypes.DWORD), ("High", wintypes.LONG)]

    class TP(ctypes.Structure):
        _fields_ = [("Count", wintypes.DWORD), ("Luid", LUID), ("Attr", wintypes.DWORD)]

    tok = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x28, ctypes.byref(tok)):
        return False
    luid = LUID()
    if not advapi32.LookupPrivilegeValueW(None, "SeShutdownPrivilege", ctypes.byref(luid)):
        return False
    tp = TP(1, luid, 0x2)
    advapi32.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None)
    kernel32.CloseHandle(tok)
    return True


def hibernate(dry: bool = False, fallback_sleep: bool = True, log=print) -> Tuple[bool, str]:
    """Hibernace (nebo uspání, když hibernace ve Windows není zapnutá). Vrací (ok, popis)."""
    caps = power_caps()
    want_hib = caps["hibernate"] or not fallback_sleep
    what = "hibernace" if want_hib else "uspání"
    if dry:
        log(f"[POWER] (dry) {what} by teď proběhla")
        return True, f"(zkušebně) {what}"
    if not IS_WIN:
        return False, "není Windows"
    _enable_shutdown_privilege()
    if powrprof.SetSuspendState(bool(want_hib), False, False):
        return True, what
    if want_hib:
        try:
            r = subprocess.run(["shutdown", "/h"], capture_output=True, timeout=15,
                               creationflags=0x08000000)
            if r.returncode == 0:
                return True, what
        except Exception as e:
            log(f"[POWER] shutdown /h: {e}")
    return False, f"{what} selhalo (kód {ctypes.get_last_error()})"


_mutex = None


def single_instance(name: str = "Local\\XrealKorenSupervisor") -> bool:
    """True = jsme jediní. Mutex se drží do konce procesu."""
    global _mutex
    if not IS_WIN:
        return True
    _mutex = kernel32.CreateMutexW(None, False, name)
    return ctypes.get_last_error() != 183          # ERROR_ALREADY_EXISTS


# ------------------------------------------------------------- displej notebooku
# Topologie jako Win+P: 1 = jen tento počítač, 2 = duplikovat, 4 = rozšířit, 8 = jen druhá obrazovka
TOPO_INTERNAL, TOPO_CLONE, TOPO_EXTEND, TOPO_EXTERNAL = 1, 2, 4, 8


def display_topology() -> Optional[int]:
    """Aktuální topologie z databáze Windows (jako Win+P), None = nejde zjistit."""
    if not IS_WIN:
        return None
    QDC_DATABASE_CURRENT = 0x4
    npath, nmode = wintypes.UINT(), wintypes.UINT()
    if user32.GetDisplayConfigBufferSizes(QDC_DATABASE_CURRENT, ctypes.byref(npath), ctypes.byref(nmode)) != 0:
        return None
    paths = (ctypes.c_ubyte * (72 * max(1, npath.value)))()     # DISPLAYCONFIG_PATH_INFO = 72 B
    modes = (ctypes.c_ubyte * (64 * max(1, nmode.value)))()     # DISPLAYCONFIG_MODE_INFO = 64 B
    topo = wintypes.UINT()
    if user32.QueryDisplayConfig(QDC_DATABASE_CURRENT, ctypes.byref(npath), paths, ctypes.byref(nmode),
                                 modes, ctypes.byref(topo)) != 0:
        return None
    return topo.value


def set_display_topology(topo: int, log=print) -> bool:
    """Přepne topologii (SDC_APPLY | SDC_TOPOLOGY_*), stejně jako Win+P."""
    if not IS_WIN or topo not in (1, 2, 4, 8):
        return False
    r = user32.SetDisplayConfig(0, None, 0, None, 0x80 | topo)
    if r != 0:
        log(f"[DISP] SetDisplayConfig({topo}) selhal: {r}")
    return r == 0


# ------------------------------------------------------------------- autostart
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "XrealKoren"


def autostart_command(script: str) -> str:
    exe = sys.executable
    low = exe.lower()
    if low.endswith("python.exe"):
        exe = exe[:-len("python.exe")] + "pythonw.exe"
    return f'"{exe}" "{script}"'


def autostart_get() -> Optional[str]:
    if not IS_WIN:
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, RUN_NAME)[0]
    except OSError:
        return None


def autostart_set(on: bool, script: str, log=print) -> bool:
    """Spouštění s Windows (klíč Run v HKCU, jen pro tohoto uživatele, bez admin práv)."""
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, autostart_command(script))
            else:
                try:
                    winreg.DeleteValue(k, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except OSError as e:
        log(f"[AUTOSTART] {e}")
        return False
