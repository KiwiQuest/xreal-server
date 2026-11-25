"""Sniffer portu 52996-52999 + klaves: co posila brýle? (nic do nich neposila)

    python tools/sniff.py            Ctrl+C = souhrn
"""
import os
import sys
import threading
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from xreal import protocol, winapi                              # noqa: E402

lock = threading.Lock()
stats = Counter()


def log(tag, msg):
    with lock:
        print(time.strftime("%H:%M:%S"), f"[{tag}]", msg, flush=True)


def reader(tag, port):
    import socket
    try:
        s = socket.create_connection((protocol.HOST, port), timeout=5)
    except OSError as e:
        log(tag, f"port {port}: {e}")
        return
    log(tag, f"port {port} pripojeno")
    s.settimeout(None)
    shown = 0
    while True:
        try:
            d = s.recv(65536)
        except OSError:
            return
        if not d:
            log(tag, "spojeni ukonceno")
            return
        stats[tag] += len(d)
        if tag != "IMU" and shown < 60:
            shown += 1
            log(tag, f"{len(d)} B: {d[:96].hex(' ')}")


def keys():
    prev = set()
    while True:
        cur = {vk for vk in range(8, 255) if vk not in (1, 2, 4, 5, 6) and winapi.key_state(vk)[0]}
        for vk in sorted(cur - prev):
            log("KEY", f"VK 0x{vk:02X}")
        prev = cur
        time.sleep(0.005)


if __name__ == "__main__":
    for tag, port in (("META", protocol.PORT_META), ("VIDEO", protocol.PORT_VIDEO),
                      ("IMU", protocol.PORT_IMU), ("CONTROL", protocol.PORT_CONTROL)):
        threading.Thread(target=reader, args=(tag, port), daemon=True).start()
    threading.Thread(target=keys, daemon=True).start()
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nSouhrn bajtu:", dict(stats))
