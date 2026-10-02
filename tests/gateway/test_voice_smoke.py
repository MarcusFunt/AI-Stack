import json
import re
import tempfile
from pathlib import Path

import httpx
import pytest

from gateway import voice_smoke


ANCHOR = "AI Stack voice smoke"
AUDIO = b"mock-mp3-audio-bytes"


def make_transport(transcript="AI stack voice smoke passed clearly.", active_jobs=None, fail_at=None):
    calls = []

    def handler(request):
        calls.append(request)
        index = len(calls)
        expected = ["/control/status", "/v1/chat/completions", "/v1/audio/speech", "/v1/audio/transcriptions"]
        assert request.url.path == expected[index - 1]
        if fail_at == index:
            return httpx.Response(503, json={"detail": "provider unavailable"})
        if index == 1:
            return httpx.Response(200, json={"active_jobs": active_jobs or {"llm": 0, "tts": 0, "stt": 0}})
        if index == 2:
            payload = json.loads(request.content)
            assert payload["model"] == "local-fast"
            assert ANCHOR.lower() in payload["messages"][0]["content"].lower()
            return httpx.Response(200, json={"choices": [{"message": {"content": f"{ANCHOR} is ready."}}]})
        if index == 3:
            payload = json.loads(request.content)
            assert payload["model"] == "local-tts"
            assert payload["input"] == f"{ANCHOR} is ready."
            assert payload["response_format"] == "mp3"
            return httpx.Response(200, content=AUDIO, headers={"content-type": "audio/mpeg"})
        body = request.content
        assert b'name="model"' in body and b"local-stt" in body
        assert b'name="language"' in body and b"en" in body
        assert b'filename="voice-smoke.mp3"' in body
        assert AUDIO in body
        return httpx.Response(200, json={"text": transcript})

    return httpx.MockTransport(handler), calls


def test_pipeline_uses_gateway_in_order_and_cleans_temporary_audio(monkeypatch, capsys):
    transport, calls = make_transport()
    original_temporary_directory = tempfile.TemporaryDirectory
    created = []

    def temporary_directory(*args, **kwargs):
        value = original_temporary_directory(*args, **kwargs)
        created.append(Path(value.name))
        return value

    monkeypatch.setattr(voice_smoke.tempfile, "TemporaryDirectory", temporary_directory)
    client = httpx.Client(transport=transport)

    result = voice_smoke.run_voice_smoke(client=client, base_url="http://gateway", api_key="secret")

    assert [request.url.path for request in calls] == [
        "/control/status", "/v1/chat/completions", "/v1/audio/speech", "/v1/audio/transcriptions"
    ]
    assert all(request.headers["authorization"] == "Bearer secret" for request in calls)
    assert result["phrase_match"] is True
    assert result["audio_bytes"] == len(AUDIO)
    assert created and not created[0].exists()
    output = capsys.readouterr().out
    assert "LLM passed" in output
    assert "TTS passed" in output
    assert "STT passed" in output
    assert "passed clearly" not in output
    client.close()


def test_active_gpu_job_refuses_before_generating_text():
    transport, calls = make_transport(active_jobs={"llm": 1})
    client = httpx.Client(transport=transport)

    with pytest.raises(voice_smoke.VoiceSmokeError, match="active AI jobs"):
        voice_smoke.run_voice_smoke(client=client, base_url="http://gateway", api_key="secret")

    assert len(calls) == 1
    client.close()


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [("AI stack voice smoke passed.", True), ("The test passed.", False), ("AI stack-voice smoke!", True)],
)
def test_transcript_requires_normalized_anchor_phrase(transcript, expected):
    transport, _ = make_transport(transcript=transcript)
    client = httpx.Client(transport=transport)

    if expected:
        result = voice_smoke.run_voice_smoke(client=client, base_url="http://gateway", api_key="secret")
        assert result["phrase_match"] is True
    else:
        with pytest.raises(voice_smoke.VoiceSmokeError, match="anchor phrase was not recognized"):
            voice_smoke.run_voice_smoke(client=client, base_url="http://gateway", api_key="secret")
    client.close()


@pytest.mark.parametrize("failed_call,stage", [(2, "LLM"), (3, "TTS"), (4, "STT")])
def test_provider_failure_identifies_stage_without_echoing_response(failed_call, stage):
    transport, _ = make_transport(fail_at=failed_call)
    client = httpx.Client(transport=transport)

    with pytest.raises(voice_smoke.VoiceSmokeError, match=stage):
        voice_smoke.run_voice_smoke(client=client, base_url="http://gateway", api_key="secret")
    client.close()


def test_normalize_phrase_ignores_case_punctuation_and_spacing():
    assert voice_smoke.normalize_text("AI-stack, voice! smoke.") == re.sub(r"[^a-z0-9]+", "", ANCHOR.lower())
