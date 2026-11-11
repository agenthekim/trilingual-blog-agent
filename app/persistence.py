"""Persistent storage for article projects.

Conversation history lives in the ADK session service (see
``app/app_utils/services.py``). This module stores the work product itself:
every revision of every language version of an article, and where each one was
published. That is what lets a project be picked up again days later, in a new
session, without re-reading old chat history.

Two interchangeable backends implement ``ArticleStore``:

* ``FirestoreArticleStore``: Cloud Firestore, used when ``FIRESTORE_PROJECT_ID``
  is set. Note that this must be the project *ID*: Agent Runtime sets
  ``GOOGLE_CLOUD_PROJECT`` to the project *number*, which Firestore rejects.
* ``SqliteArticleStore``: a local SQLite file, used otherwise, so local
  development needs no cloud resources but still persists across restarts.
"""

from __future__ import annotations

import functools
import json
import os
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from app.settings import workspace_dir


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ArticleStore(Protocol):
    def save_project(self, project_id: str, fields: dict[str, Any]) -> None: ...

    def get_project(self, project_id: str) -> dict[str, Any] | None: ...

    def add_version(
        self, project_id: str, language: str, kind: str, markdown: str
    ) -> int: ...

    def latest_version(
        self, project_id: str, language: str
    ) -> dict[str, Any] | None: ...

    def record_publication(
        self, project_id: str, platform: str, record: dict[str, Any]
    ) -> None: ...

    def list_publications(self, project_id: str) -> list[dict[str, Any]]: ...


class SqliteArticleStore:
    """Local persistent store. Safe for the threads a single server process uses."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS versions (
                project_id TEXT NOT NULL, language TEXT NOT NULL,
                revision INTEGER NOT NULL, kind TEXT NOT NULL,
                markdown TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY (project_id, language, revision));
            CREATE TABLE IF NOT EXISTS publications (
                project_id TEXT NOT NULL, platform TEXT NOT NULL,
                data TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY (project_id, platform));
            """
        )

    def save_project(self, project_id: str, fields: dict[str, Any]) -> None:
        with self._lock:
            merged = {**(self._get_project(project_id) or {}), **fields}
            self._db.execute(
                "INSERT INTO projects (id, data, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET data = excluded.data, "
                "updated_at = excluded.updated_at",
                (project_id, json.dumps(merged, ensure_ascii=False), _now()),
            )
            self._db.commit()

    def _get_project(self, project_id: str) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT data FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._get_project(project_id)

    def add_version(
        self, project_id: str, language: str, kind: str, markdown: str
    ) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COALESCE(MAX(revision), 0) FROM versions "
                "WHERE project_id = ? AND language = ?",
                (project_id, language),
            ).fetchone()
            revision = int(row[0]) + 1
            self._db.execute(
                "INSERT INTO versions VALUES (?, ?, ?, ?, ?, ?)",
                (project_id, language, revision, kind, markdown, _now()),
            )
            self._db.commit()
            return revision

    def latest_version(self, project_id: str, language: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT revision, kind, markdown, created_at FROM versions "
                "WHERE project_id = ? AND language = ? ORDER BY revision DESC LIMIT 1",
                (project_id, language),
            ).fetchone()
        if not row:
            return None
        return {
            "revision": row[0],
            "kind": row[1],
            "markdown": row[2],
            "created_at": row[3],
        }

    def record_publication(
        self, project_id: str, platform: str, record: dict[str, Any]
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO publications VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_id, platform) DO UPDATE SET data = excluded.data, "
                "updated_at = excluded.updated_at",
                (project_id, platform, json.dumps(record, ensure_ascii=False), _now()),
            )
            self._db.commit()

    def list_publications(self, project_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM publications WHERE project_id = ? ORDER BY platform",
                (project_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]


class FirestoreArticleStore:
    """Cloud Firestore store: ``<collection>/{project}`` with two subcollections."""

    def __init__(self, project: str, collection: str) -> None:
        from google.cloud import firestore

        self._firestore = firestore
        self._projects = firestore.Client(project=project).collection(collection)

    def save_project(self, project_id: str, fields: dict[str, Any]) -> None:
        self._projects.document(project_id).set(
            {**fields, "updated_at": _now()}, merge=True
        )

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        snapshot = self._projects.document(project_id).get()
        return snapshot.to_dict() if snapshot.exists else None

    def _versions(self, project_id: str, language: str) -> Any:
        return self._projects.document(project_id).collection(f"versions_{language}")

    def add_version(
        self, project_id: str, language: str, kind: str, markdown: str
    ) -> int:
        latest = self.latest_version(project_id, language)
        revision = (latest["revision"] if latest else 0) + 1
        self._versions(project_id, language).document(f"{revision:04d}").set(
            {
                "revision": revision,
                "kind": kind,
                "markdown": markdown,
                "created_at": _now(),
            }
        )
        return revision

    def latest_version(self, project_id: str, language: str) -> dict[str, Any] | None:
        query = (
            self._versions(project_id, language)
            .order_by("revision", direction=self._firestore.Query.DESCENDING)
            .limit(1)
        )
        for snapshot in query.stream():
            return snapshot.to_dict()
        return None

    def record_publication(
        self, project_id: str, platform: str, record: dict[str, Any]
    ) -> None:
        self._projects.document(project_id).collection("publications").document(
            platform
        ).set({**record, "updated_at": _now()})

    def list_publications(self, project_id: str) -> list[dict[str, Any]]:
        docs = self._projects.document(project_id).collection("publications").stream()
        return sorted((d.to_dict() for d in docs), key=lambda r: r.get("platform", ""))


@functools.cache
def get_article_store() -> ArticleStore:
    """Process-wide store: Firestore when configured, local SQLite otherwise."""
    firestore_project = os.environ.get("FIRESTORE_PROJECT_ID")
    if firestore_project:
        return FirestoreArticleStore(
            project=firestore_project,
            collection=os.environ.get("FIRESTORE_COLLECTION", "blog_projects"),
        )
    return SqliteArticleStore(workspace_dir() / ".store" / "articles.sqlite3")
