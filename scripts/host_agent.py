import json
import os
import re
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
STATE_DIR = ROOT / "data" / "state"
HOST_AGENT_VERSION = "1.2"
INSTALL_JOBS = {}
INSTALL_LOCK = threading.Lock()

def load_env():
    out = {}
    try:
        for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    except OSError:
        pass
    return out

def token():
    return load_env().get("HOST_AGENT_TOKEN", "")

def run(args, timeout=20, max_output=12000):
    proc = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True,
        timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    stdout = proc.stdout if max_output is None else proc.stdout[-max_output:]
    stderr = proc.stderr if max_output is None else proc.stderr[-max_output:]
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": stdout.strip(),
        "stderr": stderr.strip(),
    }

def tailscale_exe():
    candidates = [
        Path(r"C:\Program Files\Tailscale\tailscale.exe"),
        Path(r"C:\Program Files (x86)\Tailscale\tailscale.exe"),
    ]
    for item in candidates:
        if item.exists():
            return str(item)
    return "tailscale"

def tailscale_status():
    exe = tailscale_exe()
    status = run([exe, "status", "--json"], max_output=1_000_000)
    serve = run([exe, "serve", "status"])
    serve_json = run([exe, "serve", "status", "--json"], max_output=200_000)
    parsed = {}
    routes = {}
    if status["ok"]:
        try:
            parsed = json.loads(status["stdout"])
        except json.JSONDecodeError:
            parsed = {}
    if serve_json["ok"]:
        try:
            routes = json.loads(serve_json["stdout"])
        except json.JSONDecodeError:
            routes = {}
    self_info = parsed.get("Self", {}) if isinstance(parsed, dict) else {}
    dns_name = str(self_info.get("DNSName", "")).rstrip(".")
    ips = self_info.get("TailscaleIPs") or parsed.get("TailscaleIPs", []) if isinstance(parsed, dict) else []
    web = routes.get("Web", {}) if isinstance(routes, dict) else {}
    allow_funnel = routes.get("AllowFunnel", {}) if isinstance(routes, dict) else {}
    dashboard_key = f"{dns_name}:8443" if dns_name else ""
    mcp_key = f"{dns_name}:10000" if dns_name else ""
    legacy_key = f"{dns_name}:443" if dns_name else ""
    dashboard_enabled = dashboard_key in web
    mcp_enabled = mcp_key in web
    mcp_public = bool(allow_funnel.get(mcp_key))
    legacy_443 = legacy_key in web
    return {
        "installed": status["code"] != 9009,
        "online": bool(self_info.get("Online", status["ok"])),
        "dns_name": dns_name,
        "tailscale_ips": ips,
        "serve_status": serve["stdout"] or serve["stderr"],
        "status_error": None if status["ok"] else status["stderr"] or status["stdout"],
        "dashboard_url": f"https://{dns_name}:8443/" if dns_name and dashboard_enabled else None,
        "mcp_url": f"https://{dns_name}:10000/mcp" if dns_name and mcp_enabled else None,
        "dashboard_enabled": dashboard_enabled,
        "mcp_mode": "public" if mcp_public else ("private" if mcp_enabled else "off"),
        "legacy_443": legacy_443,
        "route_state": routes,
        "host_agent_version": HOST_AGENT_VERSION,
    }

def configure_tailscale(payload):
    exe = tailscale_exe()
    dashboard = bool(payload.get("dashboard_enabled", True))
    mcp_mode = str(payload.get("mcp_mode", "public")).lower()
    if mcp_mode not in {"public", "private", "off"}:
        raise ValueError("mcp_mode must be public, private or off")
    results = []
    if dashboard:
        results.append(run([exe, "serve", "--https=8443", "--bg", "--yes", "3000"]))
    else:
        results.append(run([exe, "serve", "--https=8443", "off"]))
    if mcp_mode == "public":
        results.append(run([exe, "funnel", "--https=10000", "--bg", "--yes", "8765"]))
    elif mcp_mode == "private":
        results.append(run([exe, "funnel", "--https=10000", "off"]))
        results.append(run([exe, "serve", "--https=10000", "--bg", "--yes", "8765"]))
    else:
        results.append(run([exe, "funnel", "--https=10000", "off"]))
        results.append(run([exe, "serve", "--https=10000", "off"]))
    if payload.get("clear_legacy_443"):
        results.append(run([exe, "serve", "--https=443", "off"]))
    failed = [r for r in results if not r["ok"]]
    return {"ok": not failed, "steps": results, "status": tailscale_status()}

MODEL_KEYS = {
    "llm": ["LLM_MODEL", "LLM_CONTEXT", "LLM_GPU_LAYERS"],
    "reasoning": ["REASONING_MODEL", "REASONING_CONTEXT", "REASONING_GPU_LAYERS"],
    "stt": ["STT_MODEL", "STT_COMPUTE_TYPE"],
    "tts": ["TTS_MODEL"],
    "vlm": ["VLM_MODEL"],
}
DEFAULTS = {
    "LLM_MODEL": "/models/daily.gguf", "LLM_CONTEXT": "16384", "LLM_GPU_LAYERS": "999",
    "REASONING_MODEL": "/models/reasoning.gguf", "REASONING_CONTEXT": "8192",
    "REASONING_GPU_LAYERS": "28", "STT_MODEL": "large-v3",
    "STT_COMPUTE_TYPE": "float16", "TTS_MODEL": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    "VLM_MODEL": "Qwen/Qwen3-VL-4B-Instruct",
}

def model_config():
    env = load_env()
    return {
        service: {key: env.get(key, DEFAULTS.get(key, "")) for key in keys}
        for service, keys in MODEL_KEYS.items()
    }

def model_inventory():
    inventory = {"llm": []}
    llm_root = ROOT / "models" / "llm"
    if llm_root.exists():
        for path in sorted(llm_root.glob("*.gguf"), key=lambda item: item.name.lower()):
            try:
                size_gib = round(path.stat().st_size / 1024 / 1024 / 1024, 3)
            except OSError:
                size_gib = None
            inventory["llm"].append({
                "name": path.name,
                "config_path": "/models/" + path.name,
                "size_gib": size_gib,
            })
    return inventory

def update_env(updates):
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    output = []
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    for key, value in remaining.items():
        output.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(output) + "\n", encoding="utf-8")

def configure_model(payload):
    service = str(payload.get("service", "")).lower()
    if service not in MODEL_KEYS:
        raise ValueError("unsupported service")
    values = payload.get("values", {})
    if not isinstance(values, dict):
        raise ValueError("values must be an object")
    allowed = set(MODEL_KEYS[service])
    updates = {}
    for key, value in values.items():
        if key not in allowed:
            continue
        text = str(value).strip()
        if not text:
            raise ValueError(f"{key} cannot be empty")
        if key.endswith("_CONTEXT") or key.endswith("_GPU_LAYERS"):
            number = int(text)
            if number < 0 or number > 1048576:
                raise ValueError(f"{key} is out of range")
        updates[key] = text
    if not updates:
        raise ValueError("no supported settings supplied")
    update_env(updates)
    recreate = bool(payload.get("recreate", True))
    compose = None
    if recreate:
        compose = run([
            "docker", "compose", "--profile", "gpu", "create",
            "--force-recreate", service,
        ], timeout=600)
    return {"ok": not recreate or bool(compose and compose["ok"]), "config": model_config(), "compose": compose}

def install_target(name):
    mapping = {
        "llm": ROOT / "models" / "llm",
        "stt": ROOT / "models" / "stt",
        "vlm": ROOT / "models" / "vlm",
        "tts": ROOT / "models" / "tts",
        "image": ROOT / "models" / "image",
        "video": ROOT / "models" / "wangp",
    }
    if name not in mapping:
        raise ValueError("unsupported target")
    return mapping[name]

def start_install(payload):
    if not shutil.which("hf"):
        raise ValueError("Hugging Face CLI ('hf') is not installed or not on PATH")
    repo = str(payload.get("repo", "")).strip()
    filename = str(payload.get("filename", "")).strip()
    target_name = str(payload.get("target", "")).strip().lower()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("repo must look like owner/name")
    if filename and (".." in filename or filename.startswith(("/", "\\"))):
        raise ValueError("invalid filename")
    target = install_target(target_name)
    target.mkdir(parents=True, exist_ok=True)
    job_id = f"hf-{int(time.time())}-{os.getpid()}-{len(INSTALL_JOBS)+1}"
    log_path = STATE_DIR / f"{job_id}.log"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    args = ["hf", "download", repo]
    if filename:
        args.append(filename)
    args += ["--local-dir", str(target)]
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        args, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    with INSTALL_LOCK:
        INSTALL_JOBS[job_id] = {
            "process": proc, "log": log, "log_path": str(log_path),
            "repo": repo, "filename": filename, "target": target_name,
            "started_at": time.time(),
        }
    return install_status(job_id)

def install_status(job_id=None):
    with INSTALL_LOCK:
        items = INSTALL_JOBS.items() if job_id is None else [(job_id, INSTALL_JOBS.get(job_id))]
        out = []
        for jid, job in items:
            if not job:
                continue
            code = job["process"].poll()
            if code is not None and not job["log"].closed:
                job["log"].flush()
                job["log"].close()
            try:
                tail = Path(job["log_path"]).read_text(encoding="utf-8", errors="replace")[-5000:]
            except OSError:
                tail = ""
            state = "running" if code is None else ("complete" if code == 0 else "failed")
            progress_matches = re.findall(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)%", tail)
            progress_percent = float(progress_matches[-1]) if progress_matches else None
            if state == "complete":
                progress_percent = 100.0
            progress_lines = [
                line.strip() for line in re.split(r"[\r\n]+", tail)
                if "%" in line and line.strip()
            ]
            progress_text = progress_lines[-1][-500:] if progress_lines else ""
            out.append({
                "id": jid, "repo": job["repo"], "filename": job["filename"],
                "target": job["target"], "started_at": job["started_at"],
                "state": state, "exit_code": code, "log_tail": tail,
                "elapsed_s": round(time.time() - job["started_at"], 1),
                "progress_percent": progress_percent,
                "progress_text": progress_text,
            })
        return out[0] if job_id and out else (out if not job_id else None)

class Handler(BaseHTTPRequestHandler):
    server_version = "LocalAIHostAgent/" + HOST_AGENT_VERSION
    def log_message(self, fmt, *args):
        return
    def send_json(self, code, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
    def authorized(self):
        expected = token()
        supplied = self.headers.get("X-Host-Agent-Token", "")
        return bool(expected) and supplied == expected
    def body(self):
        length = min(int(self.headers.get("Content-Length", "0") or 0), 1024 * 1024)
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self.send_json(200, {
                "status": "ok",
                "version": HOST_AGENT_VERSION,
                "hf_cli": bool(shutil.which("hf")),
                "tailscale": Path(tailscale_exe()).exists() or tailscale_exe() == "tailscale",
            })
        if not self.authorized():
            return self.send_json(401, {"detail": "unauthorized"})
        try:
            if path == "/tailscale/status":
                return self.send_json(200, tailscale_status())
            if path == "/models/config":
                return self.send_json(200, {
                    "config": model_config(),
                    "inventory": model_inventory(),
                    "installs": install_status(),
                    "installer_available": bool(shutil.which("hf")),
                    "hf_cli": shutil.which("hf"),
                })
            if path.startswith("/models/install/"):
                job_id = path.rsplit("/", 1)[-1]
                result = install_status(job_id)
                return self.send_json(200 if result else 404, result or {"detail": "unknown install"})
            return self.send_json(404, {"detail": "not found"})
        except Exception as exc:
            return self.send_json(500, {"detail": str(exc)[:2000]})
    def do_POST(self):
        path = urlparse(self.path).path
        if not self.authorized():
            return self.send_json(401, {"detail": "unauthorized"})
        try:
            payload = self.body()
            if path == "/tailscale/configure":
                return self.send_json(200, configure_tailscale(payload))
            if path == "/models/config":
                return self.send_json(200, configure_model(payload))
            if path == "/models/install":
                return self.send_json(202, start_install(payload))
            return self.send_json(404, {"detail": "not found"})
        except ValueError as exc:
            return self.send_json(400, {"detail": str(exc)})
        except Exception as exc:
            return self.send_json(500, {"detail": str(exc)[:2000]})

def main():
    if not token():
        raise SystemExit("HOST_AGENT_TOKEN is missing from .env")
    server = ThreadingHTTPServer(("0.0.0.0", 8788), Handler)
    print("Local AI host agent listening on :8788", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
