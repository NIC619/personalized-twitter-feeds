"""Supabase database client: items, signals, embeddings, profiles."""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from supabase import create_client, Client

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DatabaseClient:
    """Supabase database client for the preference store."""

    def __init__(self, url: str, key: str):
        """Initialize Supabase client.

        Args:
            url: Supabase project URL
            key: Supabase anon key
        """
        self.client: Client = create_client(url, key)
        logger.info("Database client initialized")

    # ================================================================ items

    @staticmethod
    def _item_record(item: dict) -> dict:
        """Map an in-memory item dict onto the items table."""
        content_type = item.get("content_type", "tweet")
        source = item.get("source")
        if not source:
            if content_type == "tweet":
                source = "twitter"
            else:
                source = (item.get("raw_data") or {}).get("source") or "blog"
        title = item.get("title")
        if not title and item.get("article"):
            title = item["article"].get("title")
        return {
            "item_id": item["item_id"],
            "source": source,
            "content_type": content_type,
            "url": item.get("url"),
            "title": title,
            "author_username": item.get("author_username"),
            "author_name": item.get("author_name"),
            "text": item.get("text") or "",
            "created_at": item.get("created_at"),
            "metrics": item.get("metrics"),
            "raw_data": item.get("raw_data"),
            "is_retweet": bool(item.get("is_retweet", False)),
            "domain": item.get("domain"),
            "filtered": bool(item.get("filtered", False)),
            "filter_score": item.get("filter_score"),
            "filter_reason": item.get("filter_reason"),
            "exploration": bool(item.get("exploration", False)),
        }

    def save_items(self, items: list[dict]) -> list[dict]:
        """Bulk upsert items (tweets, blog posts, notes, imports).

        Returns:
            List of saved records
        """
        if not items:
            logger.warning("No items to save")
            return []

        records = [self._item_record(i) for i in items]
        try:
            result = (
                self.client.table("items")
                .upsert(records, on_conflict="item_id")
                .execute()
            )
            logger.info(f"Saved {len(result.data)} items to database")
            return result.data
        except Exception as e:
            logger.error(f"Error saving items: {e}")
            raise

    def get_item(self, item_id: str) -> Optional[dict]:
        """Get an item by id."""
        try:
            result = (
                self.client.table("items")
                .select("*")
                .eq("item_id", item_id)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting item: {e}")
            raise

    def get_item_by_url(self, url: str) -> Optional[dict]:
        """Get an item by exact URL."""
        try:
            result = (
                self.client.table("items")
                .select("*")
                .eq("url", url)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting item by url: {e}")
            return None

    def get_items(self, item_ids: list[str], chunk_size: int = 200) -> dict[str, dict]:
        """Fetch many items by id → {item_id: item}."""
        out: dict[str, dict] = {}
        ids = list(dict.fromkeys(item_ids))
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            try:
                result = (
                    self.client.table("items").select("*").in_("item_id", chunk).execute()
                )
            except Exception as e:
                logger.error(f"Error fetching items: {e}")
                raise
            for row in result.data or []:
                out[row["item_id"]] = row
        return out

    def get_scored_ids(self, item_ids: list[str], chunk_size: int = 200) -> set[str]:
        """Return the subset of ``item_ids`` that already have a filter score.

        One ``IN`` query per chunk instead of one round trip per item.
        """
        scored: set[str] = set()
        ids = list(dict.fromkeys(item_ids))
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            try:
                result = (
                    self.client.table("items")
                    .select("item_id")
                    .in_("item_id", chunk)
                    .not_.is_("filter_score", "null")
                    .execute()
                )
            except Exception as e:
                logger.error(f"Error checking scored ids: {e}")
                raise
            scored.update(row["item_id"] for row in result.data or [])
        return scored

    def mark_item_sent(self, item_id: str, telegram_message_id: int) -> None:
        """Mark an item as sent to Telegram."""
        try:
            self.client.table("items").update(
                {"sent_to_telegram": _now(), "telegram_message_id": telegram_message_id}
            ).eq("item_id", item_id).execute()
            logger.info(f"Marked item {item_id} as sent")
        except Exception as e:
            logger.error(f"Error marking item as sent: {e}")
            raise

    def get_item_by_message_id(self, telegram_message_id: int) -> Optional[dict]:
        """Find the item that was sent as a given Telegram message."""
        try:
            result = (
                self.client.table("items")
                .select("*")
                .eq("telegram_message_id", telegram_message_id)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error looking up item by message id: {e}")
            return None

    def search_items_text(self, query: str, limit: int = 20, domain: Optional[str] = None) -> list[dict]:
        """Case-insensitive substring search over item text/title (fallback search)."""
        try:
            q = (
                self.client.table("items")
                .select("*")
                .or_(f"text.ilike.%{query}%,title.ilike.%{query}%")
                .order("created_at", desc=True)
                .limit(limit)
            )
            if domain:
                q = q.eq("domain", domain)
            return q.execute().data or []
        except Exception as e:
            logger.error(f"Error searching items: {e}")
            return []

    # ============================================================== signals

    def save_signal(
        self,
        item_id: str,
        kind: str,
        value: Optional[str] = None,
        strength: int = 1,
        reason: Optional[str] = None,
        source: Optional[str] = None,
        telegram_message_id: Optional[int] = None,
        created_at: Optional[str] = None,
    ) -> dict:
        """Append a preference signal (vote / note / open) for an item."""
        record = {
            "item_id": item_id,
            "kind": kind,
            "value": value,
            "strength": strength,
            "reason": reason,
            "source": source,
            "telegram_message_id": telegram_message_id,
        }
        if created_at:
            record["created_at"] = created_at
        try:
            result = self.client.table("signals").insert(record).execute()
            logger.info(f"Saved signal {kind}={value} for item {item_id}")
            return result.data[0] if result.data else {}
        except Exception as e:
            logger.error(f"Error saving signal: {e}")
            raise

    def save_signals(self, records: list[dict]) -> int:
        """Bulk insert signals (used by imports). Records use save_signal's keys."""
        if not records:
            return 0
        try:
            result = self.client.table("signals").insert(records).execute()
            return len(result.data or [])
        except Exception as e:
            logger.error(f"Error saving signals: {e}")
            raise

    def _latest_vote_id(self, item_id: str) -> Optional[int]:
        sig = (
            self.client.table("signals")
            .select("id")
            .eq("item_id", item_id)
            .eq("kind", "vote")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        return sig.data[0]["id"] if sig.data else None

    def update_latest_vote_reason(self, item_id: str, reason: str) -> bool:
        """Attach a reason to the most recent vote on an item.

        Returns:
            True if a vote existed to update.
        """
        try:
            vote_id = self._latest_vote_id(item_id)
            if vote_id is None:
                return False
            self.client.table("signals").update({"reason": reason}).eq("id", vote_id).execute()
            return True
        except Exception as e:
            logger.error(f"Error updating vote reason: {e}")
            raise

    def delete_latest_vote(self, item_id: str) -> None:
        """Remove the most recent vote on an item (reaction retracted)."""
        try:
            vote_id = self._latest_vote_id(item_id)
            if vote_id is not None:
                self.client.table("signals").delete().eq("id", vote_id).execute()
                logger.info(f"Deleted latest vote for item {item_id}")
        except Exception as e:
            logger.error(f"Error deleting vote: {e}")
            raise

    def get_recent_reasons(self, item_ids: list[str], limit: int = 20) -> list[dict]:
        """Confirmed reasons on the given items (for proposing new reasons)."""
        if not item_ids:
            return []
        try:
            result = (
                self.client.table("signals")
                .select("item_id,value,reason")
                .in_("item_id", item_ids)
                .eq("kind", "vote")
                .not_.is_("reason", "null")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error loading reasons: {e}")
            return []

    def _vote_rows(self, page_size: int = 1000, select: str = "*") -> list[dict]:
        """All vote signals, oldest first (paginated past PostgREST's cap)."""
        rows: list[dict] = []
        offset = 0
        while True:
            result = (
                self.client.table("signals")
                .select(select)
                .eq("kind", "vote")
                .in_("value", ["up", "down"])
                .order("created_at")
                .range(offset, offset + page_size - 1)
                .execute()
            )
            page = result.data or []
            rows.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return rows

    def get_labeled_items(self, page_size: int = 1000) -> list[dict]:
        """All voted items joined with their content, for offline evaluation.

        Returns:
            Item dicts each carrying ``user_vote``, ``vote_strength``,
            ``vote_reason`` and ``voted_at``. The latest vote per item wins.
        """
        try:
            rows = self._vote_rows(
                page_size,
                select="item_id,value,strength,reason,created_at,items(*)",
            )
        except Exception as e:
            logger.error(f"Error loading labeled items: {e}")
            raise

        by_id: dict[str, dict] = {}
        for row in rows:  # oldest first, so later rows overwrite
            item = row.get("items")
            if not item:
                continue
            labeled = dict(item)
            labeled["user_vote"] = row["value"]
            labeled["vote_strength"] = row.get("strength", 1)
            labeled["vote_reason"] = row.get("reason")
            labeled["voted_at"] = row.get("created_at")
            by_id[labeled["item_id"]] = labeled
        return list(by_id.values())

    def get_signals_for_profile(self, limit: int = 4000) -> list[dict]:
        """Votes and notes with item text/domain, newest first, for profile compilation."""
        try:
            result = (
                self.client.table("signals")
                .select("item_id,kind,value,strength,reason,created_at,items(text,title,author_username,domain,url,content_type)")
                .in_("kind", ["vote", "note"])
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error loading signals for profile: {e}")
            raise

    def get_author_stats(self) -> list[dict]:
        """Aggregate author performance from votes.

        Returns:
            List of author stat dicts sorted by weighted score desc, each with:
                - author_username, up, down, weighted_up, weighted_down,
                  weighted_score, avg_filter_score, total_votes, is_favorite, is_muted
        """
        try:
            rows = self._vote_rows(
                select="id,item_id,value,strength,items(author_username,is_retweet,filter_score)"
            )
        except Exception as e:
            logger.error(f"Error fetching votes for stats: {e}")
            raise

        favorites = set(self.get_favorite_authors())
        muted = set(self.get_muted_authors())

        authors: dict[str, dict] = {}
        for row in rows:
            item = row.get("items")
            if not item or not item.get("author_username"):
                logger.warning(f"Orphaned vote (no item/author): signal id={row.get('id')}")
                continue

            username = item["author_username"].lower()
            entry = authors.setdefault(username, {
                "author_username": username,
                "up": 0,
                "down": 0,
                "weighted_up": 0.0,
                "weighted_down": 0.0,
                "filter_score_sum": 0.0,
                "filter_score_count": 0,
            })
            weight = 0.5 if item.get("is_retweet") else 1.0
            if row["value"] == "up":
                entry["up"] += 1
                entry["weighted_up"] += weight
            elif row["value"] == "down":
                entry["down"] += 1
                entry["weighted_down"] += weight
            if item.get("filter_score") is not None:
                entry["filter_score_sum"] += item["filter_score"]
                entry["filter_score_count"] += 1

        stats = []
        for entry in authors.values():
            total_weighted = entry["weighted_up"] + entry["weighted_down"]
            weighted_score = entry["weighted_up"] / total_weighted if total_weighted > 0 else 0.0
            avg_filter = (
                entry["filter_score_sum"] / entry["filter_score_count"]
                if entry["filter_score_count"] > 0 else 0.0
            )
            stats.append({
                "author_username": entry["author_username"],
                "up": entry["up"],
                "down": entry["down"],
                "weighted_up": entry["weighted_up"],
                "weighted_down": entry["weighted_down"],
                "weighted_score": weighted_score,
                "avg_filter_score": avg_filter,
                "total_votes": entry["up"] + entry["down"],
                "is_favorite": entry["author_username"] in favorites,
                "is_muted": entry["author_username"] in muted,
            })

        stats.sort(key=lambda s: (-s["weighted_score"], -s["total_votes"]))
        return stats

    # ============================================================ profiles

    def save_profile(self, domain: str, content: str, stats: Optional[dict] = None) -> dict:
        """Store a new version of a compiled preference profile."""
        try:
            latest = (
                self.client.table("preference_profiles")
                .select("version")
                .eq("domain", domain)
                .order("version", desc=True)
                .limit(1)
                .execute()
            )
            version = (latest.data[0]["version"] + 1) if latest.data else 1
            result = self.client.table("preference_profiles").insert({
                "domain": domain,
                "version": version,
                "content": content,
                "stats": stats,
            }).execute()
            logger.info(f"Saved profile {domain} v{version}")
            return result.data[0] if result.data else {}
        except Exception as e:
            logger.error(f"Error saving profile: {e}")
            raise

    def get_latest_profiles(self) -> dict[str, dict]:
        """Latest version of every domain profile → {domain: row}."""
        try:
            result = (
                self.client.table("preference_profiles")
                .select("*")
                .order("version", desc=True)
                .execute()
            )
        except Exception as e:
            logger.error(f"Error loading profiles: {e}")
            return {}
        latest: dict[str, dict] = {}
        for row in result.data or []:
            latest.setdefault(row["domain"], row)  # first seen = highest version
        return latest

    # ============================================================= authors

    def save_favorite_author(self, username: str) -> dict:
        """Save an author as a favorite."""
        username = username.lower().lstrip("@")
        try:
            result = (
                self.client.table("favorite_authors")
                .upsert({"username": username}, on_conflict="username")
                .execute()
            )
            logger.info(f"Saved favorite author: @{username}")
            return result.data[0] if result.data else {}
        except Exception as e:
            logger.error(f"Error saving favorite author: {e}")
            raise

    def get_favorite_authors(self) -> list[str]:
        """Get list of favorite author usernames."""
        try:
            result = self.client.table("favorite_authors").select("username").execute()
            return [r["username"] for r in result.data]
        except Exception as e:
            logger.error(f"Error getting favorite authors: {e}")
            raise

    def is_favorite_author(self, username: str) -> bool:
        """Check if an author is a favorite."""
        username = username.lower().lstrip("@")
        try:
            result = (
                self.client.table("favorite_authors")
                .select("username")
                .eq("username", username)
                .execute()
            )
            return len(result.data) > 0
        except Exception as e:
            logger.error(f"Error checking favorite author: {e}")
            return False

    def remove_favorite_author(self, username: str) -> None:
        """Remove an author from favorites."""
        username = username.lower().lstrip("@")
        try:
            self.client.table("favorite_authors").delete().eq("username", username).execute()
            logger.info(f"Removed favorite author: @{username}")
        except Exception as e:
            logger.error(f"Error removing favorite author: {e}")
            raise

    def save_muted_author(self, username: str) -> dict:
        """Save an author as muted."""
        username = username.lower().lstrip("@")
        try:
            result = (
                self.client.table("muted_authors")
                .upsert({"username": username}, on_conflict="username")
                .execute()
            )
            logger.info(f"Saved muted author: @{username}")
            return result.data[0] if result.data else {}
        except Exception as e:
            logger.error(f"Error saving muted author: {e}")
            raise

    def get_muted_authors(self) -> list[str]:
        """Get list of muted author usernames."""
        try:
            result = self.client.table("muted_authors").select("username").execute()
            return [r["username"] for r in result.data]
        except Exception as e:
            logger.error(f"Error getting muted authors: {e}")
            raise

    def is_muted_author(self, username: str) -> bool:
        """Check if an author is muted."""
        username = username.lower().lstrip("@")
        try:
            result = (
                self.client.table("muted_authors")
                .select("username")
                .eq("username", username)
                .execute()
            )
            return len(result.data) > 0
        except Exception as e:
            logger.error(f"Error checking muted author: {e}")
            return False

    def remove_muted_author(self, username: str) -> None:
        """Remove an author from muted list."""
        username = username.lower().lstrip("@")
        try:
            self.client.table("muted_authors").delete().eq("username", username).execute()
            logger.info(f"Removed muted author: @{username}")
        except Exception as e:
            logger.error(f"Error removing muted author: {e}")
            raise

    def toggle_favorite(self, username: str) -> str:
        """Toggle favorite status: muted → default, default/favorite → favorite."""
        username = username.lower().lstrip("@")
        if self.is_muted_author(username):
            self.remove_muted_author(username)
            logger.info(f"Unmuted @{username} → default")
            return "unmuted"
        self.save_favorite_author(username)
        logger.info(f"Favorited @{username}")
        return "favorited"

    def toggle_mute(self, username: str) -> str:
        """Toggle mute status: favorite → default, default/muted → muted."""
        username = username.lower().lstrip("@")
        if self.is_favorite_author(username):
            self.remove_favorite_author(username)
            logger.info(f"Unfavorited @{username} → default")
            return "unfavorited"
        self.save_muted_author(username)
        logger.info(f"Muted @{username}")
        return "muted"

    # ========================================================== embeddings

    def save_embedding(self, item_id: str, embedding: list[float]) -> None:
        """Upsert an item embedding."""
        try:
            self.client.table("item_embeddings").upsert(
                {"item_id": item_id, "embedding": embedding},
                on_conflict="item_id",
            ).execute()
            logger.info(f"Saved embedding for item {item_id}")
        except Exception as e:
            logger.error(f"Error saving embedding: {e}")
            raise

    def save_embeddings(self, embeddings: dict[str, list[float]]) -> int:
        """Bulk upsert embeddings → count saved."""
        if not embeddings:
            return 0
        records = [{"item_id": k, "embedding": v} for k, v in embeddings.items()]
        try:
            result = (
                self.client.table("item_embeddings")
                .upsert(records, on_conflict="item_id")
                .execute()
            )
            return len(result.data or [])
        except Exception as e:
            logger.error(f"Error saving embeddings: {e}")
            raise

    def find_similar_voted(self, embedding: list[float], limit: int = 5) -> list[dict]:
        """Nearest voted items by cosine similarity.

        Returns:
            List of dicts with item_id, text, author_username, domain, vote,
            strength, reason, similarity
        """
        try:
            result = self.client.rpc(
                "match_voted_items",
                {"query_embedding": embedding, "match_count": limit},
            ).execute()
            return result.data if result.data else []
        except Exception as e:
            logger.error(f"Error finding similar items: {e}")
            return []

    def get_embeddings(self, item_ids: list[str], chunk_size: int = 100) -> dict[str, list[float]]:
        """Read stored embedding vectors for the given ids (missing ids omitted)."""
        out: dict[str, list[float]] = {}
        ids = list(dict.fromkeys(item_ids))
        for i in range(0, len(ids), chunk_size):
            chunk = ids[i : i + chunk_size]
            try:
                result = (
                    self.client.table("item_embeddings")
                    .select("item_id,embedding")
                    .in_("item_id", chunk)
                    .execute()
                )
            except Exception as e:
                logger.error(f"Error reading embeddings: {e}")
                raise
            for row in result.data or []:
                vec = row.get("embedding")
                if isinstance(vec, str):  # PostgREST serialises vector as text
                    vec = json.loads(vec)
                if vec:
                    out[row["item_id"]] = vec
        return out

    def has_embedding(self, item_id: str) -> bool:
        """Check if an embedding exists for an item."""
        try:
            result = (
                self.client.table("item_embeddings")
                .select("item_id")
                .eq("item_id", item_id)
                .execute()
            )
            return len(result.data) > 0
        except Exception as e:
            logger.error(f"Error checking embedding: {e}")
            return False

    def get_voted_items_without_embeddings(self) -> list[dict]:
        """Voted items that still need an embedding (for backfill)."""
        try:
            rows = self._vote_rows(select="item_id,items(item_id,text,title)")
        except Exception as e:
            logger.error(f"Error loading voted items: {e}")
            raise
        items = {r["item_id"]: r["items"] for r in rows if r.get("items")}
        if not items:
            return []
        have = set(self.get_embeddings(list(items)).keys())
        return [it for iid, it in items.items() if iid not in have]

    # ======================================================== newsletters

    def get_newsletter_preferences(self, domain: str) -> dict | None:
        """Get newsletter section preferences for a domain."""
        try:
            result = (
                self.client.table("newsletter_preferences")
                .select("*")
                .eq("domain", domain)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting newsletter preferences for {domain}: {e}")
            return None

    def save_newsletter_preferences(
        self, domain: str, ignored_sections: list[str], all_sections: list[str]
    ) -> dict:
        """Save newsletter section preferences for a domain."""
        record = {
            "domain": domain,
            "ignored_sections": ignored_sections,
            "all_sections": all_sections,
            "updated_at": _now(),
        }
        try:
            result = (
                self.client.table("newsletter_preferences")
                .upsert(record, on_conflict="domain")
                .execute()
            )
            logger.info(f"Saved newsletter preferences for {domain}: ignoring {len(ignored_sections)} sections")
            return result.data[0] if result.data else {}
        except Exception as e:
            logger.error(f"Error saving newsletter preferences for {domain}: {e}")
            raise

    # ============================================================ keywords

    def save_blocked_keyword(self, keyword: str) -> dict:
        """Save a keyword to the block list (whole-word, case-insensitive)."""
        keyword = keyword.strip().lower()
        if not keyword:
            raise ValueError("Keyword cannot be empty")
        try:
            result = (
                self.client.table("blocked_keywords")
                .upsert({"keyword": keyword}, on_conflict="keyword")
                .execute()
            )
            logger.info(f"Saved blocked keyword: '{keyword}'")
            return result.data[0] if result.data else {}
        except Exception as e:
            logger.error(f"Error saving blocked keyword: {e}")
            raise

    def get_blocked_keywords(self) -> list[str]:
        """Get all blocked keywords (lowercased)."""
        try:
            result = (
                self.client.table("blocked_keywords")
                .select("keyword")
                .order("keyword")
                .execute()
            )
            return [r["keyword"] for r in result.data]
        except Exception as e:
            logger.error(f"Error getting blocked keywords: {e}")
            raise

    def remove_blocked_keyword(self, keyword: str) -> None:
        """Remove a keyword from the block list."""
        keyword = keyword.strip().lower()
        try:
            self.client.table("blocked_keywords").delete().eq("keyword", keyword).execute()
            logger.info(f"Removed blocked keyword: '{keyword}'")
        except Exception as e:
            logger.error(f"Error removing blocked keyword: {e}")
            raise

    # =========================================================== error log

    def save_error_log(
        self,
        source: str,
        level: str,
        error_type: Optional[str],
        message: str,
    ) -> None:
        """Insert a single error log record.

        No internal logging calls: this is invoked from a logging handler,
        and calling `logger.*` here could loop back through the handler.
        """
        self.client.table("error_log").insert({
            "source": source,
            "level": level,
            "error_type": error_type,
            "message": message,
        }).execute()

    def get_error_logs_in_range(self, start: datetime, end: datetime) -> list[dict]:
        """Fetch error log rows between [start, end)."""
        try:
            result = (
                self.client.table("error_log")
                .select("*")
                .gte("logged_at", start.isoformat())
                .lt("logged_at", end.isoformat())
                .order("logged_at", desc=False)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error(f"Error fetching error logs: {e}")
            raise
