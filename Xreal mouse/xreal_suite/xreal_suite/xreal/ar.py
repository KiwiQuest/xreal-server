"""AR: objekty z kamery -> poloha na displeji bryli tak, aby sedely na to, na co koukas.

Model (opticky pruhledne bryle, kamera pevne na bryli):
  * pixel kamery -> uhel od opticke osy podle typu objektivu:
        rectilinear (bezny objektiv):  alfa = atan((px - w/2) / f)
        fisheye (sirokouhly, SLAM):    uhel od osy theta = r / f (ekvidistantni model),
                                       r = vzdalenost od stredu snimku, pocita se radialne
        f se odvozuje ze zorneho uhlu pres sirku snimku
  * svetovy uhel objektu  w = H(t_snimku) + alfa + offset
        H = o kolik se pohled od reference (F5) otocil doprava/dolu (z IMU)
  * KOTVENA obrazovka (Spatial Anchor): poloha na obrazovce = stred + ppd * w
        -> cil se nehybe ani pri pohybu hlavy (bryle samy kompenzuji), zpozdeni kamery nevadi
  * POVA obrazovka (head-locked):       poloha = stred + ppd * (w - H(ted))
        -> doplni se rotace od porizeni snimku
  ppd = pixelu displeje na stupen = sirka monitoru / uhlova sirka obrazovky
Uhly: vodorovne + = doprava, svisle + = dolu (stejne jako souradnice obrazovky).
"""
import bisect
import math
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass
class ArItem:
    kind: str                         # "poly" | "box" | "dot"
    pts: List[Tuple[float, float]]    # svetove uhly (h, v) ve stupnich
    label: str = ""
    color: str = "#00ff80"
    world_locked: bool = False        # True = vzdy world-locked (head kompenzace), ignoruje cfg.ar_anchored


class ArState:
    """Vrstvy objektu (klasicke obrysy, YOLO boxy, YOLO obrysy) sdilene mezi vlakny."""

    def __init__(self):
        self.layers: Dict[str, Tuple[float, List[ArItem]]] = {}

    def set_layer(self, name: str, items: List[ArItem]):
        self.layers[name] = (time.time(), items)

    def clear(self):
        self.layers = {}

    def visible_items(self, max_age: float = 0.6) -> List[ArItem]:
        now = time.time()
        out: List[ArItem] = []
        for t, items in list(self.layers.values()):
            if now - t <= max_age:
                out.extend(items)
        return out


LENSES = ("rectilinear", "fisheye")
MAX_THETA_DEG = 89.0          # fisheye: paprsky za 90 st. od osy uz nejdou rozumne promitnout


def cam_angle(px: float, py: float, w: float, h: float, fov_h_deg: float,
              flip_x: bool = False, flip_y: bool = False,
              lens: str = "rectilinear") -> Tuple[float, float]:
    """Pixel kamery -> (vodorovny, svisly) uhel od opticke osy, + = doprava / dolu.

    fov_h_deg = vodorovny zorny uhel pres celou sirku snimku.
    lens = "rectilinear" (bezny objektiv) nebo "fisheye" (ekvidistantni model, theta = r / f).
    U fisheye se uhel pocita radialne: z polomeru r vznikne theta, smer zustane stejny.
    Vystup je v obou pripadech stejny jako u rectilinear: atan2(X, Z) a atan2(Y, Z) paprsku,
    takze zbytek retezce (world_points, to_screen) se nemeni.
    """
    if flip_x:
        px = w - px
    if flip_y:
        py = h - py
    fov = math.radians(max(5.0, min(170.0, fov_h_deg)))
    dx, dy = px - w / 2.0, py - h / 2.0
    if lens != "fisheye":
        f = (w / 2.0) / math.tan(fov / 2.0)
        return math.degrees(math.atan(dx / f)), math.degrees(math.atan(dy / f))
    f = (w / 2.0) / (fov / 2.0)                   # px na radian
    r = math.hypot(dx, dy)
    if r < 1e-9:
        return 0.0, 0.0
    theta = min(r / f, math.radians(MAX_THETA_DEG))
    s, c = math.sin(theta), math.cos(theta)
    return math.degrees(math.atan2(s * dx / r, c)), math.degrees(math.atan2(s * dy / r, c))


def edge_marker(x: float, y: float, w: float, h: float,
                margin: float = 60.0) -> Optional[Tuple[float, float, float, float]]:
    """Bod mimo obrazovku (souradnice relativne k monitoru) -> znacka na okraji.

    Vraci None, kdyz je bod na obrazovce. Jinak (mx, my, ux, uy): misto znacky na okraji
    (s okrajem margin) a jednotkovy smer ze stredu k objektu (kam se otocit).
    Znacka lezi na primce stred -> objekt, takze ukazuje spravnym smerem i v rozich.
    """
    if 0 <= x <= w and 0 <= y <= h:
        return None
    cx, cy = w / 2.0, h / 2.0
    dx, dy = x - cx, y - cy
    d = math.hypot(dx, dy)
    if d < 1e-9:
        return None
    ux, uy = dx / d, dy / d
    hw, hh = max(1.0, cx - margin), max(1.0, cy - margin)
    k = min(hw / abs(ux) if abs(ux) > 1e-9 else math.inf,
            hh / abs(uy) if abs(uy) > 1e-9 else math.inf)
    return cx + ux * k, cy + uy * k, ux, uy


def lookup(hist: Sequence[Tuple[float, float, float]], t: float) -> Tuple[float, float]:
    """Natoceni hlavy (H, V) v case t z historie [(cas, h, v)] (linearni interpolace)."""
    hist = list(hist)
    if not hist:
        return 0.0, 0.0
    times = [x[0] for x in hist]
    i = bisect.bisect_left(times, t)
    if i <= 0:
        return hist[0][1], hist[0][2]
    if i >= len(hist):
        return hist[-1][1], hist[-1][2]
    (t0, h0, v0), (t1, h1, v1) = hist[i - 1], hist[i]
    a = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
    return h0 + a * (h1 - h0), v0 + a * (v1 - v0)


def world_points(pts_px: Sequence[Tuple[float, float]], w: float, h: float, cal: dict,
                 head_h: float, head_v: float) -> List[Tuple[float, float]]:
    out = []
    for px, py in pts_px:
        ax, ay = cam_angle(px, py, w, h, cal["fov"], cal.get("flip_x", False),
                           cal.get("flip_y", False), cal.get("lens", "rectilinear"))
        out.append((head_h + ax + cal["ox"], head_v + ay + cal["oy"]))
    return out


def to_screen(wh: float, wv: float, mon: dict, fov_deg: float, anchored: bool,
              head_h: float = 0.0, head_v: float = 0.0) -> Tuple[float, float]:
    ppd = mon["w"] / max(5.0, fov_deg)
    dx = wh if anchored else wh - head_h
    dy = wv if anchored else wv - head_v
    return (mon["x"] + mon["w"] / 2.0 + ppd * dx, mon["y"] + mon["h"] / 2.0 + ppd * dy)


def box_corners(x1, y1, x2, y2):
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def items_from_cv(objs, img_w: float, img_h: float, cal: dict, head_h: float, head_v: float,
                  cv2=None, max_pts: int = 14) -> List[ArItem]:
    """Kontury z vision.render [(x, y, w, h, kontura)] -> AR polygony."""
    items = []
    for x, y, w, h, c in objs[:40]:
        if cv2 is not None and c is not None and len(c) > max_pts:
            eps = 0.01 * cv2.arcLength(c, True)
            c = cv2.approxPolyDP(c, eps, True)
        if cv2 is not None and c is not None and len(c) >= 3:
            pts = [(float(p[0][0]), float(p[0][1])) for p in c][:max_pts * 2]
        else:
            pts = box_corners(x, y, x + w, y + h)
        items.append(ArItem("poly", world_points(pts, img_w, img_h, cal, head_h, head_v),
                            "", "#00ff80"))
    return items


def items_from_boxes(boxes, img_w, img_h, cal, head_h, head_v) -> List[ArItem]:
    return [ArItem("box", world_points(box_corners(x1, y1, x2, y2), img_w, img_h, cal,
                                       head_h, head_v), f"{name} {conf:.0%}", "#ffe04d")
            for (x1, y1, x2, y2, name, conf) in boxes]


def items_from_polys(polys, img_w, img_h, cal, head_h, head_v) -> List[ArItem]:
    return [ArItem("poly", world_points(pts, img_w, img_h, cal, head_h, head_v),
                   f"{name} {conf:.0%}", "#4dc3ff") for pts, name, conf in polys if len(pts) >= 3]
