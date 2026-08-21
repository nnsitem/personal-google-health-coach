"""Test package.

The env vars below MUST be set before anything imports coach.config, which
resolves DATA_DIR / DB_PATH at module level. Discovery imports this package
first, so this is the hook that keeps the suite off the real data/ directory.
"""

import os
import tempfile

# Assigned unconditionally, NOT setdefault: the Dockerfile ships
# COACH_DATA_DIR=/app/data, which is the real bind-mounted database. A
# setdefault here silently leaves it in place and the suite writes test rows
# into production data.
os.environ["COACH_DATA_DIR"] = tempfile.mkdtemp(prefix="coach-tests-")
os.environ.setdefault("TZ", "Asia/Bangkok")
# Never let a test reach a real API even if a stub is forgotten.
os.environ.pop("GEMINI_API_KEY", None)

# The code under test logs warnings and errors on purpose (implausible totals,
# surviving data points, stalled pagination). Silence them so a passing run is
# quiet and a failure is the only thing that stands out.
import logging  # noqa: E402
logging.disable(logging.CRITICAL)
