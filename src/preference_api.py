"""Agent-facing API over the preference store.

Plain functions used by the MCP server (``src/mcp_server.py``) and usable
from any Python caller. Everything here is read/write against the same
items / signals / profiles tables the Telegram bot uses.
"""

import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional

from src.claude_filter import DEFAULT_PROFILE, DOMAINS, ClaudeFilter
from src.content import generate_blog_id, is_http_url
from src.database import DatabaseClient
from src.embeddings import EmbeddingManager
from src.profile_compiler import render_preferences_md, scoring_profile

logger = logging.getLogger(__name__)


def item_id_for(url: Optional[str], text: Optional[str]) -> str:
    """Deterministic id: blog_<hash> for URLs, note_<hash> for bare text."""
    if url:
        return generate_blog_id(url)
    digest = hashlib.sha256((text or "").strip().lower().encode()).hexdigest()[:16]
    return f"note_{digest}"


def public_item(item: dict, extra: Optional[dict] = None) -> dict:
    """Trim an item row to what an agent needs."""
    out = {
        "item_id": item.get("item_id"),
        "title": item.get("title"),
        "text": (item.get("text") or "")[:600],
        "url": item.get("url"),
        "author": item.get("author_username"),
        "domain": item.get("domain"),
        "source": item.get("source"),
        "created_at": item.get("created_at"),
    }
    if extra:
        out.update(extra)
    return out


class PreferenceAPI:
    def __init__(
        self,
        db: DatabaseClient,
        embeddings: Optional[EmbeddingManager] = None,
        claude: Optional[ClaudeFilter] = None,
    ):
        self.db = db
        self.embeddings = embeddings
        self.claude = claude

    # ------------------------------------------------------------ read

    def get_profile(self, domain: Optional[str] = None) -> str:
        """Compiled profile Markdown: one domain, or the whole PREFERENCES.md."""
        profiles = self.db.get_latest_profiles()
        if domain:
            row = profiles.get(domain)
            if row:
                return row["content"]
            if domain == "ethereum-protocol" and not profiles:
                return DEFAULT_PROFILE
            return f"No compiled profile for domain '{domain}'. Known domains: {', '.join(DOMAINS)}."
        if not profiles:
            return "# Preferences\n\nNo compiled profile yet. Baseline:\n\n" + DEFAULT_PROFILE
        return render_preferences_md(profiles)

    def search_liked(self, query: str, domain: Optional[str] = None, limit: int = 10) -> list[dict]:
        """Items the reader voted on, most similar to the query.

        Uses embeddings when available (semantic, with vote + reason attached),
        otherwise a text search over all items joined with their latest vote.
        """
        limit = max(1, min(50, limit))
        if self.embeddings and self.embeddings.enabled:
            vector = self.embeddings.generate_embedding(query)
            matches = self.db.find_similar_voted(vector, limit=limit * 3 if domain else limit)
            if domain:
                matches = [m for m in matches if m.get("domain") == domain]
            ids = [m["item_id"] for m in matches[:limit]]
            items = self.db.get_items(ids)
            return [
                public_item(items.get(m["item_id"], m), {
                    "vote": m.get("vote"),
                    "strength": m.get("strength"),
                    "reason": m.get("reason"),
                    "similarity": round(float(m.get("similarity", 0)), 3),
                })
                for m in matches[:limit]
            ]
        rows = self.db.search_items_text(query, limit=limit * 3, domain=domain)
        voted = {i["item_id"]: i for i in self.db.get_labeled_items()} if rows else {}
        out = []
        for row in rows:
            labeled = voted.get(row["item_id"])
            if not labeled:
                continue
            out.append(public_item(row, {
                "vote": labeled.get("user_vote"),
                "strength": labeled.get("vote_strength"),
                "reason": labeled.get("vote_reason"),
            }))
            if len(out) >= limit:
                break
        return out

    def score_items(self, items: list[dict]) -> list[dict]:
        """Score arbitrary items (title/text/url/author) against the profile.

        Returns [{item_id, score, reason, domain}] in input order. Nothing is stored.
        """
        if not self.claude:
            raise RuntimeError("ANTHROPIC_API_KEY not configured; scoring unavailable")
        prepared = []
        for i, raw in enumerate(items):
            text = raw.get("text") or ""
            title = raw.get("title")
            if title and title not in text:
                text = f"{title}\n\n{text}" if text else title
            prepared.append({
                "item_id": raw.get("item_id") or item_id_for(raw.get("url"), text) + f"_{i}",
                "author_username": raw.get("author") or raw.get("author_username") or "unknown",
                "text": text,
                "metrics": {},
                "content_type": raw.get("content_type") or ("blog_post" if raw.get("url") else "note"),
            })
        neighbors = None
        if self.embeddings and self.embeddings.enabled:
            try:
                neighbors = self.embeddings.find_neighbors(prepared, k=3, exclude_self=True)
            except Exception as e:
                logger.warning(f"Neighbour lookup failed: {e}")
        profile = scoring_profile(self.db.get_latest_profiles(), DEFAULT_PROFILE)
        scores = {s["item_id"]: s for s in self.claude.score_tweets(prepared, neighbors=neighbors, profile=profile)}
        return [
            {
                "item_id": p["item_id"],
                "score": scores.get(p["item_id"], {}).get("score"),
                "reason": scores.get(p["item_id"], {}).get("reason"),
                "domain": scores.get(p["item_id"], {}).get("domain"),
            }
            for p in prepared
        ]

    # ----------------------------------------------------------- write

    def record_signal(
        self,
        vote: Optional[str] = None,
        url: Optional[str] = None,
        item_id: Optional[str] = None,
        text: Optional[str] = None,
        title: Optional[str] = None,
        strength: int = 1,
        reason: Optional[str] = None,
        note: Optional[str] = None,
        source: str = "agent",
        domain: Optional[str] = None,
    ) -> dict:
        """Record a vote and/or note. Creates the item if it does not exist.

        Returns the item id and what was written.
        """
        if vote not in (None, "up", "down"):
            raise ValueError("vote must be 'up', 'down' or omitted")
        if not vote and not note:
            raise ValueError("give a vote, a note, or both")
        if url and not is_http_url(url):
            raise ValueError("url must be http(s)")

        item = None
        if item_id:
            item = self.db.get_item(item_id)
            if not item:
                raise ValueError(f"unknown item_id {item_id}")
        elif url:
            item = self.db.get_item_by_url(url)
        if not item:
            if not url and not text:
                raise ValueError("give item_id, url, or text")
            item_id = item_id_for(url, text)
            body = text or title or url
            record = {
                "item_id": item_id,
                "source": source,
                "content_type": "blog_post" if url else "note",
                "url": url,
                "title": title,
                "author_username": source,
                "author_name": source,
                "text": body,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "metrics": {},
                "raw_data": {"source": source},
                "domain": domain if domain in DOMAINS else None,
            }
            self.db.save_items([record])
            item = record
        item_id = item["item_id"]

        written = []
        if vote:
            self.db.save_signal(
                item_id=item_id, kind="vote", value=vote,
                strength=2 if (vote == "up" and strength >= 2) else 1,
                reason=reason, source=source,
            )
            written.append("vote")
            if self.embeddings and self.embeddings.enabled and not self.db.has_embedding(item_id):
                try:
                    vec = self.embeddings.generate_embedding(item.get("text") or "")
                    if vec:
                        self.db.save_embedding(item_id, vec)
                except Exception as e:
                    logger.warning(f"Embedding failed for {item_id}: {e}")
        if note:
            self.db.save_signal(item_id=item_id, kind="note", reason=note, source=source)
            written.append("note")
        return {"item_id": item_id, "written": written, "url": item.get("url")}
