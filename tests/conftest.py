"""Test configuration.

``exness_fixtures`` is a plain module next to the tests (rather than a pytest
fixture set) so the recorded Exness payload shapes can be imported by name from
any test file, and so helpers stay usable outside a fixture.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


# --- a clock that is always inside a trading session ------------------------
#
# A quote is "live" only while its market is open: the weekend rule is real
# behaviour with its own tests. But any test that asserts *other* behaviour of
# a fresh quote (that it came from Exness, that it is 5 dp) must not start
# failing on a Saturday, when "market closed" is the correct answer. So such
# tests pin the clock instead of asserting against the wall calendar.

import datetime as _datetime  # noqa: E402

import pytest  # noqa: E402

#: A Wednesday midday: every weekday market is open, crypto included.
OPEN_MARKET_MOMENT = _datetime.datetime(2026, 11, 25, 12, 0, tzinfo=_datetime.UTC)


def freeze_market_clock(monkeypatch, moment=OPEN_MARKET_MOMENT) -> None:
    """Make the feed's and the session calendar's ``datetime.now()`` ``moment``."""
    import qna_bot.ai.market as market_mod
    import qna_bot.ai.sessions as sessions_mod

    # A *datetime subclass*, not a stub: these modules also call
    # ``datetime.combine`` and friends, which a bare fake would not provide.
    class _Clock(_datetime.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001 - matches datetime.now
            return moment if tz is not None else moment.replace(tzinfo=None)

    monkeypatch.setattr(market_mod, "datetime", _Clock)
    monkeypatch.setattr(sessions_mod, "datetime", _Clock)


@pytest.fixture
def open_market(monkeypatch):
    """Run this test as if it were a Wednesday trading session."""
    freeze_market_clock(monkeypatch)
