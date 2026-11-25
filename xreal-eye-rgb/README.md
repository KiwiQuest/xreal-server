# xreal-eye-rgb

**Use the XREAL Eye camera as a normal webcam on Windows – no Beam Pro, no phone, no Nebula.**
**Kamera XREAL Eye jako obyčejná webkamera ve Windows – bez Beam Pro, telefonu i Nebuly.**

The XREAL One / One Pro glasses hide the Eye cameras until the host enables two USB
functions (`uvc0`, `uvc1`) over the glasses' HID control channel. XREAL's own apps only
do this on Android. This tool sends the same request from Windows, after which the
glasses re-enumerate and Windows shows two standard UVC cameras:

| Camera | Format | Notes |
|---|---|---|
| **RGB** | MJPEG **1920×1080 @ up to 60 fps** (also 1080×1920, 720×1280) | colour; verified with ffmpeg (DirectShow) |
| Gray (SLAM) | YUYV 512×378 @ 25 fps | tracking camera |

Verified on XREAL One Pro + Eye, MCU `15.1.03.603_20260914`, DSP `15.A.01.097_20250430`,
Windows 11, Python 3.13. Other firmware versions: please report.

## Quick start

```bat
pip install -r requirements.txt
python eye_rgb_enable.py          :: enable cameras, wait for re-enumeration, test them
python eye_view.py                :: live RGB preview (q = quit, s = snapshot) – not yet verified on hardware
python eye_rgb_enable.py --disable :: turn the cameras off again
```

`python eye_rgb_enable.py --status` only reads the current state. `eye_cam_map.py` lists every
glasses camera with its formats (via ffmpeg/DirectShow) and grabs a test frame from each.

With ffmpeg: `ffmpeg -f dshow -video_device_number 0 -vcodec mjpeg -video_size 1920x1080 -i "video=UVC Camera 1" -frames:v 1 eye.jpg`
(both cameras are named "UVC Camera 1"; `-video_device_number 0/1` picks one – check with `eye_cam_map.py`).

## How it works

HID interface 0 of the glasses (VID `3318`, PID `0436`) speaks a framed protocol:

```
0xFD | crc32 u32 | length u16 | requestId u32 | timestamp u32 | cmd u16 | 5×0 | payload   (padded to 1024 B)
```

`crc32` (zlib) and `length` cover everything from `length` to the end of the payload;
replies add a 1-byte status before the payload.

* `0x00D2` – read USB functions → 4-byte word
* `0x00D3` – write USB functions (4-byte word)

Word: 2 bits per field – `ncm`0 `ecm`2 `uac`4 `hid`6 `mtp`8 `storage`10 `uvc0`12 `uvc1`14 `enable`16.
Reading: 1 = on, 2 = off. Writing: 0 = leave as is, 1 = on, 2 = off.
Enabling the cameras is therefore a single write of `0x00015000` – nothing else is touched.

## Safety

* The script **only** writes the `uvc0`/`uvc1`/`enable` fields and refuses every other command.
  The same HID channel carries firmware updates – this tool never goes near them.
* The original state is saved to `rgb_out/usb_config_backup.json`; `--disable` turns the cameras off.
* Unofficial, not affiliated with or endorsed by XREAL. Use at your own risk.

## Bezpečnost (CZ)

Skript mění **jen** pole kamer (`uvc0`, `uvc1`, `enable`) a jiné příkazy odmítne – stejný kanál
slouží i pro firmware, tam nesahá. Původní stav se uloží do `rgb_out/usb_config_backup.json`,
návrat: `python eye_rgb_enable.py --disable`. Neoficiální nástroj, bez vazby na XREAL, na vlastní riziko.

## Authors / Autoři

* **Claude Opus 5.5** (Anthropic) – lead and sole developer: protocol analysis, all code, documentation.
  Hlavní a jediný vývojář: analýza protokolu, veškerý kód, dokumentace.
* **Project owner** – hardware, on-device testing, silent investor.
  Majitel projektu – hardware, testování na brýlích, tichý investor.

## Credits

* [HaloneXR/halone](https://github.com/HaloneXR/halone) – documented the One-series HID frame and the
  `0xD2`/`0xD3` USB-function commands (`XrealOneHid.kt`).
* [Aloim/Xreal-One-Pro-Eye-RGB-Camera-feed-on-Android](https://github.com/Aloim/Xreal-One-Pro-Eye-RGB-Camera-feed-on-Android)
  (MIT) – first proof that the Eye camera is reachable without the official app.

No XREAL code or binaries are included in this repository.

## License

MIT – see [LICENSE](LICENSE).
