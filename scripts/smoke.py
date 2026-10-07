import argparse
import binascii
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_KEY = os.environ.get("AI_API_KEY", "").strip()
if not API_KEY:
    raise RuntimeError("AI_API_KEY must be present in the process environment; smoke.py does not read project environment files")
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

def _png_chunk(kind, payload):
    checksum = binascii.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

def visual_memory_fixture_png():
    width, height = 96, 64
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            if 8 <= y < 26:
                pixel = (205, 38, 52)
            elif 18 <= x < 43 and 37 <= y < 58:
                pixel = (38, 94, 184)
            elif (x // 8 + y // 8) % 2:
                pixel = (238, 242, 246)
            else:
                pixel = (255, 255, 255)
            row.extend(pixel)
        rows.append(bytes(row))
    header = struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
        + _png_chunk(b"IEND", b"")
    )

def visual_memory_smoke():
    namespace = "smoke:visual-memory"
    image = visual_memory_fixture_png()
    form, content_type = multipart(
        {
            "namespace": namespace,
            "source": "website",
            "crop_mode": "none",
            "vision_token_budget": "balanced",
        },
        [("image", "visual-memory-smoke.png", "image/png", image)],
    )
    status, _, indexed_body = api(
        "POST", "/v1/visual-memory/index", form,
        {"Content-Type": content_type}, timeout=600,
    )
    indexed = json.loads(indexed_body)
    assert status == 200 and indexed.get("frame_id"), indexed
    status, result = json_api("POST", "/v1/visual-memory/search/text", {
        "query": "red alert banner above a blue panel",
        "namespace": namespace,
        "top_k": 1,
        "include_crops": False,
    }, timeout=600)
    assert status == 200 and result.get("matches"), result
    assert result["matches"][0].get("id") == indexed["frame_id"], result
    checkpoint("visual-memory", "indexed the generated frame and found it by text")

def start(service):
    code, result = json_api("POST", f"/control/start/{service}")
    assert code == 200, result
    return result

def require_idle_gpu():
    status, control = json_api("GET", "/control/status", timeout=10)
    assert status == 200, control
    active_jobs = sum(int(value) for value in control.get("active_jobs", {}).values())
    running = control.get("running_gpu_services") or []
    owner = control.get("gpu_owner")
    if owner or active_jobs or running:
        raise RuntimeError(
            "smoke suite requires an idle GPU with no running heavyweight service; "
            "active work is preserved and no services were started"
        )

def checkpoint(name, detail="OK"):
    print(f"[PASS] {name}: {detail}", flush=True)

arguments = argparse.ArgumentParser()
arguments.add_argument("--visual-memory-only", action="store_true")
args = arguments.parse_args()
if args.visual_memory_only:
    visual_memory_smoke()
    raise SystemExit(0)

require_idle_gpu()
checkpoint("gpu-preflight", "idle; no active jobs or heavyweight services")

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

    visual_memory_smoke()

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

    reasoning_payload = {
        "model": "local-reasoning",
        "messages": [{
            "role": "user",
            "content": "State the result of 2+2 in one short sentence.",
        }],
        "max_tokens": 64,
        "temperature": 0,
    }
    status, reasoning = json_api(
        "POST", "/v1/chat/completions", reasoning_payload, timeout=600
    )
    assert status == 200 and reasoning.get("choices")
    reasoning_message = reasoning["choices"][0].get("message", {})
    reasoning_text = str(reasoning_message.get("content") or "").strip()
    assert reasoning_text, reasoning
    checkpoint("reasoning-inference", reasoning_text[:100])

    tts_payload = {
        "model": "qwen3-tts-base",
        "input": "Local AI stack smoke test.",
        "voice": "Aiden",
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
    print("GPU service release remains with supervisor leases and idle timers.", flush=True)
