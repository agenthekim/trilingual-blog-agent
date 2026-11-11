"""Web front end: the file preview and the way into the full-page editor."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import web_ui
from app.settings import workspace_dir

JOB_ID = "0123456789abcdef0123456789abcdef"
PROJECT_ID = "demo-abc123"


class FakePipeline:
    """Only what the file routes ask of a pipeline."""

    def __init__(self) -> None:
        self.saved: list[tuple[str, str, str | None]] = []

    async def overview(self) -> dict[str, Any]:
        return {
            "project_id": PROJECT_ID,
            "editable": ["post.ko.md", "slides.ko.md"],  # post.ja.md is the source
        }

    async def save_edit(self, name: str, markdown: str, title: str | None = None):
        self.saved.append((name, markdown, title))
        return {"saved": name, "check": None, "secrets": []}


@pytest.fixture
def client():
    folder = workspace_dir() / PROJECT_ID
    folder.mkdir(parents=True)
    for name in ("post.ko.md", "post.ja.md", "slides.ko.md"):
        (folder / name).write_text(f"# {name}\n\n<script>alert(1)</script>\n", "utf-8")
    job = web_ui.Job(id=JOB_ID, pipeline=FakePipeline())
    web_ui.JOBS[JOB_ID] = job
    yield TestClient(web_ui.web)
    del web_ui.JOBS[JOB_ID]


def test_preview_links_to_the_editor_only_for_files_a_person_may_change(client) -> None:
    for name in ("post.ko.md", "slides.ko.md"):
        page = client.get(f"/api/jobs/{JOB_ID}/files/{name}")
        assert page.status_code == 200
        assert (
            f'{name} &middot; <a href="/edit?job={JOB_ID}&amp;file={name}">edit</a>'
            ' &middot; <a href="?raw=1">raw text</a>' in page.text
        )
        # Article text is shown under a policy that lets no script run.
        assert "default-src 'none'" in page.headers["content-security-policy"]

    source = client.get(f"/api/jobs/{JOB_ID}/files/post.ja.md")
    assert "/edit?" not in source.text and "raw text" in source.text


def test_editor_page_is_served_and_saves_through_the_file_api(client) -> None:
    page = client.get("/edit", params={"job": JOB_ID, "file": "post.ko.md"})
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert 'id="text"' in page.text and "PUT" in page.text
    # The page itself carries no article text; it loads it as plain text.
    assert "alert(1)" not in page.text
    raw = client.get(f"/api/jobs/{JOB_ID}/files/post.ko.md", params={"raw": 1})
    assert raw.headers["content-type"].startswith("text/plain")

    saved = client.put(
        f"/api/jobs/{JOB_ID}/files/post.ko.md",
        json={"markdown": "# 새 본문\n", "title": "새 제목"},
    )
    assert saved.json()["saved"] == "post.ko.md"
    assert web_ui.JOBS[JOB_ID].pipeline.saved == [
        ("post.ko.md", "# 새 본문\n", "새 제목")
    ]
