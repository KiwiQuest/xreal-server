#!/usr/bin/env python3
"""XREAL Eye – test barevne (RGB) kamery ve Windows.

Otazka: objevi se RGB kamera Eye ve Windows jako normalni kamera (UVC)?

Postup (vychozi, interaktivni):
  1) odpojis bryle  -> skript si zapamatuje zarizeni a kamery
  2) pripojis bryle s Eye -> skript zjisti, co pribylo
  3) otestuje vsechny kamery (DirectShow i Media Foundation):
     rozliseni, fps, barva vs. sede, ulozi snimek do rgb_out/
  4) vypise VERDIKT a ulozi rgb_out/report.txt + report.json

Pouziti:
  python rgb_probe.py              interaktivne (doporuceno)
  python rgb_probe.py --quick      bez odpojovani, jen otestuje vsechny kamery
  python rgb_probe.py --show 1     zivy nahled kamery s indexem 1 (q = konec)

Pred testem: v Nebule vypni Power Saving Mode, Eye musi byt nasazene na brylich.
Potrebuje: opencv-python, volitelne pygrabber (nazvy kamer). start.bat je doinstaluje.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "rgb_out")
RESOLUTIONS = [(2048, 1512), (1920, 1080), (1600, 1200), (1280, 720), (640, 480)]
MAX_INDEX = 8
XREAL_HINTS = ("XREAL", "NREAL", "EYE", "VID_3318")

try:
    import cv2
except Exception:
    cv2 = None

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


# ─────────────────────────── seznam zarizeni (Windows) ──────────────────────
def _ps(cmd: str) -> str:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=60)
        return r.stdout
    except Exception as e:
        return f"__ERR__ {e}"


def pnp_devices():
    """Kamery, obrazova zarizeni a vse, co vypada jako XREAL."""
    cmd = ("Get-PnpDevice -PresentOnly | Where-Object { "
           "$_.Class -in 'Camera','Image' -or "
           "$_.FriendlyName -match 'XREAL|Nreal|Eye' -or $_.InstanceId -match 'VID_3318' } | "
           "Select-Object Class,FriendlyName,Status,InstanceId | ConvertTo-Json -Compress")
    raw = _ps(cmd).strip()
    if not raw or raw.startswith("__ERR__"):
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    return [{"class": d.get("Class") or "", "name": d.get("FriendlyName") or "",
             "status": d.get("Status") or "", "id": d.get("InstanceId") or ""} for d in data]


def dshow_names():
    """Nazvy kamer v poradi DirectShow (= index pro cv2.CAP_DSHOW)."""
    try:
        from pygrabber.dshow_graph import FilterGraph
        return list(FilterGraph().get_input_devices())
    except Exception:
        pass
    ff = shutil.which("ffmpeg")
    if ff:
        try:
            r = subprocess.run([ff, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                               capture_output=True, text=True, timeout=20)
            names = []
            for line in r.stderr.splitlines():
                if "(video)" in line and '"' in line:
                    names.append(line.split('"')[1])
            return names
        except Exception:
            pass
    return None


def is_xreal(text: str) -> bool:
    t = text.upper()
    return any(h in t for h in XREAL_HINTS)


# ─────────────────────────────── test kamery ────────────────────────────────
def colorfulness(frame) -> float:
    """0 = sedy obraz (vsechny kanaly stejne), > ~4 = barevny."""
    small = frame[::8, ::8].astype("int32")
    b, g, r = small[..., 0], small[..., 1], small[..., 2]
    return float((abs(r - g).mean() + abs(g - b).mean()) / 2)


def probe_camera(idx: int, backend: int, bname: str):
    cap = cv2.VideoCapture(idx, backend)
    if not cap.isOpened():
        cap.release()
        return None
    res_ok, best, best_frame = [], None, None
    for w, h in RESOLUTIONS:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        frame = None
        for _ in range(6):                      # zahrati
            ok, f = cap.read()
            if ok and f is not None:
                frame = f
        if frame is None:
            continue
        ah, aw = frame.shape[:2]
        if (aw, ah) not in res_ok:
            res_ok.append((aw, ah))
        if best is None or aw * ah > best[0] * best[1]:
            best, best_frame = (aw, ah), frame
    if best is None:
        cap.release()
        return {"index": idx, "backend": bname, "opened": True, "frames": False}

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, best[0])
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, best[1])
    n, t0, last = 0, time.time(), best_frame
    while n < 30 and time.time() - t0 < 6:
        ok, f = cap.read()
        if ok and f is not None:
            n += 1
            last = f
    fps = n / max(time.time() - t0, 1e-3)
    cap.release()

    os.makedirs(OUT, exist_ok=True)
    snap = os.path.join(OUT, f"cam{idx}_{bname}_{best[0]}x{best[1]}.jpg")
    cv2.imwrite(snap, last)
    col = colorfulness(last)
    return {"index": idx, "backend": bname, "opened": True, "frames": True,
            "resolutions": [f"{w}x{h}" for w, h in res_ok], "best": f"{best[0]}x{best[1]}",
            "fps": round(fps, 1), "brightness": round(float(last.mean()), 1),
            "colorfulness": round(col, 2), "color": col > 4.0, "snapshot": snap}


def probe_all():
    results = []
    for bname, backend in (("DSHOW", cv2.CAP_DSHOW), ("MSMF", cv2.CAP_MSMF)):
        misses = 0
        for idx in range(MAX_INDEX):
            print(f"  zkousim {bname} index {idx} …", end="", flush=True)
            r = probe_camera(idx, backend, bname)
            if r is None:
                print(" nic")
                misses += 1
                if misses >= 2:
                    break
                continue
            misses = 0
            if r["frames"]:
                print(f" {r['best']} {r['fps']} fps {'BAREVNA' if r['color'] else 'sede'}")
            else:
                print(" otevrena, ale bez snimku")
            results.append(r)
    return results


# ─────────────────────────────── nahled ─────────────────────────────────────
def show(idx: int):
    cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print("Kamera se neotevrela.")
        return
    for w, h in RESOLUTIONS:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        ok, f = cap.read()
        if ok:
            break
    print("q = konec, s = ulozit snimek")
    while True:
        ok, f = cap.read()
        if not ok:
            continue
        h, w = f.shape[:2]
        cv2.putText(f, f"{w}x{h} color={colorfulness(f):.1f}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 128), 2)
        cv2.imshow(f"kamera {idx}", f)
        k = cv2.waitKey(1) & 0xFF
        if k == ord('q'):
            break
        if k == ord('s'):
            os.makedirs(OUT, exist_ok=True)
            p = os.path.join(OUT, f"show_cam{idx}_{int(time.time())}.jpg")
            cv2.imwrite(p, f)
            print("ulozeno", p)
    cap.release()
    cv2.destroyAllWindows()


# ─────────────────────────────── main ───────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="bez odpojovani bryli")
    ap.add_argument("--show", type=int, default=None, help="zivy nahled kamery INDEX")
    ap.add_argument("--wait-plug", action="store_true",
                    help="bez klavesnice: sam pozna odpojeni a pripojeni bryli")
    a = ap.parse_args()
    if a.wait_plug:
        a.quick = False

    if sys.platform != "win32":
        print("Tento test je pro Windows.")
    if a.show is not None:
        if cv2 is None:
            print("Chybi opencv-python (pip install opencv-python).")
            return
        show(a.show)
        return

    lines = []
    def log(s=""):
        print(s)
        lines.append(s)

    before_pnp, before_names = [], []
    if a.wait_plug:
        def xreal_present(devs):
            return any(is_xreal(d["name"] + d["id"]) for d in devs)
        before_pnp = pnp_devices()
        if xreal_present(before_pnp):
            print("\n1) Bryle jsou pripojene – ODPOJ je (cekam az zmizi, max 3 min) …", flush=True)
            t0 = time.time()
            while xreal_present(before_pnp) and time.time() - t0 < 180:
                time.sleep(2)
                before_pnp = pnp_devices()
            time.sleep(3)
            before_pnp = pnp_devices()
        before_names = dshow_names() or []
        old_ids = {d["id"] for d in before_pnp}
        print("\n2) PRIPOJ bryle s nasazenym Eye (cekam max 3 min) …", flush=True)
        t0 = time.time()
        while time.time() - t0 < 180:
            time.sleep(2)
            if any(d["id"] not in old_ids for d in pnp_devices()):
                break
        print("   Neco se pripojilo, cekam 12 s na kamery …", flush=True)
        time.sleep(12)
    elif not a.quick:
        input("\n1) ODPOJ bryle od notebooku a stiskni Enter … ")
        before_pnp, before_names = pnp_devices(), dshow_names() or []
        input("2) PRIPOJ bryle s nasazenym Eye, pockej ~10 s a stiskni Enter … ")
        time.sleep(1)

    after_pnp, after_names = pnp_devices(), dshow_names()

    log("=" * 64)
    log("XREAL Eye – RGB test ve Windows   " + time.strftime("%Y-%m-%d %H:%M:%S"))
    log("=" * 64)

    log("\n[ZARIZENI] kamery / obraz / XREAL (Get-PnpDevice):")
    for d in after_pnp:
        tag = " <-- XREAL" if is_xreal(d["name"] + d["id"]) else ""
        log(f"  {d['class']:<8} {d['status']:<8} {d['name']}{tag}")
        log(f"           {d['id']}")
    if not after_pnp:
        log("  (nic nenalezeno, nebo PowerShell nevratil data)")

    new_pnp = []
    if not a.quick:
        old_ids = {d["id"] for d in before_pnp}
        new_pnp = [d for d in after_pnp if d["id"] not in old_ids]
        log("\n[NOVE PO PRIPOJENI]")
        for d in new_pnp:
            log(f"  + {d['class']:<8} {d['name']}  ({d['id']})")
        if not new_pnp:
            log("  (zadne nove kamery/XREAL zarizeni)")

    log("\n[KAMERY DirectShow, poradi = index]")
    if after_names is None:
        log("  (nazvy nezjisteny – nainstaluj pygrabber: pip install pygrabber)")
    else:
        for i, n in enumerate(after_names):
            mark = ""
            if not a.quick and n not in before_names:
                mark += "  <-- NOVA"
            if is_xreal(n):
                mark += "  <-- XREAL?"
            log(f"  [{i}] {n}{mark}")

    results = []
    if cv2 is None:
        log("\n[TEST KAMER] preskoceno – chybi opencv-python (pip install opencv-python)")
    else:
        log("\n[TEST KAMER]")
        results = probe_all()
        for r in results:
            name = ""
            if after_names and r["backend"] == "DSHOW" and r["index"] < len(after_names):
                name = after_names[r["index"]]
            if not r["frames"]:
                log(f"  {r['backend']} [{r['index']}] {name}: otevrena, ZADNY snimek")
                continue
            log(f"  {r['backend']} [{r['index']}] {name}")
            log(f"      rozliseni: {', '.join(r['resolutions'])}  (nejvyssi {r['best']})")
            log(f"      fps ~{r['fps']}  jas {r['brightness']}  barevnost {r['colorfulness']}"
                f"  -> {'BAREVNY obraz' if r['color'] else 'SEDY obraz'}")
            log(f"      snimek: {r['snapshot']}")

    # ── verdikt
    log("\n" + "=" * 64)
    new_cam_names = [n for n in (after_names or []) if not a.quick and n not in before_names]
    xreal_cam_names = [n for n in (after_names or []) if is_xreal(n)]
    new_cam_pnp = [d for d in new_pnp if d["class"] in ("Camera", "Image")]
    candidates = set(new_cam_names) | set(xreal_cam_names)
    color_hits = [r for r in results if r.get("frames") and r["color"] and r["backend"] == "DSHOW"
                  and after_names and r["index"] < len(after_names)
                  and after_names[r["index"]] in candidates]

    if color_hits:
        r = color_hits[0]
        log(f"VERDIKT: ✅ RGB kamera Eye JE ve Windows dostupna: [{r['index']}] "
            f"{after_names[r['index']]}  {r['best']} @ ~{r['fps']} fps")
        log(f"         Nahled: python rgb_probe.py --show {r['index']}")
    elif candidates or new_cam_pnp:
        log("VERDIKT: ⚠ Windows po pripojeni vidi novou kameru, ale barevny obraz z ni neprisel.")
        log("         Zkontroluj snimky v rgb_out/, Power Saving v Nebule, a zkus --show INDEX.")
    elif a.quick:
        log("VERDIKT: ? Rychly rezim – bez porovnani pred/po. Podivej se, ktera kamera je od bryli,")
        log("         nebo spust test bez --quick.")
    else:
        log("VERDIKT: ❌ Po pripojeni bryli s Eye se ve Windows ZADNA nova kamera neobjevila.")
        log("         RGB kamera Eye tedy ve Windows (s touto konfiguraci) jako UVC kamera neni.")
    log("=" * 64)

    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(OUT, "report.json"), "w", encoding="utf-8") as f:
        json.dump({"before_pnp": before_pnp, "after_pnp": after_pnp, "new_pnp": new_pnp,
                   "before_names": before_names, "after_names": after_names,
                   "cameras": results}, f, ensure_ascii=False, indent=2)
    print(f"\nReport: {os.path.join(OUT, 'report.txt')}")


if __name__ == "__main__":
    main()
