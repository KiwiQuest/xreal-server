"""XREAL Head Mouse 2.0 - spusteni.

    python run.py                  zive z bryli
    python run.py --dry            nic nehybe ani neklika (jen okno a diagnostika)
    python run.py --replay dump.bin  prehrani zaznamu IMU (implicitne --dry)
    python run.py --mode view      zacit v rezimu "stred pohledu"
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from xreal import winapi                                    # noqa: E402
from xreal.config import Config, Runtime                    # noqa: E402
from xreal.engine import Engine, NullOutput, WinOutput      # noqa: E402


def main():
    args = sys.argv[1:]
    replay = None
    if "--replay" in args:
        replay = args[args.index("--replay") + 1]
    mode = args[args.index("--mode") + 1] if "--mode" in args else None
    dry = "--dry" in args or replay is not None or not winapi.IS_WIN

    winapi.set_dpi_aware()
    cfg = Config().load()
    if mode in ("relative", "view", "look"):
        cfg.mode = mode
    rt = Runtime()
    engine = Engine(cfg, rt, io=NullOutput() if dry else WinOutput(), replay=replay)

    vanish = None if dry else winapi.get_mouse_vanish()
    if vanish:
        winapi.set_mouse_vanish(False)     # stisk klavesy by jinak schoval kurzor
    engine.start()
    try:
        from xreal.gui import App
        App(cfg, rt, engine).run()
    finally:
        engine.stop()
        cfg.save()
        if vanish:
            winapi.set_mouse_vanish(True)


if __name__ == "__main__":
    main()
