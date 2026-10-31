"""Newsletter issue → scored items.

One place for the pipeline shared by ``/newsletter <url>`` and the mailbox
step of the daily run: issue text → LLM extraction of the linked items →
items (deduplicated against the store) → scoring.
"""

import logging
from typing import Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from src.blog_fetcher import BlogFetcher

logger = logging.getLogger(__name__)

TRACKING_PARAMS = ("utm_", "ref", "ref_src", "mc_cid", "mc_eid", "fbclid", "gclid", "_hsenc", "_hsmi")


def clean_url(url: str) -> str:
    """Drop fragment and common tracking query params so the same article
    gets the same item id whichever newsletter linked it."""
    parsed = urlparse(url)
    query = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(k == p or k.startswith(p) for p in TRACKING_PARAMS)
    ]
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))


class NewsletterProcessor:
    """Extract, dedupe and score the links in a newsletter issue."""

    def __init__(
        self,
        claude,
        blog_fetcher: BlogFetcher,
        db,
        score: Callable[[list[dict]], list[dict]],
    ):
        """
        Args:
            claude: ClaudeFilter (for ``extract_newsletter_entries``)
            blog_fetcher: BlogFetcher (page fetch, item building, link resolution)
            db: DatabaseClient
            score: function(items) → items, scoring and persisting new items
        """
        self.claude = claude
        self.blog_fetcher = blog_fetcher
        self.db = db
        self.score = score

    # ------------------------------------------------------------ entry points

    def from_page(self, url: str) -> dict:
        """An issue published on the web (``/newsletter <url>``)."""
        page = self.blog_fetcher.fetch_page(url)
        if not page:
            return {"title": url, "entries": 0, "items": []}
        return self.process(page["title"], page["text"], issue_url=url)

    def from_email(self, mail: dict) -> dict:
        """An issue received by email (``parse_email`` output)."""
        sender = mail.get("sender") or ""
        issue_url = f"mailto:{sender}" if sender else None
        return self.process(
            mail.get("subject") or "(no subject)",
            mail.get("text") or "",
            issue_url=issue_url,
            extra_raw={"newsletter_from": sender, "newsletter_sender_name": mail.get("sender_name")},
            resolve_links=True,
        )

    # ----------------------------------------------------------------- pipeline

    def process(
        self,
        title: str,
        text: str,
        issue_url: Optional[str] = None,
        extra_raw: Optional[dict] = None,
        resolve_links: bool = False,
    ) -> dict:
        """Returns {title, entries, items} where ``items`` are all linked items
        in issue order, each scored (fresh ones now, known ones from the store)."""
        if not text.strip():
            return {"title": title, "entries": 0, "items": []}
        entries = self.claude.extract_newsletter_entries(title, text)
        if not entries:
            return {"title": title, "entries": 0, "items": []}
        if resolve_links:
            entries = self._resolve(entries)

        items = []
        seen: set[str] = set()
        for e in entries:
            post = self.blog_fetcher.build_post(
                url=clean_url(e["url"]), title=e["title"], body=e.get("summary") or "",
                author=e.get("author"), source="newsletter",
                original_url=issue_url,
                extra_raw={
                    "newsletter_url": issue_url, "newsletter_title": title,
                    "section": e.get("section"), **(extra_raw or {}),
                },
            )
            if post["item_id"] in seen:
                continue
            seen.add(post["item_id"])
            items.append(post)

        # Skip ones already scored (same link in several issues / feeds)
        already = self.db.get_scored_ids([i["item_id"] for i in items])
        fresh = [i for i in items if i["item_id"] not in already]
        if fresh:
            self.score(fresh)
        scored = {i["item_id"]: i for i in fresh}
        known = self.db.get_items([i["item_id"] for i in items if i["item_id"] in already]) if already else {}
        ordered = [scored.get(i["item_id"]) or known.get(i["item_id"]) for i in items]
        logger.info(f"Newsletter '{title}': {len(entries)} links, {len(fresh)} new")
        return {"title": title, "entries": len(entries), "items": [i for i in ordered if i]}

    def _resolve(self, entries: list[dict]) -> list[dict]:
        """Follow every link to its destination: email links are almost all
        click-tracking redirects, and the item id is derived from the URL."""
        out = []
        for e in entries:
            final = self.blog_fetcher.resolve_url(e["url"])
            if final and final.startswith(("http://", "https://")):
                e = {**e, "url": clean_url(final)}
            out.append(e)
        return out
