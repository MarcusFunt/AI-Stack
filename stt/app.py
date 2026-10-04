import gc
import io
import os
import threading

import av
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio

MODEL_NAME = os.getenv("STT_MODEL", "large-v3")
COMPUTE_TYPE = os.getenv("STT_COMPUTE_TYPE", "float16")
DOWNLOAD_ROOT = os.getenv("STT_DOWNLOAD_ROOT", "/models")
MAX_AUDIO_BYTES = int(os.getenv("STT_MAX_AUDIO_BYTES", str(20 * 1024 * 1024)))
MAX_AUDIO_SECONDS = int(os.getenv("STT_MAX_AUDIO_SECONDS", "3600"))
MAX_PROMPT_CHARS = int(os.getenv("STT_MAX_PROMPT_CHARS", "4000"))
SAMPLE_RATE = 16000
app = FastAPI(title="faster-whisper service")
model = None
lock = threading.Lock()

def get_model():
    global model
    if model is None:
        with lock:
            if model is None:
                model = WhisperModel(
                    MODEL_NAME,
                    device="cuda",
                    compute_type=COMPUTE_TYPE,
                    download_root=DOWNLOAD_ROOT,
                )
    return model


def _timestamp(seconds: float, *, decimal_separator: str = ".") -> str:
    milliseconds = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, fraction = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02}{decimal_separator}{fraction:03}"

@app.get("/health")
def health():
    return {"status": "ready", "model": MODEL_NAME, "loaded": model is not None}

@app.post("/internal/benchmark/unload")
def unload_for_benchmark():
    global model
    with lock:
        previous = model
        model = None
    del previous
    gc.collect()
    return {"status": "unloaded"}
@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    model_name: str | None = Form(default=None, alias="model"),
    response_format: str = Form(default="json"),
    prompt: str | None = Form(default=None),
    temperature: float = Form(default=0.0),
    timestamp_granularities: list[str] | None = Form(default=None, alias="timestamp_granularities[]"),
):
    data = await file.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio upload exceeds size limit")
    if not data:
        raise HTTPException(400, "audio upload is empty")
    if response_format not in {"json", "verbose_json", "text", "srt", "vtt"}:
        raise HTTPException(400, "response_format must be json, verbose_json, text, srt, or vtt")
    if prompt is not None and len(prompt) > MAX_PROMPT_CHARS:
        raise HTTPException(413, "prompt exceeds character limit")
    if not 0.0 <= temperature <= 1.0:
        raise HTTPException(400, "temperature must be between 0 and 1")
    granularities = timestamp_granularities or []
    if any(value not in {"segment", "word"} for value in granularities):
        raise HTTPException(400, "timestamp_granularities[] values must be 'segment' or 'word'")

    # Validate the media container and reject overlong audio before loading
    # the heavyweight Whisper model.
    try:
        with av.open(io.BytesIO(data), mode="r") as container:
            if not container.streams.audio:
                raise HTTPException(400, "upload contains no audio stream")
            duration = None
            if container.duration is not None:
                duration = float(container.duration / av.time_base)
            else:
                stream = container.streams.audio[0]
                if stream.duration is not None and stream.time_base is not None:
                    duration = float(stream.duration * stream.time_base)
            if duration is not None and duration > MAX_AUDIO_SECONDS:
                raise HTTPException(413, "audio duration exceeds limit")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, "invalid audio upload") from exc

    try:
        audio = decode_audio(io.BytesIO(data), sampling_rate=SAMPLE_RATE)
    except Exception as exc:
        raise HTTPException(400, "invalid audio upload") from exc

    if audio.size == 0:
        raise HTTPException(400, "audio contains no decodable samples")
    if len(audio) / SAMPLE_RATE > MAX_AUDIO_SECONDS:
        raise HTTPException(413, "audio duration exceeds limit")

    segments, info = get_model().transcribe(
        audio,
        language=language,
        vad_filter=True,
        beam_size=5,
        initial_prompt=prompt,
        temperature=temperature,
        word_timestamps="word" in granularities,
    )
    items = []
    for segment in segments:
        item = {"start": segment.start, "end": segment.end, "text": segment.text}
        if "word" in granularities:
            item["words"] = [
                {"start": word.start, "end": word.end, "word": word.word}
                for word in (getattr(segment, "words", None) or [])
            ]
        items.append(item)
    text = "".join(item["text"] for item in items).strip()
    if response_format == "text":
        return PlainTextResponse(text)
    if response_format in {"srt", "vtt"}:
        decimal_separator = "," if response_format == "srt" else "."
        entries = []
        for index, item in enumerate(items, start=1):
            start = _timestamp(item["start"], decimal_separator=decimal_separator)
            end = _timestamp(item["end"], decimal_separator=decimal_separator)
            entries.append(f"{index}\n{start} --> {end}\n{item['text'].strip()}\n")
        body = "\n".join(entries)
        if response_format == "vtt":
            body = "WEBVTT\n\n" + body
        media_type = "text/vtt" if response_format == "vtt" else "text/plain"
        return PlainTextResponse(body, media_type=media_type)
    return {
        "text": text,
        "language": info.language,
        "language_probability": info.language_probability,
        "segments": items,
    }
