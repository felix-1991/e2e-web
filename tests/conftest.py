"""Test source checkout before and independently of a wheel install."""

from pathlib import Path
import os
import sys

if os.getenv("E2E_WEB_TEST_INSTALLED") != "1":
    sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
