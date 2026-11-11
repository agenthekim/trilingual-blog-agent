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
        self.synced: list[tuple[str, str, bool]] = []
        self.finalized = False
        self.approval_hints = ["Approve to create a draft on devto."]
        self.said: list[Any] = []

    async def overview(self) -> dict[str, Any]:
        return {
            "project_id": PROJECT_ID,
            "editable": ["post.ko.md", "slides.ko.md"],  # post.ja.md is the source
            "rows": [
                {"platform": "devto", "language": "en", "finalized": self.finalized}
            ],
        }

    async def finalize_post(self, language: str):
        if language == "ja":
            raise ValueError("There is no localized post in this language to finalize.")
        self.finalized = True
        return {"language": language, "finalized": True, "check": None}

    async def request_draft(self, platform: str, tags: list[str]) -> None:
        self.said.append(("request", platform, tags))

    async def answer_approvals(self, confirmed: bool, payload=None) -> None:
        self.said.append(("approval", confirmed, payload))

    async def say(self, text: str) -> None:
        self.said.append(("say", text))

    async def save_edit(self, name: str, markdown: str, title: str | None = None):
        self.saved.append((name, markdown, title))
        return {"saved": name, "check": None, "secrets": []}

    async def sync_deck_text(self, language: str, direction: str, apply: bool):
        if language == "en":
            raise ValueError("There is no Google Slides deck for this language yet.")
        self.synced.append((language, direction, apply))
        return {"direction": direction, "slides": [2], "applied": apply}


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


def test_deck_sync_writes_only_when_the_request_says_a_person_approved(client) -> None:
    url = f"/api/jobs/{JOB_ID}/deck/sync"
    asked = client.post(url, json={"language": "ko", "direction": "push"})
    assert asked.json() == {"direction": "push", "slides": [2], "applied": False}
    done = client.post(
        url, json={"language": "ko", "direction": "pull", "approved": True}
    )
    assert done.json()["applied"] is True
    job = web_ui.JOBS[JOB_ID]
    assert job.pipeline.synced == [("ko", "push", False), ("ko", "pull", True)]
    assert job.busy is False

    assert (
        client.post(url, json={"language": "en", "direction": "push"}).status_code
        == 400
    )
    assert (
        client.post(url, json={"language": "ko", "direction": "both"}).status_code
        == 422
    )
    job.busy = True  # the agent is working: the deck is not touched meanwhile
    assert (
        client.post(url, json={"language": "ko", "direction": "push"}).status_code
        == 409
    )
    assert job.busy is True and len(job.pipeline.synced) == 2


def test_posting_needs_a_finalized_text_and_carries_the_persons_choice(client) -> None:
    import time

    url = f"/api/jobs/{JOB_ID}/draft"
    body = {"platform": "devto", "tags": ["ai"], "approved": True, "public": True}
    refused = client.post(url, json=body)
    assert refused.status_code == 409 and "Finalize" in refused.json()["detail"]
    job = web_ui.JOBS[JOB_ID]
    assert job.pipeline.said == []  # the agent was not even asked

    assert (
        client.post(f"/api/jobs/{JOB_ID}/finalize", json={"language": "ja"}).status_code
        == 400
    )
    assert client.post(f"/api/jobs/{JOB_ID}/finalize", json={"language": "en"}).json()[
        "finalized"
    ]

    assert client.post(url, json=body).json() == {"accepted": True}
    for _ in range(50):  # the post runs in the background
        if not client.get(f"/api/jobs/{JOB_ID}").json()["busy"]:
            break
        time.sleep(0.02)
    assert ("approval", True, {"visibility": "public"}) in job.pipeline.said

    job.pipeline.said.clear()
    client.post(url, json={**body, "public": False})
    for _ in range(50):
        if not client.get(f"/api/jobs/{JOB_ID}").json()["busy"]:
            break
        time.sleep(0.02)
    assert ("approval", True, {"visibility": "draft"}) in job.pipeline.said


def test_mermaid_blocks_are_shown_as_images_drawn_once(client, monkeypatch) -> None:
    monkeypatch.setenv("BLOG_AGENT_RENDER_MERMAID", "true")
    code = 'flowchart TD\n    A["<b>Plan</b>"] --> B\n'
    folder = workspace_dir() / PROJECT_ID
    (folder / "post.ko.md").write_text(
        f"# 글\n\n```mermaid\n{code}```\n\n끝.\n", encoding="utf-8"
    )
    drawn: list[str] = []

    async def draw(source: str) -> bytes | None:
        drawn.append(source)
        return b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"

    monkeypatch.setattr(web_ui, "_draw_diagram", draw)
    page = client.get(f"/api/jobs/{JOB_ID}/files/post.ko.md").text
    diagram_id = web_ui._diagram_id(code)
    assert f'src="/api/jobs/{JOB_ID}/diagrams/{diagram_id}"' in page
    # The source stays on the page, as text, under the drawing.
    assert "&lt;b&gt;Plan&lt;/b&gt;" in page and "<b>Plan</b>" not in page

    for _ in range(2):  # the second request is answered from what was kept
        image = client.get(f"/api/jobs/{JOB_ID}/diagrams/{diagram_id}")
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/svg+xml")
        assert "default-src 'none'" in image.headers["content-security-policy"]
    assert drawn == [code]

    # A digest that matches no diagram of this article draws nothing.
    assert client.get(f"/api/jobs/{JOB_ID}/diagrams/{'0' * 32}").status_code == 404
    assert client.get(f"/api/jobs/{JOB_ID}/diagrams/..%2Fx").status_code == 404
    assert len(drawn) == 1

    monkeypatch.setenv("BLOG_AGENT_RENDER_MERMAID", "false")
    plain = client.get(f"/api/jobs/{JOB_ID}/files/post.ko.md").text
    assert "/diagrams/" not in plain and "flowchart TD" in plain
