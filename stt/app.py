import io
import os
import threading

import av
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio

MODEL_NAME = os.getenv("STT_MODEL", "large-v3")
COMPUTE_TYPE = os.getenv("STT_COMPUTE_TYPE", "float16")
DOWNLOAD_ROOT = os.getenv("STT_DOWNLOAD_ROOT", "/models")
MAX_AUDIO_BYTES = int(os.getenv("STT_MAX_AUDIO_BYTES", str(20 * 1024 * 1024)))
MAX_AUDIO_SECONDS = int(os.getenv("STT_MAX_AUDIO_SECONDS", "3600"))
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

@app.get("/health")
def health():
    return {"status": "ready", "model": MODEL_NAME, "loaded": model is not None}
@app.post("/v1/audio/transcriptions")
async def transcribe(
    file: UploadFile = File(...),
    language: str | None = Form(default=None),
    model_name: str | None = Form(default=None, alias="model"),
    response_format: str = Form(default="json"),
):
    data = await file.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio upload exceeds size limit")
    if not data:
        raise HTTPException(400, "audio upload is empty")
    if response_format not in {"json", "text"}:
        raise HTTPException(400, "response_format must be 'json' or 'text'")

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
    )
    items = [
        {"start": s.start, "end": s.end, "text": s.text}
        for s in segments
    ]
    text = "".join(item["text"] for item in items).strip()
    if response_format == "text":
        return text
    return {
        "text": text,
        "language": info.language,
        "language_probability": info.language_probability,
        "segments": items,
    }
