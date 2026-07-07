"""Shared configuration for the Libby search pipelines.

Holds settings common to both the UK and US pipelines — currently just the
OpenRouter API key, loaded from a `.env` file at the repository root.

Pipeline-specific paths (data files, output locations) live in each pipeline's
own ``settings.py`` (``uk/settings.py``, ``us/settings.py``).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Repository root = parent of the libby_core package directory.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

# Load the root .env (if present) so both pipelines share one API key.
load_dotenv(REPO_ROOT / ".env")

OPEN_ROUTER_KEY: str = os.environ.get("OPEN_ROUTER_KEY", "")