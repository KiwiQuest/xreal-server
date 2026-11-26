"""Čistá logika rozpoznávání (jen numpy, bez kamery a OpenCV, testy v tests/test_faces.py).

  Gallery   všechny vzorky všech osob v jedné matici, porovnání otisku = jedno násobení
  Tracker   drží „stopy“ tváří mezi snímky (stejný člověk = stejné id), ať jméno neskáče
  Track     hlasování o identitě z posledních rozpoznání, sběr vzorků při ukládání
"""
from collections import deque
from typing import Dict, List, Optional, Tuple

import numpy as np

THRESHOLD = 0.363      # SFace: kosinová podobnost >= práh = stejná osoba (doporučení OpenCV)
MAYBE = 0.30           # práh > podobnost >= MAYBE = „možná“ (ukáže se s otazníkem, nic se nerozhoduje)


def unit(e) -> np.ndarray:
    e = np.asarray(e, np.float32).reshape(-1)
    return e / (np.linalg.norm(e) + 1e-9)


class Gallery:
    """Vzorky osob. `match` vrátí nejlepší podobnost pro každou osobu (max přes její vzorky)."""

    def __init__(self, persons: Optional[Dict[str, List[np.ndarray]]] = None):
        self.owners: List[str] = []
        self.E = np.zeros((0, 128), np.float32)
        self.version = 0
        if persons:
            self.build(persons)

    def build(self, persons: Dict[str, List[np.ndarray]]):
        owners, rows = [], []
        for pid, embs in persons.items():
            for e in embs:
                owners.append(pid)
                rows.append(unit(e))
        self.owners = owners
        self.E = np.stack(rows) if rows else np.zeros((0, 128), np.float32)
        self.version += 1

    def __len__(self):
        return len(set(self.owners))

    def match(self, e) -> List[Tuple[str, float]]:
        """[(osoba, podobnost)] seřazené od nejpodobnější."""
        if not self.owners:
            return []
        sims = self.E @ unit(e)
        best: Dict[str, float] = {}
        for pid, s in zip(self.owners, sims.tolist()):
            if s > best.get(pid, -2.0):
                best[pid] = s
        return sorted(best.items(), key=lambda kv: -kv[1])

    def best(self, e) -> Tuple[Optional[str], float]:
        m = self.match(e)
        return m[0] if m else (None, 0.0)


def level(sim: float, threshold: float = THRESHOLD, maybe: float = MAYBE) -> str:
    return "known" if sim >= threshold else "maybe" if sim >= maybe else "unknown"


def diverse(samples: List[np.ndarray], e, max_sim: float = 0.93) -> bool:
    """Nový vzorek přidat jen když není skoro stejný jako některý už sebraný (víc úhlů = lepší galerie)."""
    if not samples:
        return True
    S = np.stack([unit(s) for s in samples])
    return float(np.max(S @ unit(e))) < max_sim


def mean_unit(embs) -> np.ndarray:
    return unit(np.mean(np.stack([unit(e) for e in embs]), axis=0))


def iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1, x2, y2 = max(ax, bx), max(ay, by), min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    u = aw * ah + bw * bh - inter
    return inter / u if u > 0 else 0.0


class Track:
    VOTES = 6

    def __init__(self, tid: int, bbox, t: float):
        self.id = tid
        self.bbox = tuple(float(v) for v in bbox)
        self.t_first = self.t_seen = t
        self.t_recog = -1e9            # kdy se naposled počítal otisk
        self.votes = deque(maxlen=self.VOTES)    # (osoba nebo None, podobnost)
        self.pid: Optional[str] = None
        self.sim = 0.0
        self.level = "unknown"
        self.counted = False           # návštěva už je započítaná osobě
        # ukládání (enroll): sbírané vzorky, do kdy, cílová osoba (doplnění snímků)
        self.collect: Optional[dict] = None
        self.score = 0.0

    def update(self, bbox, t: float, alpha: float = 0.6):
        self.bbox = tuple(alpha * float(n) + (1 - alpha) * o for n, o in zip(bbox, self.bbox))
        self.t_seen = t

    def vote(self, pid: Optional[str], sim: float, threshold: float = THRESHOLD, maybe: float = MAYBE):
        """Identita = osoba s nejvyšším součtem podobností v posledních hlasech (jedno špatné
        rozpoznání jméno nepřepne). Úroveň podle průměru jejích hlasů."""
        self.votes.append((pid, float(sim)))
        tally: Dict[str, List[float]] = {}
        for p, s in self.votes:
            if p is not None and s >= maybe:
                tally.setdefault(p, []).append(s)
        if not tally:
            self.pid, self.sim, self.level = None, float(sim), "unknown"
            return
        p, ss = max(tally.items(), key=lambda kv: sum(kv[1]))
        avg = sum(ss) / len(ss)
        # osoba musí vyhrát aspoň polovinu hlasů, jinak je to „možná“
        lv = level(avg, threshold, maybe)
        if lv == "known" and len(ss) * 2 < len(self.votes):
            lv = "maybe"
        self.pid, self.sim, self.level = p, avg, lv


class Tracker:
    def __init__(self, ttl: float = 1.2, min_iou: float = 0.25):
        self.ttl, self.min_iou = ttl, min_iou
        self.tracks: Dict[int, Track] = {}
        self._next = 1

    def update(self, bboxes: list, t: float) -> List[Tuple[Track, int]]:
        """Přiřadí detekce ke stopám (chamtivě podle IoU, pak podle vzdálenosti středů).
        Vrátí [(stopa, index detekce)] a zahodí stopy, které dlouho nikdo neviděl."""
        pairs = []
        free = set(self.tracks)
        cand = []
        for i, b in enumerate(bboxes):
            for tid in self.tracks:
                tr = self.tracks[tid]
                o = iou(b, tr.bbox)
                if o < self.min_iou:
                    # rychlý pohyb hlavy s brýlemi: blízký střed a podobná velikost
                    cx, cy = b[0] + b[2] / 2, b[1] + b[3] / 2
                    tx, ty = tr.bbox[0] + tr.bbox[2] / 2, tr.bbox[1] + tr.bbox[3] / 2
                    d = ((cx - tx) ** 2 + (cy - ty) ** 2) ** 0.5
                    size = max(b[2], tr.bbox[2])
                    if d < size * 0.9 and 0.6 < b[2] / max(1.0, tr.bbox[2]) < 1.6:
                        o = 0.01 + 0.2 * (1 - d / (size * 0.9))
                    else:
                        continue
                cand.append((o, i, tid))
        cand.sort(reverse=True)
        used_d = set()
        for o, i, tid in cand:
            if i in used_d or tid not in free:
                continue
            used_d.add(i)
            free.discard(tid)
            self.tracks[tid].update(bboxes[i], t)
            pairs.append((self.tracks[tid], i))
        for i, b in enumerate(bboxes):
            if i not in used_d:
                tr = Track(self._next, b, t)
                self._next += 1
                self.tracks[tr.id] = tr
                pairs.append((tr, i))
        for tid in list(self.tracks):
            if t - self.tracks[tid].t_seen > self.ttl:
                del self.tracks[tid]
        return pairs

    def visible(self, t: float, within: float = 0.35) -> List[Track]:
        return [tr for tr in self.tracks.values() if t - tr.t_seen <= within]
