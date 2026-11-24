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
