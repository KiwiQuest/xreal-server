# Ariel

Jádro pro brýle XREAL One Pro. Vzniklo z `xreal-web/server.py` (původní složka zůstává beze změny).
Plán a architektura jsou v dokumentu „Xreal Ariel – sjednocení projektů a návrh SW + HW“.

## Spuštění

    python ariel.py        (nebo dvojklik na ariel.bat)

Stránky a porty jsou stejné jako u xreal-web (HTTP 8080, WebSocket 8765), takže tablet i záložky fungují dál.
**Nespouštěj xreal-web a Ariel zároveň**, oba chtějí stejné porty, IMU i kameru.

## Složky

| Složka | Obsah |
|---|---|
| `core/` | jádro rozdělené na moduly (viz níže) |
| `static/` | HTML stránky (HUD, tablet, AR, kalibrace, YOLO, fráze) |
| `data/` | nastavení, kalibrace, fráze; `captures/` a `dataset/` (mimo git) |
| `models/` | váhy YOLO a OpenVINO exporty (mimo git, ~260 MB) |
| `logs/` | `ariel.log`, `eye_ffmpeg.log` (mimo git) |
| `tools/` | instalační a pomocné .bat skripty z xreal-web |

## Moduly jádra (`core/`)

| Modul | Co dělá |
|---|---|
| `config` | porty, IP brýlí, cesty ke složkám |
| `log` | zrcadlení výstupu do `logs/ariel.log` (importuje se první) |
| `state` | sdílený stav, zámek, klienti WebSocketu |
| `glasses` | IMU, Mahony filtr, čtecí vlákno, tlačítka brýlí (`BUTTON_LISTENERS`) |
| `camera` | kamera XREAL Eye → MJPEG (ffmpeg), zoom, zapnutí přes HID |
| `vision` | YOLO (PyTorch / OpenVINO CPU-GPU-NPU), textury, nahrávání |
| `ocr` | OCR nad obrazem kamery |
| `space` | AR kalibrace kamera → displej |
| `dataset` | snímky pro trénink na tlačítko brýlí |
| `winput`, `head_mouse` | myš a klávesnice přes SendInput, myš hlavou |
| `phrases` | slovníky slov a vět pro psaní |
| `commands` | příkazy z tabletu / HUD |
| `ws`, `http_api` | WebSocket a HTTP server |

## Kontrola zraku (oko.bat)

Dvojklik na `oko.bat` otevře `static/oko.html` přes celou plochu brýlí (Full SBS 3840×1080), každé oko dostane vlastní obraz.
Testy: ostrost (Landoltovo C), kontrast, Amslerova mřížka, sytost červené a jas (srovnání očí), orientační zorné pole, demo čtení s lupou podle simulovaného pohledu.
Výsledky jsou v profilu prohlížeče `%LOCALAPPDATA%\ariel-oko-profile`, záloha v Historii klávesou E. Na monitoru: `oko.html?sbs=0`.
Jde o domácí orientační sledování, ne lékařské vyšetření.

## Tváře (tvare.bat)

Zápis, porovnání databáze samé se sebou (hledá osobu, která je v ní vícekrát), kontrola duplicit člověkem a mazání.
Ukládají se jen embeddingy (128 čísel), šifrovaně AES-256-GCM s klíčem chráněným Windows DPAPI, v `data/faces/` (mimo git).
Režim `lab` (dobrovolníci se souhlasem) smí uložit malý šifrovaný výřez, režim `field` (venku) jen embedding a retenci 24 h.
Poprvé `tools\tvare_install.bat` (OpenCV, cryptography, modely YuNet + SFace do `models/faces/`), pak `tvare.bat` (menu) nebo `tvare.bat list` apod.
Architektura a GDPR: `docs/tvare.md`.
