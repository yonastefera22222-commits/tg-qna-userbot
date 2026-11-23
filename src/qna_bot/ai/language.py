"""Amharic / Ethiopic-script support.

Amharic is written in the Ethiopic script, which breaks several assumptions the
responder used to make:

* question words are not Latin (``ማን`` = who, ``ምን`` = what), so the English
  keyword lists never matched and Amharic questions were silently skipped;
* numbers are written with their own digits (``፵፫`` = 43), so arithmetic looked
  like text and the instant math solver gave up;
* years are usually given in the Ethiopian calendar (``2018 ዓ.ም``), which is
  roughly seven to eight years behind the Gregorian one.

The functions here are deliberately dependency-free and cheap: they run on the
pre-filter path, before any network call.
"""

from __future__ import annotations

#: Unicode blocks holding Ethiopic script characters.
_ETHIOPIC_RANGES: tuple[tuple[int, int], ...] = (
    (0x1200, 0x137F),  # Ethiopic
    (0x1380, 0x139F),  # Ethiopic Supplement
    (0x2D80, 0x2DDF),  # Ethiopic Extended
    (0xAB00, 0xAB2F),  # Ethiopic Extended-A
)

AMHARIC = "am"
ENGLISH = "en"

_LANGUAGE_NAMES = {AMHARIC: "Amharic", ENGLISH: "English"}

#: Every code point in the Ethiopic blocks, for O(1) membership tests.
_ETHIOPIC_CODE_POINTS = frozenset(
    c for lo, hi in _ETHIOPIC_RANGES for c in range(lo, hi + 1)
)


def _is_ethiopic_char(ch: str) -> bool:
    return ord(ch) in _ETHIOPIC_CODE_POINTS


def ethiopic_ratio(text: str) -> float:
    """Fraction of the text's *letters* that are Ethiopic script.

    Digits, spaces and punctuation are ignored so a message like ``43 + 92``
    does not dilute the result, and an Amharic sentence keeps a ratio near 1.0
    even when it contains numbers.
    """
    if not text:
        return 0.0
    ethiopic = 0
    total = 0
    for ch in text:
        if ch.isdigit() or ch.isspace():
            continue
        if ord(ch) in _ETHIOPIC_CODE_POINTS:
            ethiopic += 1
            total += 1
        elif ch.isalpha():
            total += 1
    if total == 0:
        return 0.0
    return ethiopic / total


#: Share of Ethiopic letters at which a message counts as Amharic.
#:
#: Mixed questions are the normal case in these groups, not the exception:
#: "FIFA ሲተነተን ምን ማለት ነው" (0.75 Ethiopic) and "explain VAR ማለት ምን ነው" (0.41)
#: are both Amharic questions with an English term embedded. A 0.5 cut sent the
#: second one down the English path and answered it in English. A third of the
#: letters is enough to know the asker is writing Amharic.
MIXED_AMHARIC_THRESHOLD = 0.25


def detect_language(text: str, *, threshold: float = MIXED_AMHARIC_THRESHOLD) -> str:
    """Return ``"am"`` for Amharic text, else ``"en"``.

    The threshold is a share of letters, not of characters, and it is set low on
    purpose: a question that embeds an English name or term inside an Amharic
    sentence is still an Amharic question.
    """
    return AMHARIC if ethiopic_ratio(text) >= threshold else ENGLISH


def is_mixed(text: str) -> bool:
    """True when the text carries both Ethiopic and Latin letters.

    Mixed questions need their own rule: the answer should be in the language
    being *asked* (the Ethiopic one, when the Ethiopic text is the question
    frame), while any term quoted in English must be kept as the English term
    rather than translated into something the host will not recognise.
    """
    ethiopic = any(ord(ch) in _ETHIOPIC_CODE_POINTS for ch in text or "")
    latin = any(ch.isascii() and ch.isalpha() for ch in text or "")
    return ethiopic and latin


def is_amharic(text: str) -> bool:
    return detect_language(text) == AMHARIC


def language_name(code: str) -> str:
    return _LANGUAGE_NAMES.get(code, code)


#: Appended to every system prompt. Kept byte-identical across requests so the
#: provider's prompt cache still hits: appending a *stable* line does not
#: fragment the cached prefix, unlike building a per-language prompt.
#:
#: This replaces an earlier "always answer in the language the question was
#: asked in, never translate" rule. That rule was wrong for the case it was
#: meant to cover: asked what FIFA stands for, the correct answer is the French
#: expansion, and refusing to translate produced a useless answer. The rule now
#: is *most appropriate language* - the language the answer is conventionally
#: given in - which for ordinary facts is the question's language and for
#: names, acronyms, institutions and terms of art is the language the thing is
#: actually called in.
ANSWER_IN_LANGUAGE_INSTRUCTION = (
    "Answer in the language that best fits the ANSWER, not automatically the "
    "language of the question.\n"
    "- Ordinary facts, explanations and arithmetic: use the question's language "
    "(Amharic question -> Amharic answer).\n"
    "- Names, brands, institutions, acronyms and technical terms that are "
    "formally known in another language: give the formal form in that language, "
    "then the question's language if a word of context helps. Asked in Amharic "
    "what FIFA stands for, the answer is 'Fédération Internationale de Football "
    "Association', not a transliteration and not a translation.\n"
    "- Never answer a 'what does X mean / stand for' question by repeating or "
    "restating the question. It asks for the expansion or definition of X.\n"
    "- Do not transliterate a foreign name into Ethiopic letters; write it as it "
    "is actually written.\n"
    "Emoji and decoration around a question are not part of it: answer the "
    "words. Never repeat the question's emoji in your answer."
)

#: Extra guidance used only when the question itself is in Amharic.
AMHARIC_GUIDANCE = (
    "The question is in Amharic (Ethiopic script). The answer should read "
    "naturally to an Amharic speaker, but the language of the ANSWER follows the "
    "answer's own subject matter (see above): a definition or explanation in "
    "Amharic, a formal name or acronym in the language it is actually written "
    "in.\n"
    "- If the question MIXES English and Amharic (for example \"FIFA ሲተነተን ምን "
    "ማለት ነው\" or \"explain VAR ማለት ምን ነው\"), answer in Amharic, and keep any "
    "name, acronym or technical term written in English exactly as it was "
    "written unless its formal expansion is the better answer.\n"
    "- A question asking what a word MEANS or STANDS FOR (\"X ማለት ምን ነው\", "
    "\"FIFA ሲተነተን ምን ማለት ነው\") wants the definition or expansion of X, not a "
    "restatement of the question and not a sentence about the question.\n"
    "- If the question uses Amharic numerals (፩ ፪ ፫ … ፲ ፳ ፻ ፼), reply using "
    "Amharic numerals too.\n"
    "- If the question refers to a year with ዓ.ም / ዓ.ዓ, use the Ethiopian "
    "calendar in your answer, not the Gregorian one.\n"
    "- Keep the answer to the bare fact, exactly as a giveaway requires."
)


def prompt_guidance(text: str) -> str:
    """Extra system-prompt guidance for this question's language ("" if none)."""
    return AMHARIC_GUIDANCE if is_amharic(text) else ""


# ---------------------------------------------------------------------------
# Ethiopic numerals
# ---------------------------------------------------------------------------

_AMHARIC_DIGITS = {
    "፩": 1, "፪": 2, "፫": 3, "፬": 4, "፭": 5,
    "፮": 6, "፯": 7, "፰": 8, "፱": 9,
    "፲": 10, "፳": 20, "፴": 30, "፵": 40, "፶": 50,
    "፷": 60, "፸": 70, "፹": 80, "፺": 90,
}
_AMHARIC_HUNDRED = "፻"
_AMHARIC_TEN_THOUSAND = "፼"

AMHARIC_NUMERAL_CHARS = frozenset(
    set(_AMHARIC_DIGITS) | {_AMHARIC_HUNDRED, _AMHARIC_TEN_THOUSAND}
)


def parse_amharic_numeral(text: str) -> int | None:
    """Convert an Ethiopic numeral such as ``፵፫`` (43) or ``፪፻`` (200) to int.

    Ethiopic numerals are additive: a run of digits sums until ``፻`` (100) or
    ``፼`` (10 000) multiplies everything accumulated so far. ``፲፻`` is 1000,
    which is why the multiplier applies to the running total rather than to the
    single preceding digit.
    """
    cleaned = "".join(ch for ch in text if not ch.isspace())
    if not cleaned or any(ch not in AMHARIC_NUMERAL_CHARS for ch in cleaned):
        return None

    total = 0
    group = 0
    for ch in cleaned:
        if ch in _AMHARIC_DIGITS:
            group += _AMHARIC_DIGITS[ch]
        elif ch == _AMHARIC_HUNDRED:
            group = (group or 1) * 100
        elif ch == _AMHARIC_TEN_THOUSAND:
            total += (group or 1) * 10_000
            group = 0
        else:  # pragma: no cover - guarded by the check above
            return None
    return total + group


def has_amharic_numerals(text: str) -> bool:
    return any(ch in AMHARIC_NUMERAL_CHARS for ch in text or "")
