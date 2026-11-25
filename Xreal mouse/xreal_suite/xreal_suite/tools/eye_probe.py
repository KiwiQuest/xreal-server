"""Zachyti ~N sekund z portu 52997 (video Eye) a vypise, co v tom je.

    python tools/eye_probe.py [sekundy]

Vysledek ulozi do data/eye_probe.bin. Kdyz viewer format nerozpozna, posli mi vystup
tohoto skriptu a prvnich ~300 B z toho souboru (hexdump je vypsany nize).
"""
import os
import socket
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from xreal import eye, protocol                                  # noqa: E402


def main():
    secs = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
    try:
        s = socket.create_connection((protocol.HOST, protocol.PORT_VIDEO), timeout=5)
    except OSError as e:
        print("Port 52997 nedostupny:", e)
        print("Je pripojene Eye a zapnuty Spatial Anchor (u nekterych verzi Nebula beta)?")
        return
    buf = bytearray()
    t0 = time.time()
    s.settimeout(1.0)
    while time.time() - t0 < secs:
        try:
            d = s.recv(262144)
        except socket.timeout:
            continue
        if not d:
            break
        buf.extend(d)
    path = os.path.join(eye.APP_DIR, "data", "eye_probe.bin")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "wb").write(buf)
    print(f"Prijato {len(buf)} B za {secs:.0f} s ({len(buf) / secs / 1024:.0f} kB/s), ulozeno: {path}")
    print("Prvnich 96 B:", bytes(buf[:96]).hex(" "))
    fr = eye.detect_framing(bytes(buf))
    print("Rozpoznane ramcovani:", fr)
    if fr:
        frames = eye.split_frames(bytearray(buf), fr)
        print("Ramcu:", len(frames), "| delky:", Counter(len(f) for f in frames).most_common(4))
        print(f"Pro 512x378 4bit stereo cekam telo {eye.PAIR_BYTES} B (jeden obraz {eye.IMG_BYTES} B)")


if __name__ == "__main__":
    main()
