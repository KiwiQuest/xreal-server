"""Nastaveni (uklada se do JSON) a stav za behu (sdileny mezi vlakny)."""
import json
import os
from dataclasses import asdict, dataclass, field, fields

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(APP_DIR, "xreal_config.json")


@dataclass
class Config:
    # --- ukazovani ---
    mode: str = "relative"         # relative | view | look
    sens: float = 15.0             # px na stupen (relativni rezimy)
    deadzone: float = 1.0          # deg/s
    flip_h: bool = False
    flip_v: bool = False
    fov_h: float = 40.0            # uhlova sirka obrazovky v pohledu (rezim view)
    smooth_ms: float = 40.0        # vyhlazeni kurzoru (rezim view)
    center_on_enable: bool = False  # pri zapnuti skocit na stred obrazovky
    screen_idx: int = -1           # -1 = auto (XREAL), jinak index monitoru
    # --- circle click ---
    dwell_on: bool = True
    dwell_time: float = 1.2        # s
    start_speed: float = 5.0       # deg/s: pod tim se kruzek rozbehne
    cancel_speed: float = 15.0     # deg/s: nad tim se zrusi
    max_drift: float = 80.0        # px
    # --- tlacitko na bryli ---
    button_on: bool = True
    button_id: int = 0             # 0 = jakekoli
    hold_ms: float = 350.0
    double_ms: float = 300.0
    sound_on: bool = True
    # --- Eye kamera / YOLO ---
    yolo_weights: str = "yolov8n.pt"
    yolo_seg_weights: str = "yolov8n-seg.pt"
    yolo_conf: float = 0.35
    # --- AR overlay (objekty z kamery promitnute do bryli) ---
    ar_anchored: bool = True        # True = obrazovka ukotvena v prostoru (Spatial Anchor)
    ar_disp_fov: float = 50.0       # uhlova sirka zorneho pole displeje (jen pro 'pevnou' obrazovku)
    ar_latency_ms: float = 60.0     # zpozdeni kamera -> pocitac (kompenzuje se z IMU)
    ar_edge_markers: bool = True    # objekty mimo obrazovku ukazat sipkou na okraji
    # kalibrace zvlast pro kazdou kameru; lens = "fisheye" (sirokouhla SLAM kamera) | "rectilinear"
    ar_cal: dict = field(default_factory=lambda: {
        "slam": {"fov": 100.0, "ox": 0.0, "oy": 0.0, "flip_x": False, "flip_y": False,
                 "lens": "fisheye"},
        "rgb": {"fov": 70.0, "ox": 0.0, "oy": 0.0, "flip_x": False, "flip_y": False,
                "lens": "rectilinear"},
    })
    # --- psani (Dasher) ---
    dasher_lang: str = "cs"
    dasher_speed: float = 1.6

    def load(self, path: str = CONFIG_FILE):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return self
        for fld in fields(self):
            if fld.name in data:
                try:
                    setattr(self, fld.name, type(getattr(self, fld.name))(data[fld.name]))
                except (TypeError, ValueError):
                    pass
        if self.mode not in ("relative", "view", "look"):
            self.mode = "relative"
        self._merge_ar_cal()
        return self

    def _merge_ar_cal(self):
        """Doplni chybejici klice kalibrace z vychozich hodnot (stary config bez 'lens' apod.)."""
        defaults = Config.__dataclass_fields__["ar_cal"].default_factory()
        loaded = self.ar_cal if isinstance(self.ar_cal, dict) else {}
        merged = {}
        for src, dflt in defaults.items():
            cur = loaded.get(src)
            merged[src] = {**dflt, **(cur if isinstance(cur, dict) else {})}
            if merged[src].get("lens") not in ("rectilinear", "fisheye"):
                merged[src]["lens"] = dflt["lens"]
        self.ar_cal = merged

    def save(self, path: str = CONFIG_FILE):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, indent=2, ensure_ascii=False)
        except OSError:
            pass


class Runtime:
    """Stav za behu. Zapisuje hlavne engine, cte GUI."""

    def __init__(self):
        self.running = True
        self.enabled = False            # hlavni vypinac ovladani mysi
        self.lock_off = False           # klik kolieckem: kurzor uvolnen
        self.calib_request = False
        self.recenter_request = False
        self.quit_request = False
        self.status = "Startuji..."
        self.ctrl_status = "Tlačítko: připojuji..."
        self.hz = 0.0
        self.speed = 0.0                # rychlost hlavy (deg/s), vyhlazena
        self.dwell_active = False
        self.dwell_progress = 0.0
        self.flash_until = 0.0
        self.toast_text = ""
        self.toast_ok = True
        self.toast_until = 0.0
        self.last_action = "-"
        self.last_key_id = None
        self.screen_text = "Střed obrazovky: zjišťuji..."
        self.diag_text = ""
        self.pointer_xy = None          # aktualni cil kurzoru (px), pro diagnostiku
        self.monitors = []              # seznam monitoru pro GUI
        self.view_h = 0.0               # natoceni pohledu oproti referenci (deg, + = doprava)
        self.view_v = 0.0               # (deg, + = dolu)
        from collections import deque
        self.view_hist = deque(maxlen=4000)   # (cas, h, v) - historie pro kompenzaci zpozdeni kamery
