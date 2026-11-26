"""Modulový systém kořenové aplikace.

Modul je soubor v této složce (nebo v `koren_config.json` -> "modules"), který definuje
třídu odvozenou od `Module` a proměnnou `MODULE = TřídaModulu`.

Pravidla, ať jich může běžet víc nad sebou a nic nerozbily:
  - Kreslí jen do vlastní průhledné vrstvy (`ctx.layer(...)`), vrstvy se řadí podle `z`.
    Vrstvy nechytají kliky ani fokus, takže pod nimi pořád pracuje Windows i jiné moduly.
  - Kořenová nápověda a menu jsou vždy nejvýš (z = 1000) a podržení tlačítka brýlí patří
    vždy kořeni. Modul se o vstup dělí přes `on_button`, `on_imu` a `on_gesture`.
  - Každé volání modulu je chráněné: výjimka se zapíše do logu, po 3 chybách se modul vypne
    a rádce to uživateli klidně oznámí. Kořen tím nespadne.
  - `on_imu` se volá z vlákna brýlí ~1000x za sekundu: jen rychlé výpočty, nic nekreslit.
    Ostatní metody běží v hlavním vlákně (Tk).
"""
import importlib
import time
import traceback
from typing import Dict, List, Optional


class Module:
    id = "module"
    name = "Modul"
    description = ""
    z = 100                     # pořadí vrstvy (víc = výš)
    needs = ()                  # "imu", "button", "display" - rádce podle toho ví, kdy má smysl

    def __init__(self):
        self.ctx: Optional["ModuleContext"] = None
        self.active = False

    # --- životní cyklus ---
    def start(self, ctx: "ModuleContext"):
        self.ctx = ctx

    def stop(self):
        pass

    # --- události ---
    def on_mode(self, prev: str, mode: str):
        """Změna režimu kořene (DESK / BAG / WAITING / SUSPEND)."""

    def on_imu(self, sample):
        """Vzorek IMU (protocol.ImuSample), vlákno brýlí."""

    def on_button(self, down: bool, t: float):
        """Surový stisk tlačítka brýlí (když ho zrovna nedrží kořenové menu)."""

    def on_gesture(self, g: str):
        """'nod' / 'shake' mimo kořenové menu."""

    def on_focus(self, root_has_focus: bool):
        """Kořen si bere vstup (menu, otázka) nebo ho vrací."""

    def tick(self, now: float):
        """~20x za sekundu v hlavním vlákně."""

    # --- pro kořen a rádce ---
    def status(self) -> str:
        return ""

    def menu_items(self) -> list:
        """Položky (menu.MenuItem) do kořenového menu."""
        return []

    def home_tiles(self) -> list:
        """Dlaždice (modules.home.Tile) do prostorového Domova v brýlích."""
        return []

    def captures_input(self) -> bool:
        """True = modul teď chce tlačítko a gesta jen pro sebe (např. otevřený Domov).
        Ostatní moduly dostanou on_focus(True), podržení tlačítka (menu) zůstává kořeni."""
        return False

    def ui(self) -> dict:
        """Co ukázat na kořenové ploše: {"window": bool (jde otevřít jako okno), "control": bool
        (karta v panelu control modulů), "lines": [...], "actions": [{"id", "label", "on"}]}."""
        return {}

    def ui_action(self, action: str, value=None):
        """Klik na akci z kořenové plochy."""

    def on_press(self, t: float):
        """Krátký stisk tlačítka brýlí, když modul drží vstup (captures_input)."""


class ModuleContext:
    """Co kořen modulu půjčuje: vrstvy overlay, hlášky, nastavení, stav."""

    def __init__(self, app, module_id: str):
        self._app, self.id = app, module_id

    @property
    def cfg(self):
        return self._app.cfg

    @property
    def dry(self) -> bool:
        return self._app.cfg.dry_run

    @property
    def tk(self):
        return self._app.root

    def log(self, text: str):
        self._app.log(text)

    def layer(self, z: Optional[int] = None):
        return self._app.overlay.layer(self.id, z if z is not None else 100)

    def say(self, text: str):
        self._app.say(text, source=self.id)

    def toast(self, text: str, seconds: float = 2.0):
        self._app.overlay_toast(text, seconds)

    def mode(self) -> str:
        return self._app.machine.mode

    def monitor(self):
        return self._app.glasses_monitor()

    def head(self):
        """(yaw°, pitch°) hlavy: yaw + = doprava (integrovaný z gyra, jde vycentrovat),
        pitch + = nahoru (z gravitace, neujíždí). None = brýle neposílají pohyb."""
        return self._app.glasses.head()

    def ask(self, question: str, on_yes=None, on_no=None):
        self._app.ask(question, on_yes=on_yes, on_no=on_no)

    def app(self):
        """Přístup ke kořeni pro vestavěné moduly (nastavení, hibernace, režimy)."""
        return self._app


class ModuleManager:
    MAX_ERRORS = 3

    def __init__(self, log=print):
        self.log = log
        self.mods: Dict[str, Module] = {}
        self.errors: Dict[str, int] = {}
        self.failed: List[str] = []

    def load(self, ids, factory=None):
        """Načte moduly podle id. `factory(id) -> Module` jde podstrčit v testech."""
        for mid in ids:
            try:
                if factory:
                    mod = factory(mid)
                else:
                    pkg = importlib.import_module(f"{__name__}.{mid}")
                    mod = pkg.MODULE()
                self.mods[mod.id] = mod
                self.errors[mod.id] = 0
            except Exception as e:
                self.log(f"[MOD] {mid}: nelze načíst: {type(e).__name__}: {e}")
                self.failed.append(mid)

    def start_all(self, make_ctx):
        for mid, mod in list(self.mods.items()):
            if self.call(mod, "start", make_ctx(mid)) is not _FAIL:
                mod.active = True

    def stop_all(self):
        for mod in list(self.mods.values()):
            if mod.active:
                self.call(mod, "stop")
                mod.active = False

    def call(self, mod: Module, name: str, *args):
        try:
            return getattr(mod, name)(*args)
        except Exception as e:
            n = self.errors.get(mod.id, 0) + 1
            self.errors[mod.id] = n
            self.log(f"[MOD] {mod.id}.{name}: {type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}")
            if n >= self.MAX_ERRORS and mod.active:
                mod.active = False
                self.failed.append(mod.name)
                self.log(f"[MOD] {mod.id} vypnut po {n} chybách")
                try:
                    mod.stop()
                except Exception:
                    pass
            return _FAIL

    def each(self, name: str, *args):
        for mod in list(self.mods.values()):
            if mod.active:
                self.call(mod, name, *args)

    def get(self, mid: str) -> Optional[Module]:
        m = self.mods.get(mid)
        return m if m and m.active else None

    def menu_items(self) -> list:
        return self._collect("menu_items")

    def home_tiles(self) -> list:
        return self._collect("home_tiles")

    def _collect(self, name: str) -> list:
        items = []
        for mod in list(self.mods.values()):
            if mod.active:
                r = self.call(mod, name)
                if isinstance(r, list):
                    items.extend(r)
        return items

    def capturing(self):
        """Modul, který teď drží vstup, nebo None."""
        for mod in list(self.mods.values()):
            if mod.active and self.call(mod, "captures_input") is True:
                return mod
        return None


class _Fail:
    def __repr__(self):
        return "<FAIL>"


_FAIL = _Fail()
