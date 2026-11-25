"""Rychla detekce hranic a obrysu objektu v tmavem sedotonovem obraze (OpenCV + numpy).

Pipeline: [zvyseni kontrastu] -> [hrany] -> [kontury + obdelniky].
Kazdy krok se meri, takze je videt, kolik ms co stoji (a co je nejrychlejsi).
"""
import time
from typing import Dict, List, Tuple

try:
    import cv2
    import numpy as np
except Exception:                      # pragma: no cover
    cv2 = np = None

CONTRASTS = ("roztažení", "CLAHE", "žádný")
MODES = {
    "orig": "Původní (se zvýšeným kontrastem)",
    "sobel": "Hrany – Sobel (nejrychlejší)",
    "morph": "Hrany – morfologický gradient",
    "canny": "Hrany – Canny (čisté čáry)",
    "otsu": "Obrysy – prahování (Otsu) + kontury",
    "contours": "Obrysy a obdélníky – Canny + kontury",
}


def available() -> bool:
    return cv2 is not None and np is not None


def stretch(gray, p_lo: float = 1.0, p_hi: float = 99.5):
    """Roztahne histogram (tmavy obraz -> plny rozsah). Percentily z podvzorku = rychle."""
    sub = gray[::4, ::4]
    lo, hi = np.percentile(sub, (p_lo, p_hi))
    if hi - lo < 4:
        hi = lo + 4
    a = 255.0 / (hi - lo)
    return cv2.convertScaleAbs(gray, alpha=a, beta=-lo * a)


_CLAHE = None


def clahe(gray, clip: float = 3.0, tile: int = 8):
    global _CLAHE
    if _CLAHE is None:
        _CLAHE = cv2.createCLAHE(clipLimit=clip, tileGridSize=(tile, tile))
    return _CLAHE.apply(gray)


def enhance(gray, mode: str):
    if mode == "roztažení":
        return stretch(gray)
    if mode == "CLAHE":
        return clahe(stretch(gray))
    return gray


def _grad(gray, blur: int = 5):
    """Sobel derivace a L1 velikost gradientu (rychle, zaklad pro Sobel i Canny)."""
    g = cv2.GaussianBlur(gray, (blur, blur), 0)
    dx = cv2.Sobel(g, cv2.CV_16S, 1, 0, ksize=3)
    dy = cv2.Sobel(g, cv2.CV_16S, 0, 1, ksize=3)
    mag = cv2.add(cv2.convertScaleAbs(dx), cv2.convertScaleAbs(dy))
    return dx, dy, mag, float(np.median(mag[::4, ::4]))


def edges_sobel(gray, thr: float = 0.0):
    """Prah gradientu se odvodi z medianu (= urovne sumu), takze funguje i na tmavem obraze."""
    _, _, mag, med = _grad(gray)
    if thr <= 0:
        thr = max(32.0, 4.5 * med)
    return cv2.threshold(mag, thr, 255, cv2.THRESH_BINARY)[1]


def edges_morph(gray):
    k = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    g = cv2.morphologyEx(cv2.GaussianBlur(gray, (3, 3), 0), cv2.MORPH_GRADIENT, k)
    thr = max(20.0, 3.0 * float(np.median(g[::4, ::4])) + 12.0)
    return cv2.threshold(g, thr, 255, cv2.THRESH_BINARY)[1]


def edges_canny(gray, k: float = 6.0):
    """Canny nad predpocitanymi derivacemi; prahy z urovne sumu (k * median gradientu)."""
    dx, dy, _, med = _grad(gray)
    hi = max(40.0, k * med)
    return cv2.Canny(dx, dy, 0.5 * hi, hi)


def find_objects(binary, min_area: int = 120):
    """Kontury z binarniho obrazu -> [(x, y, w, h, kontura)] od nejvetsiho."""
    bw = cv2.dilate(binary, None, iterations=1)
    res = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cnts = res[0] if len(res) == 2 else res[1]
    out = []
    for c in cnts:
        if cv2.contourArea(c) >= min_area:
            x, y, w, h = cv2.boundingRect(c)
            out.append((x, y, w, h, c))
    out.sort(key=lambda o: -o[2] * o[3])
    return out


def otsu_mask(gray):
    g = cv2.GaussianBlur(gray, (5, 5), 0)
    return cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]


def render(gray, mode: str = "orig", contrast: str = "roztažení", half: bool = False,
           base_rgb=None):
    """gray: HxW uint8. base_rgb: volitelny barevny snimek stejne velikosti (kamera RGB) -
    pak se puvodni obraz a obrysy kresli do barev. Vraci (obraz, {faze: ms}, [objekty])."""
    t: Dict[str, float] = {}
    t0 = time.perf_counter()
    if half:
        gray = cv2.resize(gray, (gray.shape[1] // 2, gray.shape[0] // 2),
                          interpolation=cv2.INTER_AREA)
        if base_rgb is not None:
            base_rgb = cv2.resize(base_rgb, (gray.shape[1], gray.shape[0]),
                                  interpolation=cv2.INTER_AREA)
    enh = enhance(gray, contrast)
    t["kontrast"] = (time.perf_counter() - t0) * 1000
    objs: List = []
    t1 = time.perf_counter()
    if mode == "orig":
        out = base_rgb if base_rgb is not None else enh
    elif mode == "sobel":
        out = edges_sobel(enh)
    elif mode == "morph":
        out = edges_morph(enh)
    elif mode == "canny":
        out = edges_canny(enh)
    elif mode in ("otsu", "contours"):
        binary = otsu_mask(enh) if mode == "otsu" else edges_canny(enh)
        t["hrany"] = (time.perf_counter() - t1) * 1000
        t2 = time.perf_counter()
        objs = find_objects(binary, 60 if half else 120)
        rgb = base_rgb.copy() if base_rgb is not None else cv2.cvtColor(enh, cv2.COLOR_GRAY2RGB)
        for x, y, w, h, c in objs:
            cv2.drawContours(rgb, [c], -1, (60, 255, 120), 1)
            cv2.rectangle(rgb, (x, y), (x + w, y + h), (255, 80, 60), 1)
        t["kontury"] = (time.perf_counter() - t2) * 1000
        out = rgb
    else:
        out = enh
    if "hrany" not in t:
        t["hrany"] = (time.perf_counter() - t1) * 1000
    t["celkem"] = (time.perf_counter() - t0) * 1000
    return out, t, objs


def benchmark(gray, n: int = 100) -> List[Tuple[str, float]]:
    """Prumerny cas jedne operace v ms (na realnem snimku)."""
    enh = stretch(gray)
    jobs = [
        ("roztažení kontrastu", lambda: stretch(gray)),
        ("CLAHE", lambda: clahe(enh)),
        ("hrany Sobel", lambda: edges_sobel(enh)),
        ("hrany morfologický gradient", lambda: edges_morph(enh)),
        ("hrany Canny", lambda: edges_canny(enh)),
        ("prahování Otsu + kontury", lambda: find_objects(otsu_mask(enh))),
        ("Canny + kontury + obdélníky", lambda: find_objects(edges_canny(enh))),
        ("celý řetěz (roztažení+Canny+kontury)",
         lambda: find_objects(edges_canny(stretch(gray)))),
        ("celý řetěz na půlce rozlišení",
         lambda: find_objects(edges_canny(stretch(cv2.resize(gray, None, fx=0.5, fy=0.5,
                                                             interpolation=cv2.INTER_AREA))), 60)),
    ]
    out = []
    for name, fn in jobs:
        fn()                                               # zahrati
        t = time.perf_counter()
        for _ in range(n):
            fn()
        out.append((name, (time.perf_counter() - t) * 1000 / n))
    return out
