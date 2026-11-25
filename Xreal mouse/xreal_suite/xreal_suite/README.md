# XREAL Head Mouse 2.0

Ovládání kurzoru hlavou pro XREAL One / One Pro, circle click, tlačítko na brýlích jako myš,
kamera Eye + YOLO a psaní bez klávesnice (Dasher). Základní program nepotřebuje žádné knihovny
(jen Python 3.10+ s tkinter).

## Spuštění
    python run.py                 (nebo dvojklik na start.bat)
    python run.py --dry           nic nehýbe ani neklikne, jen okno + diagnostika
    python run.py --replay dump.bin   přehrání záznamu IMU
Konfigurace se ukládá do `xreal_config.json`.

## Režimy kurzoru (rozbalovací nabídka v okně)
| Režim | Co dělá |
|---|---|
| Relativní myš | pohyb hlavy = relativní pohyb myši (jako dřív) |
| **Střed pohledu** | kurzor je tam, kam míří střed pohledu; obrazovka ukotvená v prostoru, kurzor se pohybuje po ploše spolu s pohledem |
| Rozhlížení | kurzor stojí na středu, pohyb se posílá jako relativní pohyb myši (hry/3D) |

Starý režim "držet na středu" byl právě *Rozhlížení* – kurzor v něm stojí na místě schválně.
Pro "vycentrované, ale pohybuje se s pohledem" je režim **Střed pohledu**:
1. Nastav obrazovku (Auto = XREAL), podívej se na střed plochy a stiskni **F5** (Vycentrovat).
2. Posuvníkem **Šířka obrazovky v pohledu** (°) dolaď, aby kurzor zůstával ve středu pohledu
   (kurzor ujíždí rychleji než pohled → zvětši, pomaleji → zmenši).
3. Yaw se integruje z gyra a pomalu driftuje → občas F5. Nahoře v okně je živá diagnostika
   (yaw/pitch odchylka a cíl kurzoru), takže hned vidíš, jestli se hodnoty hýbou.
Klik kolečkem myši = uvolnit kurzor (přejde na relativní), další klik = znovu držet.

## Tlačítko na brýlích (Quick button, port 52999)
stisk = levý klik | držet = tažení | dvojstisk = pravý klik | dvojstisk a držet = circle click ZAP/VYP.
V OSD brýlí nastav Quick button na "žádná funkce", ať vedle toho nepřepíná 32:9.

## Kamera Eye + rychlé hrany a obrysy objektů
Tlačítko **Kamera Eye + YOLO…**. Podle tvých snímků teče na TCP 52997 jeden **8bitový šedotónový
obraz 512×378 na rámec** (rámec 193 862 B = 6 B hlavička + 320 B metadata + 193 536 B pixelů, ~15 fps).
Formát rámců není oficiálně zdokumentovaný, viewer ho odvozuje z dat; v rozbalovátku
*Dekódování* jde varianta přepnout ručně (první verze chybně četla 4 bity na pixel – proto šum).

Obraz je tmavý, proto je před zpracováním **Kontrast** (roztažení histogramu / CLAHE).
**Zobrazení**:
| Režim | K čemu |
|---|---|
| Původní | jen zesvětlený obraz |
| Hrany – Sobel / morfologický gradient | nejrychlejší hrany (≈ 0,3–0,5 ms) |
| Hrany – Canny | čisté tenké čáry (≈ 0,7 ms) |
| Obrysy – Otsu + kontury | jasné objekty na tmavém pozadí (≈ 0,3 ms) |
| Obrysy a obdélníky – Canny + kontury | hranice a rámečky objektů (≈ 1 ms) |
Prahy se odvozují z úrovně šumu, takže fungují i na tmavém zašuměném snímku. **½ rozlišení** zhruba
zdvojnásobí rychlost. Nad obrazem je rozpis ms za jednotlivé fáze. Tlačítko **Benchmark** změří
všechny metody (a YOLO, pokud je nainstalované) na posledním snímku z tvých brýlí – čísla výše jsou z
testu na syntetických datech, tvůj počítač bude mít jiná.
Rychlost: klasické hrany/kontury jsou řádově rychlejší (≈ 1 ms) než YOLO na CPU (desítky ms); YOLO ale
řekne, *co* objekt je. **YOLO obrysy (seg)** používá `yolov8n-seg.pt` (polygony objektů).

### Barevná (RGB) kamera Eye
Stream na portu 52997 jsou jen šedé SLAM kamery. Barevná kamera je zvlášť. Podle vývojářské FAQ XREAL
se na Windows přes USB-C objeví mezi kamerami počítače (jde přepnout v appce *Kamera*; v Nebule musí být
vypnutý **Power Saving Mode**). Postup: tlačítko **Najít barevné kamery** → ze seznamu vyber tu od brýlí
(nad polem se vypíšou i názvy kamer podle Windows) → obraz je barevný, hrany/obrysy/YOLO fungují nad ním.
FAQ uvádí 1280×720 @ 30 fps; komunitní Android projekt píše o 2048×1512 HEVC přes UVC po HID příkazu –
který z toho platí na tvém Windows, ukáže seznam kamer.

## AR overlay: objekty z kamery přímo v brýlích
V Eye okně zaškrtni **AR overlay v brýlích**. Přes celou obrazovku brýlí (obrazovka 2) se vykreslí
průhledné okno (nechytá kliky ani fokus) s obrysy, YOLO boxy a štítky tak, aby seděly na reálné objekty.
Jak to funguje (a proč se dá počítat správně):
1. Pixel kamery se převede na úhel od osy kamery (podle **zorného úhlu kamery** a **typu objektivu**).
2. K němu se přičte natočení hlavy z IMU **v okamžiku pořízení snímku** (historie 4 s), takže zpoždění
   kamery nevadí – objekt dostane „světový" úhel.
3. **Kotvená obrazovka** (Spatial Anchor, tak běží i stream Eye): poloha na obrazovce = střed + px/° × úhel.
   Obrazovka se při otáčení hlavy sama posouvá (to dělají brýle), takže se overlay s objektem nehýbe.
   **Pevná obrazovka** (head-locked): odečte se aktuální natočení hlavy.
Použité úhly: *Šířka obrazovky v pohledu* (posuvník v hlavním okně, stejná jako pro kurzor) u kotvené,
*ar_disp_fov* (≈ 50°) u pevné.
**Než začneš kalibrovat, zkontroluj ukotvení.** Podívej se v brýlích na plochu Windows a otoč hlavou:
- plocha zůstává v místnosti → brýle kotví → v okně Eye nech zaškrtnuté **kotvená obrazovka**,
- plocha jede s hlavou → brýle nekotví → zapni v brýlích ukotvení (např. Body Anchor), nebo odškrtni
  *kotvená obrazovka*, ať natočení dopočítává aplikace.
Když to nesedí, overlay jede s hlavou: objekt dáš doprostřed, otočíš hlavou a overlay zůstane uprostřed
pohledu, zatímco skutečný objekt odjede na druhou stranu.

**Širokoúhlý objektiv (fisheye).** Kamery Eye pro SLAM snímají „vypouklou čočkou“, aby zabraly co nejvíc.
Běžný výpočet (jako u normálního objektivu) pak sedí jen uprostřed a k okrajům se plete čím dál víc –
u kamery se 100° záběrem zhruba o 1° ve 20°, 2,5° ve 30° a 5° ve 40° od středu. Proto má každá kamera
nastavení objektivu: SLAM výchozí **fisheye**, barevná **běžný**. Přepíná se zaškrtávátkem *fisheye* v okně
Eye nebo klávesou **L** v kalibraci. Výpočet fisheye je ekvidistantní model (úhel od osy roste rovnoměrně
se vzdáleností od středu snímku), který se kalibruje stejně jako dřív jedním zorným úhlem.
*Po aktualizaci na tuto verzi je potřeba SLAM kameru překalibrovat* (stará kalibrace byla doladěná pro
běžný objektiv). Tip: posun (šipky) dolaď na předmětu **uprostřed** pohledu, zorný úhel (PgUp/PgDn) na
předmětu **u okraje** – když sedí obojí, sedí i všechno mezi tím.

**Objekty mimo obrazovku.** Kamera vidí víc, než displej brýlí umí ukázat (One 50°, One Pro 57°
diagonálně, svisle jen kolem 26–30°). Pojmenované objekty z YOLO, které jsou mimo zobrazitelnou plochu,
se ukážou jako **šipka na okraji** se jménem – ukazuje směr, kam se otočit. U kotvené obrazovky se kreslí
jen do virtuální plochy, takže objekt mimo ni zůstane šipkou, i když se na něj podíváš. Vypíná se
zaškrtávátkem *šipky mimo obraz*.

**Kalibrace** (F2 nebo zaškrtávátko): šipky posouvají overlay (Shift = jemně), PgUp/PgDn mění zorný úhel
kamery, L přepíná objektiv běžný/fisheye, X/Y zrcadlí obraz, Home resetuje. Kalibruj na něčem s jasnými okraji (kniha, monitor) v
pracovní vzdálenosti – kamera je jinde než oči, takže u blízkých věcí vzniká paralaxa.
Důležité: u kotvené obrazovky musí kurzor v režimu *Střed pohledu* sedět na pohled (F5 = vycentrovat),
protože ze stejného sledování natočení se počítá poloha overlaye; yaw časem driftuje.
Neověřeno s reálnými brýlemi: zorný úhel SLAM/RGB kamery neznám (výchozí 100° / 70° je odhad),
jestli SLAM kamerám přesně sedí ekvidistantní fisheye model (pokud ne, přijde na řadu kalibrace šachovnicí
přes OpenCV), orientace SLAM obrazu (zrcadlení) taky ne – proto kalibrace. Testy ověřují jen matematiku.

## YOLO
`setup_yolo.bat` vytvoří `.venv`, nainstaluje ultralytics + OpenCV a ověří prostředí
(`python -m xreal.yolo`). Váhy `yolov8n.pt` se při prvním použití stáhnou (internet).
V okně Eye zaškrtni **YOLO**.

## Psaní hlavou (Dasher)
Tlačítko **Psaní (Dasher)…**. Žádné klikání: písmena jsou pruhy, jejichž výška odpovídá
pravděpodobnosti pokračování. Kurzor vpravo od čáry = přibližuje (píše), vlevo = maže,
mimo okno = pauza. Funguje v jakémkoli režimu kurzoru, nejlépe ve **Střed pohledu**.
Model je malý; vlastní text dej do `data/corpus_cs.txt` (čím víc, tím lépe se píše).

## Testy
    python -m unittest discover -s tests
Pokrývají fúzi, všechny režimy kurzoru, dwell, tlačítko, Eye dekodér (syntetická data), Dasher
a smoke test GUI. Skutečné okno, SendInput a brýle testovat nelze mimo Windows s brýlemi.
