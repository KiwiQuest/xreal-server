"""Hlavni okno: rezimy kurzoru, nastaveni, diagnostika, odkazy na Eye a psani."""
from . import winapi
from .config import Config, Runtime
from .overlay import RingOverlay
from .pointer import MODES, MODE_LABELS

CHECKS = [
    ("dwell_on", "Circle click (klikání po zastavení)"),
    ("button_on", "Tlačítko na brýlích = myš"),
    ("sound_on", "Zvuková odezva při přepnutí"),
    ("center_on_enable", "Při zapnutí skočit kurzorem na střed obrazovky"),
    ("flip_h", "Obrátit vodorovný směr"),
    ("flip_v", "Obrátit svislý směr"),
]
SLIDERS = [
    ("sens", "Citlivost (relativní)", 2, 60, "{:.0f} px/°"),
    ("deadzone", "Mrtvá zóna", 0, 5, "{:.1f} °/s"),
    ("fov_h", "Šířka obrazovky v pohledu", 10, 120, "{:.0f} °"),
    ("smooth_ms", "Vyhlazení kurzoru", 0, 200, "{:.0f} ms"),
    ("dwell_time", "Doba do kliku", 0.4, 3.0, "{:.1f} s"),
    ("start_speed", "Zpomalení (start)", 1, 30, "{:.0f} °/s"),
    ("cancel_speed", "Zrušení (rychlost)", 5, 80, "{:.0f} °/s"),
    ("max_drift", "Max. posun", 10, 200, "{:.0f} px"),
    ("hold_ms", "Podržení tlačítka", 150, 800, "{:.0f} ms"),
    ("double_ms", "Okno dvojstisku", 150, 600, "{:.0f} ms"),
]


class App:
    def __init__(self, cfg: Config, rt: Runtime, engine):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.cfg, self.rt, self.engine = cfg, rt, engine
        self.child_windows = {}

        root = self.root = tk.Tk()
        root.title("XREAL Head Mouse")
        root.resizable(False, False)
        root.attributes("-topmost", True)
        frm = ttk.Frame(root, padding=14)
        frm.grid()
        self.frm = frm
        row = 0

        self.status_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.status_var).grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        self.toggle_btn = ttk.Button(frm, command=self.toggle)
        self.toggle_btn.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(8, 8), ipady=8)
        row += 1

        # --- rezim kurzoru ---
        ttk.Label(frm, text="Režim kurzoru").grid(row=row, column=0, sticky="w")
        self.mode_var = tk.StringVar(value=MODE_LABELS[cfg.mode])
        self.mode_combo = ttk.Combobox(frm, textvariable=self.mode_var, state="readonly", width=46,
                                       values=[MODE_LABELS[m] for m in MODES])
        self.mode_combo.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(10, 0))
        self.mode_combo.bind("<<ComboboxSelected>>", self._on_mode)
        row += 1
        self.diag_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.diag_var, foreground="#4d8fe1").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(2, 6))
        row += 1

        # --- zaskrtavatka ---
        self.check_vars = {}
        for attr, text in CHECKS:
            var = tk.BooleanVar(value=getattr(cfg, attr))
            ttk.Checkbutton(frm, text=text, variable=var,
                            command=lambda a=attr, v=var: setattr(cfg, a, v.get())
                            ).grid(row=row, column=0, columnspan=3, sticky="w")
            self.check_vars[attr] = var
            row += 1

        # --- vyber obrazovky ---
        ttk.Label(frm, text="Obrazovka (brýle)").grid(row=row, column=0, sticky="w", pady=(8, 0))
        self.screen_var = tk.StringVar()
        self.screen_combo = ttk.Combobox(frm, textvariable=self.screen_var, state="readonly", width=34)
        self.screen_combo.grid(row=row, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=(8, 0))
        self.screen_combo.bind("<<ComboboxSelected>>", self._on_screen)
        self._screens_key = None
        self.refresh_screens()
        row += 1
        self.screen_info_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.screen_info_var, foreground="#4d8fe1").grid(
            row=row, column=0, columnspan=3, sticky="w")
        row += 1

        # --- posuvniky ---
        self.slider_vars, self.updaters = {}, {}
        for attr, text, lo, hi, fmt in SLIDERS:
            self._slider(row, attr, text, lo, hi, fmt)
            row += 1

        # --- rychlost hlavy ---
        ttk.Label(frm, text="Rychlost hlavy (zelená = start, červená = zrušení)").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(12, 2))
        row += 1
        self.BW, self.BH, self.BMAX = 360, 20, 60.0
        self.bar = tk.Canvas(frm, width=self.BW, height=self.BH, bg="#20242b", highlightthickness=0)
        self.bar.grid(row=row, column=0, columnspan=3)
        row += 1
        self.info_var = tk.StringVar()
        ttk.Label(frm, textvariable=self.info_var, foreground="#4d8fe1", justify="left").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(10, 0))
        row += 1

        # --- tlacitka ---
        ttk.Button(frm, text="Kalibrovat gyro", command=lambda: setattr(rt, "calib_request", True)
                   ).grid(row=row, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(frm, text="Vycentrovat pohled (F5)",
                   command=lambda: setattr(rt, "recenter_request", True)
                   ).grid(row=row, column=1, sticky="ew", pady=(12, 0), padx=10)
        ttk.Button(frm, text="Konec", command=self.quit).grid(row=row, column=2, sticky="ew", pady=(12, 0))
        row += 1
        ttk.Button(frm, text="Kamera Eye + YOLO…", command=self.open_eye
                   ).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(6, 0), padx=(0, 10))
        ttk.Button(frm, text="Psaní (Dasher)…", command=self.open_dasher
                   ).grid(row=row, column=2, sticky="ew", pady=(6, 0))
        row += 1
        ttk.Label(frm, foreground="#777", wraplength=430, justify="left", text=(
            "Tlačítko na brýlích: stisk = levý klik | držet = tažení | dvojstisk = pravý klik | "
            "dvojstisk a držet = circle click ZAP/VYP\n"
            "F8/Pause zap-vyp | F9 kalibrace | F5 vycentrovat | F10 konec | "
            "klik kolečkem myši: uvolnit / znovu držet kurzor")
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.overlay = RingOverlay(root, cfg, rt)
        self._n = 0
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self.tick()

    # ------------------------------------------------------------------
    def _slider(self, row, attr, text, lo, hi, fmt):
        tk, ttk, cfg = self.tk, self.ttk, self.cfg
        var = tk.DoubleVar(value=getattr(cfg, attr))
        ttk.Label(self.frm, text=text).grid(row=row, column=0, sticky="w", pady=(6, 0))
        val = ttk.Label(self.frm, width=10, anchor="e")
        val.grid(row=row, column=2, pady=(6, 0))

        def on(_=None):
            v = var.get()
            setattr(cfg, attr, type(getattr(cfg, attr))(v))
            val.config(text=fmt.format(v))

        ttk.Scale(self.frm, from_=lo, to=hi, variable=var, length=240, command=on
                  ).grid(row=row, column=1, padx=10, pady=(6, 0))
        on()
        self.slider_vars[attr], self.updaters[attr] = var, on

    def toggle(self):
        self.rt.enabled = not self.rt.enabled

    def _on_mode(self, _=None):
        self.cfg.mode = MODES[self.mode_combo.current()]

    def _on_screen(self, _=None):
        self.cfg.screen_idx = self.screen_combo.current() - 1
        self.engine.refresh_monitor()

    def refresh_screens(self):
        mons = self.rt.monitors
        key = tuple((m["x"], m["y"], m["w"], m["h"]) for m in mons)
        if key == self._screens_key:
            return
        self._screens_key = key
        labels = ["Auto (XREAL / druhá obrazovka)"]
        for i, m in enumerate(mons):
            labels.append(f"{i + 1}: {m['w']}×{m['h']} na ({m['x']}, {m['y']})"
                          + (" – primární" if m["primary"] else "")
                          + (" – XREAL" if m["xreal"] else ""))
        self.screen_combo["values"] = labels
        idx = self.cfg.screen_idx
        self.screen_var.set(labels[idx + 1 if 0 <= idx < len(mons) else 0])

    def open_eye(self):
        from .eye import EyeWindow
        self._open_child("eye", lambda: EyeWindow(self.root, self.cfg, self.rt, self.engine))

    def open_dasher(self):
        from .dasher import DasherWindow
        self._open_child("dasher", lambda: DasherWindow(self.root, self.cfg))

    def _open_child(self, name, factory):
        w = self.child_windows.get(name)
        try:
            if w is not None and w.win.winfo_exists():
                w.win.lift()
                return
        except Exception:
            pass
        self.child_windows[name] = factory()

    def quit(self):
        for w in list(self.child_windows.values()):
            try:
                w.close()
            except Exception:
                pass
        self.root.destroy()

    # ------------------------------------------------------------------
    def tick(self):
        rt, cfg = self.rt, self.cfg
        if rt.quit_request:
            self.quit()
            return
        self.overlay.tick()
        self._n += 1
        if self._n % 4 == 0:
            self.status_var.set(f"{rt.status}   |   {rt.hz:.0f} Hz")
            self.toggle_btn.config(text="Ovládání myši: ZAPNUTO  (klikni pro vypnutí)" if rt.enabled
                                   else "Ovládání myši: VYPNUTO  (klikni pro zapnutí)")
            self.diag_var.set(rt.diag_text)
            label = MODE_LABELS[cfg.mode]
            if self.mode_var.get() != label:
                self.mode_var.set(label)
            for attr, var in self.check_vars.items():
                if var.get() != getattr(cfg, attr):
                    var.set(getattr(cfg, attr))
            for attr, var in self.slider_vars.items():
                if abs(var.get() - getattr(cfg, attr)) > 1e-3:
                    var.set(getattr(cfg, attr))
                    self.updaters[attr]()
            kid = "-" if rt.last_key_id is None else rt.last_key_id
            held = "  |  kurzor UVOLNĚN" if rt.lock_off else ""
            self.info_var.set(f"{rt.ctrl_status}  (id tlačítka: {kid}){held}\n"
                              f"Poslední akce: {rt.last_action}")
            self.screen_info_var.set(rt.screen_text)
            self.refresh_screens()
            bar, BW, BH, BMAX = self.bar, self.BW, self.BH, self.BMAX
            bar.delete("all")
            bar.create_rectangle(0, 0, min(rt.speed, BMAX) / BMAX * BW, BH, fill="#4d8fe1", width=0)
            for val, col in ((cfg.start_speed, "#4de1a1"),
                             (max(cfg.cancel_speed, cfg.start_speed + 1), "#e14d4d")):
                xx = min(val, BMAX) / BMAX * BW
                bar.create_line(xx, 0, xx, BH, fill=col, width=3)
        self.root.after(16, self.tick)

    def run(self):
        self.root.mainloop()
