"""Nastavení kořenové aplikace (JSON vedle aplikace, chybějící klíče doplní výchozí hodnoty)."""
import json
import os
import sys
from dataclasses import asdict, dataclass, field, fields

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_DIR = os.path.dirname(APP_DIR)
CONFIG_FILE = os.path.join(APP_DIR, "koren_config.json")
LOG_DIR = os.path.join(APP_DIR, "logs")
# Hotové řešení myši hlavou (XREAL Head Mouse 2.0), ze kterého kořen bere protokol a engine.
SUITE_DIR = os.path.join(REPO_DIR, "Xreal mouse", "xreal_suite", "xreal_suite")


def use_suite():
    """Zpřístupní balík `xreal` z Head Mouse 2.0 (protokol brýlí, fúze IMU, engine myši)."""
    if os.path.isdir(SUITE_DIR) and SUITE_DIR not in sys.path:
        sys.path.insert(0, SUITE_DIR)


@dataclass
class Config:
    # --- provozní režim (profiles.py): auto | tray | monitor | glasses ---
    profile: str = "auto"
    autostart: bool = True             # spouštět s Windows (klíč Run v HKCU)
    saved_topology: int = 0            # Win+P stav před zhasnutím displeje notebooku (0 = nic)
    glasses_display: dict = field(default_factory=dict)   # potvrzený monitor brýlí {path, edid, name}
    # --- přechod "notebook v tašce" ---
    wait_minutes: float = 5.0          # jak dlouho po zavření víka čekat na brýle, pak hibernovat
    bag_lost_grace_s: float = 20.0     # v tašce: brýle zmizely -> chvíli tolerovat výpadek kabelu
    hibernate_fallback_sleep: bool = True   # hibernace vypnutá ve Windows -> uspat
    dry_run: bool = False              # nic nehibernuje ani nehýbe myší, jen hlásí (vývoj)
    # --- ovládání bez rukou ---
    menu_hold_s: float = 2.0           # podržení tlačítka brýlí -> kořenové menu
    menu_step_deg: float = 7.0         # o kolik stupňů pohnout hlavou na další položku
    menu_dwell_s: float = 2.5          # podržet pohled na položce = potvrzení
    prompt_timeout_s: float = 20.0     # otázka bez reakce -> zopakovat jednodušeji
    physical_input_fresh_s: float = 30.0   # fyzická klávesnice/myš použitá před méně než X s = má ruce
    # --- overlay ---
    overlay_alpha: float = 0.88
    overlay_scale: float = 1.0
    hint_seconds: float = 8.0          # jak dlouho nechat klidnou nápovědu, než se zmenší
    sound_on: bool = True
    # --- moduly ---
    modules: dict = field(default_factory=lambda: {
        "head_mouse": True,
        "clock": True,
        "voice": False,
    })

    def load(self, path: str = CONFIG_FILE):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return self
        for fld in fields(self):
            if fld.name not in data:
                continue
            cur = getattr(self, fld.name)
            try:
                if isinstance(cur, dict):
                    if isinstance(data[fld.name], dict):
                        merged = dict(cur)
                        merged.update(data[fld.name])
                        setattr(self, fld.name, merged)
                else:
                    setattr(self, fld.name, type(cur)(data[fld.name]))
            except (TypeError, ValueError):
                pass
        return self

    def save(self, path: str = CONFIG_FILE):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, indent=2, ensure_ascii=False)
            os.replace(tmp, path)                 # atomicky: rozpracovaný zápis nerozbije nastavení
        except OSError:
            pass
