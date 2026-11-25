#!/usr/bin/env python3
"""XREAL One / One Pro + Eye: enable the Eye cameras as UVC webcams on Windows (HID).
XREAL One / One Pro + Eye: zapnuti kamer Eye jako UVC webkamer ve Windows (HID).

The glasses hide the Eye cameras until the host enables the USB functions
"uvc0"/"uvc1". This sends the same request the official XREAL library sends
(NRBSPSetUsbConfigAll). Protocol cross-checked against:
  * HaloneXR/halone - XrealOneHid.kt (frame layout, commands 0xD2/0xD3)
  * Aloim/Xreal-One-Pro-Eye-RGB-Camera-feed-on-Android (MIT) - call sequence

Frame (little-endian, padded to 1024 B), HID interface 0:
  0xFD | crc32 | length u16 | requestId u32 | timestamp u32 | cmd u16 | 5x0 | payload
  crc32 (zlib) and length cover everything from `length` to the end of the payload
  (length = 17 + payload). Replies carry a 1-byte status (0 = OK) before the payload.
Commands: 0x00D2 = read USB functions, 0x00D3 = write USB functions (4-byte word).
Word: 2 bits per field - ncm 0, ecm 2, uac 4, hid 6, mtp 8, storage 10,
      uvc0 12, uvc1 14, enable 16.  Read: 1 = on, 2 = off.
      Write: 0 = leave as is, 1 = turn on, 2 = turn off.
This script only ever writes uvc0/uvc1 (+ enable) and refuses any other command
(the same channel carries firmware updates - never touched).

Verified (One Pro, MCU 15.1.03.603, DSP 15.A.01.097, Windows 11):
  uvc0 + uvc1 on  ->  two "UVC Camera 1" devices:
    * RGB  : MJPEG 1920x1080 @ up to 60 fps (also 1080x1920, 720x1280)
    * gray : YUYV 512x378 @ 25 fps (SLAM camera)

Usage / Pouziti:
  python eye_rgb_enable.py --status       read state only
  python eye_rgb_enable.py                enable uvc0 + uvc1, wait, list & test cameras
  python eye_rgb_enable.py --only uvc1    toggle a single function (experiments)
  python eye_rgb_enable.py --disable      turn the cameras off again (revert)
Requires: pip install hidapi   (+ opencv-python pygrabber for the camera test)
"""
import argparse
import json
import os
import sys
import time
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "rgb_out")
VID, PID = 0x3318, 0x0436
HID_INTERFACE = 0
FRAME = 1024
HDR = 22
CHK = 17
CMD_GET_USB = 0x00D2
CMD_SET_USB = 0x00D3
CMD_MCU_VER = 0x0026
CMD_DSP_VER = 0x0018
ALLOWED = {CMD_GET_USB, CMD_SET_USB, CMD_MCU_VER, CMD_DSP_VER}
FIELDS = [("ncm", 0), ("ecm", 2), ("uac", 4), ("hid", 6), ("mtp", 8),
          ("storage", 10), ("uvc0", 12), ("uvc1", 14), ("enable", 16)]
ON, OFF = 1, 2

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


# ───────────────────────────── ramec ────────────────────────────────────────
def frame(cmd: int, req: int, payload: bytes = b"") -> bytes:
    p = bytearray(FRAME)
    p[0] = 0xFD
    ln = CHK + len(payload)
    p[5:7] = ln.to_bytes(2, "little")
    p[7:11] = req.to_bytes(4, "little")
    p[11:15] = b"\0\0\0\0"
    p[15:17] = cmd.to_bytes(2, "little")
    p[HDR:HDR + len(payload)] = payload
    p[1:5] = (zlib.crc32(bytes(p[5:5 + ln])) & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(p)


def parse(b: bytes):
    """-> (cmd, req, status, payload) nebo None."""
    i = b.find(b"\xfd")
    if i < 0:
        return None
    b = b[i:]
    if len(b) < HDR + 1:
        return None
    ln = int.from_bytes(b[5:7], "little")
    end = 5 + ln
    if ln < CHK + 1 or end > len(b):
        return None
    if (zlib.crc32(b[5:end]) & 0xFFFFFFFF) != int.from_bytes(b[1:5], "little"):
        return None
    return (int.from_bytes(b[15:17], "little"), int.from_bytes(b[7:11], "little"),
            b[HDR], bytes(b[HDR + 1:end]))


def decode(word: int) -> str:
    return "0x%08x (%s)" % (word, " ".join(f"{n}={(word >> s) & 3}" for n, s in FIELDS))


# ───────────────────────────── HID ──────────────────────────────────────────
class Glasses:
    def __init__(self):
        try:
            import hid
        except ImportError:
            sys.exit("Chybi hidapi:  python -m pip install hidapi")
        self.hid = hid
        devs = [d for d in hid.enumerate(VID, PID) if d.get("interface_number") == HID_INTERFACE]
        if not devs:
            allv = hid.enumerate(VID, PID)
            print("Nalezena HID rozhrani bryli:")
            for d in allv:
                print(f"  if={d.get('interface_number')} usage_page=0x{d.get('usage_page', 0):04x} "
                      f"usage=0x{d.get('usage', 0):04x}")
            sys.exit("HID rozhrani 0 bryli nenalezeno – jsou bryle pripojene?")
        devs.sort(key=lambda d: -(d.get("usage_page") or 0))   # vendor page (0xFFxx) prvni
        last_err = None
        for d in devs:
            try:
                h = hid.device()
                h.open_path(d["path"])
                self.dev, self.info = h, d
                break
            except Exception as e:
                last_err = e
        else:
            sys.exit(f"HID rozhrani nejde otevrit ({last_err}). Zavri Nebulu a zkus znovu.")
        print(f"HID otevreno: if={self.info.get('interface_number')} "
              f"usage_page=0x{self.info.get('usage_page', 0):04x} {self.info.get('product_string')}")
        self.req = int(time.time()) & 0xFFFF
        self.prefix = None          # b"\0" (bez report ID) nebo b"" – zjisti se pri prvnim zapisu

    def _write(self, pkt: bytes) -> bool:
        orders = [self.prefix] if self.prefix is not None else [b"\0", b""]
        for pre in orders:
            try:
                n = self.dev.write(pre + pkt)
            except Exception:
                n = -1
            if n and n > 0:
                self.prefix = pre
                return True
        return False

    def request(self, cmd: int, payload: bytes = b"", timeout: float = 1.5):
        if cmd not in ALLOWED:
            raise ValueError(f"prikaz 0x{cmd:04x} neni povoleny")
        self.req += 1
        req = self.req
        if not self._write(frame(cmd, req, payload)):
            raise IOError("HID zapis selhal")
        deadline = time.time() + timeout
        while time.time() < deadline:
            data = self.dev.read(FRAME + 1, 100)
            if not data:
                continue
            r = parse(bytes(data))
            if not r or r[0] != cmd or r[1] != req:
                continue
            if r[2] != 0:
                raise IOError(f"bryle odpovedely na 0x{cmd:04x} statusem {r[2]}")
            return r[3]
        raise TimeoutError(f"bez odpovedi na 0x{cmd:04x}")

    def usb_word(self) -> int:
        p = self.request(CMD_GET_USB)
        if len(p) < 4:
            raise IOError(f"kratka odpoved ({len(p)} B)")
        return int.from_bytes(p[:4], "little")

    def set_fields(self, **fields):
        word = ON << 16                                   # enable = 1
        for name, val in fields.items():
            word |= val << dict(FIELDS)[name]
        allowed = (3 << 12) | (3 << 14) | (3 << 16)       # jen uvc0, uvc1, enable
        assert word & ~allowed == 0
        print(f"Zapisuji USB funkce: {decode(word)}")
        self.request(CMD_SET_USB, word.to_bytes(4, "little"))

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass


def version(g: Glasses, cmd: int) -> str:
    try:
        return g.request(cmd).decode("utf-8", "replace").strip("\0 ")
    except Exception as e:
        return f"? ({e})"


# ───────────────────────────── main ─────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="jen precist stav / read state only")
    ap.add_argument("--only", choices=["uvc0", "uvc1"], default=None,
                    help="prepnout jen jednu UVC funkci (vychozi: obe, overeno) / toggle only one")
    ap.add_argument("--disable", action="store_true", help="vypnout kamery (navrat) / turn cameras off")
    ap.add_argument("--no-probe", action="store_true", help="po zapnuti netestovat kamery / skip camera test")
    a = ap.parse_args()
    fields = [a.only] if a.only else ["uvc0", "uvc1"]

    g = Glasses()
    print("MCU firmware:", version(g, CMD_MCU_VER))
    print("DSP firmware:", version(g, CMD_DSP_VER))
    try:
        before = g.usb_word()
    except Exception as e:
        g.close()
        sys.exit(f"Cteni USB funkci selhalo: {e}")
    print("Aktualni USB funkce:", decode(before))

    os.makedirs(OUT, exist_ok=True)
    bk = os.path.join(OUT, "usb_config_backup.json")
    if not os.path.exists(bk):
        with open(bk, "w") as f:
            json.dump({"word": before, "decoded": decode(before), "time": time.ctime()}, f, indent=2)
        print("Zaloha puvodniho stavu:", bk)

    if a.status:
        g.close()
        return

    sys.path.insert(0, HERE)
    try:
        import rgb_probe as rp
    except Exception:
        rp = None
    cams_before = rp.dshow_names() if rp else None
    pnp_before = {d["id"] for d in rp.pnp_devices()} if rp else set()

    try:
        g.set_fields(**{f: OFF if a.disable else ON for f in fields})
        print("OK – bryle se ted znovu pripoji (displej muze na chvili zhasnout).")
    except Exception as e:
        print(f"Zapis selhal: {e}")
        g.close()
        sys.exit(1)
    g.close()

    print("Cekam 10 s na znovupripojeni …", flush=True)
    time.sleep(10)
    try:
        g2 = Glasses()
        print("Nove USB funkce:", decode(g2.usb_word()))
        g2.close()
    except SystemExit:
        print("(HID po znovupripojeni zatim neodpovida – nevadi)")
    except Exception as e:
        print(f"(Kontrolni cteni selhalo: {e})")

    if a.disable or not rp:
        return

    print("\n[NOVA ZARIZENI PO ZAPNUTI]")
    new = [d for d in rp.pnp_devices() if d["id"] not in pnp_before]
    for d in new:
        print(f"  + {d['class']:<8} {d['status']:<8} {d['name']}  ({d['id']})")
    if not new:
        print("  (zadne)")
    names = rp.dshow_names()
    print("\n[KAMERY DirectShow]")
    for i, n in enumerate(names or []):
        mark = "  <-- NOVA" if cams_before is not None and n not in cams_before else ""
        print(f"  [{i}] {n}{mark}")
    new_cams = [d for d in new if d["class"] in ("Camera", "Image")]
    if new_cams or (names and cams_before is not None and set(names) - set(cams_before)):
        print("\n✅ Windows vidi novou kameru od bryli. Testuji obraz …")
        if not a.no_probe and rp.cv2 is not None:
            for r in rp.probe_all():
                if r.get("frames"):
                    print(f"  {r['backend']} [{r['index']}] {r['best']} ~{r['fps']} fps "
                          f"{'BAREVNY' if r['color'] else 'SEDY'}  -> {r['snapshot']}")
                else:
                    print(f"  {r['backend']} [{r['index']}] otevrena, ale BEZ snimku "
                          f"(pravdepodobne HEVC – viz poznamka)")
        print("\nPoznamka: kamera posila H.265/HEVC. Kdyz Windows kameru ukaze, ale obraz nejde,")
        print("nainstaluj 'HEVC Video Extensions' z Microsoft Store nebo zkus ffmpeg -f dshow.")
    else:
        print("\n❌ Po zapnuti se zadna nova kamera neobjevila.")
        print("   Zkus odpojit/pripojit bryle a spustit znovu. / Replug the glasses and retry.")
    print("\nVratit puvodni stav:  python eye_rgb_enable.py --disable")


if __name__ == "__main__":
    main()
