from pathlib import Path


def load_env_file(path):
    values = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def require_env_value(path, key):
    value = load_env_file(path).get(key, "")
    if not value:
        raise RuntimeError(f"{key} is missing from {path}")
    return value
