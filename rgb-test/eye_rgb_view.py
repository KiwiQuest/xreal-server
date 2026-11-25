#!/usr/bin/env python3
"""XREAL Eye RGB – zjisti, jestli z "UVC Camera 1" tecou data, a dekoduj je ffmpegem.

Predpoklad: RGB uz je zapnute (eye_rgb_enable.py) a Windows vidi "UVC Camera 1".
Kamera posila H.265/HEVC; aplikace Kamera ukaze cerno, kdyz chybi HEVC dekoder.

Kroky:
  1) ffmpeg (z balicku imageio-ffmpeg) vypise formaty kamery (DirectShow)
  2) zkusi 6 s zaznamu bez prekodovani (-c:v copy) -> rgb_out/eye_raw.mkv
  3) dekoduje zaznam na JPG snimky -> rgb_out/eye_XX.jpg  (ffmpeg ma vlastni HEVC dekoder)
  4) zaloha: OpenCV MSMF v "raw" rezimu -> rgb_out/eye_raw_msmf.bin (+ hledani HEVC NAL)
Vysledek: rgb_out/view_report.txt a prvni snimek se otevre.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "rgb_out")
CAM = "UVC Camera 1"
os.makedirs(OUT, exist_ok=True)
LOG = []

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def log(s=""):
    print(s, flush=True)
    LOG.append(s)


def ffmpeg_exe():
    import shutil
    for p in (r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\ffmpeg.exe"):
        if os.path.exists(p):
            log(f"ffmpeg: {p}")
            return p
    p = shutil.which("ffmpeg")
    if p:
        log(f"ffmpeg: {p}")
        return p
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as e:
        log(f"imageio-ffmpeg nedostupne: {e}")
        return None


def run(cmd, timeout=40):
    log("$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"")
        out = out.decode("utf-8", "replace") if isinstance(out, bytes) else out
        err = (e.stderr or b"")
        err = err.decode("utf-8", "replace") if isinstance(err, bytes) else err
        return -999, out + err


def tail(text, n=25):
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join("    " + l for l in lines[-n:])


def nal_scan(data: bytes):
    """Spocita HEVC NAL typy (start code 00 00 01)."""
    counts = {}
    i = 0
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= len(data):
            break
        t = (data[i + 3] >> 1) & 0x3F
        counts[t] = counts.get(t, 0) + 1
        i += 3
    return counts


def main():
    log("=" * 60)
    log("XREAL Eye RGB – test dat a dekodovani   " + time.ctime())
    log("=" * 60)
    ff = ffmpeg_exe()
    jpgs = []
    if ff:
        log("\n[1] Formaty kamery (DirectShow):")
        _, out = run([ff, "-hide_banner", "-list_options", "true", "-f", "dshow",
                      "-i", f"video={CAM}"], timeout=20)
        log(tail(out, 40))

        raw = os.path.join(OUT, "eye_raw.mkv")
        if os.path.exists(raw):
            os.remove(raw)
        for attempt, extra in (("vychozi", []), ("vcodec hevc", ["-vcodec", "hevc"])):
            log(f"\n[2] Zaznam 6 s bez prekodovani ({attempt}):")
            rc, out = run([ff, "-hide_banner", "-f", "dshow", "-rtbufsize", "512M", *extra,
                           "-i", f"video={CAM}", "-t", "6", "-c:v", "copy", "-y", raw], timeout=40)
            log(tail(out, 15))
            if os.path.exists(raw) and os.path.getsize(raw) > 10000:
                log(f"    -> zaznam {os.path.getsize(raw)} B")
                break
        if os.path.exists(raw) and os.path.getsize(raw) > 10000:
            log("\n[3] Dekodovani na snimky:")
            for f in os.listdir(OUT):
                if f.startswith("eye_") and f.endswith(".jpg"):
                    os.remove(os.path.join(OUT, f))
            rc, out = run([ff, "-hide_banner", "-i", raw, "-vf", "fps=1", "-y",
                           os.path.join(OUT, "eye_%02d.jpg")], timeout=60)
            log(tail(out, 12))
            jpgs = sorted(f for f in os.listdir(OUT) if f.startswith("eye_") and f.endswith(".jpg"))
            log(f"    -> snimku: {len(jpgs)}")
        else:
            log("    -> DirectShow zaznam nevznikl")

    if not jpgs:
        log("\n[4] Zaloha: OpenCV MSMF raw rezim")
        try:
            import cv2
            for idx in range(0, 5):
                cap = cv2.VideoCapture(idx, cv2.CAP_MSMF)
                if not cap.isOpened():
                    cap.release()
                    continue
                cap.set(cv2.CAP_PROP_CONVERT_RGB, 0)
                try:
                    cap.set(cv2.CAP_PROP_FORMAT, -1)
                except Exception:
                    pass
                ok, f = cap.read()
                if ok and f is not None and f.ndim == 3 and f.shape[1] > 100:
                    log(f"    MSMF {idx}: bezny obraz {f.shape} (to neni Eye)")
                    cap.release()
                    continue
                buf = bytearray()
                t0 = time.time()
                while time.time() - t0 < 6:
                    ok, f = cap.read()
                    if ok and f is not None:
                        buf += f.tobytes()
                cap.release()
                log(f"    MSMF {idx}: {len(buf)} B dat")
                if buf:
                    p = os.path.join(OUT, f"eye_raw_msmf{idx}.bin")
                    open(p, "wb").write(buf)
                    nals = nal_scan(bytes(buf))
                    log(f"    HEVC NAL typy (32=VPS 33=SPS 34=PPS 19/20=IDR 1=P): {nals}")
                    if ff and nals:
                        rc, out = run([ff, "-hide_banner", "-f", "hevc", "-i", p, "-vf", "fps=1",
                                       "-y", os.path.join(OUT, "eye_%02d.jpg")], timeout=60)
                        log(tail(out, 8))
                        jpgs = sorted(f for f in os.listdir(OUT)
                                      if f.startswith("eye_") and f.endswith(".jpg"))
                        if jpgs:
                            break
        except Exception as e:
            log(f"    chyba: {e}")

    log("\n" + "=" * 60)
    if jpgs:
        log(f"VERDIKT: ✅ RGB obraz z Eye dekodovan ve Windows – {len(jpgs)} snimku v rgb_out/")
        try:
            os.startfile(os.path.join(OUT, jpgs[0]))
        except Exception:
            pass
    else:
        log("VERDIKT: ❌ Zadny snimek – viz vystup vyse (formaty / chyby ffmpeg).")
    log("=" * 60)
    with open(os.path.join(OUT, "view_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(LOG) + "\n")


if __name__ == "__main__":
    main()
