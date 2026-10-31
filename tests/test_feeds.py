"""Tests for RSS/Atom parsing, discovery and entry → item conversion."""

from unittest.mock import MagicMock

import pytest

from src.feeds import FeedFetcher, find_feed_links, parse_feed

RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel>
  <title>Example Blog</title>
  <link>https://example.com</link>
  <item>
    <title>First post</title>
    <link>https://example.com/first</link>
    <guid>https://example.com/first</guid>
    <pubDate>Tue, 28 Oct 2026 10:00:00 GMT</pubDate>
    <dc:creator>Alice</dc:creator>
    <description><![CDATA[<p>Hello <b>world</b></p>]]></description>
  </item>
  <item>
    <title>Second</title>
    <link>https://example.com/second</link>
    <content:encoded><![CDATA[Long body text here]]></content:encoded>
  </item>
</channel>
</rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Site</title>
  <link rel="alternate" href="https://atom.example"/>
  <link rel="self" href="https://atom.example/atom.xml"/>
  <entry>
    <id>tag:atom.example,2026:1</id>
    <title>Atom entry</title>
    <link rel="alternate" href="https://atom.example/e1"/>
    <published>2026-10-29T08:00:00Z</published>
    <author><name>Bob</name></author>
    <summary>Summary text</summary>
  </entry>
</feed>"""


class TestParseFeed:
    def test_rss(self):
        feed = parse_feed(RSS)
        assert feed["title"] == "Example Blog" and feed["site_url"] == "https://example.com"
        e = feed["entries"]
        assert [x["url"] for x in e] == ["https://example.com/first", "https://example.com/second"]
        assert e[0]["author"] == "Alice"
        assert e[0]["summary"] == "Hello world"
        assert e[0]["published"].startswith("2026-10-28T10:00:00")
        assert e[1]["summary"] == "Long body text here"
        assert e[1]["published"] is None

    def test_atom(self):
        feed = parse_feed(ATOM)
        assert feed["title"] == "Atom Site" and feed["site_url"] == "https://atom.example"
        e = feed["entries"][0]
        assert e["url"] == "https://atom.example/e1" and e["author"] == "Bob"
        assert e["id"] == "tag:atom.example,2026:1"
        assert e["published"].startswith("2026-10-29T08:00:00")

    def test_not_a_feed(self):
        with pytest.raises(ValueError):
            parse_feed("<html><body>nope</body></html>")
        with pytest.raises(ValueError):
            parse_feed("not xml at all")


class TestFindFeedLinks:
    def test_alternate_links(self):
        html = ('<html><head>'
                '<link rel="alternate" type="application/rss+xml" href="/feed">'
                '<link rel="stylesheet" href="/x.css">'
                '<link rel="alternate" type="application/atom+xml" href="https://cdn.example/atom.xml">'
                '</head></html>')
        assert find_feed_links(html, "https://site.example/page") == [
            "https://site.example/feed", "https://cdn.example/atom.xml",
        ]


def _resp(text, url, ctype="text/html"):
    r = MagicMock()
    r.text = text
    r.url = url
    r.headers = {"content-type": ctype}
    r.raise_for_status = MagicMock()
    return r


class TestDiscoverFeed:
    def test_direct_feed_url(self):
        f = FeedFetcher(blog_fetcher=MagicMock())
        f._client = MagicMock()
        f._client.get.return_value = _resp(RSS, "https://example.com/feed", "application/rss+xml")
        found = f.discover_feed("https://example.com/feed")
        assert found["feed_url"] == "https://example.com/feed" and found["title"] == "Example Blog"

    def test_html_page_with_link_tag(self):
        f = FeedFetcher(blog_fetcher=MagicMock())
        f._client = MagicMock()
        html = '<html><head><link rel="alternate" type="application/rss+xml" href="/rss.xml"></head></html>'
        f._client.get.side_effect = lambda url: {
            "https://x.substack.com": _resp(html, "https://x.substack.com"),
            "https://x.substack.com/rss.xml": _resp(ATOM, "https://x.substack.com/rss.xml", "application/xml"),
        }.get(url) or (_ for _ in ()).throw(__import__("httpx").HTTPError("404"))
        found = f.discover_feed("x.substack.com")
        assert found["feed_url"] == "https://x.substack.com/rss.xml"

    def test_common_paths_fallback(self):
        import httpx
        f = FeedFetcher(blog_fetcher=MagicMock())
        f._client = MagicMock()
        def get(url):
            if url == "https://blog.example":
                return _resp("<html></html>", url)
            if url == "https://blog.example/feed":
                return _resp(RSS, url, "application/rss+xml")
            raise httpx.HTTPError("404")
        f._client.get.side_effect = get
        assert f.discover_feed("https://blog.example")["feed_url"] == "https://blog.example/feed"

    def test_nothing_found(self):
        import httpx
        f = FeedFetcher(blog_fetcher=MagicMock())
        f._client = MagicMock()
        f._client.get.side_effect = httpx.HTTPError("down")
        assert f.discover_feed("https://nowhere.example") is None


class TestEntriesToItems:
    def test_builds_items_and_enriches_short_summaries(self):
        from src.blog_fetcher import BlogFetcher
        bf = MagicMock(wraps=BlogFetcher.__new__(BlogFetcher))
        bf.build_post = BlogFetcher.build_post
        bf.enrich_post = MagicMock(side_effect=lambda p: p)
        f = FeedFetcher(blog_fetcher=bf)
        feed = {"url": "https://example.com/feed", "title": "Example Blog"}
        entries = parse_feed(RSS)["entries"]

        items = f.entries_to_items(feed, entries, max_items=5)

        assert len(items) == 2
        assert items[0]["source"] == "rss" and items[0]["content_type"] == "blog_post"
        assert items[0]["author_name"] == "Alice" and items[1]["author_name"] == "Example Blog"
        assert items[0]["raw_data"]["feed_url"] == "https://example.com/feed"
        assert bf.enrich_post.call_count == 2  # both summaries are short

    def test_max_items(self):
        from src.blog_fetcher import BlogFetcher
        bf = MagicMock(); bf.build_post = BlogFetcher.build_post; bf.enrich_post = lambda p: p
        f = FeedFetcher(blog_fetcher=bf)
        items = f.entries_to_items({"url": "u"}, parse_feed(RSS)["entries"], max_items=1, fetch_full_text=False)
        assert len(items) == 1
