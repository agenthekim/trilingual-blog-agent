"""The kinds of address a source document can be read from, in one table.

Everything that depends on "which addresses can be a source" is made from
``SOURCE_KINDS``: how a pasted address is classified, which hosts the server
may fetch to read it (INV-8), which blog platform is recorded as the source
and never written to (INV-4), and the sentences that refuse an address. A new
kind is one more row; nothing else lists hosts.

This module imports nothing from ``app`` so that ``app.settings`` can build
its host list from it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

import httpx

_GDOC_PATH = re.compile(r"^/document/(?:u/\d+/)?d/([A-Za-z0-9_-]{10,})(?:/.*)?$")
_MEDIUM_PATH = re.compile(r"^/(@[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")
# The same post under its author's own Medium name: author.medium.com/slug.
_MEDIUM_AUTHOR_HOST = re.compile(r"^([a-z0-9][a-z0-9-]*)\.medium\.com$")
_MEDIUM_SLUG_PATH = re.compile(r"^/([A-Za-z0-9_.-]+)$")


def _https_url(url: str | httpx.URL) -> httpx.URL | None:
    """``url`` parsed when it is https without a port or a user name, else None.

    The address is parsed with httpx, the library that then connects to it, so
    the host that is checked is the host that is contacted.
    """
    try:
        parsed = httpx.URL(url.strip() if isinstance(url, str) else url)
    except (httpx.InvalidURL, TypeError, AttributeError):
        return None
    if parsed.scheme != "https" or parsed.port is not None or parsed.userinfo:
        return None
    return parsed


def _host(parsed: httpx.URL) -> str:
    return parsed.host.lower().removeprefix("www.")


def google_doc_id(url: str) -> str | None:
    """The document id of an https Google Docs document address, else None."""
    parsed = _https_url(url)
    if parsed is None or parsed.host.lower() != "docs.google.com":
        return None
    match = _GDOC_PATH.match(parsed.path)
    return match.group(1) if match else None


def medium_post(url: str) -> tuple[str, str] | None:
    """(author or publication, slug) of an https Medium post address, else None.

    Two forms name the same post: ``medium.com/@author/slug`` and
    ``author.medium.com/slug``. Either way the post is read from
    ``medium.com/feed/@author``; no other host is contacted (INV-8). A
    publication's own domain is not recognised: nothing in such an address
    says that it is Medium.
    """
    parsed = _https_url(url)
    if parsed is None:
        return None
    host, path = _host(parsed), parsed.path.rstrip("/")
    if host == "medium.com":
        match = _MEDIUM_PATH.match(path)
        return (match.group(1), match.group(2)) if match else None
    author, slug = _MEDIUM_AUTHOR_HOST.match(host), _MEDIUM_SLUG_PATH.match(path)
    if author and slug:
        return f"@{author.group(1)}", slug.group(1)
    return None


def _on(*hosts: str) -> Callable[[str], bool]:
    """A recogniser for a post on a blog host: any https address on it."""

    def recognises(url: str) -> bool:
        parsed = _https_url(url)
        return parsed is not None and _host(parsed) in hosts

    return recognises


@dataclass(frozen=True)
class SourceKind:
    """One kind of address a source document can be read from."""

    name: str
    # The name people know it by; refusals are read by people.
    label: str
    # What an address of this kind looks like, for the refusal sentences.
    example: str
    # Whether a bare address is of this kind.
    recognises: Callable[[str], bool]
    # Hosts that may be fetched to read it, redirects included (INV-8).
    hosts: tuple[str, ...]
    # Hosts allowed by their ending as well (an export served from a subdomain).
    host_suffixes: tuple[str, ...] = ()
    # The blog platform recorded as the source and never written to (INV-4).
    # None: the document is a draft and no blog platform is the source.
    platform: str | None = None

    @property
    def is_draft(self) -> bool:
        return self.platform is None

    def may_fetch(self, url: str | httpx.URL) -> httpx.URL | None:
        """``url`` parsed when reading this kind may contact it, else None."""
        parsed = _https_url(url)
        if parsed is None:
            return None
        host = _host(parsed)
        if host in self.hosts or parsed.host.lower().endswith(self.host_suffixes):
            return parsed
        return None


SOURCE_KINDS: dict[str, SourceKind] = {
    kind.name: kind
    for kind in (
        SourceKind(
            name="qiita",
            label="Qiita",
            example="https://qiita.com/author/items/...",
            recognises=_on("qiita.com"),
            hosts=("qiita.com",),
            platform="qiita",
        ),
        SourceKind(
            name="devto",
            label="DEV Community",
            example="https://dev.to/author/slug",
            recognises=_on("dev.to"),
            hosts=("dev.to",),
            platform="devto",
        ),
        SourceKind(
            name="naver_blog",
            label="Naver Blog",
            example="https://blog.naver.com/author/...",
            recognises=_on("blog.naver.com", "m.blog.naver.com"),
            hosts=("blog.naver.com", "m.blog.naver.com"),
            platform="naver_blog",
        ),
        SourceKind(
            name="gdoc",
            label="Google Docs",
            example="https://docs.google.com/document/d/...",
            recognises=lambda url: google_doc_id(url) is not None,
            hosts=("docs.google.com",),
            host_suffixes=(".googleusercontent.com",),
        ),
        SourceKind(
            name="medium",
            label="Medium",
            example="https://medium.com/@author/slug or https://author.medium.com/slug",
            recognises=lambda url: medium_post(url) is not None,
            hosts=("medium.com",),
        ),
    )
}


def kind_of(url: str) -> SourceKind | None:
    """The kind of a bare address, or None when it cannot be a source."""
    for kind in SOURCE_KINDS.values():
        if kind.recognises(url):
            return kind
    return None


def blog_hosts() -> dict[str, str]:
    """Host to platform, for the kinds where a blog platform is the source."""
    return {
        host: kind.platform
        for kind in SOURCE_KINDS.values()
        if kind.platform is not None
        for host in kind.hosts
    }


def fetch_hosts() -> tuple[str, ...]:
    """Every host that may be fetched to read a source; suffixes as ``*.host``."""
    hosts: list[str] = []
    for kind in SOURCE_KINDS.values():
        hosts.extend(kind.hosts)
        hosts.extend(f"*{suffix}" for suffix in kind.host_suffixes)
    return tuple(hosts)


def _listed(items: list[str]) -> str:
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def blog_hosts_sentence() -> str:
    """The blog hosts a post can be read from, as words: 'a, b or c'."""
    hosts = list(blog_hosts())
    if len(hosts) < 2:
        return "".join(hosts)
    return ", ".join(hosts[:-1]) + " or " + hosts[-1]


def readable_sources_sentence() -> str:
    """Where a source can be read from, for a refusal a person reads."""
    posts = [kind.label for kind in SOURCE_KINDS.values() if not kind.is_draft]
    drafts = [
        f"{kind.label} ({kind.example})"
        for kind in SOURCE_KINDS.values()
        if kind.is_draft
    ]
    return (
        f"posts are read from {_listed(posts)}, and drafts from "
        f"{_listed(drafts)}, over https"
    )
