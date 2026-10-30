"""Fetch web pages and normalise them into items.

Two entry points:

* ``fetch_blog_post(url)``: one article → item dict (title/author/body/date
  extracted from the HTML).
* ``fetch_page(url)``: the visible text of any page (a newsletter issue, a
  digest), for the LLM to extract entries from. HTML heuristics about
  sections and list layouts were retired in favour of that extraction.
"""

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.content import generate_blog_id

logger = logging.getLogger(__name__)

# Max body length to store (characters)
MAX_BODY_LENGTH = 5000
# Max page text handed to the LLM for entry extraction
MAX_PAGE_TEXT = 60_000
# Timeout for HTTP requests
REQUEST_TIMEOUT = 30


def domain_of(url: str) -> str:
    return urlparse(url).netloc.removeprefix("www.")


class BlogFetcher:
    """Fetches and normalises articles and pages."""

    def __init__(self):
        self._client = httpx.Client(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; PersonalCurator/1.0)",
            },
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    # ---------------------------------------------------------------- posts

    def fetch_blog_post(self, url: str) -> dict | None:
        """Fetch a single article and return a normalised item dict."""
        try:
            response = self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.error(f"Error fetching blog post {url}: {e}")
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        final_url = str(response.url)

        title = self._extract_title(soup) or final_url
        author = self._extract_author(soup)
        body = self._extract_body(soup) or ""
        date = self._extract_date(soup)
        return self.build_post(
            url=final_url, title=title, body=body, author=author,
            published=date, source="blog", original_url=url,
        )

    @staticmethod
    def build_post(
        url: str,
        title: str,
        body: str = "",
        author: str | None = None,
        published: str | None = None,
        source: str = "blog",
        original_url: str | None = None,
        extra_raw: dict | None = None,
    ) -> dict:
        """Normalised item dict for an article-like thing (pipeline compatible)."""
        domain = domain_of(url)
        body_truncated = (body or "")[:MAX_BODY_LENGTH]
        text = title
        if body_truncated:
            text += f"\n\n{body_truncated[:500]}"
        raw = {"source": source, "original_url": original_url or url}
        if extra_raw:
            raw.update(extra_raw)
        return {
            "item_id": generate_blog_id(url),
            "source": source,
            "author_username": domain,
            "author_name": author or domain,
            "title": title,
            "text": text,
            "created_at": published or datetime.now(timezone.utc).isoformat(),
            "metrics": {},
            "url": url,
            "raw_data": raw,
            "article": {"title": title, "body": body_truncated, "url": url},
            "content_type": "blog_post",
            "is_retweet": False,
        }

    def enrich_post(self, post: dict) -> dict:
        """Fill in the article body (and author) by fetching the post's URL."""
        url = post["url"]
        try:
            response = self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning(f"Could not fetch article content for {url}: {e}")
            return post

        soup = BeautifulSoup(response.text, "html.parser")
        body = self._extract_body(soup)
        if body:
            body_truncated = body[:MAX_BODY_LENGTH]
            post.setdefault("article", {})["body"] = body_truncated
            title = post.get("title") or post.get("article", {}).get("title") or url
            post["text"] = title + f"\n\n{body_truncated[:500]}"
        if post.get("author_name") in (None, domain_of(url)):
            author = self._extract_author(soup)
            if author:
                post["author_name"] = author
        if not post.get("created_at"):
            post["created_at"] = self._extract_date(soup) or datetime.now(timezone.utc).isoformat()
        return post

    # ---------------------------------------------------------------- pages

    def fetch_page(self, url: str) -> dict | None:
        """Visible text of a page, for LLM entry extraction.

        Links are kept inline as ``anchor text <href>`` so the model can
        return real destination URLs.

        Returns:
            {url, title, text} or None.
        """
        try:
            response = self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.error(f"Error fetching page {url}: {e}")
            return None
        soup = BeautifulSoup(response.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form"]):
            tag.decompose()
        title = self._extract_title(soup) or str(response.url)
        root = (
            soup.find("article")
            or soup.find(attrs={"role": "main"})
            or soup.find("main")
            or soup.body
            or soup
        )
        for a in root.find_all("a", href=True):
            href = a["href"]
            anchor = a.get_text(strip=True)
            if href.startswith("http") and anchor:
                a.replace_with(f"{anchor} <{href}>")
        text = root.get_text("\n", strip=True)
        return {
            "url": str(response.url),
            "title": title,
            "text": text[:MAX_PAGE_TEXT],
        }

    def resolve_url(self, url: str) -> str | None:
        """Resolve a potentially click-tracked URL to its final destination."""
        try:
            response = self._client.head(url)
            return str(response.url)
        except httpx.HTTPError:
            try:
                response = self._client.get(url)
                return str(response.url)
            except httpx.HTTPError as e:
                logger.warning(f"Could not resolve URL {url}: {e}")
                return None

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _extract_title(soup: BeautifulSoup) -> str | None:
        """Extract article title from HTML."""
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            return og_title["content"].strip()
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            return title_tag.string.strip()
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)
        return None

    @staticmethod
    def _extract_author(soup: BeautifulSoup) -> str | None:
        """Extract author from HTML meta tags."""
        meta_author = soup.find("meta", attrs={"name": "author"})
        if meta_author and meta_author.get("content"):
            return meta_author["content"].strip()
        og_author = soup.find("meta", property="article:author")
        if og_author and og_author.get("content"):
            return og_author["content"].strip()
        return None

    @staticmethod
    def _extract_body(soup: BeautifulSoup) -> str | None:
        """Extract main article body text from HTML."""
        article = soup.find("article")
        if article:
            return article.get_text("\n", strip=True)
        for selector in [
            {"class_": "post-body"},
            {"class_": "article-body"},
            {"class_": "entry-content"},
            {"class_": "post-content"},
            {"class_": "cooked"},  # Discourse forums (ethresear.ch)
            {"role": "main"},
        ]:
            content = soup.find(attrs=selector)
            if content:
                return content.get_text("\n", strip=True)
        main = soup.find("main")
        if main:
            return main.get_text("\n", strip=True)
        return None

    @staticmethod
    def _extract_date(soup: BeautifulSoup) -> str | None:
        """Extract publication date from HTML meta tags."""
        for prop in ["article:published_time", "og:article:published_time"]:
            meta = soup.find("meta", property=prop)
            if meta and meta.get("content"):
                return meta["content"].strip()
        meta_date = soup.find("meta", attrs={"name": "date"})
        if meta_date and meta_date.get("content"):
            return meta_date["content"].strip()
        time_tag = soup.find("time", datetime=True)
        if time_tag:
            return time_tag["datetime"].strip()
        return None
