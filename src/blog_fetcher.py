"""Blog post and newsletter fetching/parsing."""

import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from src.content import generate_blog_id

logger = logging.getLogger(__name__)

# Max body length to store (characters)
MAX_BODY_LENGTH = 5000
# Timeout for HTTP requests
REQUEST_TIMEOUT = 30


class BlogFetcher:
    """Fetches and normalizes blog posts and newsletters."""

    def __init__(self):
        self._client = httpx.Client(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; PersonalCurator/1.0)",
            },
        )

    def fetch_blog_post(self, url: str) -> dict | None:
        """Fetch a single blog post and return a normalized dict.

        The returned dict is compatible with the tweet pipeline (same field names).
        """
        try:
            response = self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.error(f"Error fetching blog post {url}: {e}")
            return None

        soup = BeautifulSoup(response.text, "html.parser")
        final_url = str(response.url)

        title = self._extract_title(soup)
        author = self._extract_author(soup)
        body = self._extract_body(soup)
        date = self._extract_date(soup)
        domain = urlparse(final_url).netloc.removeprefix("www.")

        if not title:
            title = final_url

        # Truncate body for storage
        body_truncated = body[:MAX_BODY_LENGTH] if body else ""

        # Build text field (used for embeddings) — title + first part of body
        text = title
        if body_truncated:
            text += f"\n\n{body_truncated[:500]}"

        return {
            "tweet_id": generate_blog_id(url),
            "author_username": domain,
            "author_name": author or domain,
            "text": text,
            "created_at": date or datetime.now(timezone.utc).isoformat(),
            "metrics": {},
            "url": final_url,
            "raw_data": {"source": "blog", "original_url": url},
            "article": {
                "title": title,
                "body": body_truncated,
                "url": final_url,
            },
            "content_type": "blog_post",
            "is_retweet": False,
        }

    def parse_newsletter(
        self, url: str, ignored_sections: list[str] | None = None,
    ) -> list[dict]:
        """Parse a newsletter URL and extract all blog post entries.

        Returns a list of normalized dicts compatible with the tweet pipeline.
        Each entry has content from the newsletter description plus any
        fetched article content.

        Args:
            url: Newsletter URL to parse
            ignored_sections: Section names to skip (case-insensitive match).
                If None, all sections are included.
        """
        try:
            response = self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.error(f"Error fetching newsletter {url}: {e}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        entries = self._extract_newsletter_entries(soup, ignored_sections)
        logger.info(f"Extracted {len(entries)} entries from newsletter")

        if not entries:
            # Check if we found sections but no entries — likely an unsupported layout
            container, htag = self._find_newsletter_container(soup)
            headings = [h.get_text(strip=True) for h in container.find_all(htag)]
            links = soup.find_all("a", href=True)
            logger.warning(
                f"No entries extracted from newsletter. "
                f"Layout debug: container=<{container.name}>, heading_tag={htag}, "
                f"headings={len(headings)}, total_links={len(links)}"
            )

        posts = []
        for entry in entries:
            post_url = entry["url"]
            # Resolve click-tracked URLs by following redirects
            resolved_url = self._resolve_url(post_url)
            if not resolved_url:
                continue

            post = self._build_newsletter_post(entry, resolved_url)
            if post:
                posts.append(post)

        logger.info(f"Built {len(posts)} blog posts from newsletter")
        return posts

    def extract_sections(self, url: str) -> list[str]:
        """Extract section headings from a newsletter URL.

        Looks for <h3> headings in the article content which are the
        standard section delimiters in Substack-style newsletters.

        Args:
            url: Newsletter URL

        Returns:
            List of section heading strings
        """
        try:
            response = self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.error(f"Error fetching newsletter for sections {url}: {e}")
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        return self._get_section_headings(soup)

    def fetch_and_enrich_post(self, post: dict) -> dict:
        """Fetch the actual article content for a newsletter post.

        Enriches the post dict with full article body from the URL.
        """
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
            post["article"]["body"] = body_truncated
            # Update text field for better embeddings
            post["text"] = post["article"]["title"] + f"\n\n{body_truncated[:500]}"

        # Try to extract author from the actual page if we only have domain
        domain = urlparse(url).netloc.removeprefix("www.")
        if post["author_name"] == domain:
            author = self._extract_author(soup)
            if author:
                post["author_name"] = author

        return post

    @staticmethod
    def _find_newsletter_container(soup: BeautifulSoup) -> tuple:
        """Find the DOM container and heading tag name for newsletter sections.

        Supported layouts:
        - Substack: <article> > div.body.markup > h3 + ul
        - Mailerlite: <table> > td > h2 + ul (email-style table layout)
        - Generic: any element with the most direct h2/h3 children

        Returns:
            (container_element, heading_tag) e.g. (div_element, "h3")
        """
        # Try Substack-style: div with class "body" inside article
        article = soup.find("article")
        if article:
            body_div = article.find("div", class_="body")
            if body_div:
                direct_h3s = [c for c in body_div.children
                              if getattr(c, "name", None) == "h3"]
                if direct_h3s:
                    return body_div, "h3"

            direct_h3s = [c for c in article.children
                          if getattr(c, "name", None) == "h3"]
            if direct_h3s:
                return article, "h3"

        # Generic: find element with most direct h2 or h3 children
        body = soup.find("body") or soup
        best = body
        best_count = 0
        best_tag = "h3"

        for element in body.find_all(True):  # all tags
            for htag in ("h3", "h2"):
                count = sum(1 for c in element.children
                            if getattr(c, "name", None) == htag)
                if count > best_count:
                    best_count = count
                    best = element
                    best_tag = htag

        return (best, best_tag) if best_count > 0 else (body, "h3")

    @staticmethod
    def _get_section_headings(soup: BeautifulSoup) -> list[str]:
        """Extract section headings that contain article links.

        Supports both h2 and h3 as section delimiters depending on the
        newsletter layout. Headings with no linked list items underneath
        are skipped (e.g. subtitles, decorative headings).
        """
        container, htag = BlogFetcher._find_newsletter_container(soup)
        sections = []
        for heading in container.find_all(htag):
            text = heading.get_text(strip=True)
            if not text or len(text) <= 1:
                continue
            # Check if this section has any <ul> with <li><a> before the next heading
            has_links = False
            sibling = heading.find_next_sibling()
            while sibling and getattr(sibling, "name", None) != htag:
                if getattr(sibling, "name", None) == "ul":
                    for li in sibling.find_all("li", recursive=False):
                        if li.find("a", href=True):
                            has_links = True
                            break
                if has_links:
                    break
                sibling = sibling.find_next_sibling()
            if has_links:
                sections.append(text)
        return sections

    def _extract_newsletter_entries(
        self,
        soup: BeautifulSoup,
        ignored_sections: list[str] | None = None,
    ) -> list[dict]:
        """Extract blog post entries from newsletter HTML, grouped by section.

        Supports both h2-sectioned (mailerlite) and h3-sectioned (Substack)
        newsletters. Sections are detected by the heading tag that appears
        most frequently as direct children of the content container.

        Args:
            soup: Parsed newsletter HTML
            ignored_sections: Section names to skip (case-insensitive).
                If None, all sections are included.
        """
        container, htag = BlogFetcher._find_newsletter_container(soup)
        ignored = {s.lower() for s in (ignored_sections or [])}

        entries = []
        seen_urls = set()
        current_section = None

        # Walk through top-level children of the container to track sections
        for element in container.children:
            if not hasattr(element, "name") or element.name is None:
                continue

            # Detect section boundaries
            if element.name == htag:
                current_section = element.get_text(strip=True)
                continue

            # Skip content in ignored sections
            if current_section and current_section.lower() in ignored:
                continue

            # Look for list items with links
            lis = []
            if element.name == "ul":
                lis = element.find_all("li", recursive=False)
            elif element.name == "li":
                lis = [element]

            for li in lis:
                entry = self._extract_entry_from_li(li, seen_urls)
                if entry:
                    entry["section"] = current_section
                    entries.append(entry)

        return entries

    def _extract_entry_from_li(self, li, seen_urls: set) -> dict | None:
        """Extract a single article entry from a <li> element.

        Returns:
            Entry dict with url, title, author, description, or None if invalid.
        """
        links = li.find_all("a", href=True)
        if not links:
            return None

        first_link = links[0]
        url = first_link.get("href", "").strip()
        title = first_link.get_text(strip=True)

        if not url or not title:
            return None
        if not url.startswith("http"):
            return None
        if len(title) < 10:
            return None
        if url in seen_urls:
            return None
        seen_urls.add(url)

        author = self._extract_author_from_li(li, first_link)
        description = self._extract_description_from_li(li, first_link)

        return {
            "url": url,
            "title": title,
            "author": author,
            "description": description,
        }

    def _extract_author_from_li(self, li, title_link) -> str | None:
        """Extract author name from a newsletter list item.

        Looks for "by Author Name" pattern after the title link.
        """
        full_text = li.get_text(" ", strip=True)
        title_text = title_link.get_text(strip=True)

        # Find text after the title
        idx = full_text.find(title_text)
        if idx == -1:
            return None

        after_title = full_text[idx + len(title_text):].strip()

        # Look for "by Author" pattern
        if after_title.lower().startswith("by "):
            author_text = after_title[3:].strip()
            # Take text up to the first sentence boundary or description start
            for sep in [".", "—", "–", "-", ",", "\n"]:
                if sep in author_text:
                    author_text = author_text[:author_text.index(sep)].strip()
                    break
            # Clean up: remove "et al" trailing text, limit length
            if author_text and len(author_text) < 100:
                return author_text

        return None

    def _extract_description_from_li(self, li, title_link) -> str:
        """Extract description text from a newsletter list item."""
        full_text = li.get_text(" ", strip=True)
        title_text = title_link.get_text(strip=True)

        idx = full_text.find(title_text)
        if idx == -1:
            return ""

        after_title = full_text[idx + len(title_text):].strip()

        # Skip past "by Author" prefix if present
        if after_title.lower().startswith("by "):
            # Find where description starts (after author)
            for sep in [".", "—", "–"]:
                sep_idx = after_title.find(sep)
                if sep_idx != -1:
                    after_title = after_title[sep_idx + 1:].strip()
                    break

        # Strip leading punctuation/whitespace left after author removal
        after_title = after_title.lstrip(" .,;:—–-")

        return after_title[:500] if after_title else ""

    def _build_newsletter_post(self, entry: dict, resolved_url: str) -> dict | None:
        """Build a normalized post dict from a newsletter entry."""
        domain = urlparse(resolved_url).netloc.removeprefix("www.")
        title = entry["title"]
        description = entry.get("description", "")
        author = entry.get("author")

        text = title
        if description:
            text += f"\n\n{description[:200]}"

        return {
            "tweet_id": generate_blog_id(resolved_url),
            "author_username": domain,
            "author_name": author or domain,
            "text": text,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metrics": {},
            "url": resolved_url,
            "raw_data": {
                "source": "newsletter",
                "original_url": entry["url"],
                "newsletter_description": description,
            },
            "article": {
                "title": title,
                "body": description,
                "url": resolved_url,
            },
            "content_type": "blog_post",
            "is_retweet": False,
        }

    def _resolve_url(self, url: str) -> str | None:
        """Resolve a potentially click-tracked URL to its final destination."""
        try:
            response = self._client.head(url)
            return str(response.url)
        except httpx.HTTPError:
            # Fall back to GET if HEAD fails
            try:
                response = self._client.get(url)
                return str(response.url)
            except httpx.HTTPError as e:
                logger.warning(f"Could not resolve URL {url}: {e}")
                return None

    @staticmethod
    def _extract_title(soup: BeautifulSoup) -> str | None:
        """Extract article title from HTML."""
        # Try Open Graph title first
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            return og_title["content"].strip()

        # Try <title> tag
        title_tag = soup.find("title")
        if title_tag and title_tag.string:
            return title_tag.string.strip()

        # Try first <h1>
        h1 = soup.find("h1")
        if h1:
            return h1.get_text(strip=True)

        return None

    @staticmethod
    def _extract_author(soup: BeautifulSoup) -> str | None:
        """Extract author from HTML meta tags or page content."""
        # Try meta author tag
        meta_author = soup.find("meta", attrs={"name": "author"})
        if meta_author and meta_author.get("content"):
            return meta_author["content"].strip()

        # Try Open Graph article:author
        og_author = soup.find("meta", property="article:author")
        if og_author and og_author.get("content"):
            return og_author["content"].strip()

        return None

    @staticmethod
    def _extract_body(soup: BeautifulSoup) -> str | None:
        """Extract main article body text from HTML."""
        # Try <article> tag first
        article = soup.find("article")
        if article:
            return article.get_text("\n", strip=True)

        # Try common content containers
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

        # Fallback: try main tag
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

        # Try time tag
        time_tag = soup.find("time", datetime=True)
        if time_tag:
            return time_tag["datetime"].strip()

        return None
