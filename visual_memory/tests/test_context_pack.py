import pytest

from visual_memory.context_pack import build_context_pack


def test_context_pack_deduplicates_parents_crops_and_preserves_diverse_evidence():
    pack = build_context_pack(
        current_frame={"id": "current-frame", "observation_id": "current-observation"},
        retrieved=[
            {"record_id": "web-1", "parent_id": "web-parent", "score": 0.98, "diversity_key": "website"},
            {"record_id": "web-1-crop", "parent_id": "web-parent", "score": 0.97, "diversity_key": "website"},
            {"record_id": "game-1", "parent_id": "game-parent", "score": 0.92, "diversity_key": "game"},
            {"record_id": "web-2", "parent_id": "web-parent-2", "score": 0.90, "diversity_key": "website"},
            {"record_id": "current-frame", "parent_id": "current-frame", "score": 1.0},
        ],
        crops=[
            {"crop_id": "crop-1", "parent_id": "current-frame", "crop": {"type": "center"}},
            {"crop_id": "crop-1", "parent_id": "current-frame", "crop": {"type": "center"}},
            {"crop_id": "crop-2", "parent_id": "web-parent", "crop": {"type": "header"}},
        ],
        structured_context={"viewport": {"width": 1280, "height": 720}},
        text_context=[{"chunk_hash": "a", "text": "first"}, {"chunk_hash": "a", "text": "duplicate"},
                      {"chunk_hash": "b", "text": "second"}],
        max_reference_images=2,
        max_crops=1,
        max_text_items=2,
    ).to_dict()

    assert pack["current_frame"]["id"] == "current-frame"
    assert [item["parent_id"] for item in pack["retrieved"]] == ["web-parent", "game-parent"]
    assert [item["crop_id"] for item in pack["crops"]] == ["crop-1"]
    assert [item["chunk_hash"] for item in pack["text_context"]] == ["a", "b"]
    assert pack["structured_context"]["viewport"]["width"] == 1280


def test_context_pack_enforces_maxima_and_text_character_budget():
    with pytest.raises(ValueError, match="maximum of 4"):
        build_context_pack({"id": "current"}, max_reference_images=5)
    with pytest.raises(ValueError, match="maximum of 6"):
        build_context_pack({"id": "current"}, max_crops=7)

    pack = build_context_pack(
        {"id": "current"},
        text_context=[{"chunk_hash": "a", "text": "abcdef"}, {"chunk_hash": "b", "text": "ghijkl"}],
        max_text_items=2,
        max_text_chars=8,
    ).to_dict()
    assert [item["text"] for item in pack["text_context"]] == ["abcdef", "gh"]
    assert len("".join(item["text"] for item in pack["text_context"])) == 8


def test_current_frame_is_required_and_reference_selection_is_deterministic():
    with pytest.raises(ValueError, match="current_frame"):
        build_context_pack(None)
    candidates = [
        {"record_id": "second", "parent_id": "p2", "score": 0.8},
        {"record_id": "first", "parent_id": "p1", "score": 0.8},
    ]
    first = build_context_pack({"id": "current"}, retrieved=candidates).to_dict()
    second = build_context_pack({"id": "current"}, retrieved=candidates).to_dict()
    assert [item["record_id"] for item in first["retrieved"]] == ["second", "first"]
    assert first == second


def test_context_pack_only_considers_the_top_twenty_retrieved_candidates():
    candidates = [
        {"record_id": f"frame-{number}", "parent_id": f"parent-{number}", "score": 1.0 - number / 100}
        for number in range(20)
    ]
    candidates.append({"record_id": "outside-window", "parent_id": "outside-window", "score": 1.0})

    pack = build_context_pack({"id": "current"}, retrieved=candidates, max_reference_images=4).to_dict()

    assert all(item["record_id"] != "outside-window" for item in pack["retrieved"])
