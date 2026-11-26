"""Stavový automat přechodu "notebook v tašce".

Čistá logika bez Windows a bez vláken: dostane událost a čas, vrátí seznam akcí.
Díky tomu jde celé chování otestovat (tests/test_states.py) a v aplikaci se nedá
dostat do stavu, ze kterého by vedla cesta jen přes klávesnici.

Režimy:
  DESK      běžná práce, aplikace jen v systray
  BAG       víko zavřené (nebo ručně zapnuto) a brýle připojené: ovládání jen hlavou
  WAITING   víko zavřené, brýle chybí: odpočet, pak hibernace
  SUSPEND   právě se hibernuje / uspává
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

DESK, BAG, WAITING, SUSPEND = "DESK", "BAG", "WAITING", "SUSPEND"

MODE_LABELS = {
    DESK: "Běžná práce",
    BAG: "Notebook v tašce",
    WAITING: "Čekám na brýle",
    SUSPEND: "Hibernuji",
}

# Události (jednoduché n-tice, ať se dají posílat přes frontu z libovolného vlákna)
#   ("lid", "open" | "closed" | None)
#   ("glasses", bool)               brýle přítomné (USB nebo živé IMU), už odfiltrované
#   ("tick",)                       pravidelný tik hlavní smyčky
#   ("manual", bool)                ruční režim bez rukou (stolní PC, Jetson, otevřené víko)
#   ("hibernate_now",)              uživatel potvrdil hibernaci
#   ("wait_longer",)                uživatel chce čekat déle
#   ("resumed",)                    Windows se probudily
#   ("suspend_failed", "důvod")
Event = Tuple

WARN_AT = (60, 30, 10)        # kdy před hibernací upozornit (s)


@dataclass
class Facts:
    lid: Optional[str] = None          # "open" / "closed" / None = notebook bez víka nebo neznámo
    glasses: bool = False
    manual: bool = False


@dataclass
class Machine:
    wait_s: float = 300.0
    lost_grace_s: float = 20.0
    mode: str = DESK
    facts: Facts = field(default_factory=Facts)
    deadline: Optional[float] = None        # WAITING: kdy hibernovat
    lost_since: Optional[float] = None      # BAG: kdy brýle zmizely
    since: float = 0.0                      # kdy začal aktuální režim
    reason: str = "start"
    _warned: set = field(default_factory=set)

    # ------------------------------------------------------------------ dotazy
    @property
    def remaining(self) -> Optional[float]:
        return None if self.deadline is None else max(0.0, self.deadline - self._now)

    _now: float = 0.0

    @property
    def hands_free(self) -> bool:
        """Uživatel nemá klávesnici ani myš (podle režimu)."""
        return self.mode in (BAG, WAITING)

    # ----------------------------------------------------------------- přechody
    def _enter(self, mode: str, now: float, reason: str) -> List[tuple]:
        if mode == self.mode:
            return []
        prev = self.mode
        self.mode, self.since, self.reason = mode, now, reason
        if mode != WAITING:
            self.deadline = None
            self._warned = set()
        if mode != BAG:
            self.lost_since = None
        return [("mode", prev, mode, reason)]

    def _start_wait(self, now: float, reason: str) -> List[tuple]:
        acts = self._enter(WAITING, now, reason)
        if acts:
            self.deadline = now + self.wait_s
            self._warned = set()
            acts.append(("say", f"Brýle nejsou připojené. Za {_fmt(self.wait_s)} notebook uspím do hibernace."))
            acts.append(("beep", "wait"))
        return acts

    def _evaluate(self, now: float, reason: str) -> List[tuple]:
        """Rozhodne režim jen z faktů (po startu, probuzení, změně víka...)."""
        f = self.facts
        hands_free_wanted = f.lid == "closed" or f.manual
        if not hands_free_wanted:
            return self._enter(DESK, now, reason)
        if f.glasses:
            acts = self._enter(BAG, now, reason)
            if acts:
                acts.append(("say", "Notebook v tašce. Ovládáš hlavou, podržením tlačítka na brýlích otevřeš menu."))
                acts.append(("beep", "bag"))
            return acts
        if f.lid == "closed":
            return self._start_wait(now, reason)
        # ruční režim bez brýlí (víko otevřené): není co ovládat, zůstat u stolu
        return self._enter(DESK, now, "ruční režim čeká na brýle")

    def handle(self, ev: Event, now: float) -> List[tuple]:
        self._now = now
        kind = ev[0]
        f = self.facts
        if kind == "lid":
            f.lid = ev[1]
            if self.mode == SUSPEND:
                return []
            return self._evaluate(now, "víko zavřené" if ev[1] == "closed" else "víko otevřené")
        if kind == "glasses":
            f.glasses = bool(ev[1])
            if self.mode == SUSPEND:
                return []
            if self.mode == BAG and not f.glasses:
                if self.lost_since is None:
                    self.lost_since = now
                    return [("say", "Brýle se odpojily. Chvíli počkám, jestli se vrátí."), ("beep", "lost")]
                return []
            if self.mode == BAG and f.glasses and self.lost_since is not None:
                self.lost_since = None
                return [("say", "Brýle jsou zpět."), ("beep", "bag")]
            return self._evaluate(now, "brýle připojeny" if f.glasses else "brýle odpojeny")
        if kind == "manual":
            f.manual = bool(ev[1])
            if self.mode == SUSPEND:
                return []
            return self._evaluate(now, "ruční režim bez rukou" if f.manual else "ruční režim vypnut")
        if kind == "hibernate_now":
            if self.mode == SUSPEND:
                return []
            return self._enter(SUSPEND, now, "na přání uživatele") + [("hibernate",)]
        if kind == "wait_longer":
            if self.mode == WAITING:
                self.deadline = max(self.deadline or now, now) + self.wait_s
                self._warned = set()
                return [("say", f"Dobře, čekám dalších {_fmt(self.wait_s)}.")]
            return []
        if kind == "resumed":
            self.mode = DESK                     # po probuzení vždy znovu rozhodnout
            self.deadline = self.lost_since = None
            self._warned = set()
            return self._evaluate(now, "probuzení")
        if kind == "suspend_failed":
            self.mode = DESK
            acts = [("say", f"Hibernace se nepovedla: {ev[1]}")]
            return acts + self._evaluate(now, "hibernace selhala")
        if kind == "tick":
            return self._tick(now)
        return []

    def _tick(self, now: float) -> List[tuple]:
        if self.mode == BAG and self.lost_since is not None and now - self.lost_since >= self.lost_grace_s:
            if self.facts.lid == "closed":
                return self._start_wait(now, "brýle se nevrátily")
            return self._enter(DESK, now, "brýle se nevrátily")
        if self.mode == WAITING and self.deadline is not None:
            left = self.deadline - now
            if left <= 0:
                return self._enter(SUSPEND, now, "brýle se nepřipojily") + [("hibernate",)]
            acts = []
            for w in WARN_AT:
                if left <= w < self.wait_s and w not in self._warned:
                    self._warned.add(w)
                    acts.append(("say", f"Za {_fmt(w)} hibernuji. Připoj brýle, nebo otevři víko."))
                    acts.append(("beep", "warn"))
            return acts
        return []


def _fmt(s: float) -> str:
    s = int(round(s))
    if s >= 60 and s % 60 == 0:
        m = s // 60
        return f"{m} minutu" if m == 1 else f"{m} minuty" if m < 5 else f"{m} minut"
    if s >= 60:
        return f"{s // 60} min {s % 60} s"
    return f"{s} sekund" if s >= 5 or s == 0 else f"{s} sekundy" if s > 1 else "1 sekundu"
