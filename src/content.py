"""Content ID utilities for multi-source content (tweets, blog posts)."""

import hashlib
import re


def generate_blog_id(url: str) -> str:
    """Generate a deterministic content ID from a blog post URL.

    Uses SHA256 hash of the canonical URL with a 'blog_' prefix.
    Same URL always produces the same ID.
    """
    canonical = url.strip().rstrip("/").lower()
    url_hash = hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return f"blog_{url_hash}"


def is_blog_content(content_id: str) -> bool:
    """Check if a content ID represents a blog post."""
    return content_id.startswith("blog_")


def is_tweet_url(url: str) -> bool:
    """Check if a URL is a Twitter/X tweet URL."""
    return bool(
        re.match(
            r"https?://(?:www\.)?(?:twitter|x)\.com/[A-Za-z0-9_]+/status/\d+",
            url,
        )
    )


def is_twitter_profile_url(url: str) -> bool:
    """Check if a URL is a Twitter/X profile URL (not a tweet URL)."""
    return bool(
        re.match(
            r"https?://(?:www\.)?(?:twitter|x)\.com/[A-Za-z0-9_]+/?$",
            url,
        )
    )


def is_http_url(text: str) -> bool:
    """Check if text looks like an HTTP(S) URL."""
    return bool(re.match(r"https?://\S+", text))


def retweeter_username(item: dict) -> str:
    """Lower-cased username of whoever retweeted ``item``, or "".

    New rows keep the retweeter in ``retweeted_by`` (top level or in
    ``raw_data``). Rows written before retweets were stored as the original
    have the retweeter as ``author_username`` and the original in
    ``raw_data.retweeted_from``.
    """
    if not item.get("is_retweet"):
        return ""
    raw = item.get("raw_data") or {}
    by = item.get("retweeted_by") or raw.get("retweeted_by")
    if isinstance(by, dict) and by.get("author_username"):
        return by["author_username"].lower()
    return (item.get("author_username") or "").lower()
