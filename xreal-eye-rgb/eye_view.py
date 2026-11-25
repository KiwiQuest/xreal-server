#!/usr/bin/env python3
"""Live preview of the XREAL Eye RGB camera on Windows (OpenCV, DirectShow, MJPEG).
Zivy nahled RGB kamery XREAL Eye ve Windows.

Run eye_rgb_enable.py first. Keys: q = quit, s = save snapshot.
"""
import os
import sys
import time

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
SIZES = [(1920, 1080), (1080, 1920), (720, 1280)]


def dshow_names():
    try:
        from pygrabber.dshow_graph import FilterGraph
        return list(FilterGraph().get_input_devices())
    except Exception:
        return []


def open_rgb():
    names = dshow_names()
    candidates = [i for i, n in enumerate(names) if n.startswith("UVC Camera")] or list(range(6))
    for idx in candidates:
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if not cap.isOpened():
            continue
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        w, h = SIZES[0]
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, 60)
        for _ in range(10):
            ok, f = cap.read()
            if ok and f is not None:
                break
        if ok and f is not None and f.shape[1] >= 1000:
            print(f"RGB camera: index {idx} {names[idx] if idx < len(names) else ''} {f.shape[1]}x{f.shape[0]}")
            return cap
        cap.release()
    return None


def main():
    cap = open_rgb()
    if cap is None:
        sys.exit("RGB camera not found - run eye_rgb_enable.py first / nejdriv spust eye_rgb_enable.py")
    n, t0, fps = 0, time.time(), 0.0
    while True:
        ok, f = cap.read()
        if not ok:
            continue
        n += 1
        if time.time() - t0 >= 1:
            fps, n, t0 = n / (time.time() - t0), 0, time.time()
        view = cv2.resize(f, (960, int(960 * f.shape[0] / f.shape[1])))
        cv2.putText(view, f"{f.shape[1]}x{f.shape[0]}  {fps:.0f} fps", (10, 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 128), 2)
        cv2.imshow("XREAL Eye RGB", view)
        k = cv2.waitKey(1) & 0xFF
        if k == ord("q"):
            break
        if k == ord("s"):
            p = os.path.join(HERE, f"eye_{int(time.time())}.jpg")
            cv2.imwrite(p, f)
            print("saved", p)
    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
