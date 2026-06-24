from __future__ import annotations

from modules.community.shard_tracker.mercy import MERCY_CONFIGS, format_percent, mercy_state


def test_mercy_before_threshold():
    config = MERCY_CONFIGS["sacred"]
    state = mercy_state("sacred", 5)

    assert state.chance == config.base_rate
    assert state.threshold == config.threshold
    assert state.percent == 6.0


def test_mercy_after_threshold_adds_increment():
    config = MERCY_CONFIGS["ancient"]
    pulls = config.threshold + 3
    state = mercy_state("ancient", pulls)

    expected = config.base_rate + (pulls - config.threshold) * config.increment
    assert state.chance == expected


def test_mercy_caps_at_hundred_percent():
    state = mercy_state("void", 240)

    assert state.chance == 1.0
    assert state.cap_at >= state.threshold


def test_format_percent_precision():
    assert format_percent(0.0123) == "1.23%"
    assert format_percent(0.25) == "25.0%"


def test_remnant_mythic_mercy_examples():
    assert mercy_state("remnant_mythic", 0).chance == 0.025
    assert mercy_state("remnant_mythic", 24).chance == 0.025
    assert mercy_state("remnant_mythic", 25).chance == 0.035
    assert mercy_state("remnant_mythic", 26).chance == 0.045


def test_remnant_mythic_caps_at_hundred_percent():
    assert mercy_state("remnant_mythic", 200).chance == 1.0
