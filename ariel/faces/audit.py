"""Auditni log (data/faces/audit.log): kdo/co/kdy. Nikdy neobsahuje biometricka data, jen pseudonymni ID."""
import json
from datetime import datetime, timezone
from pathlib import Path


def audit(data_dir: Path, action: str, *ids, **info):
    line = {"t": datetime.now(timezone.utc).replace(microsecond=0).isoformat(), "a": action}
    if ids:
        line["ids"] = list(ids)
    line.update(info)
    try:
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        with open(Path(data_dir) / "audit.log", "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass
