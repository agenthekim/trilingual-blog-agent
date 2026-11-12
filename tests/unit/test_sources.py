"""The table of source kinds, and what is made from it (SRC-1, SRC-8, INV-8)."""

from __future__ import annotations

import dataclasses

from app import platforms, settings, sources
from app.pipeline import classify_source

ADDRESSES = {
    "qiita": "https://qiita.com/someone/items/0123456789abcdef0123",
    "devto": "https://dev.to/someone/a-post-1a2b",
    "naver_blog": "https://blog.naver.com/someone/223456789012",
    "gdoc": "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/edit",
    "medium": "https://medium.com/@someone/a-post-1a2b3c",
}


def test_every_source_kind_is_in_one_table_and_the_fetch_list_comes_from_it(
    monkeypatch,
) -> None:
    kinds = sources.SOURCE_KINDS
    assert list(kinds) == ["qiita", "devto", "naver_blog", "gdoc", "medium"]
    # A blog platform is the source of the first three; the other two are
    # drafts, with no platform closed to writing (INV-4).
    assert {name: kind.platform for name, kind in kinds.items()} == {
        "qiita": "qiita",
        "devto": "devto",
        "naver_blog": "naver_blog",
        "gdoc": None,
        "medium": None,
    }
    for name, address in ADDRESSES.items():
        assert sources.kind_of(address) is kinds[name]
        assert [k.name for k in kinds.values() if k.recognises(address)] == [name]
        expected = "url" if kinds[name].platform else name
        assert classify_source(address) == (expected, address)
        # Reading a kind may contact the host its own address is on.
        assert kinds[name].may_fetch(address) is not None

    # The list of blog hosts that may be fetched is the table's, not a copy.
    assert settings.ALLOWED_ARTICLE_HOSTS == sources.blog_hosts()
    assert settings.ALLOWED_ARTICLE_HOSTS == {
        "qiita.com": "qiita",
        "dev.to": "devto",
        "blog.naver.com": "naver_blog",
        "m.blog.naver.com": "naver_blog",
    }
    assert sources.fetch_hosts() == (
        "qiita.com",
        "dev.to",
        "blog.naver.com",
        "m.blog.naver.com",
        "docs.google.com",
        "*.googleusercontent.com",
        "medium.com",
    )
    for host in settings.ALLOWED_ARTICLE_HOSTS:
        assert platforms._allowed_public_url(f"https://{host}/x") is not None
    # Google Docs and Medium are checked where they are read, by their rows.
    gdoc, medium = kinds["gdoc"], kinds["medium"]
    assert platforms._allowed_gdocs_export_url(
        "https://doc-0c-9s-docstext.googleusercontent.com/export/abc"
    )
    assert medium.may_fetch("https://medium.com/feed/@someone")
    for elsewhere in (
        "https://medium.com/feed/@someone",  # not where a document is exported
        "https://googleusercontent.com.example.org/x",
        "http://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz",
        "https://docs.google.com:8443/x",
        "https://someone@docs.google.com/x",
    ):
        assert platforms._allowed_gdocs_export_url(elsewhere) is None
    for elsewhere in (
        "https://medium.com.example.org/feed/@someone",
        "https://notmedium.com/feed/@someone",
        "http://medium.com/feed/@someone",
        "https://docs.google.com/feed/@someone",
    ):
        assert medium.may_fetch(elsewhere) is None
    assert gdoc.may_fetch("https://medium.com/@someone/a-post-1a2b3c") is None
    assert platforms._allowed_public_url("https://medium.com/feed/@someone") is None

    # The sentence that refuses an address names every kind, from the table.
    sentence = sources.readable_sources_sentence()
    assert sentence == (
        "posts are read from Qiita, DEV Community and Naver Blog, and drafts "
        "from Google Docs (https://docs.google.com/document/d/...) and Medium "
        "(https://medium.com/@author/slug or https://author.medium.com/slug), "
        "over https"
    )
    assert sources.blog_hosts_sentence() == (
        "qiita.com, dev.to, blog.naver.com or m.blog.naver.com"
    )

    # One more row is all a new kind takes: it is recognised and named.
    notes = dataclasses.replace(
        medium,
        name="notes",
        label="Notes",
        example="https://notes.example.org/...",
        recognises=lambda url: url.startswith("https://notes.example.org/"),
        hosts=("notes.example.org",),
    )
    assert classify_source("https://notes.example.org/a")[0] == "elsewhere"
    monkeypatch.setitem(sources.SOURCE_KINDS, "notes", notes)
    assert classify_source("https://notes.example.org/a")[0] == "notes"
    assert "Notes (https://notes.example.org/...)" in (
        sources.readable_sources_sentence()
    )
    assert "notes.example.org" in sources.fetch_hosts()


def test_a_medium_post_on_its_authors_own_name_is_the_same_post() -> None:
    from app.pipeline import classify_source

    slug = "4-achieving-the-vibe-e36858b1b25e"
    own_name = f"https://aimldl.medium.com/{slug}"
    assert sources.medium_post(own_name) == ("@aimldl", slug)
    assert sources.medium_post(f"https://medium.com/@aimldl/{slug}") == (
        "@aimldl",
        slug,
    )
    assert sources.medium_post(own_name + "/") == ("@aimldl", slug)
    assert classify_source(own_name) == ("medium", own_name)
    assert sources.kind_of(own_name).name == "medium"

    # The author's name is not a host that is fetched: the feed is still read
    # from medium.com alone (INV-8).
    medium = sources.SOURCE_KINDS["medium"]
    assert medium.hosts == ("medium.com",)
    assert medium.may_fetch("https://aimldl.medium.com/feed") is None
    assert medium.may_fetch("https://medium.com/feed/@aimldl")

    for not_a_post in (
        "https://aimldl.medium.com/",  # the author's page, not a post
        "https://aimldl.medium.com/a/b",
        "http://aimldl.medium.com/" + slug,  # not https
        "https://a.b.medium.com/" + slug,
        "https://aimldl.medium.com.example.org/" + slug,
        "https://aimldl.notmedium.com/" + slug,
        "https://blog.example.com/" + slug,  # a publication's own domain
    ):
        assert sources.medium_post(not_a_post) is None, not_a_post
        assert classify_source(not_a_post)[0] == "elsewhere", not_a_post
