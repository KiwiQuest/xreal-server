"""Pruhledny overlay pres celou obrazovku bryli: objekty z kamery sedi na realnych objektech."""
import copy
import time

from . import winapi
from .ar import ArState, edge_marker, to_screen
from .config import Config

KEY = "#010101"
HELP = ("KALIBRACE AR   šipky = posun (Shift = jemně)   PgUp/PgDn = zorný úhel kamery   "
        "L = objektiv běžný/fisheye   X / Y = zrcadlit   Home = reset   F2 = hotovo")
MAX_MARKERS = 12            # sipky k objektum mimo obrazovku (vic by zahltilo okraj)
VK = {"left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "pgup": 0x21, "pgdn": 0x22,
      "home": 0x24, "shift": 0x10, "x": 0x58, "y": 0x59, "l": 0x4C, "f2": 0x71}


class ArOverlay:
    def __init__(self, root, cfg, rt, engine, state: ArState, get_source, on_calib_change=None):
        import tkinter as tk
        self.tk, self.root, self.cfg, self.rt, self.engine = tk, root, cfg, rt, engine
        self.state, self.get_source, self.on_calib_change = state, get_source, on_calib_change
        self.calib = False
        self.alive = True
        self.last = time.time()
        self.prev = {k: False for k in VK}
        self.geom = None
        win = self.win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=KEY)
        try:
            win.attributes("-transparentcolor", KEY)
        except tk.TclError:
            pass
        self.cv = tk.Canvas(win, bg=KEY, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self._fit()
        win.update_idletasks()
        winapi.make_overlay_window(win)
        self.tick()

    # ------------------------------------------------------------------
    def monitor(self):
        m = self.engine.monitor if self.engine else None
        if m:
            return m
        return {"x": 0, "y": 0, "w": self.win.winfo_screenwidth(), "h": self.win.winfo_screenheight()}

    def _fit(self):
        m = self.monitor()
        g = (m["x"], m["y"], m["w"], m["h"])
        if g != self.geom:
            self.geom = g
            self.win.geometry(f"{m['w']}x{m['h']}+{m['x']}+{m['y']}")
            self.cv.config(width=m["w"], height=m["h"])
        return m

    def set_calibration(self, on: bool):
        self.calib = on

    def close(self):
        self.alive = False
        try:
            self.win.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _keys(self, dt):
        cal = self.cfg.ar_cal[self.get_source()]
        cur = {k: winapi.key_state(v)[0] for k, v in VK.items()}
        edge = {k: cur[k] and not self.prev[k] for k in VK}
        self.prev = cur
        if edge["f2"]:
            self.calib = not self.calib
            if self.on_calib_change:
                self.on_calib_change(self.calib)
        if not self.calib:
            return
        slow = cur["shift"]
        rate = (1.5 if slow else 6.0) * dt
        if cur["left"]:
            cal["ox"] -= rate
        if cur["right"]:
            cal["ox"] += rate
        if cur["up"]:
            cal["oy"] -= rate
        if cur["down"]:
            cal["oy"] += rate
        frate = (6.0 if slow else 25.0) * dt
        if cur["pgup"]:
            cal["fov"] = min(170.0, cal["fov"] + frate)
        if cur["pgdn"]:
            cal["fov"] = max(10.0, cal["fov"] - frate)
        if edge["x"]:
            cal["flip_x"] = not cal["flip_x"]
        if edge["y"]:
            cal["flip_y"] = not cal["flip_y"]
        if edge["l"]:
            cal["lens"] = "rectilinear" if cal.get("lens") == "fisheye" else "fisheye"
        if edge["home"]:
            cal.update(copy.deepcopy(Config().ar_cal[self.get_source()]))

    def _draw_marker(self, x, y, w, h, it):
        m = edge_marker(x, y, w, h)
        if m is None:
            return
        mx, my, ux, uy = m
        self.cv.create_line(mx - ux * 46, my - uy * 46, mx, my, fill=it.color, width=5,
                            arrow="last", arrowshape=(16, 20, 7))
        # popisek na stranu do obrazovky, aby nevyjel ven
        anchor = ("e" if ux > 0.5 else "w" if ux < -0.5 else "center")
        self.cv.create_text(mx - ux * 56, my - uy * 56, anchor=anchor, text=it.label,
                            fill=it.color, font=("Segoe UI", 13, "bold"))

    def tick(self):
        if not self.alive:
            return
        now = time.time()
        dt = min(0.1, now - self.last)
        self.last = now
        self._keys(dt)
        mon = self._fit()
        cfg, rt, cv = self.cfg, self.rt, self.cv
        fov = cfg.fov_h if cfg.ar_anchored else cfg.ar_disp_fov
        cv.delete("all")
        w, h = mon["w"], mon["h"]
        drawn = markers = 0
        for it in self.state.visible_items():
            # dot/fill = vždy world-locked (kompenzace hlavou) + FOV displeje
            # poly/box  = SLAM kamera, řídí cfg.ar_anchored
            _is_spatial = it.kind in ("dot", "fill")
            _anchored = False if _is_spatial else cfg.ar_anchored
            _fov = cfg.ar_disp_fov if _is_spatial else fov
            pts = [to_screen(a, b, mon, _fov, _anchored, rt.view_h, rt.view_v)
                   for a, b in it.pts]
            xs = [p[0] - mon["x"] for p in pts]
            ys = [p[1] - mon["y"] for p in pts]
            if not xs:
                continue
            if max(xs) < -50 or min(xs) > w + 50 or max(ys) < -50 or min(ys) > h + 50:
                # cely objekt mimo zobrazitelnou plochu (kamera vidi vic nez displej):
                # jen pojmenovane objekty (YOLO) dostanou sipku na okraji, obrysy ne
                if cfg.ar_edge_markers and it.label and markers < MAX_MARKERS:
                    self._draw_marker(sum(xs) / len(xs), sum(ys) / len(ys), w, h, it)
                    markers += 1
                continue
            flat = [c for xy in zip(xs, ys) for c in xy]
            if it.kind in ("box", "poly") and len(flat) >= 4:
                cv.create_line(*flat, flat[0], flat[1], fill=it.color, width=3, joinstyle="round")
            elif it.kind == "dot" and xs:
                r = 10
                cv.create_oval(xs[0]-r, ys[0]-r, xs[0]+r, ys[0]+r, outline=it.color, width=3)
                cv.create_line(xs[0]-r-5, ys[0], xs[0]+r+5, ys[0], fill=it.color, width=2)
                cv.create_line(xs[0], ys[0]-r-5, xs[0], ys[0]+r+5, fill=it.color, width=2)
            elif it.kind == "fill" and len(flat) >= 6:
                cv.create_polygon(*flat, fill=it.color, outline=it.color,
                                  stipple="gray25", width=2, joinstyle="round")
            if it.label:
                cv.create_text(xs[0], ys[0] - 6, anchor="sw", text=it.label, fill=it.color,
                               font=("Segoe UI", 15, "bold"))
            drawn += 1
            if drawn >= 80:
                break
        if self.calib:
            cal = cfg.ar_cal[self.get_source()]
            cv.create_line(w / 2 - 30, h / 2, w / 2 + 30, h / 2, fill="#ff40ff", width=2)
            cv.create_line(w / 2, h / 2 - 30, w / 2, h / 2 + 30, fill="#ff40ff", width=2)
            cv.create_text(w / 2, 40, text=HELP, fill="#ffffff", font=("Segoe UI", 16, "bold"))
            cv.create_text(w / 2, 76, fill="#ffff80", font=("Consolas", 16, "bold"), text=(
                f"zdroj: {self.get_source()}   objektiv: {cal.get('lens', 'rectilinear')}   "
                f"zorný úhel {cal['fov']:.1f}°   posun x {cal['ox']:+.2f}° "
                f"y {cal['oy']:+.2f}°   zrcadlení x:{'ano' if cal['flip_x'] else 'ne'} "
                f"y:{'ano' if cal['flip_y'] else 'ne'}   "
                f"režim: {'kotvená obrazovka' if cfg.ar_anchored else 'pevná obrazovka'}"))
        self.win.after(33, self.tick)
