"""Turn things the user sends to the bot into items.

Pure helpers: parse a message into an explicit instruction, build minimal
items for URLs that could not be fetched and for free text / forwarded posts.

The grammar is deliberately small and explicit (no guessing from word
counts). A message is one of:

* ``:like <url|tweet id>... [comment]`` → upvote each target; the comment is
  saved as the reason. ``:dislike`` (alias ``:down``) does the same with a
  downvote.
* ``:star <@handle|profile url>...`` → toggle starred authors.
* ``:note <text>`` → save the text as a note item (upvoted).
* A message containing URLs and no verb → the URLs are liked (tweet or any
  page); profile URLs star their author; the remaining words are the reason.
* A forwarded post → saved as an item with its origin (upvoted).
* A reply to a delivered item → a note on it (handled before this module).

Anything else is not captured; the bot answers with the grammar.
"""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from src.content import generate_blog_id, is_http_url, is_tweet_url, is_twitter_profile_url

USERNAME_RE = re.compile(r"^@?[A-Za-z0-9_]{1,15}$")
VERBS = {
    ":like": "like",
    ":dislike": "dislike",
    ":down": "dislike",
    ":star": "star",
    ":note": "note",
}
USAGE = (
    "I didn't catch that. You can send:\n"
    "• a URL (tweet, article, anything), optionally with words → liked, words saved as the reason\n"
    "• :like URL [why]  ·  :dislike URL [why]\n"
    "• :star @handle (or a profile URL)\n"
    "• :note your thought\n"
    "• a forwarded post → saved\n"
    "• a reply to any item → note on it"
)


@dataclass
class Classified:
    action: Optional[str] = None                          # like | dislike | star | note | None
    tweet_refs: list[str] = field(default_factory=list)   # tweet URLs or numeric ids
    urls: list[str] = field(default_factory=list)         # other http(s) URLs
    usernames: list[str] = field(default_factory=list)    # profile URLs / @handles
    text: Optional[str] = None                            # note text (":note", forwards)
    comment: Optional[str] = None                         # words around the targets (vote reason)
    unknown: list[str] = field(default_factory=list)

    @property
    def vote(self) -> Optional[str]:
        return {"like": "up", "dislike": "down"}.get(self.action or "")


def classify_message(text: str, forwarded: bool = False) -> Classified:
    """Parse a plain message into an instruction (see module docstring)."""
    out = Classified()
    text = (text or "").strip()
    if not text:
        return out
    tokens = text.split()

    verb = VERBS.get(tokens[0].lower())
    if verb:
        out.action = verb
        rest = tokens[1:]
        if verb == "note":
            body = text[len(tokens[0]):].strip()
            if body:
                out.text = body
            else:
                out.unknown.append(text)
            return out
        if verb == "star":
            for tok in rest:
                if is_twitter_profile_url(tok) or USERNAME_RE.match(tok):
                    out.usernames.append(tok)
                else:
                    out.unknown.append(tok)
            if not out.usernames:
                out.unknown.append(text)
            return out
        # like / dislike
        words = _split_targets(rest, out)
        if not (out.tweet_refs or out.urls):
            out.unknown.append(text)
        elif words:
            out.comment = " ".join(words)
        return out

    # No verb: URLs decide
    words = _split_targets(tokens, out, allow_numeric=False)
    if out.tweet_refs or out.urls or out.usernames:
        if out.tweet_refs or out.urls:
            out.action = "like"
        elif out.usernames:
            out.action = "star"
        if words and out.action == "like":
            out.comment = " ".join(words)
        return out

    if forwarded:
        out.action = "note"
        out.text = text
        return out

    out.unknown.append(text)
    return out


def _split_targets(tokens: list[str], out: Classified, allow_numeric: bool = True) -> list[str]:
    """Sort tokens into tweet refs / URLs / profile URLs; return the leftover words."""
    words: list[str] = []
    for tok in tokens:
        if is_http_url(tok):
            if is_twitter_profile_url(tok):
                out.usernames.append(tok)
            elif is_tweet_url(tok):
                out.tweet_refs.append(tok)
            else:
                out.urls.append(tok)
        elif allow_numeric and tok.isdigit() and len(tok) >= 10:
            out.tweet_refs.append(tok)
        else:
            words.append(tok)
    return words


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
