"""pytest configuration for the limit-up tracker contract tests.

Contract §4.6: `LIMITUP_DISABLE_BG=1` must disable the background refresh /
concept-map threads (QA-only mode).  We set it *before* app.py is ever imported,
and drop any inherited `LIMITUP_FAKE_FAIL` so the unit suite never runs against a
forced-failure snapshot.  The project root is placed on sys.path so `import app`
finds app.py.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

os.environ["LIMITUP_DISABLE_BG"] = "1"
os.environ.pop("LIMITUP_FAKE_FAIL", None)

# Keep QA runs side-effect free: importing app.py must not leave __pycache__ in the
# project root (that directory is not in qa-verifier's write scope).
sys.dont_write_bytecode = True

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
