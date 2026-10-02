"""On-demand LLM -> TTS -> STT smoke test through the authenticated gateway."""

import os
import sys
import tempfile
import time
from pathlib import Path

import httpx


ANCHOR_PHRASE = "AI Stack voice smoke"
PROMPT = (
    'Write one short, natural English test sentence that includes the exact phrase '
    f'"{ANCHOR_PHRASE}". Return only the sentence.'
)
DEFAULT_BASE_URL = "http://127.0.0.1:8000"


class VoiceSmokeError(RuntimeError):
    """A stage-specific, safe-to-display failure from the voice smoke test."""


def normalize_text(value):
    return "".join(character.lower() for character in str(value) if character.isalnum())


def _report(message):
    print(f"[voice-smoke] {message}", flush=True)


def _request(client, method, url, headers, stage, **kwargs):
    try:
        response = client.request(method, url, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        raise VoiceSmokeError(f"{stage} failed: gateway request error ({type(exc).__name__})") from None
    if response.status_code >= 400:
        raise VoiceSmokeError(f"{stage} failed: gateway returned HTTP {response.status_code}")
    return response


def _timed_stage(name, callback, timings):
    _report(f"{name} started")
    started = time.perf_counter()
    try:
        result = callback()
    except VoiceSmokeError as exc:
        elapsed = time.perf_counter() - started
        timings[name.lower()] = elapsed
        _report(f"{name} failed after {elapsed:.2f}s")
        raise
    except Exception as exc:
        elapsed = time.perf_counter() - started
        timings[name.lower()] = elapsed
        _report(f"{name} failed after {elapsed:.2f}s")
        raise VoiceSmokeError(f"{name} failed: invalid gateway response ({type(exc).__name__})") from None
    elapsed = time.perf_counter() - started
    timings[name.lower()] = elapsed
    _report(f"{name} passed in {elapsed:.2f}s")
    return result


def _run(client, base_url, api_key):
    base_url = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    timings = {}

    def check_gpu():
        response = _request(client, "GET", base_url + "/control/status", headers, "GPU preflight")
        try:
            active_jobs = response.json().get("active_jobs")
            if not isinstance(active_jobs, dict):
                raise ValueError("active_jobs is missing")
            busy = {
                str(name): int(count or 0)
                for name, count in active_jobs.items()
                if int(count or 0) > 0
            }
        except (AttributeError, TypeError, ValueError):
            raise VoiceSmokeError("GPU preflight failed: supervisor status is invalid") from None
        if busy:
            detail = ", ".join(f"{name}={count}" for name, count in sorted(busy.items()))
            raise VoiceSmokeError(f"GPU preflight failed: active AI jobs prevent voice smoke: {detail}")

    _timed_stage("GPU preflight", check_gpu, timings)

    def generate_text():
        response = _request(
            client,
            "POST",
            base_url + "/v1/chat/completions",
            {**headers, "Content-Type": "application/json"},
            "LLM",
            json={
                "model": "local-fast",
                "messages": [{"role": "user", "content": PROMPT}],
                "max_tokens": 64,
                "temperature": 0,
                "stream": False,
            },
        )
        try:
            text = response.json()["choices"][0]["message"]["content"]
        except (IndexError, KeyError, TypeError, ValueError):
            raise VoiceSmokeError("LLM failed: response did not contain generated text") from None
        if not isinstance(text, str) or not text.strip():
            raise VoiceSmokeError("LLM failed: response text was empty")
        if normalize_text(ANCHOR_PHRASE) not in normalize_text(text):
            raise VoiceSmokeError("LLM failed: generated text did not include the requested anchor phrase")
        return text.strip()

    generated_text = _timed_stage("LLM", generate_text, timings)

    def synthesize():
        response = _request(
            client,
            "POST",
            base_url + "/v1/audio/speech",
            {**headers, "Content-Type": "application/json"},
            "TTS",
            json={
                "model": "local-tts",
                "input": generated_text,
                "voice": "qwen-default",
                "response_format": "mp3",
            },
        )
        audio = response.content
        if not audio:
            raise VoiceSmokeError("TTS failed: gateway returned an empty audio file")
        return audio

    audio = _timed_stage("TTS", synthesize, timings)

    def transcribe(audio_path):
        with audio_path.open("rb") as audio_file:
            response = _request(
                client,
                "POST",
                base_url + "/v1/audio/transcriptions",
                headers,
                "STT",
                data={"model": "local-stt", "language": "en", "response_format": "json"},
                files={"file": ("voice-smoke.mp3", audio_file, "audio/mpeg")},
            )
        try:
            transcript = response.json().get("text")
        except (AttributeError, TypeError, ValueError):
            raise VoiceSmokeError("STT failed: response did not contain a transcript") from None
        if not isinstance(transcript, str) or not transcript.strip():
            raise VoiceSmokeError("STT failed: transcript was empty")
        if normalize_text(ANCHOR_PHRASE) not in normalize_text(transcript):
            raise VoiceSmokeError("STT failed: anchor phrase was not recognized")

    with tempfile.TemporaryDirectory(prefix="ai-stack-voice-smoke-") as directory:
        audio_path = Path(directory) / "voice-smoke.mp3"
        audio_path.write_bytes(audio)
        _timed_stage("STT", lambda: transcribe(audio_path), timings)

    _report(f"Voice smoke passed; audio={len(audio)} bytes; transcript anchor matched")
    return {
        "status": "passed",
        "phrase_match": True,
        "audio_bytes": len(audio),
        "timings_seconds": timings,
    }


def run_voice_smoke(client=None, base_url=None, api_key=None):
    """Run the pipeline; injectable client/base/key keep tests fully offline."""
    api_key = os.getenv("AI_API_KEY", "").strip() if api_key is None else api_key.strip()
    if not api_key:
        raise VoiceSmokeError("configuration failed: AI_API_KEY is unavailable")
    base_url = base_url or os.getenv("AI_STACK_GATEWAY_URL", DEFAULT_BASE_URL)
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=httpx.Timeout(600.0, connect=10.0))
    try:
        return _run(client, base_url, api_key)
    finally:
        if owns_client:
            client.close()


def main():
    try:
        run_voice_smoke()
        return 0
    except VoiceSmokeError as exc:
        _report(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
