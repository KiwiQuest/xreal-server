"""Psani hlavou bez klavesnice: zoomovaci rozhrani ve stylu Dasher.

Proc: ukazovani hlavou je spojite 2D ovladani, ale nepresne a unavne - klikani na
klavesnici je tedy pomale. Dasher nepotrebuje zadne kliky: pismena jsou v pruzich, jejichz
vyska odpovida pravdepodobnosti, ze nasledujou (jazykovy model). Staci miřit do pruhu
s pozadovanym pismenem - zoom te do nej vtahne a pismeno se napise samo. Caste pokracovani
jsou obrovske, takze psani je rychle a robustni proti trasu hlavy.

Ovladani: kurzor (hlavou) VPRAVO od cary = priblizuje, VLEVO = oddaluje (maze).
"""
import math
import os
import time
from collections import defaultdict
from typing import Dict, List, Tuple

from .config import APP_DIR

ALPHABET = "abcdefghijklmnopqrstuvwxyzáčďéěíňóřšťúůýž .,?!"
CROSS_X = 0.40           # vodorovna pozice cary (zlomek sirky): vlevo oddalit, vpravo priblizit

SAMPLE_CS = """
ahoj jak se máš dnes je krásný den a svítí slunce půjdeme se projít do parku
potřebuji napsat zprávu kamarádovi a zeptat se jestli má čas na kafe
děkuji ti moc za pomoc to je opravdu super nápad zítra se uvidíme
můžeš mi prosím poslat ten soubor který jsem včera potřeboval
dobré ráno dnes pracuji doma a večer půjdu na procházku se psem
kde je nejbližší obchod s potravinami a kolik hodin otevírají
rád bych objednal dvě kávy a jeden koláč prosím vás
v pátek mám schůzku s kolegy ohledně nového projektu a programu
brýle fungují skvěle můžu psát bez klávesnice jen pohybem hlavy
mám se dobře a doufám že ty také se těším na víkend s rodinou
prosím zavolej mi až budeš mít chvilku je to docela důležité
dnes jsem se naučil něco nového a chci to ukázat všem svým přátelům
děkuji za odpověď budu na to myslet a ozvu se co nejdříve
kolik to stojí a kdy to můžete dodat prosím o cenovou nabídku
"""

SAMPLE_EN = """
hello how are you today it is a beautiful day and the sun is shining
i need to send a message to my friend and ask if he has time for coffee
thank you very much for your help that is a really great idea
could you please send me the file that i needed yesterday
good morning today i am working from home and tonight i will walk the dog
where is the nearest shop and what time does it open
i would like to order two coffees and a piece of cake please
on friday i have a meeting with my colleagues about the new project
the glasses work great i can type without a keyboard just by moving my head
please call me when you have a moment it is quite important
"""


class LanguageModel:
    """Znakovy model (kontext 3 znaku) s vyhlazenim a zalohou na kratsi kontexty."""

    def __init__(self, text: str, order: int = 3):
        self.order = order
        self.counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        text = "".join(c for c in text.lower().replace("\n", " ") if c in ALPHABET)
        for i, ch in enumerate(text):
            for k in range(0, order + 1):
                if i - k < 0:
                    break
                self.counts[text[i - k:i]][ch] += 1
        self._cache: Dict[str, List[Tuple[str, float, float]]] = {}

    def children(self, context: str) -> List[Tuple[str, float, float]]:
        """[(znak, od, do)] - rozdeleni intervalu 0..1 podle pravdepodobnosti."""
        ctx = context.lower()[-self.order:]
        hit = self._cache.get(ctx)
        if hit is not None:
            return hit
        probs = {c: 0.0 for c in ALPHABET}
        weights = [0.10, 0.20, 0.30, 0.40]           # od kratkych po dlouhe kontexty
        used = 0.0
        for k in range(min(self.order, len(ctx)), -1, -1):
            sub = ctx[len(ctx) - k:] if k else ""
            table = self.counts.get(sub)
            if not table:
                continue
            tot = sum(table.values())
            w = weights[min(k, len(weights) - 1)]
            for c, n in table.items():
                probs[c] += w * n / tot
            used += w
        floor = 0.004                                   # kazde pismeno jde vzdy trefit
        norm = sum(probs.values()) or 1.0
        probs = {c: (p / norm) * (1 - floor * len(ALPHABET)) + floor for c, p in probs.items()}
        total = sum(probs.values())
        out, a = [], 0.0
        for c in ALPHABET:
            b = a + probs[c] / total
            out.append((c, a, b))
            a = b
        out[-1] = (out[-1][0], out[-1][1], 1.0)
        self._cache[ctx] = out
        return out


def load_model(lang: str = "cs") -> LanguageModel:
    text = SAMPLE_CS if lang == "cs" else SAMPLE_EN
    path = os.path.join(APP_DIR, "data", f"corpus_{lang}.txt")      # vlastni korpus = lepsi psani
    try:
        with open(path, encoding="utf-8") as f:
            text += "\n" + f.read()
    except OSError:
        pass
    return LanguageModel(text)


class DasherEngine:
    """Cista logika (bez GUI): viewport [v0, v1] uvnitr intervalu aktualniho uzlu."""

    def __init__(self, model: LanguageModel, speed: float = 1.6):
        self.model, self.speed = model, speed
        self.text = ""
        self.v0, self.v1 = 0.0, 1.0
        self.stack: List[Tuple[float, float]] = []
        self.on_commit = None            # callback(znak)
        self.on_delete = None            # callback()

    def children(self):
        return self.model.children(self.text)

    def step(self, px: float, py: float, dt: float):
        """px, py = poloha ukazovatka 0..1 v okne (py 0 = nahore)."""
        w = self.v1 - self.v0
        z = (px - CROSS_X) / (1.0 - CROSS_X)             # -0.67 .. 1
        s = math.exp(-self.speed * z * dt)               # <1 priblizuje, >1 oddaluje
        s = min(max(s, 0.5), 1.0 / 0.5)
        ty = self.v0 + py * w                            # bod pod ukazovatkem
        f = 0.5 + (py - 0.5) * s                         # kam se ma posunout na obrazovce
        nw = w * s
        self.v0 = ty - f * nw
        self.v1 = self.v0 + nw
        self._settle()

    def _settle(self):
        for _ in range(8):
            # 1) cely viewport uvnitr potomka -> napsat znak
            for ch, a, b in self.children():
                if a <= self.v0 and self.v1 <= b:
                    self.text += ch
                    self.stack.append((a, b))
                    span = b - a
                    self.v0, self.v1 = (self.v0 - a) / span, (self.v1 - a) / span
                    if self.on_commit:
                        self.on_commit(ch)
                    break
            else:
                # 2) viewport presahuje uzel -> smazat posledni znak
                if (self.v0 < 0.0 or self.v1 > 1.0) and self.stack and self.v1 - self.v0 > 1.0:
                    a, b = self.stack.pop()
                    self.text = self.text[:-1]
                    span = b - a
                    self.v0, self.v1 = a + self.v0 * span, a + self.v1 * span
                    if self.on_delete:
                        self.on_delete()
                    continue
                return

    def reset(self):
        self.text = ""
        self.v0, self.v1 = 0.0, 1.0
        self.stack.clear()


# ------------------------------ okno --------------------------------------
_COLORS = ["#d9e8ff", "#ffe3d9", "#e0f5d6", "#fff4cc", "#f0dcff", "#d6f5f2"]


class DasherWindow:
    W, H = 760, 460

    def __init__(self, root, cfg):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.cfg = tk, cfg
        win = self.win = tk.Toplevel(root)
        win.title("Psaní hlavou – Dasher")
        self.engine = DasherEngine(load_model(cfg.dasher_lang), cfg.dasher_speed)
        self.engine.on_commit = self._commit
        self.engine.on_delete = self._delete
        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")
        self.out = tk.Text(top, height=2, font=("Segoe UI", 16), wrap="word")
        self.out.pack(fill="x")
        bar = ttk.Frame(win, padding=(8, 0))
        bar.pack(fill="x")
        self.send_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Psát také do aktivního okna", variable=self.send_var
                        ).pack(side="left")
        ttk.Button(bar, text="Kopírovat", command=self.copy).pack(side="left", padx=8)
        ttk.Button(bar, text="Vymazat", command=self.clear).pack(side="left")
        ttk.Label(bar, text="  kurzor VPRAVO od čáry = psát, VLEVO = mazat, MIMO okno = pauza"
                  ).pack(side="left", padx=10)
        self.cv = tk.Canvas(win, width=self.W, height=self.H, bg="#101418", highlightthickness=0)
        self.cv.pack(padx=8, pady=8)
        self.last = time.time()
        self.alive = True
        win.protocol("WM_DELETE_WINDOW", self.close)
        self.tick()

    def _commit(self, ch):
        self.out.insert("end", ch)
        if self.send_var.get():
            from . import winapi
            winapi.type_text(ch)

    def _delete(self):
        try:
            self.out.delete("end-2c", "end-1c")
        except Exception:
            pass
        if self.send_var.get():
            from . import winapi
            winapi.press_backspace()

    def copy(self):
        self.win.clipboard_clear()
        self.win.clipboard_append(self.engine.text)

    def clear(self):
        self.engine.reset()
        self.out.delete("1.0", "end")

    def close(self):
        self.alive = False
        self.win.destroy()

    def _draw(self):
        cv, W, H = self.cv, self.W, self.H
        cv.delete("all")
        e = self.engine
        span = e.v1 - e.v0
        cv.create_line(W * CROSS_X, 0, W * CROSS_X, H, fill="#345", dash=(4, 4))
        cv.create_oval(W * CROSS_X - 5, H / 2 - 5, W * CROSS_X + 5, H / 2 + 5, outline="#f66")
        for i, (ch, a, b) in enumerate(e.children()):
            y0, y1 = (a - e.v0) / span * H, (b - e.v0) / span * H
            if y1 < 0 or y0 > H or y1 - y0 < 3:
                continue
            col = _COLORS[ord(ch) % len(_COLORS)] if ch != " " else "#2b3340"
            cv.create_rectangle(W * 0.45, max(y0, 0), W - 2, min(y1, H), fill=col, outline="#101418")
            size = int(max(9, min(60, (y1 - y0) * 0.5)))
            yc = min(max((y0 + y1) / 2, size), H - size)
            cv.create_text(W * 0.45 + 12, yc, anchor="w", text="␣" if ch == " " else ch,
                           font=("Segoe UI", size, "bold"), fill="#0b0f14" if ch != " " else "#9ab")
            # druha uroven (co nasleduje) - napoveda
            if y1 - y0 > 90:
                sub = e.model.children(e.text + ch)
                for (c2, a2, b2) in sub:
                    yy0, yy1 = y0 + a2 * (y1 - y0), y0 + b2 * (y1 - y0)
                    if yy1 - yy0 < 14:
                        continue
                    cv.create_text(W * 0.78, (yy0 + yy1) / 2, text="␣" if c2 == " " else c2,
                                   fill="#334", font=("Segoe UI", int(min(22, (yy1 - yy0) * 0.6))))

    def tick(self):
        if not self.alive:
            return
        now = time.time()
        dt = min(0.1, now - self.last)
        self.last = now
        px, py = self.win.winfo_pointerxy()
        cx, cy = self.cv.winfo_rootx(), self.cv.winfo_rooty()
        fx, fy = (px - cx) / self.W, (py - cy) / self.H
        if 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0:
            self.engine.speed = self.cfg.dasher_speed
            self.engine.step(fx, fy, dt)
        self._draw()
        self.win.after(16, self.tick)
