"""Test configuration for the skill-local Python package."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "cad-stainless-quote"
    / "scripts"
)

if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
