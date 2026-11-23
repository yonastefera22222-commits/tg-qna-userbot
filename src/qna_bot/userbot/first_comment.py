"""The "first comment" race.

Giveaway hosts announce a prize for whoever comments first, and the winning
move is a reply that lands before anyone else's. That is a latency contest, and
an AI round-trip is the one thing that cannot be in the path: a 400 ms model
call loses to a human who was already typing.

So this is handled exactly like the local math solver - recognised and answered
in-process, in microseconds, with no provider call, no prompt, no key, and no
chance of the model deciding the message "is not a question" and replying SKIP.

**Recognition is deliberately loose.** Hosts do not type "first comment"; they
type `First Comment 🔥`, `1st comment`, `first cmt`, `FC`, `f.c.`, or they wrap
the rule in a sentence: "first comment wins". A trigger that misses one variant
loses the giveaway, so the matcher tolerates decoration, punctuation,
abbreviations and the common typos, while a length guard keeps it from firing
on a long paragraph that merely mentions the words.
"""

from __future__ import annotations

import re

#: What the bot says when it wins the race. Deliberately two words, no emoji,
#: no punctuation: it has to look like a person in a group chat.
FIRST_COMMENT_ANSWER = "I win"

#: A message longer than this is a sentence, not a race signal. Without a guard
#: "the first comment on yesterday's post was rude" would trigger a reply. A real
#: trigger is a few words at most - "first comment", "1st cmt wins", "FC🔥" - so
#: the limit is kept tight and long announcements fall through to the normal
#: filter instead.
MAX_TRIGGER_WORDS = 5
MAX_TRIGGER_CHARS = 32

#: "first" in its usual misspellings, "1st" and "1 st".
_FIRST = r"(?:first|1st|1\s?st|fst|frst|firts|frist|fierst)"

#: "comment" and every abbreviation a host might actually use. "com" is
#: included because "first com" is common shorthand; it is safe here because
#: the whole token still has to follow "first".
_COMMENT = r"(?:comments?|cmt|cmnt|cment|comnt|comm?|com)\.?"

#: "first comment" / "1st cmt" / "first-comment" / "firstcomment".
_PHRASE_RE = re.compile(
    rf"\b{_FIRST}[\s\-_]*{_COMMENT}\b",
    re.IGNORECASE,
)

#: "comment first" - hosts phrase it both ways.
_REVERSED_RE = re.compile(
    rf"\b{_COMMENT}[\s\-_]*{_FIRST}\b",
    re.IGNORECASE,
)

#: Standalone abbreviations: "fc", "f.c.", "f/c", "f-c". Only accepted when the
#: whole message is essentially just that token, so "FC Barcelona" and a score
#: line like "FC 2-1" never fire.
_ABBREV_RE = re.compile(r"^(?:the\s+)?f\s*[./\-]?\s*c\s*[.!]*$", re.IGNORECASE)

#: Amharic: "first comment" and the common transliteration.
_AMHARIC_RE = re.compile(r"የመጀመሪያ\s*አስተያየት|መጀመሪያ\s*አስተያየት|ፊርስት\s*ኮመንት")

#: Reporting language: "first comment was mine", "first comment won". A host
#: issues an instruction, never a report.
_PAST_RE = re.compile(r"\b(?:won|was|were|had|did|mine|yours|theirs|ours)\b")

#: Words that legitimately surround the phrase in a host's announcement and do
#: not change its meaning ("first comment wins", "drop first comment below").
_FILLER_WORDS = frozenset({
    "the", "a", "an", "this", "is", "be", "to", "wins", "win", "winner",
    "wins!", "below", "now", "go", "drop", "get", "put", "write", "type",
    "say", "says", "said", "me", "my", "your", "you", "here", "first",
    "comment", "comments", "fast", "fastest", "quick", "quickly", "please",
    "pls", "plz", "guys", "fam", "team", "let's", "lets", "who", "who's",
    "whoever", "gets", "got", "it", "and", "or", "will", "would", "can",
})

_PUNCT_RE = re.compile(r"[^\w\s'’አ-ፚ-]+", re.UNICODE)


def _squash(text: str) -> str:
    """Words only, lowercased: decoration and punctuation removed."""
    return _PUNCT_RE.sub(" ", text).strip().lower()


def is_first_comment(text: str, *, max_words: int = MAX_TRIGGER_WORDS) -> bool:
    """True when the message is a "first comment" signal.

    Accepts decoration, punctuation, abbreviations and common misspellings;
    rejects long sentences that merely contain the words.
    """
    if not text:
        return False

    # The raw text is checked for Amharic before squashing, since the Ethiopic
    # block is not punctuation but must not be eaten either.
    if _AMHARIC_RE.search(text):
        return _within_length(text, max_words)

    squashed = _squash(text)
    if not squashed:
        return False

    # Past tense means someone is reporting a race, not starting one. No host
    # announces "first comment won" - that is a bystander describing last time.
    if _PAST_RE.search(squashed):
        return False

    if _ABBREV_RE.match(squashed):
        return True

    if not (_PHRASE_RE.search(squashed) or _REVERSED_RE.search(squashed)):
        return False

    # Past the length limit the phrase has to be the message, not a detail in
    # it: "first comment on yesterday was rude" is a sentence about a comment,
    # while "first comment wins" is an instruction. Filler alone is how a host
    # phrases it; anything carrying its own content words is prose.
    return _within_length(squashed, max_words) or _only_filler_around_phrase(squashed)


def _within_length(text: str, max_words: int) -> bool:
    """A race signal is short. A paragraph that mentions the phrase is not one."""
    words = text.split()
    return len(words) <= max_words and len(text) <= MAX_TRIGGER_CHARS


def _only_filler_around_phrase(text: str) -> bool:
    """True when every word is filler *or* the trigger itself.

    A host writes "drop your first comment below"; a bystander writes "first
    comment on yesterday was rude". The second carries content words ("yesterday",
    "rude") and is prose, so it must not fire.
    """
    words = text.split()
    if not words:
        return False
    for word in words:
        bare = word.strip(".,!?")
        if bare in _FILLER_WORDS:
            continue
        if _PHRASE_RE.fullmatch(bare) or _REVERSED_RE.fullmatch(bare):
            continue
        return False
    return True
