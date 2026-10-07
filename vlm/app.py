import copy
import io
import json
import os
import threading

import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError
from transformers import AutoModelForImageTextToText, AutoProcessor, GenerationConfig

MODEL_NAME = os.getenv("VLM_MODEL", "Qwen/Qwen3-VL-4B-Instruct")
LOCAL_FILES_ONLY = os.getenv("VLM_LOCAL_FILES_ONLY", "1").strip().lower() not in {"0", "false", "no"}
MAX_IMAGE_BYTES = int(os.getenv("VLM_MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))
MAX_IMAGE_PIXELS = int(os.getenv("VLM_MAX_IMAGE_PIXELS", "40000000"))
MAX_CONTEXT_CROPS = int(os.getenv("VLM_MAX_CONTEXT_CROPS", "6"))
MAX_CONTEXT_REFERENCES = int(os.getenv("VLM_MAX_CONTEXT_REFERENCES", "4"))
MAX_CONTEXT_TOTAL_BYTES = int(os.getenv("VLM_MAX_CONTEXT_TOTAL_BYTES", str(48 * 1024 * 1024)))
MAX_CONTEXT_JSON_CHARS = int(os.getenv("VLM_MAX_CONTEXT_JSON_CHARS", "64000"))

app = FastAPI(title="robotics VLM service")
model = None
processor = None
lock = threading.Lock()


def get_model():
    global model, processor
    if model is None or processor is None:
        with lock:
            if model is None or processor is None:
                processor = AutoProcessor.from_pretrained(
                    MODEL_NAME,
                    local_files_only=LOCAL_FILES_ONLY,
                )

                base_generation_config = GenerationConfig.from_pretrained(
                    MODEL_NAME,
                    local_files_only=LOCAL_FILES_ONLY,
                )
                base_generation_config.do_sample = False
                base_generation_config.temperature = None
                base_generation_config.top_p = None
                base_generation_config.top_k = None

                model = AutoModelForImageTextToText.from_pretrained(
                    MODEL_NAME,
                    device_map="auto",
                    dtype="auto",
                    local_files_only=LOCAL_FILES_ONLY,
                    generation_config=base_generation_config,
                )
                model.eval()
    return model, processor


@app.get("/health")
def health():
    return {
        "status": "ready",
        "model": MODEL_NAME,
        "loaded": model is not None,
        "local_files_only": LOCAL_FILES_ONLY,
    }


@app.post("/v1/vision/analyze")
async def analyze(
    image: UploadFile = File(...),
    prompt: str = Form(
        default="Describe the scene for a robot, including objects, geometry, obstacles, and affordances."
    ),
    max_new_tokens: int = Form(default=512, ge=1, le=2048),
):
    data = await image.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "image upload exceeds size limit")
    if not data:
        raise HTTPException(400, "image upload is empty")

    try:
        pil = Image.open(io.BytesIO(data))
        width, height = pil.size
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise HTTPException(413, "image dimensions exceed pixel limit")
        pil.load()
        pil = pil.convert("RGB")
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(400, "invalid image upload") from exc
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": pil},
                {"type": "text", "text": prompt},
            ],
        }
    ]

    vlm, proc = get_model()
    inputs = proc.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
    )
    inputs = inputs.to(vlm.device)

    generation_config = copy.deepcopy(vlm.generation_config)
    generation_config.max_length = None
    generation_config.max_new_tokens = max_new_tokens
    generation_config.do_sample = False
    generation_config.temperature = None
    generation_config.top_p = None
    generation_config.top_k = None
    generation_config.return_dict_in_generate = False

    with torch.inference_mode():
        generated_ids = vlm.generate(
            **inputs,
            generation_config=generation_config,
        )

    input_length = inputs["input_ids"].shape[1]
    generated_only = generated_ids[:, input_length:]
    text = proc.batch_decode(
        generated_only,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )[0].strip()

    return {"model": MODEL_NAME, "text": text}


def _decode_context_image(data: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(data))
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
            raise HTTPException(413, "image dimensions exceed pixel limit")
        image.load()
        return image.convert("RGB")
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(400, "invalid image upload") from exc


async def _read_context_upload(upload: UploadFile) -> bytes:
    data = await upload.read(MAX_IMAGE_BYTES + 1)
    await upload.close()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "context image exceeds size limit")
    if not data:
        raise HTTPException(400, "context image is empty")
    return data


@app.post("/v1/vision/analyze-context")
async def analyze_context(
    current_image: UploadFile = File(...),
    crop_images: list[UploadFile] = File(default=[]),
    reference_images: list[UploadFile] = File(default=[]),
    context_json: str = Form(default="{}"),
    analysis_profile: str = Form(default="generic"),
    prompt: str = Form(default="Analyze the current image using the supplied visual and structured context."),
    max_new_tokens: int = Form(default=512, ge=1, le=2048),
):
    if analysis_profile not in {"generic", "website", "game"}:
        raise HTTPException(400, "analysis_profile must be generic, website, or game")
    if len(context_json) > MAX_CONTEXT_JSON_CHARS:
        raise HTTPException(413, "context_json exceeds size limit")
    try:
        structured_context = json.loads(context_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "context_json must be valid JSON") from exc
    if not isinstance(structured_context, dict):
        raise HTTPException(400, "context_json must be a JSON object")
    if len(crop_images) > MAX_CONTEXT_CROPS:
        raise HTTPException(413, "too many crop images")
    if len(reference_images) > MAX_CONTEXT_REFERENCES:
        raise HTTPException(413, "too many reference images")
    if len(prompt) > 16_000:
        raise HTTPException(400, "prompt exceeds 16000 characters")

    raw_current = await _read_context_upload(current_image)
    raw_crops = [await _read_context_upload(upload) for upload in crop_images]
    raw_references = [await _read_context_upload(upload) for upload in reference_images]
    total_bytes = sum(len(data) for data in [raw_current, *raw_crops, *raw_references])
    if total_bytes > MAX_CONTEXT_TOTAL_BYTES:
        raise HTTPException(413, "context images exceed total byte limit")

    current = _decode_context_image(raw_current)
    crops = [_decode_context_image(data) for data in raw_crops]
    references = [_decode_context_image(data) for data in raw_references]
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image", "image": current},
                {"type": "text", "text": f"Analysis profile: {analysis_profile}. Current image."},
                *[{"type": "image", "image": image} for image in crops],
                *[{"type": "image", "image": image} for image in references],
                {"type": "text", "text": "Structured visual context JSON:\n" + json.dumps(
                    structured_context, ensure_ascii=False, separators=(",", ":")
                )},
                {"type": "text", "text": prompt},
            ],
        }
    ]

    vlm, proc = get_model()
    inputs = proc.apply_chat_template(
        messages,
        tokenize=True,
        add_generation_prompt=True,
        return_dict=True,
        return_tensors="pt",
    )
    inputs = inputs.to(vlm.device)
    generation_config = copy.deepcopy(vlm.generation_config)
    generation_config.max_length = None
    generation_config.max_new_tokens = max_new_tokens
    generation_config.do_sample = False
    generation_config.temperature = None
    generation_config.top_p = None
    generation_config.top_k = None
    generation_config.return_dict_in_generate = False
    with torch.inference_mode():
        generated_ids = vlm.generate(**inputs, generation_config=generation_config)
    input_length = inputs["input_ids"].shape[1]
    generated_only = generated_ids[:, input_length:]
    text = proc.batch_decode(
        generated_only,
        skip_special_tokens=True,
        clean_up_tokenization_spaces=False,
    )[0].strip()
    return {
        "model": MODEL_NAME,
        "text": text,
        "images_received": {"current": 1, "crops": len(crops), "references": len(references)},
        "analysis_profile": analysis_profile,
    }
