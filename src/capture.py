"""Turn things the user sends to the bot into items.

Pure helpers: classify a message, build minimal items for URLs that could
not be fetched and for free text / forwarded posts.
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from src.content import generate_blog_id, is_http_url, is_tweet_url, is_twitter_profile_url

# Shortest free text worth storing as an item on its own
MIN_TEXT_CAPTURE = 20
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{1,15}$")


@dataclass
class Classified:
    tweet_refs: list[str] = field(default_factory=list)   # tweet URLs or numeric ids
    urls: list[str] = field(default_factory=list)         # other http(s) URLs
    usernames: list[str] = field(default_factory=list)    # profile URLs / @handles
    text: Optional[str] = None                            # free text worth capturing
    unknown: list[str] = field(default_factory=list)


def classify_message(text: str, forwarded: bool = False) -> Classified:
    """Decide what a plain message asks for.

    - tweet URLs / numeric ids → like that tweet
    - profile URLs / @handles (whole message is handles) → star
    - any other URL → capture the page
    - forwarded posts, or free text of MIN_TEXT_CAPTURE+ chars without URLs → capture the text
    """
    out = Classified()
    text = (text or "").strip()
    if not text:
        return out
    tokens = text.split()

    for tok in tokens:
        if is_http_url(tok):
            if is_twitter_profile_url(tok):
                out.usernames.append(tok)
            elif is_tweet_url(tok):
                out.tweet_refs.append(tok)
            else:
                out.urls.append(tok)

    if out.tweet_refs or out.urls or out.usernames:
        # Leftover plain words alongside a URL are a comment, not a command
        return out

    if all(tok.isdigit() for tok in tokens):
        out.tweet_refs.extend(tokens)
        return out

    if all(USERNAME_RE.match(tok) for tok in tokens) and (
        len(tokens) == 1 or all(tok.startswith("@") for tok in tokens)
    ):
        out.usernames.extend(tokens)
        return out

    if forwarded or len(text) >= MIN_TEXT_CAPTURE:
        out.text = text
        return out

    out.unknown.append(text)
    return out


def text_item(text: str, origin: Optional[dict] = None) -> dict:
    """Item for free text or a forwarded post.

    Args:
        text: The message text
        origin: Optional forward metadata: {name, username, url, date}
    """
    origin = origin or {}
    url = origin.get("url")
    item_id = generate_blog_id(url) if url else "note_" + hashlib.sha256(
        text.strip().lower().encode()
    ).hexdigest()[:16]
    author = origin.get("username") or origin.get("name") or "me"
    first_line = text.strip().splitlines()[0][:120]
    return {
        "item_id": item_id,
        "source": "telegram",
        "content_type": "post" if origin else "note",
        "url": url,
        "title": first_line if len(text) > len(first_line) else None,
        "author_username": author,
        "author_name": origin.get("name") or author,
        "text": text.strip(),
        "created_at": origin.get("date") or datetime.now(timezone.utc).isoformat(),
        "metrics": {},
        "raw_data": {"source": "telegram", "forward": origin or None},
        "is_retweet": False,
    }


def minimal_url_item(url: str) -> dict:
    """Item for a URL whose page could not be fetched."""
    domain = urlparse(url).netloc.removeprefix("www.")
    return {
        "item_id": generate_blog_id(url),
        "source": "telegram",
        "content_type": "blog_post",
        "url": url,
        "title": None,
        "author_username": domain or "unknown",
        "author_name": domain or "unknown",
        "text": url,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "metrics": {},
        "raw_data": {"source": "telegram", "unfetched": True},
        "is_retweet": False,
    }


def forward_origin(message) -> Optional[dict]:
    """Extract {name, username, url, date} from a Telegram forwarded message, if any."""
    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None
    info: dict = {}
    chat = getattr(origin, "chat", None)
    sender = getattr(origin, "sender_user", None)
    if chat is not None:
        info["name"] = getattr(chat, "title", None) or getattr(chat, "username", None)
        info["username"] = getattr(chat, "username", None)
        msg_id = getattr(origin, "message_id", None)
        if info.get("username") and msg_id:
            info["url"] = f"https://t.me/{info['username']}/{msg_id}"
    elif sender is not None:
        info["name"] = getattr(sender, "full_name", None) or getattr(sender, "username", None)
        info["username"] = getattr(sender, "username", None)
    else:
        info["name"] = getattr(origin, "sender_user_name", None) or getattr(origin, "author_signature", None)
    date = getattr(origin, "date", None)
    if date is not None:
        info["date"] = date.isoformat() if hasattr(date, "isoformat") else str(date)
    return {k: v for k, v in info.items() if v} or {"name": "forwarded"}
