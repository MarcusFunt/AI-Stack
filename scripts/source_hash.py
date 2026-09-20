import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTEXTS = {
    "docker-control": ROOT / "docker-control",
    "supervisor": ROOT / "supervisor",
    "telemetry": ROOT / "telemetry",
    "gateway": ROOT / "gateway",
    "dashboard": ROOT / "dashboard",
    "mcp": ROOT / "mcp",
    "agent-lab": ROOT / "agent_lab",
    "agent-evaluator": ROOT / "agent_eval",
    "agent-eval-runner": ROOT / "agent_eval",
    "stt": ROOT / "stt",
    "vlm": ROOT / "vlm",
}
EXCLUDED_DIRS = {".git", "node_modules", "dist", "__pycache__"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".log"}

def source_hash(service):
    root = CONTEXTS[service]
    digest = hashlib.sha256()
    files = sorted(p for p in root.rglob("*") if p.is_file())
    for path in files:
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        digest.update(rel.as_posix().encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in CONTEXTS:
        raise SystemExit("usage: source_hash.py <" + "|".join(CONTEXTS) + ">")
    print(source_hash(sys.argv[1]))
