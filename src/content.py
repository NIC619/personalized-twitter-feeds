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


_X_HOSTS = ("twitter.com", "x.com", "www.twitter.com", "www.x.com", "mobile.twitter.com")


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url or "")
    return m.group(1).lower() if m else ""


def tweet_links(item: dict) -> list[dict]:
    """Links in a tweet, from the X API's ``entities.urls`` (stored in raw_data).

    Each entry: ``tco`` (the t.co form as it appears in the text), ``url``
    (unwound or expanded), ``display`` (X's short display form),
    ``external`` (not an x.com/twitter.com link), ``media`` (a photo/video
    link to a tweet), and X's own ``title``/``description`` for the page
    when it supplied them. Nothing here fetches anything: X resolved the
    page, we only render what it returned.
    """
    raw = item.get("raw_data") or {}
    entities = raw.get("entities") or item.get("entities") or {}
    out: list[dict] = []
    seen: set[str] = set()
    for u in entities.get("urls") or []:
        tco = u.get("url") or ""
        expanded = u.get("unwound_url") or u.get("expanded_url") or tco
        if not tco or tco in seen:
            continue
        seen.add(tco)
        host = _host(expanded)
        is_x = host in _X_HOSTS
        out.append({
            "tco": tco,
            "url": expanded,
            "display": u.get("display_url") or expanded,
            "external": not is_x,
            "media": is_x and bool(re.search(r"/(photo|video)/\d+", expanded)),
            "title": (u.get("title") or "").strip() or None,
            "description": (u.get("description") or "").strip() or None,
        })
    return out


def link_previews(item: dict, limit: int = 2) -> list[dict]:
    """External links that X returned a page title for, most useful first."""
    return [l for l in tweet_links(item) if l["external"] and l["title"]][:limit]
