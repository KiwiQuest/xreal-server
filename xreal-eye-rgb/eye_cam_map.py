#!/usr/bin/env python3
"""XREAL Eye – mapa UVC kamer: zapne uvc0 i uvc1 a vypise, co ktera kamera umi.

Proc: "UVC Camera 1" (uvc1) se hlasi jako YUYV 512x378 @25 fps – to je rozmer
sedych SLAM kamer, ne RGB 2048x1512. Zkusime zapnout i uvc0 a podivat se,
jestli se objevi druha kamera s jinym formatem (HEVC / vetsi rozliseni).

Kroky: zapne uvc0 (uvc1 nechava) -> 10 s -> pro kazdou kameru od bryli vypise
formaty (ffmpeg dshow) a zkusi 1 snimek v nabizenem formatu.
Vysledek: rgb_out/cam_map_report.txt (+ rgb_out/map_*.png)
"""
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
OUT = os.path.join(HERE, "rgb_out")
os.makedirs(OUT, exist_ok=True)
LOG = []

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def log(s=""):
    print(s, flush=True)
    LOG.append(s)


def ffmpeg():
    import shutil
    for p in (r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\ffmpeg\ffmpeg.exe"):
        if os.path.exists(p):
            return p
    return shutil.which("ffmpeg")


def run(cmd, timeout=25):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        err = e.stderr or b""
        return err.decode("utf-8", "replace") if isinstance(err, bytes) else err


def main():
    import eye_rgb_enable as en
    import rgb_probe as rp
    log("=" * 60)
    log("XREAL Eye – mapa UVC kamer   " + time.ctime())
    log("=" * 60)
    g = en.Glasses()
    before = g.usb_word()
    log("USB funkce pred: " + en.decode(before))
    if "--skip-enable" not in sys.argv:
        g.set_fields(uvc0=en.ON, uvc1=en.ON)
        g.close()
        log("Zapnuto uvc0 + uvc1, cekam 10 s …")
        time.sleep(10)
        g = en.Glasses()
    log("USB funkce ted:  " + en.decode(g.usb_word()))
    g.close()

    log("\n[ZARIZENI XREAL – kamery]")
    for d in rp.pnp_devices():
        if d["class"] in ("Camera", "Image") and "VID_3318" in d["id"].upper():
            log(f"  {d['status']:<6} {d['name']}  ({d['id']})")

    names = rp.dshow_names() or []
    cams = [n for n in names if n != "Integrated Camera" and not n.startswith("Meta Quest")]
    log("\n[KAMERY DirectShow od bryli] " + (", ".join(cams) or "(zadne)"))
    ff = ffmpeg()
    if not ff:
        log("ffmpeg nenalezen")
        return finish()
    seen = {}
    for name in cams:
        num = seen.get(name, 0)
        seen[name] = num + 1
        tag = re.sub(r"\W+", "_", name) + f"_{num}"
        log(f"\n--- {name} (#{num}) ---")
        out = run([ff, "-hide_banner", "-list_options", "true", "-f", "dshow",
                   "-video_device_number", str(num), "-i", f"video={name}"])
        opts = [l.split("]", 1)[-1].strip() for l in out.splitlines()
                if "pixel_format=" in l or "vcodec=" in l]
        for o in opts:
            log("  " + o)
        if not opts:
            log("  (zadne formaty)\n" + "\n".join("    " + l for l in out.splitlines()[-6:]))
        for o in opts[:3]:
            m_s = re.search(r"max s=(\d+x\d+)", o)
            m_pf = re.search(r"pixel_format=(\S+)", o)
            m_vc = re.search(r"vcodec=(\S+)", o)
            args = [ff, "-hide_banner", "-f", "dshow", "-rtbufsize", "256M",
                    "-video_device_number", str(num)]
            if m_s:
                args += ["-video_size", m_s.group(1)]
            if m_pf:
                args += ["-pixel_format", m_pf.group(1)]
            if m_vc:
                args += ["-vcodec", m_vc.group(1)]
            png = os.path.join(OUT, f"map_{tag}_{(m_s.group(1) if m_s else 'x')}.png")
            args += ["-i", f"video={name}", "-frames:v", "3", "-update", "1", "-y", png]
            res = run(args, timeout=20)
            ok = os.path.exists(png) and os.path.getsize(png) > 1000
            log(f"  snimek {o[:60]} -> {'OK ' + png if ok else 'NE'}")
            if not ok:
                log("\n".join("    " + l for l in res.splitlines()[-4:]))
    return finish()


def finish():
    with open(os.path.join(OUT, "cam_map_report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(LOG) + "\n")
    log("\nReport: rgb_out\\cam_map_report.txt")


if __name__ == "__main__":
    main()
