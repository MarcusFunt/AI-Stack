import base64
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from env_utils import load_env_file

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / ".env"
STATE_DIR = ROOT / "data" / "state"
HOST_AGENT_VERSION = "1.4"
INSTALL_JOBS = {}
INSTALL_LOCK = threading.Lock()
OPERATION_DIR = STATE_DIR / "operations"
OPERATION_PROCS = {}
OPERATION_LOCK = threading.Lock()

def load_env():
    return load_env_file(ENV_PATH)

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

def read_text_tail(path, max_chars=12000):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[-max_chars:]
    except OSError:
        return ""

def write_json_atomic(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp.replace(path)

def opencode_exe():
    return shutil.which("opencode.cmd") or shutil.which("opencode")

def opencode_status():
    exe = opencode_exe()
    version = ""
    version_error = ""
    if exe:
        result = run([exe, "--version"], timeout=8, max_output=2000)
        version = (result["stdout"] or result["stderr"]).strip()
        if not result["ok"]:
            version_error = result["stderr"] or result["stdout"]

    env = load_env()
    password = env.get("OPENCODE_SERVER_PASSWORD", "")
    server_running = False
    server_info = {}
    server_error = ""
    if password:
        raw = f"opencode:{password}".encode("utf-8")
        header = "Basic " + base64.b64encode(raw).decode("ascii")
        request = Request(
            "http://127.0.0.1:4096/api/info",
            headers={"Authorization": header},
        )
        try:
            with urlopen(request, timeout=0.75) as response:
                server_info = json.loads(response.read().decode("utf-8"))
                server_running = True
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            server_error = str(exc)

    return {
        "installed": bool(exe),
        "executable": exe,
        "version": version,
        "version_error": version_error,
        "config_present": (ROOT / "opencode.jsonc").exists(),
        "server_running": server_running,
        "server_url": "http://127.0.0.1:4096",
        "server_info": server_info,
        "server_error": server_error,
        "log_tail": read_text_tail(STATE_DIR / "opencode-server.log", 8000),
        "error_tail": read_text_tail(STATE_DIR / "opencode-server-error.log", 8000),
    }

def opencode_control(action):
    if action not in {"start", "stop"}:
        raise ValueError("unsupported OpenCode action")
    script = ROOT / "scripts" / "opencode.ps1"
    script_action = "serve" if action == "start" else "stop-server"
    control_log = STATE_DIR / "opencode-control.log"
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(control_log, "a", encoding="utf-8") as log:
        proc = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script), script_action,
            ],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    if proc.returncode != 0:
        detail = read_text_tail(control_log, 4000) or "OpenCode command failed"
        raise RuntimeError(detail[-4000:])
    return {
        "ok": True,
        "action": action,
        "result": {"ok": True, "code": proc.returncode, "log_tail": read_text_tail(control_log, 4000)},
        "status": opencode_status(),
    }

def operation_meta_path(job_id):
    return OPERATION_DIR / f"{job_id}.json"

def operation_log_path(job_id):
    return OPERATION_DIR / f"{job_id}.log"

def load_operation(job_id):
    path = operation_meta_path(job_id)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None

def save_operation(meta):
    write_json_atomic(operation_meta_path(meta["id"]), meta)

def public_operation(meta):
    if not meta:
        return None
    result = {k: v for k, v in meta.items() if k != "log_path"}
    result["log_tail"] = read_text_tail(meta.get("log_path") or operation_log_path(meta["id"]), 16000)
    return result

def _finish_operation(job_id):
    with OPERATION_LOCK:
        proc = OPERATION_PROCS.get(job_id)
    if proc is None:
        return
    code = proc.wait()
    with OPERATION_LOCK:
        meta = load_operation(job_id) or {"id": job_id}
        meta["state"] = "complete" if code == 0 else "failed"
        meta["exit_code"] = code
        meta["finished_at"] = time.time()
        save_operation(meta)
        OPERATION_PROCS.pop(job_id, None)

def operation_status(job_id):
    if not re.fullmatch(r"op-[a-f0-9]{12}", str(job_id)):
        return None
    with OPERATION_LOCK:
        proc = OPERATION_PROCS.get(job_id)
        meta = load_operation(job_id)
        if not meta:
            return None
        if proc is not None:
            code = proc.poll()
            if code is not None and meta.get("state") == "running":
                meta["state"] = "complete" if code == 0 else "failed"
                meta["exit_code"] = code
                meta["finished_at"] = time.time()
                save_operation(meta)
                OPERATION_PROCS.pop(job_id, None)
        return public_operation(meta)

def list_operations(limit=20):
    OPERATION_DIR.mkdir(parents=True, exist_ok=True)
    metas = []
    for path in OPERATION_DIR.glob("op-*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, dict):
            metas.append(meta)
    metas.sort(key=lambda item: float(item.get("started_at", 0)), reverse=True)
    return [operation_status(item["id"]) or public_operation(item) for item in metas[:max(1, min(int(limit), 100))]]

def rollback_snapshots():
    snapshots = []
    for path in sorted(STATE_DIR.glob("update-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            continue
        snapshots.append({
            "file": path.name,
            "timestamp": data.get("timestamp") or path.stem.removeprefix("update-"),
            "created_at": path.stat().st_mtime,
            "comfyui_sha": ((data.get("comfyui") or {}).get("sha")),
            "wangp_sha": ((data.get("wangp") or {}).get("sha")),
            "images": sorted((data.get("rollback_images") or {}).keys()),
        })
    return snapshots[:50]

def _operation_args(action, payload):
    ai = ROOT / "scripts" / "ai.ps1"
    if action == "burn-in":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ai), "burn-in"], None
    if action == "update":
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ai), "update"], None
    if action == "opencode-smoke":
        script = ROOT / "scripts" / "opencode-smoke.ps1"
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)], None
    if action == "rollback":
        snapshot_name = str(payload.get("snapshot", "")).strip()
        snapshot_path = None
        if snapshot_name:
            if Path(snapshot_name).name != snapshot_name or not re.fullmatch(r"update-[0-9]{8}-[0-9]{6}\.json", snapshot_name):
                raise ValueError("invalid rollback snapshot")
            snapshot_path = STATE_DIR / snapshot_name
            if not snapshot_path.exists():
                raise ValueError("rollback snapshot not found")
        args = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ai), "rollback"]
        if snapshot_path:
            args += ["-Snapshot", str(snapshot_path)]
        return args, snapshot_name or None
    raise ValueError("unsupported maintenance action")

def start_operation(payload):
    action = str(payload.get("action", "")).strip().lower()
    args, snapshot = _operation_args(action, payload)
    with OPERATION_LOCK:
        for proc in OPERATION_PROCS.values():
            if proc.poll() is None:
                raise ValueError("another maintenance operation is already running")

        job_id = "op-" + uuid.uuid4().hex[:12]
        OPERATION_DIR.mkdir(parents=True, exist_ok=True)
        log_path = operation_log_path(job_id)
        log = open(log_path, "w", encoding="utf-8")
        proc = subprocess.Popen(
            args,
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        meta = {
            "id": job_id,
            "action": action,
            "state": "running",
            "started_at": time.time(),
            "finished_at": None,
            "exit_code": None,
            "pid": proc.pid,
            "snapshot": snapshot,
            "log_path": str(log_path),
        }
        save_operation(meta)
        OPERATION_PROCS[job_id] = proc

    def waiter():
        try:
            _finish_operation(job_id)
        finally:
            try:
                log.close()
            except OSError:
                pass

    threading.Thread(target=waiter, daemon=True).start()
    return operation_status(job_id)

def recover_operations():
    OPERATION_DIR.mkdir(parents=True, exist_ok=True)
    for path in OPERATION_DIR.glob("op-*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(meta, dict) and meta.get("state") == "running":
            meta["state"] = "unknown"
            meta["finished_at"] = time.time()
            meta["note"] = "host agent restarted while the operation was running"
            save_operation(meta)

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
    comfyui_key = f"{dns_name}:8444" if dns_name else ""
    wangp_key = f"{dns_name}:8445" if dns_name else ""
    mcp_key = f"{dns_name}:10000" if dns_name else ""
    legacy_key = f"{dns_name}:443" if dns_name else ""
    dashboard_enabled = dashboard_key in web
    comfyui_enabled = comfyui_key in web
    wangp_enabled = wangp_key in web
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
        "comfyui_url": f"https://{dns_name}:8444/" if dns_name and comfyui_enabled else None,
        "wangp_url": f"https://{dns_name}:8445/" if dns_name and wangp_enabled else None,
        "mcp_url": f"https://{dns_name}:10000/mcp" if dns_name and mcp_enabled else None,
        "dashboard_enabled": dashboard_enabled,
        "studio_enabled": comfyui_enabled and wangp_enabled,
        "studio_routes": {"comfyui": comfyui_enabled, "wangp": wangp_enabled},
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
    studio_enabled = payload.get("studio_enabled")
    if studio_enabled is not None:
        if bool(studio_enabled):
            results.append(run([exe, "serve", "--https=8444", "--bg", "--yes", "8189"]))
            results.append(run([exe, "serve", "--https=8445", "--bg", "--yes", "7870"]))
        else:
            results.append(run([exe, "serve", "--https=8444", "off"]))
            results.append(run([exe, "serve", "--https=8445", "off"]))
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
    "LLM_MODEL": "/models/daily.gguf", "LLM_CONTEXT": "32768", "LLM_GPU_LAYERS": "999",
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
            if path == "/opencode/status":
                return self.send_json(200, opencode_status())
            if path == "/operations":
                return self.send_json(200, {"operations": list_operations()})
            if path.startswith("/operations/"):
                job_id = path.rsplit("/", 1)[-1]
                result = operation_status(job_id)
                return self.send_json(200 if result else 404, result or {"detail": "unknown operation"})
            if path == "/maintenance/snapshots":
                return self.send_json(200, {"snapshots": rollback_snapshots()})
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
            if path == "/opencode/start":
                return self.send_json(200, opencode_control("start"))
            if path == "/opencode/stop":
                return self.send_json(200, opencode_control("stop"))
            if path == "/operations/start":
                return self.send_json(202, start_operation(payload))
            return self.send_json(404, {"detail": "not found"})
        except ValueError as exc:
            return self.send_json(400, {"detail": str(exc)})
        except Exception as exc:
            return self.send_json(500, {"detail": str(exc)[:2000]})

def main():
    if not token():
        raise SystemExit("HOST_AGENT_TOKEN is missing from .env")
    recover_operations()
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
