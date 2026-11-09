import os
import json
import math
from pathlib import Path


def finite(value, low, high):
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"Expected a value between {low} and {high}")
    return value


def motor_id(value):
    if isinstance(value, bool) or int(value) != float(value) or not 1 <= int(value) <= 254:
        raise ValueError("Motor ID must be an integer from 1 to 254")
    return int(value)


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DATA_DIR = Path(os.environ.get("UMI_DATA_DIR", ROOT / "data")).expanduser().resolve()
HTTP_PORT = int(os.environ.get("UMI_HTTP_PORT", "8080"))
HTTPS_PORT = int(os.environ.get("UMI_HTTPS_PORT", "8443"))


def prepare_data():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("settings.json", "tasks.json", "setup.json"):
        destination = DATA_DIR / name
        if not destination.exists():
            save_json(destination, load_json(ROOT / "config" / name))
