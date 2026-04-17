"""Pre-LLM keyword blocklist filter for tweets and blog posts."""

import logging
import re

logger = logging.getLogger(__name__)


def compile_keyword_pattern(keywords: list[str]) -> re.Pattern | None:
    """Compile a combined whole-word, case-insensitive regex from keywords.

    Returns None if keywords is empty (indicating no filter should be applied).
    """
    cleaned = [k.strip() for k in keywords if k and k.strip()]
    if not cleaned:
        return None
    # Sort longest-first so that multi-word phrases win over shorter prefixes.
    cleaned.sort(key=len, reverse=True)
    escaped = [re.escape(k) for k in cleaned]
    pattern = r"\b(?:" + "|".join(escaped) + r")\b"
    return re.compile(pattern, re.IGNORECASE)


def _item_text_blob(item: dict) -> str:
    """Concatenate fields we want to match against into a single string.

    Covers the main visible fields for both tweets and blog posts: `text`,
    `article.title`, `article.body`, and `quoted_tweet.text`.
    """
    parts = [item.get("text") or ""]

    article = item.get("article")
    if isinstance(article, dict):
        parts.append(article.get("title") or "")
        parts.append(article.get("body") or "")

    quoted = item.get("quoted_tweet")
    if isinstance(quoted, dict):
        parts.append(quoted.get("text") or "")

    return "\n".join(parts)


def find_blocked_match(item: dict, pattern: re.Pattern) -> str | None:
    """Return the matched keyword if the item contains any blocked keyword."""
    blob = _item_text_blob(item)
    match = pattern.search(blob)
    return match.group(0) if match else None


def filter_blocked_keywords(
    items: list[dict],
    keywords: list[str],
    exempt_authors: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Split items into (kept, blocked) using the keyword blocklist.

    Matches whole-word, case-insensitive across `text`, `article.title`,
    `article.body`, and `quoted_tweet.text`.

    Favorite authors are exempt from this filter — their content always passes
    through (muted authors are already dropped upstream). To change this
    behavior later, remove the `exempt_authors` check below.
    """
    pattern = compile_keyword_pattern(keywords)
    if pattern is None:
        return items, []

    exempt = {a.lower() for a in (exempt_authors or set())}

    kept: list[dict] = []
    blocked: list[dict] = []
    for item in items:
        author = (item.get("author_username") or "").lower()
        if author in exempt:
            kept.append(item)
            continue

        match = find_blocked_match(item, pattern)
        if match:
            item["blocked_keyword"] = match
            blocked.append(item)
            logger.info(
                f"Blocked by keyword '{match}': @{author} {item.get('tweet_id', '?')}"
            )
        else:
            kept.append(item)

    return kept, blocked
