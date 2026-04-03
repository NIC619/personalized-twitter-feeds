"""Tests for blog fetcher and content utilities."""

from unittest.mock import MagicMock, patch

import pytest

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
