import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AI_PS1 = os.path.join(ROOT, "scripts", "ai.ps1")
with open(os.path.join(ROOT, ".env"), encoding="utf-8") as f:
    API_KEY = next(
        line.split("=", 1)[1].strip()
        for line in f
        if line.startswith("AI_API_KEY=")
    )
BASE = "http://127.0.0.1:8090"
AUTH = {"Authorization": f"Bearer {API_KEY}"}

def request(method, url, data=None, headers=None, timeout=600):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, response.headers, response.read()

def api(method, path, data=None, headers=None, timeout=600):
    merged = dict(AUTH)
    if headers:
        merged.update(headers)
    return request(method, BASE + path, data, merged, timeout)

def json_api(method, path, payload=None, timeout=600):
    data = None if payload is None else json.dumps(payload).encode()
    status, _, body = api(
        method, path, data,
        {"Content-Type": "application/json"},
        timeout,
    )
    return status, json.loads(body)
def multipart(fields, files):
    boundary = "----ai-stack-" + uuid.uuid4().hex
    parts = []
    for name, value in fields.items():
        parts += [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode(),
            b"\r\n",
        ]
    for name, filename, content_type, content in files:
        parts += [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
        ]
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"

def start(service):
    code, result = json_api("POST", f"/control/start/{service}")
    assert code == 200, result
    return result

def checkpoint(name, detail="OK"):
    print(f"[PASS] {name}: {detail}", flush=True)
try:
    status, _, body = request("GET", BASE + "/health", timeout=10)
    assert status == 200 and json.loads(body)["status"] == "ok"
    checkpoint("control-plane")

    try:
        request("GET", BASE + "/v1/models", timeout=10)
        raise AssertionError("unauthenticated request unexpectedly succeeded")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
    checkpoint("gateway-auth")

    status, models = json_api("GET", "/v1/models")
    assert status == 200 and len(models["data"]) >= 7
    checkpoint("model-registry", f'{len(models["data"])} logical models')

    payload = {
        "model": "local-fast",
        "messages": [{"role": "user", "content": "Reply with exactly SMOKE_OK"}],
        "max_tokens": 16,
        "temperature": 0,
    }
    status, result = json_api("POST", "/v1/chat/completions", payload)
    assert status == 200 and result["choices"]
    checkpoint("llm-buffered")

    status, control = json_api("GET", "/control/status")
    assert status == 200 and "idle_stop_in_seconds" in control
    assert control["idle_stop_in_seconds"].get("llm", 0) > 0
    checkpoint("idle-shutdown-scheduled")

    payload["stream"] = True
    raw = json.dumps(payload).encode()
    status, _, stream_body = api(
        "POST", "/v1/chat/completions", raw,
        {"Content-Type": "application/json"},
    )
    assert status == 200 and b"data:" in stream_body
    checkpoint("llm-streaming")
    tts_payload = {
        "model": "qwen3-tts-base",
        "input": "Local AI stack smoke test.",
        "voice": "qwen-default",
        "response_format": "mp3",
    }
    status, _, audio = api(
        "POST", "/v1/audio/speech",
        json.dumps(tts_payload).encode(),
        {"Content-Type": "application/json"},
    )
    assert status == 200 and len(audio) > 1000
    checkpoint("tts", f"{len(audio)} bytes")

    form, content_type = multipart(
        {"model": "large-v3", "response_format": "json"},
        [("file", "smoke.mp3", "audio/mpeg", audio)],
    )
    status, _, stt_body = api(
        "POST", "/v1/audio/transcriptions", form,
        {"Content-Type": content_type},
    )
    stt = json.loads(stt_body)
    assert status == 200 and stt.get("text", "").strip()
    checkpoint("stt", stt["text"].strip()[:80])

    image_path = os.path.join(ROOT, "data", "input", "vlm-smoke.png")
    with open(image_path, "rb") as f:
        image = f.read()
    form, content_type = multipart(
        {
            "prompt": "Describe the colored shapes and their approximate locations.",
            "max_new_tokens": 128,
        },
        [("image", "vlm-smoke.png", "image/png", image)],
    )
    status, _, vlm_body = api(
        "POST", "/v1/vision/analyze", form,
        {"Content-Type": content_type},
    )
    vlm = json.loads(vlm_body)
    assert status == 200 and vlm.get("text")
    checkpoint("vlm", str(vlm["text"])[:100])

    start("comfyui")
    status, _, comfy_body = api("GET", "/v1/comfy/system_stats", timeout=120)
    comfy = json.loads(comfy_body)
    assert status == 200 and comfy.get("devices")
    checkpoint("comfyui", comfy["devices"][0].get("name", "GPU detected"))

    start("wangp")
    deadline = time.time() + 180
    wan_status = None
    while time.time() < deadline:
        try:
            wan_status, _, _ = request(
                "GET", "http://127.0.0.1:7860/", timeout=5
            )
            if wan_status == 200:
                break
        except Exception:
            pass
        time.sleep(2)
    assert wan_status == 200
    checkpoint("wangp", "HTTP 200")
    print("ALL_SMOKE_TESTS_PASSED", flush=True)
finally:
    subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", AI_PS1,
            "stop-all",
        ],
        cwd=ROOT,
        check=False,
    )
