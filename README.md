# XREAL Server pro Windows

**Ovládání brýlí XREAL One / One Pro (+ kamera XREAL Eye) přímo z Windows, bez Nebuly, bez telefonu a bez Beam Pro.**
English version: [README.en.md](README.en.md)

> Neoficiální projekt. Nemá žádnou vazbu na společnost XREAL, není jí podporován ani schválen. Používáš na vlastní riziko.

## Proč tento projekt vznikl

Brýle XREAL jsme si koupili za desítky tisíc korun. S nástupem nové „XR“ generace ale XREAL nechal stávající uživatele
na holičkách: aplikace Nebula pro Windows nefunguje tak, jak byla slíbena, a funkce, za které jsme zaplatili
(kamera Eye, prostorové ovládání, práce bez telefonu), zůstaly zamčené v ekosystému Androidu a nových zařízení.

Tento projekt je nesouhlas vyjádřený kódem. Bere hardware, který už lidé vlastní, a dělá z něj plnohodnotný
nástroj pro Windows. Prvním konkrétním výsledkem je **zprovoznění kamery XREAL Eye jako obyčejné webkamery ve Windows**
(složka [`xreal-eye-rgb/`](xreal-eye-rgb/)), což oficiálně možné není.

## Pro koho a na čem

Projekt je postavený pro **Windows** a počítá s provozem na všech zařízeních s Windows, podle toho, jaký hardware uživatel má:

- notebook s integrovanou grafikou (základní režim, nároky drženy nízko),
- vývojové desky s Windows,
- NVIDIA Jetson a moduly s lokálním LLM (offline asistent, rozpoznávání obrazu),
- pevná PC s plnou GPU.

Funkce se škálují podle dostupného výkonu (CPU / GPU / NPU přes OpenVINO, volitelné YOLO a OCR).

## Cíl: kořenová aplikace ovládaná jen hlavou

Hlavním cílem je **kořenová aplikace v systray**, kterou jde ovládat **pouze hlavou**, bez klávesnice a myši:

- **Režim „notebook v tašce“:** po zavření víka (ve Windows nastaveno „nedělat nic“) aplikace zkontroluje, zda jsou připojené brýle.
  Pokud ano, přepne se do ovládání jen brýlemi. Pokud ne, spustí odpočet a čeká na brýle; když se nepřipojí, notebook hibernuje.
- **Myš hlavou** (už dnes funguje dobře), klik prodlevou (dwell), tlačítko na brýlích jako myš.
- **Nenásilný help a navigační overlay:** průhledný, klidný, vždy říká, jaké má uživatel právě možnosti, a pozná, když uživatel
  nemá jak ovládat nebo s ovládáním zápasí.
- **Modulární jádro:** moduly se načítají podle výběru, jsou průhledné a může jich běžet víc nad sebou.
- **Stabilita na prvním místě:** uživatel je na cestě a k notebooku se nedostane, aplikace se nesmí zaseknout v místě, odkud se bez klávesnice nedá dostat.

Do budoucna: podpora vlastních HW senzorů a modulů (ESP32, IMU, radar) připojených k serveru.

## Obsah repozitáře

| Složka | Co obsahuje | Stav |
|---|---|---|
| [`xreal-root/`](xreal-root/) | **XREAL Kořen**: kořenová aplikace v systray. Víko + brýle → režim „notebook v tašce“, odpočet a hibernace, menu a nápověda ovládané jen hlavou a tlačítkem brýlí, modulový systém, supervizor. Katalog a návrh: [`ARCHITEKTURA.md`](xreal-root/ARCHITEKTURA.md). | první verze |
| [`xreal-eye-rgb/`](xreal-eye-rgb/) | **Kamera XREAL Eye jako webkamera ve Windows** (RGB 1920×1080 až 60 fps + šedá SLAM kamera). Zapnutí USB funkcí přes HID kanál brýlí, MIT licence. | funguje, ověřeno |
| [`ariel/`](ariel/) | **Ariel Core**: současné jádro serveru rozdělené na moduly (IMU, kamera, YOLO, OCR, AR kalibrace, myš hlavou, HTTP + WebSocket, tablet a HUD stránky, kontrola zraku, šifrovaný modul tváří). | hlavní větev vývoje |
| [`xreal-web/`](xreal-web/) | Původní monolitický server (`server.py`), ze kterého vzniklo Ariel. | archiv |
| [`Xreal mouse/`](Xreal%20mouse/) | XREAL Head Mouse 2.0: myš hlavou, circle click, tlačítko brýlí, Dasher psaní bez klávesnice, testy. | funguje |
| [`rgb-test/`](rgb-test/) | Vývojové sondy a log, na kterých se kamera Eye rozchodila (`rgb_out_enable.txt`). | podklady |

### Co v repozitáři záměrně není

- Unity projekty (hry a vizualizace) a jejich buildy.
- Soukromé materiály k hardwaru.
- Váhy modelů YOLO / OpenVINO (stáhnou se instalačními skripty), snímky z kamery, datasety, logy.
- Databáze tváří a klíče (`data/faces/`), lokální nastavení zařízení.
- Textury třetích stran s autorskými právy (stránky v `static/` počítají se složkou `static/textures/`, kterou si doplníš sám).

## Rychlý start

```bat
:: kamera Eye jako webkamera
cd xreal-eye-rgb
pip install -r requirements.txt
python eye_rgb_enable.py

:: jádro Ariel (HTTP 8080, WebSocket 8765)
cd ariel
python ariel.py
```

Podrobnosti jsou v README každé složky. Ariel a xreal-web nespouštěj současně, používají stejné porty, IMU i kameru.

## Bezpečnost

Nástroje pro kameru zapisují do brýlí **jen** pole USB funkcí kamer (`uvc0`, `uvc1`, `enable`) a jiné příkazy odmítnou.
Stejný HID kanál slouží i pro aktualizace firmwaru, tam kód nesahá. Původní stav se ukládá a jde vrátit (`--disable`).

## Autoři

- **Claude** (Anthropic): vývoj, analýza protokolu, kód a dokumentace.
- **Majitel projektu** ([@KiwiQuest](https://github.com/KiwiQuest)): hardware, testování na brýlích, směr projektu.

Poděkování projektům [HaloneXR/halone](https://github.com/HaloneXR/halone) a
[Aloim/Xreal-One-Pro-Eye-RGB-Camera-feed-on-Android](https://github.com/Aloim/Xreal-One-Pro-Eye-RGB-Camera-feed-on-Android),
které jako první popsaly HID rámec a cestu ke kameře Eye. Repozitář neobsahuje žádný kód ani binárky od XREAL.

## Licence

Složka `xreal-eye-rgb/` je pod licencí MIT (viz její `LICENSE`). Pro zbytek repozitáře licence zatím nebyla zvolena.
