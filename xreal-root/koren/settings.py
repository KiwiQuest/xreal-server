"""Okno nastavení a stavu (obyčejné okno Windows, ovládá se myší a klávesnicí).

Režim Tray: otevře se jen na požádání (dvojklik / menu v systray). Režim GUI na druhém
monitoru: je trvale vidět na druhém monitoru a ukazuje živý stav. Okno nic nepočítá samo,
jen čte stav aplikace a zapisuje do nastavení.
"""
import os
import tkinter as tk
from tkinter import ttk

from . import profiles
from .states import MODE_LABELS


class SettingsWindow:
    def __init__(self, app):
        self.app = app
        self.win = None
        self.vars = {}
        self.labels = {}
        self.pinned = False            # režim GUI na monitoru: okno drží kořen

    @property
    def visible(self) -> bool:
        return self.win is not None and self.win.winfo_exists() and self.win.state() != "withdrawn"

    def show(self, monitor=None, pinned=False):
        self.pinned = pinned
        if self.win is None or not self.win.winfo_exists():
            self._build()
        self._load()
        self.win.deiconify()
        if monitor:
            self.win.update_idletasks()             # přirozená velikost podle obsahu a DPI
            w, h = self.win.winfo_reqwidth(), self.win.winfo_reqheight()
            x = monitor["x"] + max(0, (monitor["w"] - w) // 2)
            y = monitor["y"] + max(0, (monitor["h"] - h) // 2)
            self.win.geometry(f"+{x}+{y}")
        self.win.lift()
        self.win.attributes("-topmost", True)            # vynořit nad ostatní okna, pak normálně
        self.win.after(1500, lambda: self.win.winfo_exists() and self.win.attributes("-topmost", False))
        self.refresh()

    def hide(self):
        if self.win is not None and self.win.winfo_exists():
            self.win.withdraw()

    # ----------------------------------------------------------------- stavba
    def _build(self):
        app = self.app
        w = self.win = tk.Toplevel(app.root)
        w.title("XREAL Kořen: nastavení")
        w.protocol("WM_DELETE_WINDOW", self.hide)
        pad = {"padx": 10, "pady": 4}
        # dvě záložky, ať se okno vejde i na displej brýlí 1280×720
        self.nb = ttk.Notebook(w)
        self.nb.pack(fill="both", expand=True, padx=6, pady=(6, 0))
        t1, t2 = ttk.Frame(self.nb), ttk.Frame(self.nb)
        self.nb.add(t1, text="Stav a monitor brýlí")
        self.nb.add(t2, text="Režim a nastavení")

        st = ttk.LabelFrame(t1, text="Stav")
        st.pack(fill="x", **pad)
        for key, title in (("mode", "Režim"), ("profile", "Provozní režim"), ("lid", "Víko"),
                           ("glasses", "Brýle"), ("display", "Displej brýlí"), ("input", "Ovládání"),
                           ("lidact", "Zavření víka ve Windows"), ("mods", "Moduly")):
            row = ttk.Frame(st)
            row.pack(fill="x", padx=8, pady=1)
            ttk.Label(row, text=title + ":", width=22).pack(side="left")
            lab = ttk.Label(row, text="…", wraplength=360, justify="left")
            lab.pack(side="left", fill="x")
            self.labels[key] = lab

        df = self.disp_frame = ttk.LabelFrame(t1, text="Monitor brýlí")
        df.pack(fill="x", **pad)
        ttk.Label(df, text="Vyber monitor, který jsou brýle, a potvrď. Uloží se podle jména a zařízení,\n"
                           "ne podle pořadí, takže ho kořen najde i jako třetí monitor nebo na jiném portu.",
                  foreground="#666", justify="left").pack(anchor="w", padx=8, pady=(2, 4))
        r = ttk.Frame(df)
        r.pack(fill="x", padx=8, pady=2)
        self.vars["disp"] = tk.StringVar()
        self.disp_box = ttk.Combobox(r, textvariable=self.vars["disp"], state="readonly", width=46)
        self.disp_box.pack(side="left", fill="x", expand=True)
        ttk.Button(r, text="Toto jsou brýle", command=self._confirm_display).pack(side="left", padx=6)
        self.disp_state = ttk.Label(df, text="")
        self.disp_state.pack(anchor="w", padx=8, pady=(2, 4))
        self._disp_list = []

        pf = ttk.LabelFrame(t2, text="Provozní režim (Ctrl+Alt+T = kdykoliv Tray)")
        pf.pack(fill="x", **pad)
        self.vars["profile"] = tk.StringVar()
        for p in profiles.PROFILES:
            ttk.Radiobutton(pf, text=profiles.LABELS[p], value=p, variable=self.vars["profile"],
                            command=self._profile_changed).pack(anchor="w", padx=8)
        ttk.Label(pf, text="Zavřené víko přebije cokoliv: notebook v tašce, ovládání hlavou.",
                  foreground="#666").pack(anchor="w", padx=8, pady=(2, 4))

        sf = ttk.LabelFrame(t2, text="Nastavení")
        sf.pack(fill="x", **pad)
        self.vars["wait"] = tk.DoubleVar()
        self.vars["hold"] = tk.DoubleVar()
        r = ttk.Frame(sf)
        r.pack(fill="x", padx=8, pady=2)
        ttk.Label(r, text="Čekat na brýle po zavření víka (min):", width=36).pack(side="left")
        ttk.Spinbox(r, from_=0.5, to=60, increment=0.5, width=6, textvariable=self.vars["wait"]).pack(side="left")
        r = ttk.Frame(sf)
        r.pack(fill="x", padx=8, pady=2)
        ttk.Label(r, text="Podržet tlačítko brýlí pro menu (s):", width=36).pack(side="left")
        ttk.Spinbox(r, from_=1, to=5, increment=0.5, width=6, textvariable=self.vars["hold"]).pack(side="left")
        for key, text in (("sound", "Pípání (zavřené víko, odpočet)"),
                          ("autostart", "Spouštět s Windows")):
            self.vars[key] = tk.BooleanVar()
            ttk.Checkbutton(sf, text=text, variable=self.vars[key]).pack(anchor="w", padx=8)
        mf = ttk.LabelFrame(t2, text="Moduly (běží jen v režimu Vše v brýlích a se zavřeným víkem)")
        mf.pack(fill="x", **pad)
        for mid, mod in app.mods.mods.items():
            self.vars["mod_" + mid] = tk.BooleanVar()
            ttk.Checkbutton(mf, text=f"{mod.name}: {mod.description}", variable=self.vars["mod_" + mid]
                            ).pack(anchor="w", padx=8)
        self.vars["head_on"] = tk.BooleanVar()
        ttk.Checkbutton(mf, text="Ovládání hlavou zapnuté teď (v režimech 1-3 se samo nezapíná)",
                        variable=self.vars["head_on"], command=self._head_toggle).pack(anchor="w", padx=8, pady=(4, 2))

        bf = ttk.Frame(w)
        bf.pack(fill="x", padx=10, pady=8)
        ttk.Button(bf, text="Uložit", command=self._save).pack(side="left")
        ttk.Button(bf, text="Otevřít log", command=self._open_log).pack(side="left", padx=6)
        ttk.Button(bf, text="Skrýt", command=self.hide).pack(side="right")

    def _load(self):
        c = self.app.cfg
        self.vars["profile"].set(c.profile)
        self.vars["wait"].set(c.wait_minutes)
        self.vars["hold"].set(c.menu_hold_s)
        self.vars["sound"].set(c.sound_on)
        self.vars["autostart"].set(c.autostart)
        for mid in self.app.mods.mods:
            self.vars["mod_" + mid].set(bool(c.modules.get(mid, False)))

    # ----------------------------------------------------------------- monitor brýlí
    def _fill_displays(self):
        app = self.app
        cur = [d.get("gdi") for d in self._disp_list]
        new = [d.get("gdi") for d in app.displays]
        if cur != new or not self.disp_box["values"]:
            self._disp_list = list(app.displays)
            self.disp_box["values"] = [d["label"] for d in self._disp_list]
            if app.gdisp:
                self.vars["disp"].set(app.gdisp["label"])
        if app.gdisp is None:
            txt = "Brýle mezi monitory nevidím. Vyber je ručně, nebo zkontroluj, že je Windows používají."
        elif app.gconfirmed:
            txt = f"Potvrzeno: {app.gdisp['label']}"
        else:
            txt = f"Odhad podle jména: {app.gdisp['label']}  →  potvrď prosím"
        self.disp_state.config(text=txt, foreground="#2a7a3a" if app.gconfirmed else "#b06a00")

    def _confirm_display(self):
        lab = self.vars["disp"].get()
        for d in self._disp_list:
            if d["label"] == lab:
                self.app.confirm_glasses_display(d)
                self._fill_displays()
                return

    def focus_display(self):
        """Otevřeno kvůli nepotvrzenému monitoru: zvýraznit výběr."""
        if self.win is not None:
            self.nb.select(0)
            self._fill_displays()
            self.disp_box.focus_set()

    # ----------------------------------------------------------------- akce
    def _profile_changed(self):
        self.app.set_profile(self.vars["profile"].get())

    def _head_toggle(self):
        self.app.set_head_mouse(self.vars["head_on"].get())

    def _save(self):
        app, c = self.app, self.app.cfg
        try:
            c.wait_minutes = max(0.5, float(self.vars["wait"].get()))
            c.menu_hold_s = max(1.0, float(self.vars["hold"].get()))
        except (tk.TclError, ValueError):
            pass
        c.sound_on = bool(self.vars["sound"].get())
        app.machine.wait_s = c.wait_minutes * 60.0
        app.btn.hold_s = c.menu_hold_s
        for mid in app.mods.mods:
            c.modules[mid] = bool(self.vars["mod_" + mid].get())
        app.set_autostart(bool(self.vars["autostart"].get()))
        app.set_profile(self.vars["profile"].get())        # uloží a srovná moduly
        app.overlay_toast("Nastavení uloženo", 1.5)

    def _open_log(self):
        try:
            os.startfile(self.app.log.path)
        except Exception:
            pass

    # ----------------------------------------------------------------- živý stav
    def refresh(self):
        if not self.visible:
            return
        app = self.app
        s = app.snapshot()
        L = self.labels
        L["mode"].config(text=MODE_LABELS.get(s.mode, s.mode))
        eff = app.profile_eff
        L["profile"].config(text=profiles.LABELS[app.cfg.profile] +
                            (f"  →  teď {profiles.SHORT[eff]}" if app.cfg.profile == profiles.AUTO else ""))
        L["lid"].config(text={"open": "otevřené", "closed": "zavřené"}.get(s.lid, "notebook bez víka / neznámé"))
        g = []
        g.append("USB ano" if s.glasses_usb else "USB ne")
        if app.plan.streams:
            g.append("pohyb OK" if s.imu_alive else "pohyb nepřichází")
            g.append("tlačítko OK" if s.button_alive else "tlačítko nepřipojené")
        else:
            g.append("spojení vypnuté (šetří výkon)")
        L["glasses"].config(text=", ".join(g))
        L["display"].config(text=(app.gdisp["label"] + ("" if app.gconfirmed else "  (odhad, potvrď níže)"))
                            if app.gdisp else "nenalezen")
        self._fill_displays()
        age = s.physical_input_age
        hm = app.mods.get("head_mouse")
        L["input"].config(text=(f"klávesnice/myš použity před {age:.0f} s" if age is not None
                                else "klávesnice/myš zatím nepoužity")
                          + ("  ·  hlava ZAP" if hm and hm.on else "  ·  hlava vyp"))
        L["lidact"].config(text="nedělat nic (OK)" if s.lid_action_ok else
                           "není „Nedělat nic“, v tašce se notebook uspí!" if s.lid_action_ok is False else "neznámé")
        act = [m.name for m in app.mods.mods.values() if m.active]
        L["mods"].config(text=", ".join(act) if act else "žádný neběží")
        self.vars["head_on"].set(bool(hm and hm.on))
