"""Provozní režimy kořene: co má v kterém režimu běžet (čistá logika, testy v tests/).

V režimech 1-3 má uživatel myš a klávesnici, ovládání hlavou se samo nezapíná
(jde zapnout ručně). Jen zavřené víko (režim 4) počítá s tím, že ruce nemá.

  1  TRAY      jen ikona v systray a okno nastavení na požádání. Nic dalšího neběží:
               žádné moduly, žádný overlay, žádné spojení s IMU (jen levná kontrola USB a víka).
  2  MONITOR   okno nastavení a stavu stále na druhém monitoru, uživatel kontroluje nastavení.
               Spojení s brýlemi běží (živý stav), moduly ne.
  3  GLASSES   vše běží jen v brýlích (moduly, overlay), víko je otevřené a displej notebooku
               nic neukazuje.
  4  (víko zavřené)  přebije cokoliv: notebook v tašce / čekání na brýle / hibernace,
               tady se ovládání hlavou zapne samo.

  AUTO         výchozí: brýle připojené = GLASSES, nepřipojené = TRAY.
Do TRAY jde přepnout kdykoliv: systray, Ctrl+Alt+T, položka v menu brýlí, okno nastavení.
"""
from dataclasses import dataclass

from .states import BAG, SUSPEND, WAITING

AUTO, TRAY, MONITOR, GLASSES = "auto", "tray", "monitor", "glasses"
PROFILES = (AUTO, TRAY, MONITOR, GLASSES)
LABELS = {
    AUTO: "Automaticky (podle brýlí)",
    TRAY: "Tray: jen nastavení, nic neběží",
    MONITOR: "GUI na druhém monitoru",
    GLASSES: "Vše v brýlích (víko otevřené)",
}
SHORT = {AUTO: "auto", TRAY: "Tray", MONITOR: "GUI na monitoru", GLASSES: "Vše v brýlích"}


def effective(profile: str, glasses_present: bool) -> str:
    if profile == AUTO:
        return GLASSES if glasses_present else TRAY
    return profile if profile in PROFILES else TRAY


@dataclass(frozen=True)
class Plan:
    streams: bool          # spojení s IMU a tlačítkem brýlí
    modules: bool          # moduly (myš hlavou, hodiny...)
    overlay: bool          # kořenová nápověda a menu v brýlích
    gui: bool              # okno nastavení trvale viditelné
    laptop_screen_off: bool
    tick_ms: int


def plan(eff: str, mode: str) -> Plan:
    """eff = efektivní profil, mode = režim stavového automatu."""
    if mode == SUSPEND:
        return Plan(False, False, True, False, False, 250)
    if mode in (BAG, WAITING):           # víko zavřené (nebo ruční režim bez rukou): běží vše
        return Plan(True, True, True, False, False, 30)
    if eff == GLASSES:
        return Plan(True, True, True, False, True, 30)
    if eff == MONITOR:
        return Plan(True, False, False, True, False, 100)
    return Plan(False, False, False, False, False, 250)
