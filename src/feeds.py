"""RSS / Atom feeds: discovery, parsing (stdlib only) and fetching new entries.

Subscribed feeds are polled during the daily run; new entries are fetched in
full and scored like tweets. ``discover_feed`` turns a site or newsletter URL
into its feed URL so ``/feed add`` accepts either.
"""

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import httpx
from bs4 import BeautifulSoup

from src.blog_fetcher import BlogFetcher, domain_of

logger = logging.getLogger(__name__)

FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+json")
COMMON_FEED_PATHS = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml", "/feed/", "/rss/")
ATOM_NS = "{http://www.w3.org/2005/Atom}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"
DC_NS = "{http://purl.org/dc/elements/1.1/}"


def _strip_html(text: Optional[str]) -> str:
    if not text:
        return ""
    return BeautifulSoup(text, "html.parser").get_text(" ", strip=True)


def _parse_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    value = value.strip()
    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def parse_feed(xml_text: str) -> dict:
    """Parse RSS 2.0 or Atom into {title, site_url, entries: [...]}.

    Each entry: {id, title, url, summary, author, published}.
    Raises ValueError when the document is not a feed.
    """
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError as e:
        raise ValueError(f"not XML: {e}") from e

    tag = root.tag.lower()
    if tag.endswith("rss") or tag.endswith("rdf"):
        channel = root.find("channel")
        if channel is None:
            channel = root
        feed = {
            "title": (channel.findtext("title") or "").strip(),
            "site_url": (channel.findtext("link") or "").strip(),
            "entries": [],
        }
        for item in channel.findall("item") or root.findall("item"):
            url = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or url).strip()
            summary = item.findtext(f"{CONTENT_NS}encoded") or item.findtext("description") or ""
            feed["entries"].append({
                "id": guid or url,
                "title": (item.findtext("title") or url).strip(),
                "url": url,
                "summary": _strip_html(summary)[:2000],
                "author": (item.findtext(f"{DC_NS}creator") or item.findtext("author") or "").strip() or None,
                "published": _parse_date(item.findtext("pubDate") or item.findtext(f"{DC_NS}date")),
            })
        return feed

    if tag == f"{ATOM_NS}feed" or tag.endswith("feed"):
        ns = ATOM_NS if root.tag.startswith(ATOM_NS) else ""
        site_url = ""
        for link in root.findall(f"{ns}link"):
            if link.get("rel", "alternate") == "alternate":
                site_url = link.get("href", "")
                break
        feed = {"title": (root.findtext(f"{ns}title") or "").strip(), "site_url": site_url, "entries": []}
        for entry in root.findall(f"{ns}entry"):
            url = ""
            for link in entry.findall(f"{ns}link"):
                if link.get("rel", "alternate") == "alternate":
                    url = link.get("href", "")
                    break
            if not url:
                url = (entry.findtext(f"{ns}id") or "").strip()
            author_el = entry.find(f"{ns}author/{ns}name")
            summary = entry.findtext(f"{ns}content") or entry.findtext(f"{ns}summary") or ""
            feed["entries"].append({
                "id": (entry.findtext(f"{ns}id") or url).strip(),
                "title": (entry.findtext(f"{ns}title") or url).strip(),
                "url": url,
                "summary": _strip_html(summary)[:2000],
                "author": author_el.text.strip() if author_el is not None and author_el.text else None,
                "published": _parse_date(entry.findtext(f"{ns}published") or entry.findtext(f"{ns}updated")),
            })
        return feed

    raise ValueError(f"unrecognised feed root <{root.tag}>")


def find_feed_links(html: str, base_url: str) -> list[str]:
    """Feed URLs advertised in a page's <link rel=alternate> tags."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for link in soup.find_all("link", href=True):
        rel = " ".join(link.get("rel") or []).lower()
        typ = (link.get("type") or "").lower()
        if "alternate" in rel and typ in FEED_TYPES:
            out.append(urljoin(base_url, link["href"]))
    return out


class FeedFetcher:
    """Discover feeds and fetch their new entries as items."""

    def __init__(self, blog_fetcher: Optional[BlogFetcher] = None, timeout: float = 30.0):
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; PersonalCurator/1.0)"},
        )
        self.blog_fetcher = blog_fetcher or BlogFetcher()

    def close(self) -> None:
        self._client.close()

    def _get(self, url: str) -> Optional[httpx.Response]:
        try:
            response = self._client.get(url)
            response.raise_for_status()
            return response
        except httpx.HTTPError as e:
            logger.debug(f"GET {url} failed: {e}")
            return None

    def discover_feed(self, url: str) -> Optional[dict]:
        """Find the feed for a site/newsletter URL.

        Returns:
            {feed_url, title, site_url, entries} (parsed) or None.
        """
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        candidates: list[str] = [url]
        response = self._get(url)
        if response is not None:
            ctype = response.headers.get("content-type", "")
            body = response.text
            if "xml" in ctype or body.lstrip().startswith("<?xml") or "<rss" in body[:500] or "<feed" in body[:500]:
                try:
                    parsed = parse_feed(body)
                    return {"feed_url": str(response.url), **parsed}
                except ValueError:
                    pass
            candidates = find_feed_links(body, str(response.url)) + candidates
        parsed_url = urlparse(url)
        base = f"{parsed_url.scheme}://{parsed_url.netloc}"
        candidates += [base + p for p in COMMON_FEED_PATHS]

        seen: set[str] = set()
        for cand in candidates:
            if cand in seen or cand == url:
                continue
            seen.add(cand)
            resp = self._get(cand)
            if resp is None:
                continue
            try:
                parsed = parse_feed(resp.text)
            except ValueError:
                continue
            return {"feed_url": str(resp.url), **parsed}
        return None

    def fetch_entries(self, feed_url: str) -> list[dict]:
        """Parsed entries for a feed URL (newest first as served)."""
        response = self._get(feed_url)
        if response is None:
            raise RuntimeError(f"feed unreachable: {feed_url}")
        return parse_feed(response.text)["entries"]

    def entries_to_items(
        self,
        feed: dict,
        entries: list[dict],
        max_items: int = 10,
        fetch_full_text: bool = True,
    ) -> list[dict]:
        """Turn feed entries into pipeline items, fetching full text when asked."""
        items = []
        for entry in entries[:max_items]:
            url = entry.get("url")
            if not url:
                continue
            post = self.blog_fetcher.build_post(
                url=url,
                title=entry.get("title") or url,
                body=entry.get("summary") or "",
                author=entry.get("author") or feed.get("title"),
                published=entry.get("published"),
                source="rss",
                extra_raw={"feed_url": feed.get("url") or feed.get("feed_url"), "feed_title": feed.get("title")},
            )
            if fetch_full_text and len(entry.get("summary") or "") < 600:
                post = self.blog_fetcher.enrich_post(post)
            items.append(post)
        return items

    @staticmethod
    def label(feed: dict) -> str:
        return feed.get("title") or domain_of(feed.get("url") or feed.get("feed_url") or "")
