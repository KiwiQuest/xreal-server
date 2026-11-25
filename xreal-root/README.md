# XREAL Kořen

Kořenová aplikace v systray pro Windows. Běží pořád, hlídá víko notebooku a brýle XREAL a umí
přejít do ovládání **jen hlavou a tlačítkem brýlí**, když uživatel nemá klávesnici ani myš
(notebook v tašce, uživatel na cestě).

Návrh, katalog hotových částí a plán modulů: [ARCHITEKTURA.md](ARCHITEKTURA.md).

## Spuštění

```bat
koren.bat                      :: bez okna konzole, se supervizorem (doporučeno)
python koren.py --dry          :: zkušebně: nehibernuje, nehýbe myší, v systray zkoušky víka
python koren.py --wait 2       :: čekat na brýle 2 minuty místo 5
python -m unittest discover tests
```

Potřebuje jen Python 3.10+ s tkinter. Bere protokol brýlí a myš hlavou ze složky
`../Xreal mouse/xreal_suite/xreal_suite` (Head Mouse 2.0), takže obě složky musí být vedle sebe.
**Nespouštěj zároveň Ariel ani samostatnou Head Mouse**, kořen drží spojení s brýlemi sám.

### Nastavení Windows

Ovládací panely → Možnosti napájení → Nastavení tlačítek napájení → **Při zavření víka: Nedělat nic**
(na baterii i v síti). Když to tak není, kořen to ukáže v systray i v brýlích.

## Jak se chová

| Situace | Co kořen udělá |
|---|---|
| Pracuješ u notebooku | sedí v systray, nic neukazuje |
| Zavřeš víko a brýle jsou připojené | režim **Notebook v tašce**: zapne myš hlavou, klidně ukáže možnosti |
| Zavřeš víko a brýle nejsou | **odpočet** (5 min, pípnutí před koncem); připojíš brýle → režim v tašce, nic → hibernace |
| V tašce se odpojí kabel | 20 s tolerance, pak odpočet do hibernace |
| Otevřeš víko | zpět k běžné práci, odpočet zrušen |
| Probuzení z hibernace | znovu rozhodne podle víka a brýlí |

Ovládání bez rukou: **podržet tlačítko brýlí 2 s = menu**, hlava nahoru/dolů = výběr, podržet pohled nebo
krátký stisk = potvrdit, **kývnutí = ano**, **zavrtění = ne**. Otázka bez odpovědi se jednou zopakuje
jednodušeji a pak platí NE.

V systray (pravé tlačítko): stav, ruční **Režim bez rukou** (stolní PC, Jetson), zapínání modulů, log.

## Složky

| Cesta | Obsah |
|---|---|
| `koren.py`, `koren.bat` | spuštění a supervizor (restart po pádu nebo zaseknutí) |
| `koren/states.py` | stavový automat DESK / BAG / WAITING / SUSPEND |
| `koren/advisor.py` | rádce: jaké ovládání má uživatel, co mu ukázat, poznání potíží |
| `koren/gestures.py`, `koren/menu.py` | kývnutí, zavrtění, podržení tlačítka, menu ovládané hlavou |
| `koren/overlay.py` | průhledné vrstvy přes brýle |
| `koren/glasses.py` | jediné spojení s brýlemi (IMU, tlačítko, přítomnost na USB) |
| `koren/win.py` | systray, víko, hibernace, USB, fyzický vstup (ctypes) |
| `koren/modules/` | moduly: `head_mouse`, `clock`, `voice` |
| `tests/` | testy čisté logiky |
| `koren_config.json`, `logs/` | vznikají za běhu, nejsou v gitu |
