"""Reading the local ``.env`` file, in one place.

The page and the API server read ``.env`` when they are imported, so a
developer's settings apply without exporting them. Unit tests must not pick
those settings up (OPS-2): with ``BLOG_AGENT_NO_DOTENV`` set, nothing is read.

This module imports nothing from ``app``: it runs before the settings are read.
"""

from __future__ import annotations

import os
from pathlib import Path

NO_DOTENV = "BLOG_AGENT_NO_DOTENV"


def load_env_file(path: Path | None = None) -> bool:
    """Load ``.env`` into the environment. Returns whether a file was read.

    Nothing is read when ``BLOG_AGENT_NO_DOTENV`` is set to a non-empty value,
    or when ``python-dotenv`` is not installed (the environment is then
    expected to be set already). Without a path, the file is searched for the
    way ``python-dotenv`` does it.
    """
    if os.environ.get(NO_DOTENV):
        return False
    try:
        from dotenv import load_dotenv
    except ImportError:
        return False
    return bool(load_dotenv(path) if path is not None else load_dotenv())
