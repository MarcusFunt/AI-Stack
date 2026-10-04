import asyncio
import io
import logging
import os
import subprocess
import threading
from contextlib import asynccontextmanager
from typing import Literal

import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from qwen_tts import Qwen3TTSModel


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger("qwen-tts-openai")

MODEL_ID = os.getenv("QWEN_MODEL_ID", "Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice")
REFERENCE_AUDIO = os.getenv(
    "QWEN_REFERENCE_AUDIO",
    "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-TTS-Repo/clone.wav",
)
REFERENCE_TEXT = os.getenv(
    "QWEN_REFERENCE_TEXT",
    "Okay. Yeah. I resent you. I love you. I respect you. But you know what? You blew it! And thanks to you.",
)
ATTN_IMPLEMENTATION = os.getenv("QWEN_ATTN_IMPLEMENTATION", "sdpa")
VOICE_ID = os.getenv("QWEN_VOICE_ID", "qwen-default").strip() or "qwen-default"
DEFAULT_CUSTOM_VOICE = "Aiden"

model: Qwen3TTSModel | None = None
model_type: str | None = None
voice_prompt = None
supported_speakers: list[str] = []
default_voice = VOICE_ID
model_error: str | None = None
generation_lock = threading.Lock()


class SpeechRequest(BaseModel):
    model: str | None = None
    input: str = Field(min_length=1)
    voice: str | None = None
    language: str = "English"
    instruct: str | None = Field(default=None, max_length=2000)
    response_format: Literal["mp3", "wav", "opus", "flac", "pcm"] = "mp3"
    speed: float | None = Field(default=None, ge=0.25, le=4.0)


def load_model() -> None:
    global model, model_type, voice_prompt, supported_speakers, default_voice, model_error
    try:
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available inside the Qwen TTS container")
        device_name = torch.cuda.get_device_name(0)
        logger.info("Loading %s on %s with %s attention", MODEL_ID, device_name, ATTN_IMPLEMENTATION)
        loaded_model = Qwen3TTSModel.from_pretrained(
            MODEL_ID,
            device_map="cuda:0",
            dtype=torch.bfloat16,
            attn_implementation=ATTN_IMPLEMENTATION,
        )
        loaded_model_type = getattr(getattr(loaded_model, "model", None), "tts_model_type", None)

        if loaded_model_type == "custom_voice":
            speakers = loaded_model.get_supported_speakers() or []
            if not speakers:
                raise RuntimeError("CustomVoice checkpoint returned no supported speakers")
            loaded_voice_prompt = None
            speaker_by_key = {speaker.casefold(): speaker for speaker in speakers}
            requested_default = VOICE_ID
            if requested_default.casefold() not in speaker_by_key:
                requested_default = DEFAULT_CUSTOM_VOICE
            if requested_default.casefold() not in speaker_by_key:
                requested_default = speakers[0]
            selected_default = speaker_by_key[requested_default.casefold()]
            logger.info("CustomVoice model ready with %d speakers; default=%s", len(speakers), selected_default)
        elif loaded_model_type == "base":
            loaded_voice_prompt = loaded_model.create_voice_clone_prompt(
                ref_audio=REFERENCE_AUDIO,
                ref_text=REFERENCE_TEXT,
                x_vector_only_mode=False,
            )
            speakers = [VOICE_ID]
            selected_default = VOICE_ID
            logger.info("Qwen Base model and reusable voice-clone prompt are ready")
        else:
            raise RuntimeError(f"Unsupported Qwen TTS model type: {loaded_model_type!r}")

        model = loaded_model
        model_type = loaded_model_type
        voice_prompt = loaded_voice_prompt
        supported_speakers = list(speakers)
        default_voice = selected_default
        model_error = None
    except Exception as exc:  # Surface startup failures on /health without killing container logs.
        model = None
        model_type = None
        voice_prompt = None
        supported_speakers = []
        default_voice = VOICE_ID
        model_error = f"{type(exc).__name__}: {exc}"
        logger.exception("Qwen TTS model initialization failed")


def encode_audio(wav, sample_rate: int, response_format: str) -> tuple[bytes, str]:
    raw_wav = io.BytesIO()
    sf.write(raw_wav, wav, sample_rate, format="WAV", subtype="PCM_16")
    if response_format == "wav":
        return raw_wav.getvalue(), "audio/wav"
    if response_format == "pcm":
        pcm = io.BytesIO()
        sf.write(pcm, wav, sample_rate, format="RAW", subtype="PCM_16")
        return pcm.getvalue(), "application/octet-stream"
    formats = {
        "mp3": ("mp3", "audio/mpeg"),
        "opus": ("opus", "audio/ogg"),
        "flac": ("flac", "audio/flac"),
    }
    codec, media_type = formats[response_format]
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-f", codec, "pipe:1"],
        input=raw_wav.getvalue(),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg encoding failed: {result.stderr.decode(errors='replace')}")
    return result.stdout, media_type


def synthesize(request: SpeechRequest) -> tuple[bytes, str]:
    if model is None:
        raise RuntimeError(model_error or "Qwen model is not ready")

    language = request.language.strip() or "English"
    if model_type == "custom_voice":
        speaker_by_key = {speaker.casefold(): speaker for speaker in supported_speakers}
        requested_speaker = (request.voice or default_voice).strip()
        speaker = speaker_by_key.get(requested_speaker.casefold())
        if speaker is None:
            choices = ", ".join(supported_speakers)
            raise ValueError(f"unsupported voice {requested_speaker!r}; supported voices: {choices}")
        with generation_lock, torch.inference_mode():
            wavs, sample_rate = model.generate_custom_voice(
                text=request.input,
                speaker=speaker,
                language=language,
                instruct=request.instruct or "",
            )
    elif model_type == "base":
        if voice_prompt is None:
            raise RuntimeError(model_error or "Qwen Base voice-clone prompt is not ready")
        if request.instruct and request.instruct.strip():
            raise ValueError("expressive instructions require a CustomVoice model")
        requested_voice = (request.voice or default_voice).strip() or default_voice
        if requested_voice != default_voice:
            logger.info(
                "Using configured Qwen voice profile %s for requested voice %s",
                default_voice,
                requested_voice,
            )
        with generation_lock, torch.inference_mode():
            wavs, sample_rate = model.generate_voice_clone(
                text=request.input,
                language=language,
                voice_clone_prompt=voice_prompt,
            )
    else:
        raise RuntimeError(f"Unsupported Qwen TTS model type: {model_type!r}")

    return encode_audio(wavs[0], sample_rate, request.response_format)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(load_model)
    yield


app = FastAPI(title="Qwen3-TTS OpenAI Adapter", version="1.1.0", lifespan=lifespan)


@app.get("/health")
def health():
    if model is None:
        return JSONResponse(status_code=503, content={"status": "starting_or_failed", "error": model_error})
    return {
        "status": "ready",
        "model": MODEL_ID,
        "model_type": model_type,
        "device": torch.cuda.get_device_name(0),
        "voices": supported_speakers,
    }


@app.get("/v1/models")
def models():
    model_name = "qwen3-tts-customvoice" if model_type == "custom_voice" else "qwen3-tts-base"
    return {"object": "list", "data": [{"id": model_name, "object": "model", "owned_by": "local"}]}


@app.get("/v1/audio/voices")
def voices():
    """Expose the speakers supported by the loaded Qwen model."""
    return {"voices": supported_speakers or [VOICE_ID]}


@app.post("/v1/audio/speech")
async def create_speech(request: SpeechRequest):
    try:
        payload, media_type = await asyncio.to_thread(synthesize, request)
        return Response(content=payload, media_type=media_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Speech generation failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
