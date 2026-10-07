from PIL import Image, ImageDraw
import pytest

from visual_memory.crops import generate_crops


def patterned_image(size=(160, 120)):
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 79, 59), fill="red")
    draw.rectangle((80, 0, 159, 59), fill="blue")
    draw.rectangle((0, 60, 79, 119), fill="green")
    draw.rectangle((80, 60, 159, 119), fill="black")
    draw.line((0, 0, 159, 119), fill="yellow", width=3)
    return image


def test_crop_order_and_normalized_coordinates_are_deterministic():
    first = generate_crops(patterned_image())
    second = generate_crops(patterned_image())

    assert [crop.crop_type for crop in first] == [crop.crop_type for crop in second]
    assert [crop.crop_type for crop in first][:10] == [
        "full", "top_left", "top_right", "bottom_left", "bottom_right",
        "top_strip", "bottom_strip", "left_strip", "right_strip", "center",
    ]
    assert first[0].box == {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0}
    assert first[1].box == {"x": 0.0, "y": 0.0, "w": 0.5, "h": 0.5}
    assert first[9].box == {"x": 0.25, "y": 0.25, "w": 0.5, "h": 0.5}
    assert first[0].image.size == (160, 120)
    assert first[1].image.size == (80, 60)


def test_crop_generation_rejects_empty_and_low_variance_frames():
    with pytest.raises(ValueError, match="non-empty"):
        generate_crops(Image.new("RGB", (0, 0)))
    assert generate_crops(Image.new("RGB", (80, 60), "gray")) == []


def test_near_identical_regions_are_removed_after_the_first_occurrence():
    image = Image.new("RGB", (128, 128), "black")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 63, 63), fill="red")
    draw.rectangle((64, 0, 127, 63), fill="red")
    draw.rectangle((0, 64, 63, 127), fill="blue")
    draw.rectangle((64, 64, 127, 127), fill="green")

    crops = generate_crops(image, min_variance=0, near_identical_distance=0)
    types = [crop.crop_type for crop in crops]

    assert "top_left" in types
    assert "top_right" not in types
