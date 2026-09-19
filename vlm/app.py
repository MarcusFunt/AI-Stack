import copy
import io
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
