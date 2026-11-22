"""Tests for fast local message pre-filtering."""

from qna_bot.storage.database import GroupConfig
from qna_bot.userbot.filters import should_evaluate_message


def _make_group(**overrides) -> GroupConfig:
    defaults = dict(
        chat_id=-1001,
        title="Test Group",
        enabled=True,
        detection_mode="giveaway_aggressive",
        local_math_enabled=True,
        trigger_keywords="",
    )
    defaults.update(overrides)
    return GroupConfig(**defaults)


def test_skips_trivial_chat_and_commands() -> None:
    g = _make_group()
    for msg in ("", "   ", "/start", "gm", "congrats!", "https://t.me/test", "🔥🔥🔥"):
        assert should_evaluate_message(msg, g).should_process is False


def test_giveaway_aggressive_catches_prompts_without_qmark() -> None:
    g = _make_group(detection_mode="giveaway_aggressive")
    for msg in (
        "Capital of Ethiopia",
        "Unscramble: L E P A P",
        "47 * 83",
        "Who won the 2022 World Cup?",
        "Q1: Name the largest ocean",
    ):
        assert should_evaluate_message(msg, g).should_process is True


def test_strict_mode_requires_qmark_or_quiz_format() -> None:
    g = _make_group(detection_mode="strict")
    assert should_evaluate_message("Capital of Ethiopia", g).should_process is False
    assert should_evaluate_message("Capital of Ethiopia?", g).should_process is True
    assert should_evaluate_message("Q1: Capital of Ethiopia", g).should_process is True
    # Math fast-path still works when local_math_enabled is True
    assert should_evaluate_message("25 * 4", g).should_process is True


def test_custom_trigger_keywords_override() -> None:
    g = _make_group(trigger_keywords="Q:, Giveaway")
    assert should_evaluate_message("What is 2 + 2?", g).should_process is False
    assert should_evaluate_message("Q: What is 2 + 2?", g).should_process is True
    assert should_evaluate_message("Giveaway round 1: Capital of Kenya", g).should_process is True
