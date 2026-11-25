#!/usr/bin/env python3
"""XREAL Kořen - kořenová aplikace v systray (Windows).

    pythonw koren.py            spustí supervizor + aplikaci (doporučeno, koren.bat)
    python koren.py --dry       zkušebně: nehibernuje, nehýbe myší, v systray jsou zkoušky víka
    python koren.py --child     jen aplikace bez supervizoru (ladění)
    python koren.py --wait 2    jiná doba čekání na brýle (minuty) pro tento běh

Supervizor hlídá, že aplikace žije: když spadne nebo se zasekne (přestane psát heartbeat),
spustí ji znovu. Uživatel bez klávesnice a myši tak nikdy nezůstane bez ovládání.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

HEARTBEAT = os.path.join(HERE, "logs", "heartbeat.txt")
HANG_S = 25.0             # heartbeat starší než tohle = zaseknuté
RESTART_WINDOW = 120.0    # víc než 5 pádů za 2 minuty = počkat déle (nezahltit systém)


def _child_args(argv):
    return [a for a in argv if a != "--child"]


def supervise(argv):
    from koren import win
    if not win.single_instance():
        print("XREAL Kořen už běží.")
        return 0
    os.makedirs(os.path.dirname(HEARTBEAT), exist_ok=True)
    exe = sys.executable
    crashes = []
    while True:
        try:
            os.remove(HEARTBEAT)
        except OSError:
            pass
        started = time.time()
        p = subprocess.Popen([exe, os.path.abspath(__file__), "--child"] + _child_args(argv), cwd=HERE)
        code = None
        while code is None:
            time.sleep(2.0)
            code = p.poll()
            if code is not None:
                break
            try:
                age = time.time() - os.path.getmtime(HEARTBEAT)
            except OSError:
                age = time.time() - started
            if age > HANG_S and time.time() - started > HANG_S:
                _log(f"[SUP] aplikace neodpovídá {age:.0f} s, restartuji")
                p.kill()
                p.wait(10)
                code = -9
        if code == 0:
            return 0                                  # uživatel ukončil
        now = time.time()
        crashes = [t for t in crashes if now - t < RESTART_WINDOW] + [now]
        _log(f"[SUP] aplikace skončila s kódem {code}, restart ({len(crashes)}x za 2 min)")
        time.sleep(2.0 if len(crashes) <= 5 else 30.0)


def _log(text):
    try:
        os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
        with open(os.path.join(HERE, "logs", "koren.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
    except OSError:
        pass


def child(argv):
    from koren.app import App
    from koren.config import Config
    cfg = Config().load()
    if "--dry" in argv:
        cfg.dry_run = True
    if "--wait" in argv:
        try:
            cfg.wait_minutes = float(argv[argv.index("--wait") + 1])
        except (IndexError, ValueError):
            pass
    App(cfg, heartbeat=HEARTBEAT).run()
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    sys.exit(child(args) if "--child" in args else supervise(args))
