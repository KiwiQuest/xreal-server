"""Test skutecneho rozliseni kamery XREAL Eye: z kazdeho rezimu UVC nahraje 3 s (bez prekodovani)
do captures/_res_test/. Pred spustenim ZAVRI server (kamera smi mit jen jednoho uzivatele).
Bryle poloz na stul (at se nehybou) proti necemu s drobnym detailem - vytisteny text, obrazovka."""
import re
import subprocess
import sys
import time
from pathlib import Path

FF = r"C:\ffmpeg\ffmpeg.exe" if Path(r"C:\ffmpeg\ffmpeg.exe").exists() else "ffmpeg"
OUT = Path(__file__).resolve().parent / "captures" / "_res_test"
MODES = [(1920, 1080, 30), (1920, 1080, 60), (1080, 1920, 30), (720, 1280, 30)]
NAME = "UVC Camera 1"


def run(args, timeout=30):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return (r.stdout or "") + (r.stderr or ""), r.returncode


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    lst, _ = run([FF, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
    n = len(re.findall(r'"UVC Camera 1"\s*\(video\)', lst))
    rgb = None
    for num in range(n):
        opts, _ = run([FF, "-hide_banner", "-list_options", "true", "-f", "dshow",
                       "-video_device_number", str(num), "-i", f"video={NAME}"])
        if "vcodec=mjpeg" in opts:
            rgb = num
            (OUT / "modes.txt").write_text(opts, encoding="utf-8")
            break
    if rgb is None:
        print("RGB kamera Eye nenalezena (bezi server? zavri ho; nebo spust server jednou, at zapne kamery)")
        return 1
    print(f"RGB kamera: \"{NAME}\" #{rgb}")
    for w, h, fps in MODES:
        out = OUT / f"mode_{w}x{h}_{fps}.mjpeg"
        print(f"  nahravam {w}x{h} @{fps} ...", end=" ", flush=True)
        log, rc = run([FF, "-hide_banner", "-loglevel", "error", "-f", "dshow", "-rtbufsize", "256M",
                       "-video_device_number", str(rgb), "-video_size", f"{w}x{h}", "-framerate", str(fps),
                       "-vcodec", "mjpeg", "-i", f"video={NAME}", "-t", "3", "-c:v", "copy", "-f", "mjpeg",
                       "-y", str(out)], timeout=40)
        ok = out.exists() and out.stat().st_size > 10000
        print(f"OK {out.stat().st_size // 1024} kB" if ok else f"nejde ({log.strip()[-120:]})")
        time.sleep(1.5)
    print(f"\nHotovo -> {OUT}\nNapis Claudovi, analyzu udela sam.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
