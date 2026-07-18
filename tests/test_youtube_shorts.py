import random

from scripts.merge_state import merge_states
from scripts.render_short import (
    build_metadata,
    load_quotes,
    make_title,
    select_quote,
    validate_channel_name,
    wrap_quote,
)


def sample_quotes():
    return [
        {"id": "q001", "quote": "A steady step can carry a brave plan forward.", "pexels_search": "walking sunrise", "category": "consistency"},
        {"id": "q002", "quote": "Useful work becomes confidence when repeated.", "pexels_search": "focused worker", "category": "confidence"},
        {"id": "q003", "quote": "Choose the direction that makes tomorrow stronger.", "pexels_search": "mountain road", "category": "growth"},
    ]


def test_quote_selection_avoids_repeats_until_catalog_is_used():
    state = {"version": 1, "cycle": 1, "used_ids": [], "history": []}
    rng = random.Random(7)
    selected = [select_quote(sample_quotes(), state, rng=rng)["id"] for _ in range(3)]
    assert len(set(selected)) == 3
    fourth = select_quote(sample_quotes(), state, rng=rng)["id"]
    assert fourth in {"q001", "q002", "q003"}
    assert len(state["used_ids"]) == 1
    assert state["cycle"] == 2


def test_wrap_quote_respects_line_width_and_safe_line_count():
    wrapped = wrap_quote("Patient effort gives every difficult goal a fair chance to become real.", width=22, max_lines=5)
    lines = wrapped.splitlines()
    assert len(lines) <= 5
    assert all(len(line) <= 22 for line in lines)


def test_metadata_contains_attribution_quote_and_safe_title():
    entry = sample_quotes()[0]
    metadata = build_metadata(
        entry,
        creator_name="Example Creator",
        source_url="https://www.pexels.com/video/example-123/",
        channel_name="Daily Resolve",
        privacy_status="private",
        music_name="calm.mp3",
    )
    assert len(metadata["title"]) <= 100
    assert metadata["title"].endswith("#shorts")
    assert entry["quote"] in metadata["description"]
    assert "Example Creator" in metadata["description"]
    assert metadata["made_for_kids"] is False
    assert metadata["privacy_status"] == "private"


def test_long_title_is_capped_at_one_hundred_characters():
    title = make_title("steady progress " * 20)
    assert len(title) <= 100
    assert title.endswith("#shorts")


def test_every_catalog_quote_fits_the_render_safe_area():
    for entry in load_quotes():
        wrapped = wrap_quote(entry["quote"])
        assert len(wrapped.splitlines()) <= 7


def test_channel_name_is_limited_to_safe_width():
    assert validate_channel_name("  Daily   Resolve  ") == "Daily Resolve"


def test_state_merge_is_idempotent_and_preserves_both_histories():
    current = {"version": 1, "cycle": 1, "used_ids": ["q001"], "history": [{"id": "q001", "used_at": "2026-01-01T00:00:00+00:00"}]}
    rendered = {"version": 1, "cycle": 1, "used_ids": ["q001", "q002"], "history": [{"id": "q002", "used_at": "2026-01-02T00:00:00+00:00"}]}
    merged = merge_states(current, rendered)
    assert merged["used_ids"] == ["q001", "q002"]
    assert [event["id"] for event in merged["history"]] == ["q001", "q002"]
    assert merge_states(merged, rendered) == merged


def test_newer_quote_cycle_does_not_restore_old_used_ids():
    current = {"version": 1, "cycle": 1, "used_ids": ["q001", "q002"], "history": []}
    rendered = {"version": 1, "cycle": 2, "used_ids": ["q003"], "history": []}
    merged = merge_states(current, rendered)
    assert merged["cycle"] == 2
    assert merged["used_ids"] == ["q003"]
