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

## Provozní režimy

Po startu s Windows (výchozí) se kořen sám podívá po brýlích: **jsou připojené → Vše v brýlích,
nejsou → Tray**. Režim jde kdykoliv změnit v systray nebo v okně nastavení, **Ctrl+Alt+T vždy přepne do Tray**.

| Režim | Co běží | Ovládání |
|---|---|---|
| 1. **Tray** | jen ikona a okno nastavení na požádání; žádné moduly, overlay ani spojení s IMU (šetří výkon) | myš a klávesnice |
| 2. **GUI na druhém monitoru** | okno nastavení a živého stavu trvale na druhém monitoru, spojení s brýlemi kvůli stavu, moduly ne | myš a klávesnice |
| 3. **Vše v brýlích** | víko otevřené, displej notebooku zhasne (jako Win+P „Jen druhá obrazovka“), moduly a overlay běží v brýlích; při odchodu z režimu se displej vrátí | myš a klávesnice |
| 4. **Zavřené víko** | přebije režimy 1-3: notebook v tašce, nebo odpočet a hibernace | jen hlava a tlačítko brýlí |

V režimech 1-3 se ovládání hlavou samo **nezapíná**, ale jde kdykoliv zapnout ručně (systray, okno nastavení,
menu v brýlích); v Tray režimu se kvůli tomu spustí jen myš hlavou a spojení s brýlemi.

### Monitor brýlí

Kořen nespoléhá na pořadí monitorů ani na název XREAL. Podle jména z Windows jen odhadne, který monitor
jsou brýle, a poprvé otevře okno nastavení, kde ho jedním klikem potvrdíš („Toto jsou brýle“). Uloží se
cesta zařízení, kód EDID a jméno, takže se brýle najdou i jako třetí monitor nebo na jiném portu.
Displej notebooku zhasne (režim 3) až po potvrzení, ať se omylem nevypne obyčejný monitor.

## Kořenová plocha

V režimu „Vše v brýlích“ a po zavření víka se přes celé brýle otevře **plocha** ve stylu `ariel/static/yolo.html`
(černé pozadí = v brýlích průhledné, zelený HUD):

- **stavový řádek**: hlavní stavová ikona, režim, rotující zprávy na zvolená témata (Systém, Brýle, Moduly, Čas,
  Info; témata se zapínají vpravo nahoře), hodiny;
- **plocha modulů**: okna 1..N (vestavěná Systém, Brýle, Moduly, Log a okna modulů). Okna jdou posouvat, měnit
  velikost, skrýt do lišty, maximalizovat, přepínat průhlednost (◐) a skládat **vedle sebe** nebo **přes sebe**;
  „+ OKNO“ přidá další, „ULOŽIT“ zapamatuje rozložení, A−/A+ mění velikost písma;
- **panel control modulů** vpravo: karty se nepřekrývají, aktivní karta ukazuje informace a ovládání modulu.

Plocha je webová stránka z lokálního serveru kořene (`127.0.0.1:8790`, jen tento počítač) v okně Chrome / Edge bez
rámečku, které kořen položí přesně na monitor brýlí. Data jdou živě (Server-Sent Events), příkazy přijímá jen
od vlastní stránky. Moduly na ni přidávají obsah přes `ui()` a `ui_action()`.

## Domov: overlay menu dlaždic

Z menu (podržet tlačítko brýlí) nebo z plochy se otevře **Domov**: dlaždice visí v prostoru kolem tebe,
otočením hlavy se mezi nimi rozhlížíš a uprostřed pohledu je zaměřovač. Dlaždici vybereš pohledem
(podržet 1,6 s, kroužek se naplní) nebo krátkým stiskem tlačítka brýlí. Pozadí je černé, takže v brýlích
je průhledné a Domov visí ve skutečném prostoru. Když se ztratí z dohledu, sám se po 3 s vrátí před tebe.

Dlaždice teď: Windows (zavře Domov, zapne myš hlavou), vlastní klávesnice na obrazovce (modul `keyboard`), prohlížeč, kamera Eye,
Průzkumník, vycentrovat, nastavení, režim Tray, hibernace (s potvrzením). Další moduly přidávají vlastní
dlaždice přes `home_tiles()`. Zpět do Domova: podržet tlačítko brýlí → menu → „Domov“.

### Klávesnice na obrazovce

Klávesnice Windows (osk.exe) běží s ochranou UIAccess: klik z myši hlavou na ni nefunguje a kořen ji
neumí ani zavřít. Proto má kořen vlastní (`keyboard`): nebere fokus, píše do aktivního okna včetně
české diakritiky a zavírá se klávesou ✕. Otevřeš ji v Domově nebo v menu.

## Jak se chová víko

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
| `koren/profiles.py` | provozní režimy Tray / GUI na monitoru / Vše v brýlích: co v nich běží |
| `koren/settings.py` | okno nastavení a stavu |
| `koren/ui_server.py`, `koren/ui/index.html`, `koren/ui_window.py` | kořenová plocha: server, stránka, okno přes brýle |
| `koren/sysinfo.py` | CPU, paměť, baterie bez knihoven |
| `koren/displays.py` | monitory podle jména a EDID, odhad a potvrzení monitoru brýlí |
| `koren/advisor.py` | rádce: jaké ovládání má uživatel, co mu ukázat, poznání potíží |
| `koren/gestures.py`, `koren/menu.py` | kývnutí, zavrtění, podržení tlačítka, menu ovládané hlavou |
| `koren/overlay.py` | průhledné vrstvy přes brýle |
| `koren/glasses.py` | jediné spojení s brýlemi (IMU, tlačítko, přítomnost na USB) |
| `koren/win.py` | systray, víko, hibernace, USB, fyzický vstup (ctypes) |
| `koren/modules/` | moduly: `home` (Domov v brýlích), `keyboard`, `head_mouse`, `clock`, `voice` |
| `tests/` | testy čisté logiky |
| `koren_config.json`, `logs/` | vznikají za běhu, nejsou v gitu |
