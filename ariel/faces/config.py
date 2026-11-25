"""Nastaveni modulu faces (data/faces/config.json). Vytvori se s vychozimi hodnotami pri prvnim spusteni."""
import json
from pathlib import Path

DEFAULTS = {
    # "lab"   = doma / test se souhlasem lidi: smi se ulozit maly sifrovany vyrez tvare pro kontrolu
    # "field" = venku: ZADNE fotky ani vyrezy, jen embeddingy, kratka retence
    "mode": "lab",
    "retention_hours": {"lab": 720, "field": 24},   # po teto dobe se zaznam sam smaze
    "threshold": 0.363,      # kosinova podobnost SFace >= threshold -> "pravdepodobne stejna osoba"
    "maybe": 0.30,           # threshold > podobnost >= maybe -> "mozna stejna osoba" (taky ke kontrole)
    "min_face_px": 64,       # mensi tvare se ignoruji (spatny embedding)
    "det_score": 0.85,       # minimalni jistota detektoru YuNet
    "min_sharpness": 40.0,   # rozmazane vyrezy (rozptyl Laplace) se ignoruji
    "camera_frames": 5,      # kolik dobrych snimku se zprumeruje do jednoho zaznamu
}


def load(data_dir: Path) -> dict:
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / "config.json"
    cfg = json.loads(json.dumps(DEFAULTS))
    if p.exists():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"[faces] config.json nejde precist ({e}), pouzivam vychozi hodnoty")
    else:
        p.write_text(json.dumps(DEFAULTS, indent=2, ensure_ascii=False), encoding="utf-8")
    if cfg["mode"] not in ("lab", "field"):
        print(f"[faces] neznamy rezim {cfg['mode']!r}, pouzivam 'field' (bezpecnejsi)")
        cfg["mode"] = "field"
    return cfg


def save(data_dir: Path, cfg: dict):
    (data_dir / "config.json").write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
