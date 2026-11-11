"""Runtime settings read from the environment. Nothing here is a secret."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

Language = Literal["ko", "en", "ja"]
Platform = Literal["naver_blog", "devto", "qiita"]

LANGUAGES: tuple[Language, ...] = ("ko", "en", "ja")

LANGUAGE_NAMES: dict[str, str] = {"ko": "Korean", "en": "English", "ja": "Japanese"}

# Each language is posted on exactly one platform.
PLATFORM_FOR_LANGUAGE: dict[str, str] = {
    "ko": "naver_blog",
    "en": "devto",
    "ja": "qiita",
}
LANGUAGE_FOR_PLATFORM: dict[str, str] = {v: k for k, v in PLATFORM_FOR_LANGUAGE.items()}

# Hosts the agent is allowed to fetch articles from. Anything else is refused,
# so a pasted URL cannot make the agent read arbitrary internal addresses.
ALLOWED_ARTICLE_HOSTS: dict[str, str] = {
    "qiita.com": "qiita",
    "dev.to": "devto",
    "blog.naver.com": "naver_blog",
    "m.blog.naver.com": "naver_blog",
}

HTTP_TIMEOUT_SECONDS = float(os.environ.get("BLOG_AGENT_HTTP_TIMEOUT", "20"))
MAX_ARTICLE_CHARS = int(os.environ.get("BLOG_AGENT_MAX_ARTICLE_CHARS", "60000"))


def workspace_dir() -> Path:
    """Directory where drafts are read from and generated files are written to."""
    root = Path(os.environ.get("BLOG_AGENT_WORKSPACE_DIR", "workspace")).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")
