"""Forex/trading mode: detection, filtering, prompt guidance, persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from qna_bot.ai import forex
from qna_bot.ai.engine import AIEngine
from qna_bot.storage.crypto import SecretCipher
from qna_bot.storage.database import Database, GroupConfig
from qna_bot.userbot.filters import should_evaluate_message as filter_message


def _group(forex_enabled: bool, mode: str = "giveaway_aggressive") -> GroupConfig:
    group = GroupConfig(chat_id=-100, title="FX Signals", enabled=True)
    group.detection_mode = mode
    group.forex_enabled = forex_enabled
    return group


@pytest.mark.parametrize(
    "text",
    [
        "EURUSD?",
        "GBP/JPY trend",
        "xauusd resistance",
        "GU bias",
        "what is a pip",
        "how does leverage work",
        "cable direction",
        "dxy correlation",
        "micro lot vs mini lot",
        "explain swap rates",
        "USDJPY or EURUSD which has bigger pips",
    ],
)
def test_recognises_forex_questions(text: str) -> None:
    assert forex.is_forex_question(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "Capital of Kenya?",
        "What is 47 * 83",
        "who won the match",
        "hello",
        "good morning everyone",
        "ስንት ነው?",
        "",
    ],
)
def test_ignores_non_forex_text(text: str) -> None:
    assert forex.is_forex_question(text) is False


@pytest.mark.parametrize(
    "text",
    [
        "EURUSD price today?",
        "what's the current gold level",
        "XAUUSD forecast",
        "market update",
        "any news on NFP",
        "where is GU now",
        "will EURUSD rise tomorrow",
    ],
)
def test_detects_live_data_requests(text: str) -> None:
    assert forex.asks_for_live_data(text) is True


@pytest.mark.parametrize(
    "text",
    ["what is a pip", "how does leverage work", "explain stop loss"],
)
def test_definitional_questions_are_not_live_data(text: str) -> None:
    assert forex.asks_for_live_data(text) is False


def test_refusal_string_is_stable() -> None:
    # The prompt tells the model to reply with exactly this; keep it terse so it
    # reads as an honest answer rather than a paragraph of hedging.
    assert forex.NO_LIVE_DATA_ANSWER == "No live data"


def test_guidance_forbids_inventing_prices() -> None:
    guidance = forex.group_guidance()
    assert forex.NO_LIVE_DATA_ANSWER in guidance
    assert "NOT invent" in guidance
    assert "pips" in guidance
    assert "100,000" in guidance


def test_prompt_guidance_only_for_forex_text() -> None:
    assert forex.prompt_guidance("what is a pip") == forex.FOREX_GUIDANCE
    assert forex.prompt_guidance("capital of Kenya") == ""


def test_forex_group_prompt_includes_guidance() -> None:
    prompt = AIEngine._build_system_prompt("giveaway", "EURUSD?", forex_group=True)
    assert forex.NO_LIVE_DATA_ANSWER in prompt
    assert "pips" in prompt


def test_normal_group_prompt_has_no_forex_rules() -> None:
    prompt = AIEngine._build_system_prompt("giveaway", "Capital of Kenya?", forex_group=False)
    assert forex.NO_LIVE_DATA_ANSWER not in prompt
    assert "100,000" not in prompt


def test_giveaway_style_still_bare_in_forex_group() -> None:
    # Forex mode adds conventions; it must not turn a giveaway answer into an
    # essay. The bare-answer rules that the 48-token cap enforces stay intact.
    prompt = AIEngine._build_system_prompt("giveaway", "what is a pip", forex_group=True)
    assert "NO preamble" in prompt
    assert "as short as possible" in prompt
    assert "Keep giveaway answers to the bare term or number" in prompt


@pytest.mark.parametrize("text", ["GU bias", "XAU support", "cable"])
def test_shorthand_passes_filter_only_in_forex_groups(text: str) -> None:
    assert filter_message(text, _group(True)).reason == "forex_signal"
    assert filter_message(text, _group(False)).should_process is False


def test_shorthand_does_not_bypass_casual_chat() -> None:
    for chatter in ("hello", "nice one", "thanks bro"):
        assert filter_message(chatter, _group(True)).should_process is False


def test_forex_flag_defaults_off() -> None:
    assert GroupConfig(chat_id=-1, title="x").forex_enabled is False


@pytest.mark.asyncio
async def test_forex_flag_round_trips(tmp_path: Path) -> None:
    cipher = SecretCipher.from_env_or_file(None, tmp_path / ".secret.key")
    db = Database(tmp_path / "forex.db", cipher)
    await db.connect()
    try:
        group = await db.upsert_group_metadata(-100123, "Forex Giveaway")
        assert group.forex_enabled is False

        group.enabled = True
        group.forex_enabled = True
        await db.save_group(group)
        stored = await db.get_group(-100123)
        assert stored is not None and stored.forex_enabled is True

        stored.forex_enabled = False
        await db.save_group(stored)
        assert (await db.get_group(-100123)).forex_enabled is False
    finally:
        await db.close()


def test_group_keyboard_has_forex_toggle() -> None:
    from qna_bot.dashboard.keyboards import group_config_kb

    group = _group(True)
    buttons = [b for row in group_config_kb(group, 0, "d").inline_keyboard for b in row]
    forex_button = next(b for b in buttons if "Forex" in b.text)
    assert "ON" in forex_button.text
    assert forex_button.callback_data == "gcfg:-100:forex:0:d"

    group.forex_enabled = False
    buttons = [b for row in group_config_kb(group, 0, "d").inline_keyboard for b in row]
    assert "OFF" in next(b for b in buttons if "Forex" in b.text).text
