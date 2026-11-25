# XREAL Server for Windows

**Drive XREAL One / One Pro glasses (and the XREAL Eye camera) straight from Windows: no Nebula, no phone, no Beam Pro.**
Česká verze: [README.md](README.md)

> Unofficial project. Not affiliated with, supported or endorsed by XREAL. Use at your own risk.

## Why this exists

We paid a lot of money for XREAL glasses. With the arrival of the new "XR" generation, XREAL left existing owners stranded:
the Nebula app for Windows does not work as promised, and features we paid for (the Eye camera, spatial control,
working without a phone) stayed locked inside Android and newer devices.

This project is that disagreement, written as code. It takes hardware people already own and turns it into a real Windows tool.
The first concrete result is **the XREAL Eye camera working as a plain webcam on Windows**
([`xreal-eye-rgb/`](xreal-eye-rgb/)), which is not officially possible.

## Who it is for, and on what hardware

Built for **Windows**, meant to run on any Windows device the user has:

- laptops with integrated graphics (baseline mode, kept lightweight),
- Windows development boards,
- NVIDIA Jetson and modules with a local LLM (offline assistant, vision),
- desktop PCs with a full GPU.

Features scale with the available compute (CPU / GPU / NPU via OpenVINO, optional YOLO and OCR).

## Goal: a head-only root application

The main goal is a **root application in the system tray**, controlled **with the head only**, no keyboard or mouse:

- **"Laptop in the bag" mode:** when the lid closes (Windows set to "do nothing"), the app checks for the glasses.
  If they are connected it switches to glasses-only control; if not, it counts down waiting for them and hibernates the laptop if they never show up.
- **Head mouse** (already works well), dwell click, the glasses' button as a mouse button.
- **Calm help and navigation overlay:** transparent and unobtrusive, always telling the user what they can do right now,
  and noticing when the user has no way to control the machine or is struggling.
- **Modular core:** modules are loaded on demand, are transparent, and several can be stacked at once.
- **Stability first:** the user is on the move and cannot reach the laptop, so the app must never get stuck where only a keyboard could help.

Later: support for custom hardware sensors and modules (ESP32, IMU, radar) connected to the server.

## Repository layout

| Folder | Contents | Status |
|---|---|---|
| [`xreal-eye-rgb/`](xreal-eye-rgb/) | **XREAL Eye camera as a Windows webcam** (RGB 1920×1080 up to 60 fps + grey SLAM camera) by enabling USB functions over the glasses' HID channel. MIT. | working, verified |
| [`ariel/`](ariel/) | **Ariel Core**: the current modular server (IMU, camera, YOLO, OCR, AR calibration, head mouse, HTTP + WebSocket, tablet and HUD pages, vision check, encrypted faces module). | main line |
| [`xreal-web/`](xreal-web/) | The original monolithic server (`server.py`) Ariel grew out of. | archive |
| [`Xreal mouse/`](Xreal%20mouse/) | XREAL Head Mouse 2.0: head mouse, circle click, glasses button, Dasher typing, tests. | working |
| [`rgb-test/`](rgb-test/) | Development probes and the log of how the Eye camera was brought up. | reference |
| [`xreal-sim/`](xreal-sim/) | Flight HUD simulator (xreal-web clone). | experiment |
| [`Adrduino/`](Adrduino/) | Arduino motor control over serial, a first step towards hardware modules. | experiment |

Not included on purpose: Unity projects and builds, private hardware material, model weights (installed by scripts),
camera captures, datasets, logs, face database and keys, local device settings, and third-party textures
(pages in `static/` expect a `static/textures/` folder you provide yourself).

## Quick start

```bat
cd xreal-eye-rgb
pip install -r requirements.txt
python eye_rgb_enable.py

cd ariel
python ariel.py          :: HTTP 8080, WebSocket 8765
```

Do not run Ariel and xreal-web at the same time; they share ports, the IMU and the camera.

## Safety

The camera tools write **only** the camera USB-function fields (`uvc0`, `uvc1`, `enable`) and refuse any other command.
The same HID channel carries firmware updates; this code never touches them. The original state is saved and can be restored (`--disable`).

## Authors

- **Claude** (Anthropic): development, protocol analysis, code and docs.
- **Project owner** ([@KiwiQuest](https://github.com/KiwiQuest)): hardware, on-device testing, project direction.

Thanks to [HaloneXR/halone](https://github.com/HaloneXR/halone) and
[Aloim/Xreal-One-Pro-Eye-RGB-Camera-feed-on-Android](https://github.com/Aloim/Xreal-One-Pro-Eye-RGB-Camera-feed-on-Android).
No XREAL code or binaries are included.

## License

`xreal-eye-rgb/` is MIT (see its `LICENSE`). No license has been chosen yet for the rest of the repository.
