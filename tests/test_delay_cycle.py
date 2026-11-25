"""The delay picker: every value reachable in one tap, up to 5 seconds.

The delay used to be a cycle, which is fine for four options and unusable for
eleven: reaching 5 s meant tapping blindly and hoping, and landing on the wrong
value during a live giveaway is worse than not changing it at all. These tests
pin the picker's shape (every choice present, exactly one marked, callback data
that the handler can parse back) rather than its pixels.
"""

from __future__ import annotations

import pytest

from qna_bot.dashboard.handlers.groups import _delay_picker_text
from qna_bot.dashboard.keyboards import (
    HUMAN_DELAY_CHOICES,
    delay_picker_kb,
    group_config_kb,
)
from qna_bot.storage.database import GroupConfig


def _group(delay_ms: int = 0) -> GroupConfig:
    return GroupConfig(chat_id=-1001234567890, title="Giveaway Arena", human_delay_ms=delay_ms)


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


# ---------------------------------------------------------------------------
# The range itself
# ---------------------------------------------------------------------------


def test_the_range_goes_up_to_five_seconds() -> None:
    assert max(HUMAN_DELAY_CHOICES) == 5000


def test_the_range_starts_at_off_and_has_no_duplicates() -> None:
    assert HUMAN_DELAY_CHOICES[0] == 0
    assert len(set(HUMAN_DELAY_CHOICES)) == len(HUMAN_DELAY_CHOICES)
    assert list(HUMAN_DELAY_CHOICES) == sorted(HUMAN_DELAY_CHOICES)


def test_no_gap_is_larger_than_the_step() -> None:
    """Any value the owner wants must be reachable without overshooting.

    Half-second steps: 1.7 s is not offered, but nothing in the useful range is
    more than 500 ms away from a choice, which is finer than a human's own
    timing variance.
    """
    steps = [
        b - a for a, b in zip(HUMAN_DELAY_CHOICES, HUMAN_DELAY_CHOICES[1:], strict=False)
    ]
    assert max(steps) <= 500


# ---------------------------------------------------------------------------
# The keyboard
# ---------------------------------------------------------------------------


def test_every_choice_has_a_button() -> None:
    markup = delay_picker_kb(_group(), back_page=0, mode_flag="d")
    labels = [b.text for b in _buttons(markup)]
    for choice in HUMAN_DELAY_CHOICES:
        expected = "Off" if choice == 0 else f"{choice / 1000:g}s"
        assert any(expected in label for label in labels), f"{expected} missing"


def test_the_current_value_is_marked_and_only_it() -> None:
    markup = delay_picker_kb(_group(2500), back_page=0, mode_flag="d")
    marked = [b.text for b in _buttons(markup) if b.text.startswith("✅")]
    assert len(marked) == 1
    assert "2.5s" in marked[0]


def test_marking_follows_the_saved_value() -> None:
    for choice in HUMAN_DELAY_CHOICES:
        markup = delay_picker_kb(_group(choice), back_page=0, mode_flag="d")
        marked = [b.text for b in _buttons(markup) if b.text.startswith("✅")]
        assert len(marked) == 1, f"delay {choice} did not mark exactly one button"


def test_each_button_carries_a_parseable_callback() -> None:
    """The handler splits on ':', so the data must survive that round trip."""
    markup = delay_picker_kb(_group(), back_page=3, mode_flag="w")
    for button in _buttons(markup):
        data = button.callback_data or ""
        if data.startswith("gdelay:") and data.split(":")[1] != "menu":
            parts = data.split(":")
            assert len(parts) == 5
            assert parts[1] == "-1001234567890"
            assert int(parts[2]) in HUMAN_DELAY_CHOICES
            assert int(parts[3]) == 3
            assert parts[4] == "w"


def test_callback_data_stays_within_the_telegram_limit() -> None:
    """Telegram rejects callback data over 64 bytes outright."""
    markup = delay_picker_kb(_group(), back_page=0, mode_flag="d")
    for button in _buttons(markup):
        assert len((button.callback_data or "").encode()) <= 64


def test_the_picker_bows_out_to_the_settings_screen() -> None:
    markup = delay_picker_kb(_group(), back_page=2, mode_flag="w")
    back = [b for b in _buttons(markup) if "Back" in (b.text or "")]
    assert back, "the picker needs a way back"
    assert back[-1].callback_data == "grp:cfg:-1001234567890:2:w"


# ---------------------------------------------------------------------------
# Entry point from the group settings screen
# ---------------------------------------------------------------------------


def test_the_settings_button_opens_the_picker_not_a_cycle() -> None:
    markup = group_config_kb(_group(1500), back_page=0, mode_flag="d")
    delay_buttons = [
        b for b in _buttons(markup) if (b.callback_data or "").startswith("gdelay:")
    ]
    assert delay_buttons, "the delay row must open the picker"
    assert delay_buttons[0].callback_data.startswith("gdelay:menu:")
    # The old cycling callback must be gone from the settings screen: tapping
    # it now saves nothing, and leaving it there would look like a dead button.
    assert not [
        b for b in _buttons(markup) if (b.callback_data or "").startswith("gcfg:")
        and (b.callback_data or "").split(":")[2] == "delay"
    ]


def test_the_settings_button_shows_the_saved_value() -> None:
    markup = group_config_kb(_group(3000), back_page=0, mode_flag="d")
    label = [b.text for b in _buttons(markup) if (b.callback_data or "").startswith("gdelay:")][0]
    assert "3s" in label


# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delay_ms,expected", [(0, "Off"), (500, "0.5s"), (5000, "5s")])
def test_the_screen_names_the_current_value(delay_ms: int, expected: str) -> None:
    text = _delay_picker_text(_group(delay_ms))
    assert expected in text


def test_the_screen_explains_that_it_covers_every_reply() -> None:
    """The owner's original complaint, answered in the place they will look."""
    text = _delay_picker_text(_group(1500))
    lowered = text.lower()
    for phrase in ("every", "instant math", "forex", "photo"):
        assert phrase in lowered
    # And the first-comment exception is stated, not hidden.
    assert "250" in text
