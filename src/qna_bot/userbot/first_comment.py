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

import itertools
import random
import re

#: What the bot says when it wins the race. Deliberately short, no emoji, no
#: punctuation: it has to look like a person in a group chat.
FIRST_COMMENT_ANSWER = "I win"

#: The rotating replies. One fixed line is the single most robotic thing this
#: bot does: the same two words from the same account, in every group, every
#: day, is a pattern nobody types. These are all things a person actually
#: writes when they win a comment race - short enough to keep the latency edge,
#: varied enough not to look scripted.
#:
#: All are plain ASCII on purpose: an emoji or Amharic variant would look like
#: one account with a repertoire, and the giveaway crowd notices. The Amharic
#: alternatives are in ``_AMHARIC_FIRST_COMMENT_ANSWERS`` and used only when the
#: trigger itself was Amharic, because replying "ጀምሩ" in English to a message
#: written in Amharic would look like a bot that did not understand it.
FIRST_COMMENT_ANSWERS: tuple[str, ...] = (
    "I win",
    "first",
    "first!",
    "me",
    "mine",
    "here",
    "first here",
    "got it",
    "done",
    "1st",
    "I'm first",
    "that's me",
)

#: Replies for an Amharic trigger. Short, idiomatic, and what a person types.
_AMHARIC_FIRST_COMMENT_ANSWERS: tuple[str, ...] = (
    "እኔ ነኝ",
    "የመጀመሪያው",
    "እኔ",
)

#: Position in the rotation. Cycling rather than picking at random: with twelve
#: choices, random repeats "I win" twice in a row about one time in twelve, which
#: is exactly the repeat the rotation exists to prevent. Cycling guarantees the
#: spread. The *start* is randomised once per process, so a redeploy does not
#: begin the sequence at "I win" again, and two bots in one group do not move in
#: lockstep.
_answer_cursor = itertools.count(random.randrange(len(FIRST_COMMENT_ANSWERS)))


def next_first_comment_answer(*, amharic: bool = False) -> str:
    """Pick the next reply, cycling so no line repeats until all are used."""
    choices = _AMHARIC_FIRST_COMMENT_ANSWERS if amharic else FIRST_COMMENT_ANSWERS
    index = next(_answer_cursor) % len(choices)
    return choices[index]


def looks_amharic(text: str) -> bool:
    """True when the message is written in Ethiopic script."""
    return any("\u1200" <= ch <= "\u137f" for ch in (text or ""))

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

#: "go" on its own. Hosts in the Ethiopian giveaway scene post the verb instead
#: of the English phrase - "ጀምሩ", "ፃፉ", "ጻፉ" ("start", "write"), "go", "GO 🔥" - and
#: a bot that only understands "first comment" loses races it was configured for.
#:
#: Kept strictly as a *whole message* match: "go" is one of the most common words
#: in English, so anything looser would answer every "go team" and "here we go".
#: The separators allow decoration around the word - "GO!", "go..", "ግ ው" is not a
#: thing, but "ጥሩ" would be, hence the explicit alternation rather than a prefix
#: match.
_GO_TOKENS = ("go", "ጀምሩ", "ጅምሩ", "ጀምር", "ጅምር", "ፃፉ", "ጻፉ", "ፃፍ", "ጻፍ", "ጹፉ", "ፁፉ")
_GO_RE = re.compile(
    r"^[\s\W]*(?:" + "|".join(_GO_TOKENS) + r")[\s\W]*$",
    re.IGNORECASE,
)

#: The same words as a *second* clause in a short instruction: "comment go",
#: "first comment — ጀምሩ". Still only accepted once a first-comment phrase is
#: already present, so this never widens the trigger on its own.
_GO_TRAILER_RE = re.compile(
    r"(?:^|\s)(?:" + "|".join(_GO_TOKENS) + r")$",
    re.IGNORECASE,
)

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

    # A bare "go" / "ጀምሩ" / "ፃፉ" is a race signal in its own right.
    if _GO_RE.match(text):
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
