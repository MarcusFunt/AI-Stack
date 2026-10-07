from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageStat


@dataclass(frozen=True)
class Crop:
    crop_type: str
    image: Image.Image
    box: dict[str, float]
    perceptual_hash: str


def perceptual_hash(image: Image.Image) -> str:
    """Return a small difference hash suitable for cheap duplicate gating."""
    gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixels = list(gray.getdata())
    bits = 0
    for y in range(8):
        row = y * 9
        for x in range(8):
            bits = (bits << 1) | int(pixels[row + x] > pixels[row + x + 1])
    return f"{bits:016x}"


def hamming_distance(left: str, right: str) -> int:
    if len(left) != len(right):
        raise ValueError("perceptual hashes must have equal lengths")
    return (int(left, 16) ^ int(right, 16)).bit_count()


def generate_crops(
    image: Image.Image,
    *,
    min_dimension: int = 16,
    min_variance: float = 2.0,
    near_identical_distance: int | None = 2,
) -> list[Crop]:
    if image is None or not hasattr(image, "size"):
        raise ValueError("image must be non-empty")
    width, height = image.size
    if width <= 0 or height <= 0:
        raise ValueError("image must be non-empty")

    specifications = [
        ("full", 0.0, 0.0, 1.0, 1.0),
        ("top_left", 0.0, 0.0, 0.5, 0.5),
        ("top_right", 0.5, 0.0, 0.5, 0.5),
        ("bottom_left", 0.0, 0.5, 0.5, 0.5),
        ("bottom_right", 0.5, 0.5, 0.5, 0.5),
        ("top_strip", 0.0, 0.0, 1.0, 0.25),
        ("bottom_strip", 0.0, 0.75, 1.0, 0.25),
        ("left_strip", 0.0, 0.0, 0.25, 1.0),
        ("right_strip", 0.75, 0.0, 0.25, 1.0),
        ("center", 0.25, 0.25, 0.5, 0.5),
    ]
    accepted: list[Crop] = []
    for crop_type, x, y, w, h in specifications:
        left = round(x * width)
        top = round(y * height)
        right = round((x + w) * width)
        bottom = round((y + h) * height)
        if right - left < min_dimension or bottom - top < min_dimension:
            continue
        crop_image = image.convert("RGB").crop((left, top, right, bottom))
        if ImageStat.Stat(crop_image.convert("L")).stddev[0] < min_variance:
            continue
        crop_hash = perceptual_hash(crop_image)
        if near_identical_distance is not None and any(
            hamming_distance(crop_hash, existing.perceptual_hash) <= near_identical_distance
            for existing in accepted
        ):
            continue
        accepted.append(
            Crop(
                crop_type=crop_type,
                image=crop_image,
                box={"x": x, "y": y, "w": w, "h": h},
                perceptual_hash=crop_hash,
            )
        )
    return accepted
