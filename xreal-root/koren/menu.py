"""Kořenové menu a otázky ano/ne ovládané jen hlavou.

Menu: položku vybírá náklon hlavy (pitch z gravitace, neujíždí), potvrzuje krátký stisk
tlačítka brýlí nebo podržení pohledu na položce (kroužek se naplní). Zavírá zavrtění hlavou
nebo položka "Zavřít". Otázka: kývnutí = ano, zavrtění = ne, bez reakce = bezpečná volba.
"""
from dataclasses import dataclass, field
from typing import Callable, List, Optional


@dataclass
class MenuItem:
    id: str
    label: str
    hint: str = ""
    action: Optional[Callable[[], None]] = None
    confirm: str = ""               # neprázdné = před provedením se zeptat (text otázky)
    enabled: bool = True


@dataclass
class Menu:
    title: str
    items: List[MenuItem]
    step_deg: float = 7.0
    dwell_s: float = 2.5
    index: int = 0
    progress: float = 0.0           # 0..1 potvrzení podržením pohledu
    armed: bool = False             # podržení pohledu potvrzuje až poté, co se hlavou vybíralo
    _ref: Optional[float] = None
    _base: int = 0

    def update(self, pitch_deg: Optional[float], dt: float) -> Optional[MenuItem]:
        """Vrací položku, pokud byla potvrzena podržením pohledu."""
        if not self.items:
            return None
        if pitch_deg is not None:
            if self._ref is None:
                self._ref, self._base = pitch_deg, self.index
            # hlava dolů = další položka (pitch roste nahoru)
            step = max(1.0, self.step_deg)
            pos = self._base + (self._ref - pitch_deg) / step
            idx = min(max(int(round(pos)), 0), len(self.items) - 1)
            if idx != self.index:
                self.index, self.progress = idx, 0.0
                self.armed = True
            if pos < -0.5 or pos > len(self.items) - 0.5:
                # za krajem seznamu posunout referenci, ať se dá hned vrátit zpět
                self._ref = pitch_deg + (self.index - self._base) * step
        cur = self.items[self.index]
        if not cur.enabled or not self.armed:
            self.progress = 0.0
            return None
        self.progress += dt / max(0.5, self.dwell_s)
        if self.progress >= 1.0:
            self.progress = 0.0
            return cur
        return None

    def current(self) -> Optional[MenuItem]:
        return self.items[self.index] if self.items else None

    def press(self) -> Optional[MenuItem]:
        cur = self.current()
        self.progress = 0.0
        return cur if cur and cur.enabled else None

    def recenter(self):
        self._ref = None


@dataclass
class Prompt:
    question: str
    on_yes: Optional[Callable[[], None]] = None
    on_no: Optional[Callable[[], None]] = None
    timeout_s: float = 20.0
    default_yes: bool = False          # co se stane bez reakce (bezpečná volba = ne)
    started: float = 0.0
    repeats: int = 0
    max_repeats: int = 1               # kolikrát zopakovat jednodušeji, než platí výchozí volba
    answered: Optional[bool] = None
    extra: dict = field(default_factory=dict)

    def expired(self, now: float) -> bool:
        return now - self.started >= self.timeout_s
