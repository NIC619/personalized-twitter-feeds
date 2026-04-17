"""Tests for blog fetcher and content utilities."""

from unittest.mock import MagicMock, patch

import pytest
from bs4 import BeautifulSoup

from src.content import generate_blog_id, is_blog_content, is_http_url, is_tweet_url


# --- Content utility tests ---


class TestGenerateBlogId:
    def test_deterministic(self):
        url = "https://ethresear.ch/t/sharded-pir-design/24552"
        id1 = generate_blog_id(url)
        id2 = generate_blog_id(url)
        assert id1 == id2

    def test_prefix(self):
        blog_id = generate_blog_id("https://example.com/post")
        assert blog_id.startswith("blog_")

    def test_length(self):
        blog_id = generate_blog_id("https://example.com/post")
        # blog_ (5) + 16 hex chars = 21
        assert len(blog_id) == 21

    def test_different_urls_different_ids(self):
        id1 = generate_blog_id("https://example.com/post-1")
        id2 = generate_blog_id("https://example.com/post-2")
        assert id1 != id2

    def test_trailing_slash_normalized(self):
        id1 = generate_blog_id("https://example.com/post")
        id2 = generate_blog_id("https://example.com/post/")
        assert id1 == id2

    def test_case_insensitive(self):
        id1 = generate_blog_id("https://Example.COM/Post")
        id2 = generate_blog_id("https://example.com/post")
        assert id1 == id2


class TestIsBlogContent:
    def test_blog_id(self):
        assert is_blog_content("blog_a1b2c3d4e5f67890") is True

    def test_tweet_id(self):
        assert is_blog_content("1234567890") is False

    def test_empty(self):
        assert is_blog_content("") is False


class TestIsTweetUrl:
    def test_twitter_url(self):
        assert is_tweet_url("https://twitter.com/user/status/123456") is True

    def test_x_url(self):
        assert is_tweet_url("https://x.com/user/status/123456") is True

    def test_blog_url(self):
        assert is_tweet_url("https://ethresear.ch/t/some-post/123") is False

    def test_not_url(self):
        assert is_tweet_url("123456") is False


class TestIsHttpUrl:
    def test_https(self):
        assert is_http_url("https://example.com") is True

    def test_http(self):
        assert is_http_url("http://example.com") is True

    def test_not_url(self):
        assert is_http_url("not a url") is False

    def test_numeric(self):
        assert is_http_url("123456") is False


# --- BlogFetcher tests ---


class TestBlogFetcher:
    @patch("src.blog_fetcher.httpx.Client")
    def test_fetch_blog_post_success(self, mock_client_cls):
        from src.blog_fetcher import BlogFetcher

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.url = "https://ethresear.ch/t/test-post/123"
        mock_response.text = """
        <html>
        <head>
            <meta property="og:title" content="Test Blog Post Title" />
            <meta name="author" content="Alice" />
        </head>
        <body>
            <article>
                <p>This is the article body content for testing.</p>
            </article>
        </body>
        </html>
        """
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        fetcher = BlogFetcher()
        post = fetcher.fetch_blog_post("https://ethresear.ch/t/test-post/123")

        assert post is not None
        assert post["tweet_id"].startswith("blog_")
        assert post["content_type"] == "blog_post"
        assert post["article"]["title"] == "Test Blog Post Title"
        assert post["author_name"] == "Alice"
        assert post["is_retweet"] is False
        assert post["url"] == "https://ethresear.ch/t/test-post/123"

    @patch("src.blog_fetcher.httpx.Client")
    def test_fetch_blog_post_failure(self, mock_client_cls):
        from src.blog_fetcher import BlogFetcher
        import httpx

        mock_client = MagicMock()
        mock_client.get.side_effect = httpx.HTTPError("Connection failed")
        mock_client_cls.return_value = mock_client

        fetcher = BlogFetcher()
        post = fetcher.fetch_blog_post("https://example.com/bad")
        assert post is None

    @patch("src.blog_fetcher.httpx.Client")
    def test_parse_newsletter_extracts_entries(self, mock_client_cls):
        from src.blog_fetcher import BlogFetcher

        newsletter_html = """
        <html><body>
        <ul>
            <li>
                <a href="https://example.com/article-1">First Article Title Here</a>
                by Alice. This article discusses important topics.
            </li>
            <li>
                <a href="https://example.com/article-2">Second Article Title Here</a>
                by Bob. Another interesting piece about research.
            </li>
        </ul>
        </body></html>
        """

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = newsletter_html
        mock_response.url = "https://newsletter.example.com/issue/1"

        # HEAD requests for URL resolution
        mock_head_response = MagicMock()

        def mock_head(url):
            resp = MagicMock()
            resp.url = url
            return resp

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.head.side_effect = mock_head
        mock_client_cls.return_value = mock_client

        fetcher = BlogFetcher()
        posts = fetcher.parse_newsletter("https://newsletter.example.com/issue/1")

        assert len(posts) == 2
        assert posts[0]["article"]["title"] == "First Article Title Here"
        assert posts[0]["content_type"] == "blog_post"
        assert posts[0]["tweet_id"].startswith("blog_")
        assert posts[1]["article"]["title"] == "Second Article Title Here"

    @patch("src.blog_fetcher.httpx.Client")
    def test_parse_newsletter_preserves_text_before_link(self, mock_client_cls):
        """Text before the <a> link (e.g. "Etherscan (block explorer)") should
        become part of the title so context isn't lost."""
        from src.blog_fetcher import BlogFetcher

        newsletter_html = """
        <html><body>
        <ul>
            <li>Etherscan (block explorer) <a href="https://example.com/etherscan">token holders overview</a>, concentration, tier distribution &amp; Gini score; beta</li>
            <li><a href="https://example.com/evmnow">open source contract metadata</a>: standard, SDK &amp; dapp UI</li>
        </ul>
        </body></html>
        """

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = newsletter_html
        mock_response.url = "https://newsletter.example.com/issue/2"

        def mock_head(url):
            resp = MagicMock()
            resp.url = url
            return resp

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.head.side_effect = mock_head
        mock_client_cls.return_value = mock_client

        fetcher = BlogFetcher()
        posts = fetcher.parse_newsletter("https://newsletter.example.com/issue/2")

        assert len(posts) == 2
        # Prefix text is folded into the title
        assert posts[0]["article"]["title"] == "Etherscan (block explorer) token holders overview"
        assert "concentration" in posts[0]["article"]["body"]
        # When there's no prefix, title is unchanged
        assert posts[1]["article"]["title"] == "open source contract metadata"

    @patch("src.blog_fetcher.httpx.Client")
    def test_blog_post_dict_compatible_with_pipeline(self, mock_client_cls):
        """Verify the returned dict has all fields needed by the tweet pipeline."""
        from src.blog_fetcher import BlogFetcher

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.url = "https://example.com/post"
        mock_response.text = """
        <html><head><title>Test Post</title></head>
        <body><main>Body text here.</main></body></html>
        """
        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        fetcher = BlogFetcher()
        post = fetcher.fetch_blog_post("https://example.com/post")

        # These fields are required by save_tweets() in database.py
        required_fields = [
            "tweet_id", "author_username", "author_name",
            "text", "created_at", "metrics", "url",
            "raw_data", "is_retweet", "content_type",
        ]
        for field in required_fields:
            assert field in post, f"Missing required field: {field}"

        # These fields are used by Claude filter
        assert "article" in post
        assert "title" in post["article"]
        assert "body" in post["article"]


# --- Section-aware parsing tests ---

SECTIONED_NEWSLETTER_HTML = """
<html><body>
<article>
<h3>Ecosystem</h3>
<ul>
    <li><a href="https://example.com/eco-1">Ecosystem Article One Title</a> by Alice. Description here.</li>
    <li><a href="https://example.com/eco-2">Ecosystem Article Two Title</a> by Bob. More info.</li>
</ul>
<h3>Sponsor: Acme Corp</h3>
<ul>
    <li><a href="https://example.com/sponsor">Sponsored Content Title Here</a> Check it out.</li>
</ul>
<h3>Developers</h3>
<ul>
    <li><a href="https://example.com/dev-1">Developer Article One Title</a> by Charlie. Dev stuff.</li>
</ul>
<h3>Regulation</h3>
<ul>
    <li><a href="https://example.com/reg-1">Regulation Article Title Here</a> by Dave. Legal stuff.</li>
</ul>
</article>
</body></html>
"""


class TestGetSectionHeadings:
    def test_extracts_h3_headings(self):
        from src.blog_fetcher import BlogFetcher

        soup = BeautifulSoup(SECTIONED_NEWSLETTER_HTML, "html.parser")
        sections = BlogFetcher._get_section_headings(soup)
        assert sections == ["Ecosystem", "Sponsor: Acme Corp", "Developers", "Regulation"]

    def test_no_headings(self):
        from src.blog_fetcher import BlogFetcher

        soup = BeautifulSoup("<html><body><ul><li>item</li></ul></body></html>", "html.parser")
        sections = BlogFetcher._get_section_headings(soup)
        assert sections == []

    def test_skips_subtitle_without_links(self):
        """h3 headings with no article links underneath should be excluded."""
        from src.blog_fetcher import BlogFetcher

        html = """
        <html><body><article>
        <h3>Subtitle with no articles</h3>
        <p>Just some text, no links here.</p>
        <h3>Real Section</h3>
        <ul><li><a href="https://example.com/a">Article Title Here</a></li></ul>
        </article></body></html>
        """
        soup = BeautifulSoup(html, "html.parser")
        sections = BlogFetcher._get_section_headings(soup)
        assert sections == ["Real Section"]


class TestSectionAwareParsing:
    @patch("src.blog_fetcher.httpx.Client")
    def test_parse_all_sections(self, mock_client_cls):
        from src.blog_fetcher import BlogFetcher

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = SECTIONED_NEWSLETTER_HTML
        mock_response.url = "https://newsletter.example.com/issue/1"

        def mock_head(url):
            resp = MagicMock()
            resp.url = url
            return resp

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.head.side_effect = mock_head
        mock_client_cls.return_value = mock_client

        fetcher = BlogFetcher()
        posts = fetcher.parse_newsletter("https://newsletter.example.com/issue/1")
        assert len(posts) == 5

    @patch("src.blog_fetcher.httpx.Client")
    def test_ignore_sections(self, mock_client_cls):
        from src.blog_fetcher import BlogFetcher

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = SECTIONED_NEWSLETTER_HTML
        mock_response.url = "https://newsletter.example.com/issue/1"

        def mock_head(url):
            resp = MagicMock()
            resp.url = url
            return resp

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.head.side_effect = mock_head
        mock_client_cls.return_value = mock_client

        fetcher = BlogFetcher()
        posts = fetcher.parse_newsletter(
            "https://newsletter.example.com/issue/1",
            ignored_sections=["Sponsor: Acme Corp", "Regulation"],
        )
        # Should skip sponsor (1 article) and regulation (1 article), leaving 3
        assert len(posts) == 3
        titles = [p["article"]["title"] for p in posts]
        assert "Sponsored Content Title Here" not in titles
        assert "Regulation Article Title Here" not in titles
        assert "Ecosystem Article One Title" in titles
        assert "Developer Article One Title" in titles

    @patch("src.blog_fetcher.httpx.Client")
    def test_ignore_sections_case_insensitive(self, mock_client_cls):
        from src.blog_fetcher import BlogFetcher

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = SECTIONED_NEWSLETTER_HTML
        mock_response.url = "https://newsletter.example.com/issue/1"

        def mock_head(url):
            resp = MagicMock()
            resp.url = url
            return resp

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client.head.side_effect = mock_head
        mock_client_cls.return_value = mock_client

        fetcher = BlogFetcher()
        posts = fetcher.parse_newsletter(
            "https://newsletter.example.com/issue/1",
            ignored_sections=["regulation"],  # lowercase
        )
        titles = [p["article"]["title"] for p in posts]
        assert "Regulation Article Title Here" not in titles

    @patch("src.blog_fetcher.httpx.Client")
    def test_extract_sections(self, mock_client_cls):
        from src.blog_fetcher import BlogFetcher

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = SECTIONED_NEWSLETTER_HTML
        mock_response.url = "https://newsletter.example.com/issue/1"

        mock_client = MagicMock()
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        fetcher = BlogFetcher()
        sections = fetcher.extract_sections("https://newsletter.example.com/issue/1")
        assert sections == ["Ecosystem", "Sponsor: Acme Corp", "Developers", "Regulation"]

    @patch("src.blog_fetcher.httpx.Client")
    def test_entries_have_section_field(self, mock_client_cls):
        """Entries extracted from sectioned HTML include their section name."""
        from src.blog_fetcher import BlogFetcher

        soup = BeautifulSoup(SECTIONED_NEWSLETTER_HTML, "html.parser")
        fetcher = BlogFetcher()
        entries = fetcher._extract_newsletter_entries(soup)
        sections_found = {e["section"] for e in entries}
        assert "Ecosystem" in sections_found
        assert "Developers" in sections_found
