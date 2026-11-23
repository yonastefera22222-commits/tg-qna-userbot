"""Emoji handling for inbound questions and outbound answers.

Giveaway hosts decorate questions heavily: ``🎯 47 * 83 ✅🔥🔥🔥``. Emoji are
multi-codepoint (flags are two regional indicators, keycaps are digit +
variation selector + combining enclosing keycap, families are ZWJ sequences),
so naive handling leaves invisible fragments behind that break parsing.

Order matters here. Variation selectors and the combining enclosing keycap are
removed *first*, which turns ``2️⃣`` back into a plain ``2`` instead of
deleting the digit with its decoration. Only then are whole emoji removed.
"""

from __future__ import annotations

import re

#: Codepoint ranges that hold emoji and their invisible joins/modifiers.
EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x1F000, 0x1F0FF),  # mahjong / playing cards
    (0x1F100, 0x1F1E5),  # enclosed alphanumerics
    (0x1F1E6, 0x1F1FF),  # regional indicators (flags are pairs of these)
    (0x1F200, 0x1F2FF),  # enclosed ideographic supplement
    (0x1F300, 0x1F5FF),  # misc symbols and pictographs
    (0x1F600, 0x1F64F),  # emoticons
    (0x1F650, 0x1F67F),  # ornamental dingbats
    (0x1F680, 0x1F6FF),  # transport and map
    (0x1F700, 0x1F77F),  # alchemical
    (0x1F780, 0x1F7FF),  # geometric shapes extended
    (0x1F800, 0x1F8FF),  # supplemental arrows-C
    (0x1F900, 0x1F9FF),  # supplemental symbols and pictographs
    (0x1FA00, 0x1FAFF),  # chess, symbols and pictographs extended-A
    (0x2600, 0x27BF),    # misc symbols + dingbats (☀ ✅ ❤ ✨ ➡)
    (0x2B00, 0x2BFF),    # stars, arrows (⬆ ⭐ ⭕)
    (0x2190, 0x21FF),    # arrows (→ ↔)
    (0x2900, 0x297F),    # supplemental arrows-B
    (0x3030, 0x3030),    # wavy dash
    (0x303D, 0x303D),    # part alternation mark
    (0x3297, 0x3297),    # circled ideograph congratulation ㊗
    (0x3299, 0x3299),    # circled ideograph secret ㊙
)

#: Zero-width glue and modifiers that are invisible on their own. Includes the
#: bidi marks Telegram inserts around mixed-script text (\u200e, \u200f) and the
#: bidirectional override \u202e, which is a single character that silently
#: reverses a whole line.
JOINERS = frozenset({
    "\u200b", "\u200c", "\u200d", "\u200e", "\u200f",
    "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",
    "\u2060", "\u2066", "\u2067", "\u2068", "\u2069", "\ufeff",
})
#: U+FE0E text presentation, U+FE0F emoji presentation.
VARIATION_SELECTORS = frozenset({"\ufe0e", "\ufe0f"})
#: U+20E3 combining enclosing keycap — the box around ``2️⃣``.
KEYCAP = "\u20e3"
#: Skin-tone modifiers U+1F3FB..U+1F3FF.
SKIN_TONES = frozenset(chr(c) for c in range(0x1F3FB, 0x1F400))

#: Purely decorative symbols that are not emoji but still carry no meaning in a
#: question (a model or a host may paste them).
DECORATIVE = frozenset({"™", "©", "®", "℠", "℃"})

_INVISIBLE = JOINERS | VARIATION_SELECTORS | {KEYCAP} | SKIN_TONES


def is_emoji_char(ch: str) -> bool:
    """True when this single codepoint is emoji, a modifier, or glue."""
    if ch in _INVISIBLE:
        return True
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in EMOJI_RANGES)


#: Unicode spaces that look like a space but are not ``" "``. ``"47 + 83"``
#: fails the arithmetic parser because it only tolerates ordinary whitespace.
EXOTIC_SPACES = {
    "\u00a0": " ",  # no-break space
    "\u1680": " ",  # ogham space mark
    "\u2000": " ", "\u2001": " ", "\u2002": " ", "\u2003": " ",
    "\u2004": " ", "\u2005": " ", "\u2006": " ", "\u2007": " ",
    "\u2008": " ", "\u2009": " ", "\u200a": " ",
    "\u202f": " ",  # narrow no-break space
    "\u205f": " ",  # medium mathematical space
    "\u3000": " ",  # ideographic space
}


def normalize_spaces(text: str) -> str:
    """Replace exotic spaces with ordinary ones and drop control characters."""
    if not text:
        return text
    return "".join(
        EXOTIC_SPACES.get(ch, ch if (ch >= " " or ch in "\n\t") else "") for ch in text
    )


def prepare_incoming(text: str) -> str:
    """Normalise a question before any local analysis.

    Emoji, joiners, bidi marks and exotic spaces are the most common reason a
    question that reads perfectly to a human fails to parse or fails to look
    like a question at all.
    """
    return normalize_spaces(strip_emoji(text))


def strip_emoji(text: str) -> str:
    """Remove every emoji, joiner and modifier, keeping the digits inside keycaps.

    ``"2️⃣ + 3️⃣"`` becomes ``"2 + 3"``; ``"🎯 47 * 83 ✅"`` becomes ``" 47 * 83 "``.
    """
    if not text:
        return ""
    return "".join(ch for ch in text if not is_emoji_char(ch) and ch not in DECORATIVE)


def collapse_emoji_runs(text: str, limit: int = 2) -> str:
    """Shorten emoji spam: ``🔥🔥🔥🔥🔥`` -> ``🔥🔥``; text is untouched."""
    if not text:
        return text
    out: list[str] = []
    run = 0
    for ch in text:
        if is_emoji_char(ch) and ch not in JOINERS and ch not in VARIATION_SELECTORS:
            run += 1
            if run <= limit:
                out.append(ch)
        else:
            run = 0
            out.append(ch)
    return "".join(out)


def strip_leading_emoji(text: str) -> str:
    """Drop a decorative prefix such as ``🎯 Nairobi`` -> ``Nairobi``.

    Only applied when real text follows, so a genuine emoji answer (a flag, a
    single letter) survives.
    """
    if not text:
        return text
    idx = 0
    while idx < len(text) and (is_emoji_char(text[idx]) or text[idx].isspace()):
        idx += 1
    remainder = text[idx:]
    if not remainder or strip_emoji(remainder).strip() == "":
        return text  # the whole answer is emoji: it may be the answer itself
    return remainder


def has_text_content(text: str) -> bool:
    """True when at least one letter or digit survives after removing emoji."""
    return any(ch.isalnum() for ch in strip_emoji(text))


#: Captions shorter than this beside a photo are treated as wrappers rather than
#: as the question itself, unless they actually look like a question.
IMAGE_CAPTION_STUB_CHARS = 120

#: Question markers in either script. Duplicated deliberately from the filter's
#: own lists: the engine must not import from ``userbot`` (the filter already
#: imports from ``ai``, and the reverse would be a cycle), and these two lists
#: are small enough that keeping them honest is cheaper than the indirection.
_QUESTION_WORDS = re.compile(
    r"\b(?:who|what|what's|whats|when|where|why|how|which|whose|whom|"
    r"name|list|identify|define|explain|tell|give|guess|solve|calculate|"
    r"compute|find|unscramble|rearrange|spell|translate|complete|fill|"
    r"true\s+or\s+false|capital\s+of|full\s+form|stands\s+for|invented|"
    r"discovered|wrote|founder|president|ceo|year|country|city|planet|"
    r"chemical\s+symbol|formula|meaning\s+of|opposite\s+of|synonym)\b",
    re.IGNORECASE,
)
_AMHARIC_QUESTION_WORDS = re.compile(
    r"(?:ማን|ምን|የት|መቼ|ለምን|እንዴት|ስንት|ስንቱ|የቱ|የትኛው|ማንኛው|ማንኛችሁ|"
    r"ስም|ስሙ|ዋና\s*ከተማ|ዋና\s*ከተማዋ|ሀገር|አገር|ፈጣሪ|ተፈጥሮ|ምርጫ|ትርጉም|"
    r"ስንተኛ|ስንት\s*ነው|ምን\s*ይባላል|ማን\s*ይባላል|ምን\s*ይሆናል)"
)


def looks_like_question(text: str) -> bool:
    """True when the text carries a question marker in either script."""
    if "?" in text or "፧" in text:
        return True
    return bool(_QUESTION_WORDS.search(text)) or bool(_AMHARIC_QUESTION_WORDS.search(text))


def is_photo_caption_stub(text: str) -> bool:
    """True when a caption is too thin to be the question itself.

    A Telegram post has to wrap its media in *something*, so giveaway posters
    routinely arrive captioned "answer below", "🔥🔥", "@ourchannel" or "Read the
    card" with the real question printed on the picture. Both the filter (which
    must not drop such a post) and the engine (which must transcribe rather than
    answer the wrapper) need this judgement, so it lives in one place.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    if len(cleaned) > IMAGE_CAPTION_STUB_CHARS:
        return False
    return not looks_like_question(cleaned)
