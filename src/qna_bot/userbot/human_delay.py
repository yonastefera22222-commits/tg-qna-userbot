"""The optional "human delay" between a message arriving and the bot posting.

Why this is its own module: the delay has to be one rule that every answer path
obeys, and there are now several of them - instant local math (sub-millisecond),
Forex formulas, OHLC period answers, plain model answers, and image questions
that spend seconds downloading and reading a photo. Applying ``sleep(delay)``
after the work finished, which is what the bot used to do, made the delay mean
"add N ms to whatever this path already cost". A 1500 ms setting therefore
posted 1.5 s after an instant arithmetic answer and 5 s after an image question.
That is not a human delay, it is noise - and to the owner it looks like the
setting is being ignored on some paths.

The rule here is instead a *floor measured from arrival*: a message that arrived
at T is posted no earlier than ``T + delay``, and as soon as possible after that.
A fast path waits out the remainder, a slow path waits for nothing at all. Every
request type is held to the same clock, so the number in the dashboard means one
thing.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

#: "First comment" is a race - the prize goes to the fastest reply, and the
#: whole feature is pointless if the bot arrives second. The owner's delay still
#: applies, but capped here: enough jitter to stop the reply looking robotic, far
#: too little to lose the race to another bot. Not a bypass - the delay is
#: honoured, just bounded by what this specific feature can afford.
FIRST_COMMENT_MAX_DELAY_MS = 250


def effective_delay_ms(human_delay_ms: int, *, first_comment: bool = False) -> int:
    """The delay this particular answer may actually use.

    Negative values are treated as zero rather than raising: the setting comes
    from an owner-editable dashboard, and a bad number must not stop answers.
    """
    delay = max(0, int(human_delay_ms or 0))
    if first_comment:
        delay = min(delay, FIRST_COMMENT_MAX_DELAY_MS)
    return delay


async def wait_for_send(
    received_at: float | None,
    human_delay_ms: int,
    *,
    first_comment: bool = False,
) -> float:
    """Hold a reply until ``received_at + delay``, then return the time waited.

    ``received_at`` is ``time.monotonic()`` taken the moment the event entered
    the handler, so it excludes nothing and includes no clock skew - unlike the
    message's own timestamp, which comes from Telegram's servers and can be
    seconds off the local clock.

    Returns the seconds actually slept, which is zero whenever the work already
    took longer than the delay. That is the normal case for an image question and
    the reason the old unconditional sleep made those replies so slow.
    """
    delay_ms = effective_delay_ms(human_delay_ms, first_comment=first_comment)
    if delay_ms <= 0 or received_at is None:
        return 0.0
    remaining = (received_at + delay_ms / 1000.0) - time.monotonic()
    if remaining <= 0:
        return 0.0
    await asyncio.sleep(remaining)
    return remaining
