"""Shared fixtures. Unit tests never call a model or a network service."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


class FakeToolContext:
    """The parts of ADK's ToolContext that the tools use."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state: dict[str, Any] = dict(state or {})
        self.tool_confirmation: Any = None
        self.confirmation_requests: list[dict[str, Any]] = []
        self.agent_name = "test_agent"
        self.invocation_id = "inv-test"
        self.function_call_id = "call-test"

    def request_confirmation(
        self, *, hint: str | None = None, payload: Any = None
    ) -> None:
        self.confirmation_requests.append({"hint": hint, "payload": payload})

    def approve(self, payload: Any = None) -> None:
        self.tool_confirmation = SimpleNamespace(confirmed=True, payload=payload)

    def decline(self) -> None:
        self.tool_confirmation = SimpleNamespace(confirmed=False)


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    """Point the workspace and article store at a temporary directory."""
    monkeypatch.setenv("BLOG_AGENT_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.delenv("FIRESTORE_PROJECT_ID", raising=False)
    monkeypatch.delenv("BLOG_AGENT_ENABLE_DLP", raising=False)
    # Sessions in memory, a fresh store per test: nothing is written to the
    # SQLite file a real run uses.
    monkeypatch.setenv("USE_IN_MEMORY_SESSION", "1")
    monkeypatch.delenv("SESSION_SERVICE_URI", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", raising=False)
    from app.app_utils.services import get_session_service
    from app.persistence import get_article_store

    get_article_store.cache_clear()
    get_session_service.cache_clear()
    yield tmp_path
    get_article_store.cache_clear()
    get_session_service.cache_clear()


@pytest.fixture
def tool_context() -> FakeToolContext:
    return FakeToolContext()


SEED_KO = """# 제목

본문 문단입니다. [문서](https://example.com/docs)를 참고하세요.

## 코드

```python
# 주석입니다
value = compute(1, 2)
```

## 정리

끝입니다.
"""


@pytest.fixture
def finalized_project(tool_context: FakeToolContext) -> FakeToolContext:
    """A session with a finalized Korean seed and both localizations present."""
    from app.tools.articles import save_seed_article_revision

    result = save_seed_article_revision(
        title="제목",
        markdown=SEED_KO,
        source_language="ko",
        code_mode="inline_markdown",
        code_repo_url="",
        finalized=True,
        tool_context=tool_context,
    )
    assert result["status"] == "success"
    tool_context.state["localized_markdown_en"] = SEED_KO.replace(
        "제목", "Title"
    ).replace("# 주석입니다", "# a comment")
    tool_context.state["localized_markdown_ja"] = SEED_KO.replace("제목", "タイトル")
    return tool_context


def finalize(tool_context: FakeToolContext, *languages: str) -> None:
    """Record a person's sign-off of the posts as they are now."""
    from app import policy, state

    marks = dict(tool_context.state.get(state.FINALIZED_POSTS) or {})
    for language in languages:
        marks[language] = policy.finalization(tool_context.state, language)
    tool_context.state[state.FINALIZED_POSTS] = marks
