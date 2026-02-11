"""Database client for Supabase operations."""

import logging
from datetime import datetime
from typing import Optional

from supabase import create_client, Client

logger = logging.getLogger(__name__)


class DatabaseClient:
    """Supabase database client for tweets, feedback, and embeddings."""

    def __init__(self, url: str, key: str):
        """Initialize Supabase client.

        Args:
            url: Supabase project URL
            key: Supabase anon key
        """
        self.client: Client = create_client(url, key)
        logger.info("Database client initialized")

    def save_tweets(self, tweets: list[dict]) -> list[dict]:
        """Bulk upsert tweets to database.

        Args:
            tweets: List of tweet dictionaries with:
                - tweet_id: Twitter ID
                - author_username: Author's username
                - author_name: Author's display name
                - text: Tweet text
                - created_at: Tweet creation time
                - metrics: Dict of likes, retweets, replies
                - url: Tweet URL
                - raw_data: Full tweet object
                - filtered: Whether tweet passed filter
                - filter_score: Claude's score
                - filter_reason: Reason for score

        Returns:
            List of saved tweet records
        """
        if not tweets:
            logger.warning("No tweets to save")
            return []

        records = []
        for tweet in tweets:
            record = {
                "tweet_id": tweet["tweet_id"],
                "author_username": tweet["author_username"],
                "author_name": tweet["author_name"],
                "text": tweet["text"],
                "created_at": tweet["created_at"],
                "metrics": tweet.get("metrics"),
                "url": tweet["url"],
                "raw_data": tweet.get("raw_data"),
                "is_retweet": tweet.get("is_retweet", False),
                "filtered": tweet.get("filtered", False),
                "filter_score": tweet.get("filter_score"),
                "filter_reason": tweet.get("filter_reason"),
            }
            records.append(record)

        try:
            result = (
                self.client.table("tweets")
                .upsert(records, on_conflict="tweet_id")
                .execute()
            )
            logger.info(f"Saved {len(result.data)} tweets to database")
            return result.data
        except Exception as e:
            logger.error(f"Error saving tweets: {e}")
            raise

    def save_feedback(
        self,
        tweet_id: str,
        vote: str,
        telegram_message_id: Optional[int] = None,
        notes: Optional[str] = None,
    ) -> dict:
        """Store user feedback for a tweet.

        Args:
            tweet_id: Twitter ID of the tweet
            vote: 'up' or 'down'
            telegram_message_id: Telegram message ID for reference
            notes: Optional user notes

        Returns:
            Saved feedback record
        """
        if vote not in ("up", "down"):
            raise ValueError(f"Invalid vote: {vote}. Must be 'up' or 'down'")

        record = {
            "tweet_id": tweet_id,
            "user_vote": vote,
            "telegram_message_id": telegram_message_id,
            "notes": notes,
        }

        try:
            result = self.client.table("feedback").insert(record).execute()
            logger.info(f"Saved feedback for tweet {tweet_id}: {vote}")
            return result.data[0] if result.data else {}
        except Exception as e:
            logger.error(f"Error saving feedback: {e}")
            raise

    def get_feedback_examples(
        self, vote_type: Optional[str] = None, limit: int = 10
    ) -> list[dict]:
        """Retrieve tweets user voted on for RAG.

        Args:
            vote_type: Filter by 'up' or 'down', or None for all
            limit: Maximum number of examples to retrieve

        Returns:
            List of tweet records with feedback
        """
        try:
            query = (
                self.client.table("feedback")
                .select("*, tweets(*)")
                .order("voted_at", desc=True)
                .limit(limit)
            )

            if vote_type:
                query = query.eq("user_vote", vote_type)

            result = query.execute()
            logger.info(f"Retrieved {len(result.data)} feedback examples")
            return result.data
        except Exception as e:
            logger.error(f"Error getting feedback examples: {e}")
            raise

    def get_unprocessed_tweets(self, limit: int = 100) -> list[dict]:
        """Get tweets not yet sent to Telegram.

        Args:
            limit: Maximum number of tweets to retrieve

        Returns:
            List of unprocessed tweet records
        """
        try:
            result = (
                self.client.table("tweets")
                .select("*")
                .eq("filtered", True)
                .is_("sent_to_telegram", "null")
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            logger.info(f"Retrieved {len(result.data)} unprocessed tweets")
            return result.data
        except Exception as e:
            logger.error(f"Error getting unprocessed tweets: {e}")
            raise

    def mark_tweet_sent(self, tweet_id: str, telegram_message_id: int) -> None:
        """Mark a tweet as sent to Telegram.

        Args:
            tweet_id: Twitter ID of the tweet
            telegram_message_id: Telegram message ID
        """
        try:
            self.client.table("tweets").update(
                {"sent_to_telegram": datetime.utcnow().isoformat(), "telegram_message_id": telegram_message_id}
            ).eq("tweet_id", tweet_id).execute()
            logger.info(f"Marked tweet {tweet_id} as sent")
        except Exception as e:
            logger.error(f"Error marking tweet as sent: {e}")
            raise

    def update_tweet_filter_results(
        self, tweet_id: str, score: float, reason: str
    ) -> None:
        """Update tweet with filter results.

        Args:
            tweet_id: Twitter ID of the tweet
            score: Claude's score for the tweet
            reason: Reason for the score
        """
        filtered = score >= 70  # Threshold from settings

        try:
            self.client.table("tweets").update(
                {
                    "filtered": filtered,
                    "filter_score": score,
                    "filter_reason": reason,
                }
            ).eq("tweet_id", tweet_id).execute()
            logger.info(f"Updated filter results for tweet {tweet_id}: score={score}")
        except Exception as e:
            logger.error(f"Error updating tweet filter results: {e}")
            raise

    def get_tweet_by_id(self, tweet_id: str) -> Optional[dict]:
        """Get a tweet by its Twitter ID.

        Args:
            tweet_id: Twitter ID of the tweet

        Returns:
            Tweet record or None if not found
        """
        try:
            result = (
                self.client.table("tweets")
                .select("*")
                .eq("tweet_id", tweet_id)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error(f"Error getting tweet: {e}")
            raise

    def save_favorite_author(self, username: str) -> dict:
        """Save an author as a favorite.

        Args:
            username: Twitter username (without @)

        Returns:
            Saved favorite author record
        """
        username = username.lower().lstrip("@")

        record = {
            "username": username,
        }

        try:
            result = (
                self.client.table("favorite_authors")
                .upsert(record, on_conflict="username")
                .execute()
            )
            logger.info(f"Saved favorite author: @{username}")
            return result.data[0] if result.data else {}
        except Exception as e:
            logger.error(f"Error saving favorite author: {e}")
            raise

    def get_favorite_authors(self) -> list[str]:
        """Get list of favorite author usernames.

        Returns:
            List of usernames
        """
        try:
            result = (
                self.client.table("favorite_authors")
                .select("username")
                .execute()
            )
            return [r["username"] for r in result.data]
        except Exception as e:
            logger.error(f"Error getting favorite authors: {e}")
            raise

    def is_favorite_author(self, username: str) -> bool:
        """Check if an author is a favorite.

        Args:
            username: Twitter username

        Returns:
            True if author is a favorite
        """
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
        """Remove an author from favorites.

        Args:
            username: Twitter username (without @)
        """
        username = username.lower().lstrip("@")
        try:
            self.client.table("favorite_authors").delete().eq(
                "username", username
            ).execute()
            logger.info(f"Removed favorite author: @{username}")
        except Exception as e:
            logger.error(f"Error removing favorite author: {e}")
            raise

    def save_muted_author(self, username: str) -> dict:
        """Save an author as muted.

        Args:
            username: Twitter username (without @)

        Returns:
            Saved muted author record
        """
        username = username.lower().lstrip("@")
        record = {"username": username}
        try:
            result = (
                self.client.table("muted_authors")
                .upsert(record, on_conflict="username")
                .execute()
            )
            logger.info(f"Saved muted author: @{username}")
            return result.data[0] if result.data else {}
        except Exception as e:
            logger.error(f"Error saving muted author: {e}")
            raise

    def get_muted_authors(self) -> list[str]:
        """Get list of muted author usernames.

        Returns:
            List of usernames
        """
        try:
            result = (
                self.client.table("muted_authors")
                .select("username")
                .execute()
            )
            return [r["username"] for r in result.data]
        except Exception as e:
            logger.error(f"Error getting muted authors: {e}")
            raise

    def is_muted_author(self, username: str) -> bool:
        """Check if an author is muted.

        Args:
            username: Twitter username

        Returns:
            True if author is muted
        """
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
        """Remove an author from muted list.

        Args:
            username: Twitter username (without @)
        """
        username = username.lower().lstrip("@")
        try:
            self.client.table("muted_authors").delete().eq(
                "username", username
            ).execute()
            logger.info(f"Removed muted author: @{username}")
        except Exception as e:
            logger.error(f"Error removing muted author: {e}")
            raise

    def toggle_favorite(self, username: str) -> str:
        """Toggle favorite status for an author.

        If muted: remove mute, reset to default.
        If default: promote to favorite.
        If already favorite: no-op (stays favorite).

        Args:
            username: Twitter username

        Returns:
            New state: "favorited" or "unfavorited" (removed mute)
        """
        username = username.lower().lstrip("@")
        if self.is_muted_author(username):
            self.remove_muted_author(username)
            logger.info(f"Unmuted @{username} → default")
            return "unmuted"
        else:
            self.save_favorite_author(username)
            logger.info(f"Favorited @{username}")
            return "favorited"

    def toggle_mute(self, username: str) -> str:
        """Toggle mute status for an author.

        If favorited: remove star, reset to default.
        If default: demote to muted.
        If already muted: no-op (stays muted).

        Args:
            username: Twitter username

        Returns:
            New state: "muted" or "unmuted" (removed star)
        """
        username = username.lower().lstrip("@")
        if self.is_favorite_author(username):
            self.remove_favorite_author(username)
            logger.info(f"Unfavorited @{username} → default")
            return "unfavorited"
        else:
            self.save_muted_author(username)
            logger.info(f"Muted @{username}")
            return "muted"
