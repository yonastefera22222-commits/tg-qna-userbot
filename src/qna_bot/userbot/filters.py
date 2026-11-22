"""Fast local pre-filters for detecting candidate Q&A / giveaway messages before hitting AI."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..ai.math_solver import try_solve_math
from ..storage.database import GroupConfig

# Common casual chat messages that are never giveaway questions
_TRIVIAL_CHAT = frozenset(
    {
        "ok",
        "okay",
        "k",
        "kk",
        "yes",
        "no",
        "yep",
        "nope",
        "yeah",
        "nah",
        "lol",
        "lmao",
        "haha",
        "hahaha",
        "nice",
        "cool",
        "thanks",
        "thank you",
        "thx",
        "ty",
        "gm",
        "gn",
        "hi",
        "hello",
        "hey",
        "yo",
        "sup",
        "congrats",
        "congratulations",
        "gg",
        "rip",
        "wow",
        "omg",
        "bruh",
        "bro",
        "wait",
        "ready",
        "go",
        "start",
        "next",
        "done",
        "me",
        "here",
        "present",
        "claim",
        "claimed",
        "winner",
        "first",
    }
)

_URL_ONLY_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
_HAS_ALNUM_RE = re.compile(r"[A-Za-z0-9\u00C0-\u024F\u0400-\u04FF\u0600-\u06FF\u1200-\u137F]")

# Question & giveaway trigger patterns
_QUESTION_WORD_RE = re.compile(
    r"\b(?:who|what|what's|whats|when|where|why|how|which|whose|whom|"
    r"name|list|identify|define|explain|tell|give|guess|solve|calculate|"
    r"compute|find|unscramble|rearrange|spell|translate|complete|fill|"
    r"true\s+or\s+false|capital\s+of|full\s+form|stands\s+for|invented|"
    r"discovered|wrote|founder|president|ceo|year|country|city|planet|"
    r"chemical\s+symbol|formula|meaning\s+of|opposite\s+of|synonym)\b",
    re.IGNORECASE,
)

_QUIZ_FORMAT_RE = re.compile(
    r"(?:^q\d*\s*[:.)\-]|^question\s*\d*\s*[:.)\-]|_{2,}|"
    r"\ba\)\s*.+\bb\)|^\d+\s*[+\-*/×÷^]\s*\d+)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class FilterDecision:
    should_process: bool
    reason: str


def should_evaluate_message(
    text: str, group: GroupConfig, *, has_image: bool = False
) -> FilterDecision:
    """Decide in <0.1ms whether a message should be forwarded to local-math / AI.

    When ``has_image`` is set the message carries a photo, which changes the
    rules: a photo with no caption is a perfectly normal way to ask a giveaway
    question ("solve this", a screenshot of a quiz), so empty text is no longer a
    reason to skip it.
    """
    cleaned = (text or "").strip()

    if not cleaned:
        if has_image:
            if not getattr(group, "vision_enabled", True):
                return FilterDecision(False, "vision_disabled")
            return FilterDecision(True, "image_only")
        return FilterDecision(False, "empty")

    # Ignore bot slash-commands (e.g. /start, /help@somebot)
    if cleaned.startswith("/"):
        return FilterDecision(False, "command")

    # Ignore pure URLs or emoji-only messages (an image counts as content)
    if not has_image and (_URL_ONLY_RE.match(cleaned) or not _HAS_ALNUM_RE.search(cleaned)):
        return FilterDecision(False, "non_text")

    if has_image and not getattr(group, "vision_enabled", True):
        return FilterDecision(False, "vision_disabled")

    # Ignore overly long essays/announcements (>600 chars usually aren't quick Q&A giveaways)
    if len(cleaned) > 600:
        return FilterDecision(False, "too_long")

    lower = cleaned.lower()

    # Check custom per-group trigger keywords first (if configured, they are required!)
    keywords = group.trigger_keyword_list()
    if keywords:
        if any(kw in lower for kw in keywords):
            return FilterDecision(True, "keyword_match")
        return FilterDecision(False, "no_keyword_match")

    # Fast bypass if it's clearly a pure math giveaway prompt
    if group.local_math_enabled and try_solve_math(cleaned) is not None:
        return FilterDecision(True, "math_detected")

    # Skip trivial 1-2 word chatter
    stripped_punct = lower.strip(" .!?,~")
    if stripped_punct in _TRIVIAL_CHAT:
        return FilterDecision(False, "trivial_chat")

    has_question_mark = "?" in cleaned
    has_q_word = bool(_QUESTION_WORD_RE.search(cleaned))
    has_quiz_fmt = bool(_QUIZ_FORMAT_RE.search(cleaned))

    mode = group.detection_mode

    if mode == "strict":
        if has_question_mark or has_quiz_fmt:
            return FilterDecision(True, "strict_match")
        return FilterDecision(False, "strict_no_qmark")

    if mode == "balanced":
        if has_question_mark or has_quiz_fmt or (has_q_word and len(cleaned.split()) >= 3):
            return FilterDecision(True, "balanced_match")
        return FilterDecision(False, "balanced_skip")

    # Default: "giveaway_aggressive"
    # Catches short giveaway trivia even without '?' like "Capital of Ethiopia",
    # "Unscramble: TLEGERMA", "First president of USA", "H2O stands for"
    if has_question_mark or has_quiz_fmt or has_q_word:
        return FilterDecision(True, "giveaway_signal")

    word_count = len(cleaned.split())
    if word_count >= 3:
        return FilterDecision(True, "giveaway_candidate")

    return FilterDecision(False, "too_short_non_question")


# ---------------------------------------------------------------------------
# Human-readable reasons
# ---------------------------------------------------------------------------

#: Plain-English explanation for every :class:`FilterDecision` reason string.
REASON_TEXT: dict[str, str] = {
    "empty": "the message had no text",
    "command": "it is a bot command (starts with /)",
    "non_text": "it was only a link or emoji",
    "too_long": "it is longer than 600 characters",
    "keyword_match": "it matched your custom trigger keywords",
    "no_keyword_match": "it did not contain any of your custom trigger keywords",
    "math_detected": "it contains arithmetic",
    "trivial_chat": "it is short casual chat (ok, lol, thanks, hi…)",
    "strict_match": "it has a question mark / quiz format",
    "strict_no_qmark": "Strict mode requires a '?' or quiz format",
    "balanced_match": "it looks like a question",
    "balanced_skip": "Balanced mode did not detect a clear question",
    "giveaway_signal": "it contains a question or trivia keyword",
    "giveaway_candidate": "it has 3+ words (aggressive mode)",
    "too_short_non_question": "it is too short and not a question (aggressive mode)",
    "image_only": "it is a photo (no caption) and photo answers are enabled",
    "vision_disabled": "it is a photo but photo answers are disabled for this group",
}


def describe_reason(reason: str) -> str:
    return REASON_TEXT.get(reason, reason)
