"""Prikazova radka:  python -m faces <prikaz>

  status                      rezim, pocty, cekajici kontroly
  enroll --camera eye|0|URL   zapis tvare z kamery (eye = XREAL Eye pres bezici Ariel)
  enroll SOUBOR|SLOZKA ...    zapis z fotek (jen lab; fotky se nekopiruji, ulozi se jen embedding)
  list                        seznam zaznamu (ID, cas, expirace, zdroj, priznak kontroly)
  dedupe                      porovna celou databazi samu se sebou
  review                      projde skupiny ke kontrole, rozhoduje clovek
  delete ID [ID ...]          smaze zaznamy
  purge                       smaze vse po retenci (deje se i samo pri kazdem spusteni)
  mode lab|field              prepne rezim (do field: smaze vsechny ulozene vyrezy)
  wipe                        smaze cely trezor i klic (nevratne)
  selftest                    otestuje trezor a porovnani na umelych datech (nepotrebuje kameru ani modely)
"""
import argparse
import sys
import time
from pathlib import Path

from . import FACES_DATA
from . import config as cfgmod

IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def _open(data_dir):
    from .vault import Vault
    return Vault(data_dir, cfgmod.load(data_dir))


def _engine(cfg):
    from .engine import FaceEngine
    return FaceEngine(cfg)


def cmd_status(a):
    from . import dedupe
    v = _open(a.data)
    print(f"Rezim: {v.cfg['mode']}   retence: {v.cfg['retention_hours'][v.cfg['mode']]} h")
    print(f"Zaznamu: {len(v.records)}   s vyrezem: {sum('thumb' in r for r in v.records)}")
    print(f"Ke kontrole: {len(dedupe.pending(v))} skupin")
    print(f"Trezor: {v.path}")


def _cam_source(s):
    if s == "eye":
        return "http://localhost:8080/eye.mjpg"
    return int(s) if s.isdigit() else s


def cmd_enroll(a):
    import cv2
    from . import dedupe
    from .engine import mean_embedding, thumb_jpg
    v = _open(a.data)
    lab = v.cfg["mode"] == "lab"
    if a.thumb and not lab:
        print("[X] --thumb jde jen v rezimu lab. Venku se fotky ani vyrezy neukladaji.")
        return 2
    eng = _engine(v.cfg)
    added = []

    if a.camera is not None:
        src = _cam_source(a.camera)
        cap = cv2.VideoCapture(src)
        if not cap.isOpened():
            print(f"[X] kamera {src!r} nejde otevrit" + (" (bezi Ariel? ariel.bat)" if a.camera == "eye" else ""))
            return 2
        want, got, t0 = v.cfg["camera_frames"], [], time.time()
        print(f"Divej se do kamery... sbiram {want} dobrych snimku (max {a.timeout} s)")
        while len(got) < want and time.time() - t0 < a.timeout:
            ok, frame = cap.read()
            if not ok:
                continue
            fs = eng.faces(frame)
            if len(fs) == 1 or (fs and a.largest):
                got.append(fs[0])
                print(f"  snimek {len(got)}/{want}  kvalita {fs[0].quality:.2f}")
            if a.preview:
                for f in fs:
                    x, y, w, h = f.bbox
                    cv2.rectangle(frame, (x, y), (x + w, y + h), (255, 255, 255), 2)
                cv2.imshow("Ariel - zapis tvare (Esc = konec)", frame)
                if cv2.waitKey(1) == 27:
                    break
            del frame                         # snimek jen v pameti
        cap.release()
        if a.preview:
            cv2.destroyAllWindows()
        if len(got) < max(2, want // 2):
            print("[X] malo dobrych snimku (v zaberu musi byt prave jedna ostra tvar, nebo pouzij --largest)")
            return 1
        best = max(got, key=lambda f: f.quality)
        rec = v.add(mean_embedding([f.emb for f in got]), src="camera",
                    quality=sum(f.quality for f in got) / len(got),
                    thumb_jpg=thumb_jpg(best.crop) if a.thumb else None, note=a.note or "")
        added.append(rec["id"])
        got.clear()
    else:
        if not lab:
            print("[X] zapis z fotek je jen v rezimu lab")
            return 2
        files = []
        for p in map(Path, a.paths):
            files += sorted(q for q in p.rglob("*") if q.suffix.lower() in IMG_EXT) if p.is_dir() else [p]
        if not files:
            print("[X] zadne obrazky")
            return 1
        for p in files:
            img = cv2.imread(str(p))
            fs = eng.faces(img)
            if not fs:
                print(f"  {p.name}: zadna pouzitelna tvar")
                continue
            for f in (fs if a.all else fs[:1]):
                rec = v.add(f.emb, src=f"file:{p.name}", quality=f.quality,
                            thumb_jpg=thumb_jpg(f.crop) if a.thumb else None, note=a.note or "")
                added.append(rec["id"])
            print(f"  {p.name}: {len(fs) if a.all else 1} tvar(i)")

    n = dedupe.run(v, only_ids=added)
    v.save()
    flagged = dedupe.flagged_ids(v) & set(added)
    print(f"Ulozeno {len(added)}: {' '.join(added)}")
    if flagged:
        print(f"[!] {len(flagged)} z nich se podoba nekomu v databazi -> ke kontrole ({n} skupin). Spust: review")
    return 0


def cmd_list(a):
    from . import dedupe
    v = _open(a.data)
    fl = dedupe.flagged_ids(v)
    if not v.records:
        print("(prazdne)")
    print(f"{'ID':10} {'zapsano':21} {'expiruje':21} {'rezim':6} {'kval':5} {'zdroj':24} K")
    for r in v.records:
        print(f"{r['id']:10} {r['t']:21} {r['exp']:21} {r['mode']:6} {r['q']:<5} "
              f"{r['src'][:24]:24} {'!' if r['id'] in fl else ''}{'' if 'thumb' not in r else ' (vyrez)'}"
              f"{'  ' + r['note'] if r.get('note') else ''}")


def cmd_dedupe(a):
    from . import dedupe
    v = _open(a.data)
    n = dedupe.run(v)
    v.save()
    print(f"Porovnano {len(v.records)} zaznamu ({len(v.records) * (len(v.records) - 1) // 2} paru).")
    print(f"Ke kontrole: {n} skupin" + (" -> spust: review" if n else ""))
    for rv in dedupe.pending(v):
        print(f"  {rv['id']}  {rv['level']:6}  {' '.join(rv['members'])}  max podobnost {rv['pairs'][0][2]}")


def _show(v, rv):
    """V rezimu lab ukaze vyrezy vedle sebe v okne (jen v pameti, nic se neuklada)."""
    import base64
    import cv2
    import numpy as np
    imgs = []
    for m in rv["members"]:
        r = v.get(m)
        if r and "thumb" in r:
            im = cv2.imdecode(np.frombuffer(base64.b64decode(r["thumb"]), np.uint8), cv2.IMREAD_COLOR)
            im = cv2.resize(im, (224, 224))
            cv2.putText(im, m, (6, 216), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
            imgs.append(im)
    if imgs:
        cv2.imshow(f"Kontrola {rv['id']}", np.hstack(imgs))
        cv2.waitKey(1)
        return True
    return False


def cmd_review(a):
    from . import dedupe
    v = _open(a.data)
    todo = dedupe.pending(v)
    if not todo:
        print("Nic ke kontrole.")
        return 0
    for rv in list(todo):
        print("-" * 60)
        print(f"Skupina {rv['id']}  ({'pravdepodobne' if rv['level'] == 'likely' else 'mozna'} stejna osoba)")
        for m in rv["members"]:
            r = v.get(m)
            print(f"  {m}  zapsano {r['t']}  zdroj {r['src']}  kvalita {r['q']}{'  ' + r['note'] if r.get('note') else ''}")
        for p in rv["pairs"]:
            print(f"    {p[0]} ~ {p[1]}  podobnost {p[2]}")
        shown = _show(v, rv) if v.cfg["mode"] == "lab" else False
        if not shown:
            print("  (bez vyrezu: rozhoduj podle casu, mista a poznamek)")
        c = input("[s] stejna osoba  [r] ruzne osoby  [p] preskocit  [q] konec : ").strip().lower()
        if c == "q":
            break
        if c == "s":
            keep = input(f"Ponechat jen jeden zaznam? zadej jeho ID ({rv['members'][0]}...), Enter = ponechat vsechny: ").strip()
            if keep and keep not in rv["members"]:
                print("  neplatne ID, ponechavam vse")
                keep = None
            n = dedupe.resolve(v, rv["id"], "same", keep=keep or None)
            print(f"  oznaceno: stejna osoba" + (f", smazano {n}" if n else ""))
        elif c == "r":
            dedupe.resolve(v, rv["id"], "different")
            print("  oznaceno: ruzne osoby (uz se nebudou hlasit)")
        v.save()
    if v.cfg["mode"] == "lab":
        try:
            import cv2
            cv2.destroyAllWindows()
        except Exception:
            pass
    return 0


def cmd_delete(a):
    v = _open(a.data)
    unknown = [i for i in a.ids if not v.get(i)]
    n = v.delete(a.ids)
    v.save()
    print(f"Smazano {n}." + (f" Neznama ID: {' '.join(unknown)}" if unknown else ""))


def cmd_purge(a):
    v = _open(a.data)              # purge probehne uz pri otevreni
    print(f"Hotovo, zbyva {len(v.records)} zaznamu.")


def cmd_mode(a):
    cfg = cfgmod.load(a.data)
    cfg["mode"] = a.mode
    cfgmod.save(a.data, cfg)
    v = _open(a.data)
    if a.mode == "field":
        n = v.drop_thumbs()
        v.save()
        print(f"Rezim field: smazano {n} vyrezu, nove zaznamy jen jako embedding, retence {cfg['retention_hours']['field']} h.")
    else:
        print(f"Rezim lab: retence {cfg['retention_hours']['lab']} h, vyrezy jen s --thumb.")


def cmd_wipe(a):
    from .vault import wipe
    if not a.yes and input("Smazat VSECHNY tvare i klic? Nevratne. Napis SMAZAT: ").strip() != "SMAZAT":
        print("Zruseno.")
        return 1
    wipe(a.data)
    print("Trezor i klic smazany.")


def cmd_selftest(a):
    import tempfile
    import numpy as np
    from . import dedupe
    from .vault import Vault, wipe
    rng = np.random.default_rng(7)
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        cfg = cfgmod.load(d)
        v = Vault(d, cfg)
        base = [rng.normal(size=128).astype(np.float32) for _ in range(6)]

        def noisy(e, s):
            x = e + rng.normal(scale=s, size=128).astype(np.float32)
            return x / np.linalg.norm(x)
        ids = [v.add(noisy(e, 0.01))["id"] for e in base]
        twin = v.add(noisy(base[2], 0.35))["id"]                 # stejna osoba jeste jednou
        triple = v.add(noisy(base[2], 0.35))["id"]               # a potreti
        n = dedupe.run(v)
        v.save()
        pend = dedupe.pending(v)
        assert n == 1 and set(pend[0]["members"]) == {ids[2], twin, triple}, pend
        v2 = Vault(d, cfg)                                       # znovu nacist ze sifrovaneho souboru
        assert len(v2.records) == 8 and len(dedupe.pending(v2)) == 1
        raw = (d / "faces.vault").read_bytes()
        assert v2.records[0]["emb"].encode() not in raw, "embedding je v souboru citelne!"
        dedupe.resolve(v2, pend[0]["id"], "same", keep=ids[2])
        v2.save()
        assert len(v2.records) == 6 and not dedupe.pending(v2)
        v2.add(noisy(base[0], 0.35)); dedupe.run(v2)
        rv = dedupe.pending(v2)[0]
        dedupe.resolve(v2, rv["id"], "different"); dedupe.run(v2)
        assert not dedupe.pending(v2), "zamitnuty par se hlasi znovu"
        cfg_f = dict(cfg, mode="field")
        v3 = Vault(d, cfg_f)
        try:
            v3.add(base[1], thumb_jpg=b"x")
            raise AssertionError("field ulozil vyrez!")
        except PermissionError:
            pass
        r = v3.add(base[1], src="file:jmeno.jpg")
        assert r["src"] == "file" and r["exp"] < v2.records[0]["exp"]
        r["exp"] = "2000-01-01T00:00:00Z"; v3.save()
        v4 = Vault(d, cfg_f)
        assert not v4.get(r["id"]), "retence nesmazala zaznam"
        wipe(d)
        assert not (d / "faces.vault").exists() and not (d / "vault.key").exists()
    print("selftest OK: sifrovani, porovnani, kontrola, mazani, retence, field bez fotek, wipe")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m faces", description="Ariel - tvare (jen embeddingy, sifrovane)")
    p.add_argument("--data", type=Path, default=FACES_DATA, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    e = sub.add_parser("enroll")
    e.add_argument("paths", nargs="*")
    e.add_argument("--camera", help="eye | 0 | URL")
    e.add_argument("--timeout", type=float, default=15)
    e.add_argument("--largest", action="store_true", help="vic tvari v zaberu: vezmi nejvetsi")
    e.add_argument("--all", action="store_true", help="z fotky zapsat vsechny tvare")
    e.add_argument("--thumb", action="store_true", help="lab: ulozit i maly sifrovany vyrez pro kontrolu")
    e.add_argument("--preview", action="store_true", help="ukazat obraz z kamery v okne")
    e.add_argument("--note", help="lab: poznamka (napr. jmeno dobrovolnika se souhlasem)")
    e.set_defaults(fn=cmd_enroll)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("dedupe").set_defaults(fn=cmd_dedupe)
    sub.add_parser("review").set_defaults(fn=cmd_review)
    d = sub.add_parser("delete")
    d.add_argument("ids", nargs="+")
    d.set_defaults(fn=cmd_delete)
    sub.add_parser("purge").set_defaults(fn=cmd_purge)
    m = sub.add_parser("mode")
    m.add_argument("mode", choices=["lab", "field"])
    m.set_defaults(fn=cmd_mode)
    w = sub.add_parser("wipe")
    w.add_argument("--yes", action="store_true")
    w.set_defaults(fn=cmd_wipe)
    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    a = p.parse_args(argv)
    if a.cmd == "enroll" and a.camera is None and not a.paths:
        p.error("enroll potrebuje --camera nebo soubory")
    try:
        return a.fn(a) or 0
    except (FileNotFoundError, ValueError, RuntimeError, PermissionError) as ex:
        print(f"[X] {ex}")
        return 2
