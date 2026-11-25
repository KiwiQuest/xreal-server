"""Rádce: ví, v jakém stavu je konfigurace, jak teď může uživatel ovládat a co mu nabídnout.

Vstupem je snímek faktů (víko, brýle, IMU, tlačítko, displej, fyzický vstup, režim) a
paměť toho, jak se uživateli daří. Výstupem je:
  - `Caps`: jaké ovládání je právě k dispozici (ruce / hlava / tlačítko / nic),
  - `Guidance`: co klidně ukázat v overlay (nadpis, věta, možnosti a jak je vyvolat),
  - varování ke konfiguraci (např. víko ve Windows nemá akci "nedělat nic").
Je to čistá logika, testuje ji tests/test_advisor.py.
"""
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .states import BAG, DESK, SUSPEND, WAITING

FULL, HEAD, BUTTON_ONLY, NONE = "full", "head", "button", "none"


@dataclass
class Snapshot:
    mode: str = DESK
    lid: Optional[str] = None
    glasses_usb: bool = False
    imu_alive: bool = False
    imu_calibrating: bool = False
    button_alive: bool = False
    glasses_display: bool = False        # monitor brýlí je ve Windows aktivní
    physical_input_age: Optional[float] = None   # s od posledního fyzického vstupu (ne SendInput)
    lid_action_ok: Optional[bool] = None  # Windows: akce víka = "nedělat nic"
    remaining: Optional[float] = None     # WAITING: zbývající čas
    glasses_lost: bool = False            # BAG: brýle vypadly, běží tolerance
    menu_open: bool = False
    prompt: Optional[str] = None
    head_mouse_on: bool = False
    modules_failed: Tuple[str, ...] = ()


@dataclass
class Caps:
    hands: bool
    head: bool
    button: bool
    display: bool

    @property
    def level(self) -> str:
        if self.hands:
            return FULL
        if self.head:
            return HEAD
        if self.button:
            return BUTTON_ONLY
        return NONE


@dataclass
class Guidance:
    title: str
    text: str
    options: List[Tuple[str, str]] = field(default_factory=list)   # (jak, co)
    tone: str = "calm"                  # calm | info | warn
    show: bool = True                   # u stolu s rukama overlay nepotřebujeme
    progress: Optional[float] = None    # 0..1 pro odpočet
    warnings: List[str] = field(default_factory=list)


def capabilities(s: Snapshot, fresh_s: float = 30.0) -> Caps:
    lid_open = s.lid != "closed"
    recent = s.physical_input_age is not None and s.physical_input_age <= fresh_s
    # se zavřeným víkem počítáme s rukama jen tehdy, když fyzický vstup opravdu přichází
    # (externí klávesnice); s otevřeným víkem má uživatel klávesnici notebooku
    hands = (lid_open and s.mode == DESK) or recent
    head = s.imu_alive and not s.imu_calibrating
    return Caps(hands=hands, head=head, button=s.button_alive,
                display=s.glasses_display or lid_open)


class Struggle:
    """Pozná, že uživatel s ovládáním zápasí: otázky bez odpovědi, menu otevřené a zavřené
    bez volby, opakované "ne", divoké pohyby bez potvrzení. Úroveň 0-2, úspěch ji sníží."""

    WINDOW = 120.0

    def __init__(self):
        self.ev = deque(maxlen=50)
        self.t_ok = 0.0

    def note(self, now: float, kind: str):
        if kind == "success":
            self.t_ok = now
            self.ev.clear()
            return
        self.ev.append((now, kind))

    def level(self, now: float) -> int:
        w = [k for t, k in self.ev if now - t <= self.WINDOW]
        score = (2 * w.count("timeout") + w.count("menu_abandoned") + w.count("shake")
                 + w.count("jitter") + 2 * w.count("no_input"))
        return 2 if score >= 5 else 1 if score >= 2 else 0


def _how(c: Caps) -> List[Tuple[str, str]]:
    opts = []
    if c.button:
        opts.append(("podrž tlačítko brýlí", "menu"))
    if c.head:
        opts.append(("kývni", "ano"))
        opts.append(("zavrť hlavou", "ne / zavřít"))
    return opts


def guidance(s: Snapshot, c: Caps, struggle: int = 0, wait_total: float = 300.0) -> Guidance:
    warn = config_warnings(s)
    if s.mode == SUSPEND:
        return Guidance("Hibernuji", "Notebook se ukládá. Po otevření víka pokračuješ, kde jsi skončil.",
                        tone="info", warnings=warn)

    if s.mode == WAITING:
        rem = s.remaining or 0.0
        m, sec = int(rem) // 60, int(rem) % 60
        text = f"Brýle nejsou připojené. Hibernace za {m}:{sec:02d}."
        opts = [("připoj brýle", "pokračovat v tašce"), ("otevři víko", "zpět k práci")]
        if c.head or c.button:
            opts.append(("podrž tlačítko brýlí", "hibernovat hned / čekat déle"))
        return Guidance("Čekám na brýle", text, opts, tone="warn" if rem <= 60 else "info",
                        progress=1.0 - rem / max(1.0, wait_total), warnings=warn)

    if s.mode == BAG:
        if s.glasses_lost:
            return Guidance("Brýle se odpojily", "Zkontroluj kabel. Za chvíli začnu odpočet do hibernace.",
                            [("připoj brýle", "pokračovat")], tone="warn", warnings=warn)
        if not c.head and not c.button:
            return Guidance("Čekám na signál z brýlí", "Brýle jsou připojené, ale zatím neposílají pohyb ani tlačítko.",
                            [("chvíli počkej", "spojení se obnovuje samo")], tone="info", warnings=warn)
        if s.imu_calibrating:
            return Guidance("Kalibrace", "Drž hlavu chvíli v klidu, nastavuji senzory brýlí.",
                            tone="info", warnings=warn)
        if struggle >= 2:
            return Guidance("Jednodušší ovládání",
                            "Vypadá to, že ovládání nejde dobře. Stačí jen tlačítko a kývnutí.",
                            [("podrž tlačítko brýlí", "menu s velkými položkami"),
                             ("kývni", "vycentrovat a znovu kalibrovat")],
                            tone="warn", warnings=warn)
        text = ("Myš ovládáš hlavou, klik = zastav pohled." if s.head_mouse_on
                else "Myš hlavou je vypnutá.")
        if struggle == 1:
            text += " Když něco nejde, podrž tlačítko brýlí, menu ti pomůže."
        return Guidance("Notebook v tašce", text, _how(c), tone="calm", warnings=warn)

    # DESK
    show = bool(warn) and not c.hands
    if s.glasses_usb and not s.imu_alive:
        return Guidance("Brýle připojené", "Čekám na data z brýlí.", tone="info", show=False, warnings=warn)
    return Guidance("Běžná práce", "Aplikace běží v systray. Zavřením víka přejdeš do režimu v tašce.",
                    tone="calm", show=show, warnings=warn)


def config_warnings(s: Snapshot) -> List[str]:
    w = []
    if s.lid_action_ok is False:
        w.append("Ve Windows není u zavření víka nastaveno „Nedělat nic“, režim v tašce se nespustí.")
    if s.mode == BAG and s.glasses_usb and not s.glasses_display:
        w.append("Brýle jsou připojené, ale Windows na nich nic nezobrazuje.")
    for m in s.modules_failed:
        w.append(f"Modul „{m}“ spadl a byl vypnut.")
    return w
