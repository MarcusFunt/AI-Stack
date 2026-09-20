import json
import os
import threading
import time
import urllib.request

from env_utils import require_env_value

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_KEY = require_env_value(os.path.join(ROOT, ".env"), "AI_API_KEY")

BASE = "http://127.0.0.1:8090"
AUTH = {"Authorization": f"Bearer {API_KEY}"}
JSON_HEADERS = {**AUTH, "Content-Type": "application/json"}

def request(path, payload, timeout=600):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + path, data=data, headers=JSON_HEADERS, method="POST"
    )
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, time.time() - started, response.read()

def status():
    req = urllib.request.Request(BASE + "/control/status", headers=AUTH)
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.loads(response.read())
def llm_result(name, output, max_tokens=180):
    payload = {
        "model": "local-fast",
        "messages": [{
            "role": "user",
            "content": "Write a numbered list of distinct engineering terms, one per line.",
        }],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    output[name] = request("/v1/chat/completions", payload)

def same_service_test():
    output = {}
    threads = [
        threading.Thread(target=llm_result, args=("a", output)),
        threading.Thread(target=llm_result, args=("b", output)),
    ]
    for thread in threads:
        thread.start()
    max_jobs = 0
    while any(thread.is_alive() for thread in threads):
        try:
            max_jobs = max(max_jobs, int(status().get("active_jobs", {}).get("llm", 0)))
        except Exception:
            pass
        time.sleep(0.1)
    for thread in threads:
        thread.join()
    assert output["a"][0] == 200 and output["b"][0] == 200
    assert max_jobs >= 2, f"expected concurrent leases, saw {max_jobs}"
    return {"max_llm_jobs": max_jobs, "times_s": [round(output["a"][1], 2), round(output["b"][1], 2)]}
def cross_service_test():
    output = {}
    long_llm = threading.Thread(target=llm_result, args=("llm", output, 260))

    def tts():
        payload = {
            "model": "qwen3-tts-base",
            "input": "Lease switching regression test.",
            "voice": "qwen-default",
            "response_format": "mp3",
        }
        output["tts"] = request("/v1/audio/speech", payload)

    long_llm.start()
    time.sleep(0.5)
    tts_thread = threading.Thread(target=tts)
    tts_thread.start()
    observed_wait = False
    while long_llm.is_alive() or tts_thread.is_alive():
        try:
            jobs = status().get("active_jobs", {})
            if jobs.get("llm", 0) and not jobs.get("tts", 0):
                observed_wait = True
        except Exception:
            pass
        time.sleep(0.15)
    long_llm.join()
    tts_thread.join()
    assert output["llm"][0] == 200 and output["tts"][0] == 200
    assert observed_wait, "did not observe TTS waiting for active LLM lease"
    return {"observed_wait": True, "llm_s": round(output["llm"][1], 2), "tts_s": round(output["tts"][1], 2)}
def stream_disconnect_test():
    payload = {
        "model": "local-fast",
        "messages": [{"role": "user", "content": "Count upward forever, one integer per line."}],
        "max_tokens": 512,
        "temperature": 0,
        "stream": True,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        BASE + "/v1/chat/completions",
        data=data,
        headers=JSON_HEADERS,
        method="POST",
    )
    response = urllib.request.urlopen(req, timeout=120)
    response.read(256)
    response.close()
    deadline = time.time() + 15
    while time.time() < deadline:
        if status().get("active_jobs", {}).get("llm", 0) == 0:
            return {"released": True}
        time.sleep(0.25)
    raise AssertionError("stream disconnect leaked an LLM lease")

if __name__ == "__main__":
    result = {
        "same_service": same_service_test(),
        "cross_service": cross_service_test(),
        "stream_disconnect": stream_disconnect_test(),
        "final_status": status(),
    }
    print(json.dumps(result, indent=2))
