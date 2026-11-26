# XREAL Kořen: katalog hotových částí a architektura

Tenhle dokument je „první krok“: co už v repozitáři funguje, z čeho kořenová aplikace vychází
a jak je postavená, aby na ni šlo postupně navěšovat moduly.

## 1. Katalog toho, co už existuje

| Část | Co umí | Stav | Jak ji kořen využije |
|---|---|---|---|
| `xreal-eye-rgb/` | Zapne kameru XREAL Eye jako webkameru ve Windows (HID rámec `0xFD`, příkazy `0xD2`/`0xD3`, pole `uvc0`/`uvc1`). RGB MJPEG 1920×1080 až 60 fps, šedá SLAM 512×378. Zálohuje a umí vrátit původní stav. | funguje, ověřeno | budoucí modul „Kamera“ (zapnutí kamery bez Nebuly) |
| `rgb-test/` | Sondy a log, na kterých se kamera rozchodila (Python, C# `EyeWebcamProbe`). | podklady | jen reference |
| `Xreal mouse/xreal_suite` (Head Mouse 2.0) | Čistě rozdělené jádro bez knihoven: `protocol` (IMU 52998, řídicí kanál 52999, tlačítko `0x2E`), `fusion` (Mahony, kalibrace gyra, pitch z gravitace), `pointer` (režimy relativní / střed pohledu / rozhlížení), `dwell` (circle click), `buttons` (klik, tažení, pravý klik, přepnutí dwell), `overlay` (kroužek u kurzoru), `hud_overlay` (HUD s moduly, virtuální klávesnice), `dasher` (psaní bez klávesnice, čeština), `eye`/`vision`/`yolo` (SLAM kamera, hrany, YOLO), `ar`/`ar_overlay` (promítnutí objektů z kamery do brýlí), `winapi` (SendInput, monitory, průhledná okna), testy. | funguje dobře | **základ kořene**: protokol, fúze, `winapi` a celý engine myši se používají beze změny |
| `Xreal mouse/XREAL_MOUSE.py` | Starší jednosouborová verze myši hlavou (F8/F9/F10). | nahrazeno suite 2.0 | ne |
| `ariel/` (Ariel Core) | Jádro serveru rozdělené na moduly: IMU a tlačítka, kamera Eye → MJPEG, YOLO (PyTorch / OpenVINO CPU-GPU-NPU), OCR, AR kalibrace, myš hlavou, SendInput, fráze pro psaní, dataset, HTTP 8080 + WebSocket 8765 (příkazy `mouse_*`, `key_*`, `type_text`, `head`, `ocr`, `zoom`, `yolo_cfg`, `ar`, `calib`, `overlay`…), stránky `tablet`, `ar`, `yolo`, `mono`, `calib`, `phrases`, `oko` (kontrola zraku v SBS), šifrovaný modul tváří. | hlavní větev serveru | budoucí moduly „Server/tablet“, „Vidění“, „Kontrola zraku“ |
| `xreal-web/` | Původní monolit `server.py`, ze kterého vzniklo Ariel. | archiv | ne |

**Důležité omezení:** IMU (52998) a řídicí kanál (52999) brýlí má držet jen jeden program. Proto
kořen je jediný vlastník spojení a data rozdává modulům. Ariel ani samostatnou Head Mouse
nespouštět zároveň s kořenem, dokud nebudou předělané na moduly (viz bod 5).

## 2. Proč Python + tkinter + ctypes

- Na tom stojí všechno hotové (Head Mouse, Ariel), kód se dá převzít bez přepisování.
- Žádné knihovny navíc: jen Python 3.10+ s tkinter. Méně závislostí = méně důvodů k pádu na cestě
  a běží to stejně na notebooku s integrovanou grafikou, vývojové desce i pevném PC.
- Systray, víko, hibernace, USB a fyzický vstup jdou přímo přes Win32 (`koren/win.py`).

## 3. Provozní režimy (`koren/profiles.py`)

| | Tray | GUI na druhém monitoru | Vše v brýlích | Zavřené víko |
|---|---|---|---|---|
| spojení s IMU a tlačítkem | ne | ano (živý stav) | ano | ano |
| moduly | ne | ne | ano | ano |
| overlay a menu v brýlích | ne | ne | ano | ano |
| okno nastavení | na požádání | trvale na druhém monitoru | na požádání | ne |
| displej notebooku | beze změny | beze změny | zhasnutý (Win+P), pak vrácen | Windows ho vypne samy |
| ovládání hlavou | ručně | ručně | ručně | samo zapnuté |
| tik hlavní smyčky | 250 ms | 100 ms | 30 ms | 30 ms |

Výchozí profil `auto`: brýle připojené = Vše v brýlích, jinak Tray. Start s Windows přes klíč `Run`
v HKCU (bez admin práv). Do Tray jde vždy: systray, **Ctrl+Alt+T**, položka v menu brýlí, okno nastavení.
Kontrola USB a víka běží ve všech režimech (je levná), aby zavření víka fungovalo i z Tray.
Stav Win+P před zhasnutím se ukládá do nastavení, takže se displej vrátí i po pádu aplikace.

## 4. Stavový automat „notebook v tašce“ (`koren/states.py`)

```
                víko zavřené + brýle            brýle zmizely (tolerance 20 s)
      ┌──────┐ ─────────────────────► ┌─────┐ ─────────────────────────────┐
      │ DESK │                         │ BAG │                              ▼
      └──────┘ ◄───── víko otevřené ── └─────┘ ◄── brýle připojeny ── ┌─────────┐
         ▲  │                                                          │ WAITING │ odpočet X min
         │  └──────── víko zavřené, brýle nejsou ─────────────────────►└─────────┘
         │                                                    čas vypršel │ nebo „Hibernovat“
         │                                                                ▼
         └──────────── probuzení: znovu rozhodnout podle víka a brýlí ── SUSPEND (hibernace)
```

- Rozhoduje jen z faktů (víko, brýle, ruční režim), takže po probuzení, pádu nebo restartu se vždy
  vrátí do správného stavu. Čistá logika bez Windows, pokrytá testy.
- Notebook bez víka (stolní PC, Jetson) nikdy sám nehibernuje; režim bez rukou se zapíná ručně.
- Výpadek kabelu brýlí v tašce: nejdřív tolerance, pak teprve odpočet. Bez odpovědi uživatele
  se nikdy neprovede nic nevratného kromě hibernace po odpočtu (ta je bezpečná, nic se neztratí).

## 5. Ovládání bez rukou a rádce

**Vstupy, které fungují vždy, i když modul drží myš:**

| Gesto | Význam |
|---|---|
| podržet tlačítko brýlí 2 s | kořenové menu (kruh nahoře ukazuje, jak dlouho ještě držet) |
| v menu: hlava nahoru / dolů | výběr položky (pitch z gravitace, neujíždí) |
| v menu: podržet pohled 2,5 s nebo krátký stisk | potvrdit |
| kývnutí | ano |
| zavrtění hlavou | ne / zavřít |

**Rádce (`koren/advisor.py`)** dostává snímek stavu (víko, brýle USB, živé IMU, tlačítko, displej brýlí,
čas od posledního *fyzického* vstupu přes Raw Input, nastavení víka ve Windows, odpočet, moduly) a vrací:

- `Caps`: jak teď uživatel může ovládat: `full` (ruce), `head`, `button`, `none`.
  Vstup z myši hlavou (SendInput) se za fyzický nepočítá, takže kořen pozná, že uživatel ruce nemá.
- `Guidance`: klidný nadpis, jedna věta a seznam „co udělat → co se stane“.
- `Struggle`: pozná potíže s ovládáním (otázka bez odpovědi, menu otevřené a zavřené bez volby,
  opakované zavrtění). Na úrovni 1 přidá radu, na úrovni 2 přepne na jednodušší nabídku
  (jen tlačítko a kývnutí). Úspěšná akce úroveň vynuluje.
- Varování ke konfiguraci: zavření víka ve Windows není „Nedělat nic“, brýle jsou na USB, ale
  Windows na nich nic nezobrazuje, modul spadl.

**Overlay (`koren/overlay.py`)** je nenásilný: po změně stavu ukáže panel dole, po 8 s se stáhne
do jednoho řádku. Znovu se rozbalí při otázce, varování nebo potížích. U stolu s rukama se neukazuje.
Otázka bez reakce se jednou zopakuje jednodušeji, pak platí bezpečná volba (NE).

## 6. Moduly (`koren/modules/`)

Modul = třída odvozená od `Module` + `MODULE = Třída`. Dostává:

| Metoda | Kdy |
|---|---|
| `start(ctx)` / `stop()` | zapnutí / vypnutí (i za běhu ze systray) |
| `on_mode(prev, mode)` | změna režimu kořene |
| `on_imu(sample)` | každý vzorek IMU (vlákno brýlí, jen rychlé výpočty) |
| `on_button(down, t)` | tlačítko brýlí, když ho nedrží kořen |
| `on_gesture(g)` | kývnutí / zavrtění mimo menu |
| `on_focus(root)` | kořen si bere vstup (menu, otázka): modul se pozastaví |
| `tick(now)` | ~30× za sekundu v hlavním vlákně |
| `menu_items()` | položky do kořenového menu |
| `status()` | text do systray |

`ctx.layer(z)` dá modulu vlastní průhlednou vrstvu přes brýle (nechytá kliky ani fokus), vrstvy se
řadí podle `z`, kořen je vždy nahoře (z = 1000). Tím může běžet víc modulů nad sebou.
Výjimka v modulu se zaloguje, po 3 chybách se modul vypne a rádce to oznámí; kořen běží dál.

**Kořenová plocha** (`ui_server.py`, `ui/index.html`) je hlavní prostředí v brýlích ve stylu `yolo.html`:
stavový řádek s rotujícími zprávami, okna modulů 1..N (vedle sebe / přes sebe s průhledností) a pevný panel
control modulů. Modul se na ní ukáže přes `ui()` (`window` = jde otevřít jako okno, `control` = karta v panelu,
`lines`, `actions`) a reaguje přes `ui_action()`. Běží v režimu Vše v brýlích a po zavření víka (`Plan.ui`).
Modul, který potřebuje vlastní obsah (video, rámečky), vrátí v `ui()` `"frame": "/mod/<id>/"`: okno pak
ukáže jeho stránku v iframe s průhledným pozadím a data si stránka bere přes `http_get(path, query)`
(`GET /mod/<id>/…`, vlákno serveru, umí i MJPEG stream). `"show"` (počítadlo) = požádat plochu o otevření
nebo přenesení okna dopředu (z Domova, z menu brýlí).

**Domov (`home`)** je overlay menu dlaždic v brýlích: dlaždice ukotvené v prostoru podle IMU (yaw z gyra,
pitch z gravitace), výběr zaměřovačem uprostřed pohledu. Když je otevřený, drží vstup (`captures_input`):
krátký stisk tlačítka a gesta jdou jemu, myš hlavou se pozastaví, podržení tlačítka (menu) zůstává kořeni.
Moduly do něj přidávají dlaždice přes `home_tiles()`.

**Hotové moduly v tomto kroku:** `head_mouse` (engine Head Mouse 2.0 krmený daty z kořene),
`clock` (čas a baterie v rohu, ukázka vrstvy), `voice` (kořen mluví přes Windows hlas, vypnutý),
`keyboard` (vlastní klávesnice na obrazovce), `home` (Domov), `faces` (Tváře, první modul s vlastní
stránkou: kamera, detekce, fronta trénování, databáze osob, rozpoznání a osobní karta, logika v `koren/faces/`).

**Další moduly podle hotových řešení (návrh pořadí):**

1. `keyboard`: virtuální klávesnice z `hud_overlay.KeyboardModule` + `dasher` (psaní bez klávesnice).
2. `eye_camera`: zapnutí kamery Eye z `xreal-eye-rgb` jedním výběrem v menu.
3. `ariel`: Ariel jako modul (HTTP/WS server, tablet), IMU a tlačítko dostane od kořene místo vlastního
   spojení (`core/glasses.py` přepnout na zdroj dat z kořene).
4. `vision`: YOLO / OCR / AR overlay podle výkonu (CPU, iGPU, NPU přes OpenVINO, Jetson).
5. `assistant`: lokální LLM na Jetsonu (hlasové ovládání, popis okolí).
6. `sensors`: ESP32 hub, druhá IMU, radar.

## 7. Stabilita

- **Supervizor** (`koren.py`): aplikace běží jako podproces. Když spadne, nebo přestane psát heartbeat
  (zaseknutá hlavní smyčka), supervizor ji do pár sekund spustí znovu. Při opakovaných pádech zpomalí.
- **Jedna instance** (mutex), **ikona přežije restart Průzkumníka** (`TaskbarCreated`).
- Každé spojení s brýlemi se samo obnovuje; 2 s bez dat = zaseknuté spojení, znovu připojit.
- Hlavní smyčka chytá chyby po krocích, okno Windows chytá chyby v callbacku.
- Nastavení se ukládá atomicky (zápis do `.tmp` a přejmenování).
- `--dry`: nic nehibernuje a nehýbe myší, v systray jsou zkoušky víka a brýlí. Testy: `python -m unittest discover tests`.

## 8. Otevřené otázky na další krok

- Mluvit (modul `voice`) ve výchozím stavu? Teď je vypnutý.
- Doba čekání na brýle (teď 5 min) a jestli po odpočtu hibernovat, nebo jen uspat.
