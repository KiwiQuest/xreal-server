"""Monitory podle jména, ne podle pořadí.

Brýle nemusí být vždy "druhý monitor" a nemusí se jmenovat XREAL (jiný firmware, jiné brýle,
redukce). Proto:
  1. Windows dají ke každému monitoru jméno z EDID (např. "XREAL One Pro"), kód výrobce a
     produktu ("MRG4100") a cestu zařízení (stálá pro stejný kus a port).
  2. Kořen podle jména jen *odhadne*, který monitor jsou brýle, a uživatel ho jedním klikem
     potvrdí v okně nastavení. Potvrzený monitor se uloží (cesta, EDID, jméno).
  3. Příště se hledá uložený: nejdřív přesná cesta, pak EDID kód, pak jméno.
"""
import re
import sys
from typing import List, Optional, Tuple

IS_WIN = sys.platform == "win32"
GUESS = re.compile(r"xreal|nreal|mrg\d", re.I)

if IS_WIN:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class _LUID(ctypes.Structure):
        _fields_ = [("Low", wintypes.DWORD), ("High", wintypes.LONG)]

    class _HDR(ctypes.Structure):
        _fields_ = [("type", ctypes.c_uint32), ("size", ctypes.c_uint32), ("adapterId", _LUID),
                    ("id", ctypes.c_uint32)]

    class _SOURCE_NAME(ctypes.Structure):
        _fields_ = [("header", _HDR), ("viewGdiDeviceName", wintypes.WCHAR * 32)]

    class _TARGET_NAME(ctypes.Structure):
        _fields_ = [("header", _HDR), ("flags", ctypes.c_uint32), ("outputTechnology", ctypes.c_uint32),
                    ("edidManufactureId", ctypes.c_uint16), ("edidProductCodeId", ctypes.c_uint16),
                    ("connectorInstance", ctypes.c_uint32), ("monitorFriendlyDeviceName", wintypes.WCHAR * 64),
                    ("monitorDevicePath", wintypes.WCHAR * 128)]

    class _PATH(ctypes.Structure):          # DISPLAYCONFIG_PATH_INFO (72 B)
        _fields_ = [("srcAdapter", _LUID), ("srcId", ctypes.c_uint32), ("srcMode", ctypes.c_uint32),
                    ("srcFlags", ctypes.c_uint32),
                    ("tgtAdapter", _LUID), ("tgtId", ctypes.c_uint32), ("tgtMode", ctypes.c_uint32),
                    ("tech", ctypes.c_uint32), ("rot", ctypes.c_uint32), ("scal", ctypes.c_uint32),
                    ("rrN", ctypes.c_uint32), ("rrD", ctypes.c_uint32), ("scan", ctypes.c_uint32),
                    ("avail", wintypes.BOOL), ("tgtFlags", ctypes.c_uint32), ("flags", ctypes.c_uint32)]

    class _RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG),
                    ("bottom", wintypes.LONG)]

    class _MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT), ("rcWork", _RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]


def _edid_code(manu: int, prod: int) -> str:
    """EDID výrobce (3 písmena, big-endian v 16 bitech) + kód produktu, např. MRG4100."""
    m = ((manu & 0xFF) << 8) | (manu >> 8)
    letters = "".join(chr(((m >> s) & 0x1F) + 64) for s in (10, 5, 0))
    return f"{letters}{prod:04X}" if letters.isalpha() else ""


def _names_by_gdi() -> dict:
    """\\\\.\\DISPLAY1 -> {'name', 'path', 'edid'} z DisplayConfig (aktivní cesty)."""
    out = {}
    npath, nmode = ctypes.c_uint32(), ctypes.c_uint32()
    if user32.GetDisplayConfigBufferSizes(2, ctypes.byref(npath), ctypes.byref(nmode)) != 0:   # ONLY_ACTIVE
        return out
    paths = (_PATH * max(1, npath.value))()
    modes = (ctypes.c_ubyte * (64 * max(1, nmode.value)))()
    if user32.QueryDisplayConfig(2, ctypes.byref(npath), paths, ctypes.byref(nmode), modes, None) != 0:
        return out
    for p in paths[:npath.value]:
        src = _SOURCE_NAME()
        src.header.type, src.header.size = 1, ctypes.sizeof(_SOURCE_NAME)
        src.header.adapterId, src.header.id = p.srcAdapter, p.srcId
        tgt = _TARGET_NAME()
        tgt.header.type, tgt.header.size = 2, ctypes.sizeof(_TARGET_NAME)
        tgt.header.adapterId, tgt.header.id = p.tgtAdapter, p.tgtId
        if user32.DisplayConfigGetDeviceInfo(ctypes.byref(src)) != 0:
            continue
        if user32.DisplayConfigGetDeviceInfo(ctypes.byref(tgt)) != 0:
            continue
        out[src.viewGdiDeviceName] = {
            "name": tgt.monitorFriendlyDeviceName.strip(),
            "path": tgt.monitorDevicePath,
            "edid": _edid_code(tgt.edidManufactureId, tgt.edidProductCodeId),
            "internal": tgt.outputTechnology in (0x80000000, 11, 13),   # vestavěný displej / eDP
        }
    return out


def list_displays() -> List[dict]:
    """[{x, y, w, h, primary, gdi, name, path, edid, internal, label}] aktivní monitory."""
    if not IS_WIN:
        return []
    try:
        names = _names_by_gdi()
    except Exception:
        names = {}
    mons = []
    proc_t = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.POINTER(_RECT), wintypes.LPARAM)

    def cb(hmon, hdc, lprect, lparam):
        mi = _MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(_MONITORINFOEXW)
        if user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
            r = mi.rcMonitor
            n = names.get(mi.szDevice, {})
            d = {"x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top,
                 "primary": bool(mi.dwFlags & 1), "gdi": mi.szDevice, "name": n.get("name", ""),
                 "path": n.get("path", ""), "edid": n.get("edid", ""), "internal": n.get("internal", False)}
            d["label"] = label(d)
            mons.append(d)
        return True

    try:
        user32.EnumDisplayMonitors(None, None, proc_t(cb), 0)
    except Exception:
        return []
    return mons


def label(d: dict) -> str:
    name = d.get("name") or ("vestavěný displej" if d.get("internal") else "monitor")
    extra = [f"{d['w']}×{d['h']}"]
    if d.get("edid"):
        extra.append(d["edid"])
    if d.get("primary"):
        extra.append("hlavní")
    return f"{name} ({', '.join(extra)})"


def looks_like_glasses(d: dict) -> bool:
    return bool(GUESS.search(" ".join((d.get("name", ""), d.get("edid", ""), d.get("path", "")))))


def find_glasses(displays: List[dict], saved: Optional[dict]) -> Tuple[Optional[dict], bool]:
    """(monitor brýlí, potvrzený?). Potvrzený = shoduje se s uloženým výběrem uživatele.
    Jinak jen odhad podle jména (uživatel ho má potvrdit), nebo None."""
    if saved:
        for key in ("path", "edid", "name"):
            v = saved.get(key)
            if v:
                for d in displays:
                    if d.get(key) == v:
                        return d, True
    guesses = [d for d in displays if looks_like_glasses(d)]
    if guesses:
        return guesses[0], False
    return None, False


def remember(d: dict) -> dict:
    """Co uložit do nastavení z potvrzeného monitoru."""
    return {"path": d.get("path", ""), "edid": d.get("edid", ""), "name": d.get("name", "")}
