"""Scheduler for daily tweet curation."""

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Optional

import schedule

from src.twitter_client import TwitterClient
from src.claude_filter import ClaudeFilter
from src.telegram_bot import TelegramCurator
from src.database import DatabaseClient
from src.embeddings import EmbeddingManager
from src.keyword_filter import filter_blocked_keywords

logger = logging.getLogger(__name__)


class DailyCurator:
    """Orchestrates the daily tweet curation workflow."""

    def __init__(
        self,
        twitter: TwitterClient,
        claude: ClaudeFilter,
        telegram: TelegramCurator,
        db: DatabaseClient,
        fetch_hours: int = 24,
        max_tweets: int = 100,
        filter_threshold: int = 70,
        favorite_threshold_offset: int = 20,
        muted_threshold_offset: int = 15,
        starred_author_max_tweets: int = 10,
        embedding_manager: Optional[EmbeddingManager] = None,
        rag_enabled: bool = True,
        neighbors_per_item: int = 3,
        exploration_per_run: int = 2,
    ):
        """Initialize daily curator with all components.

        Args:
            twitter: Twitter API client
            claude: Claude filter
            telegram: Telegram bot
            db: Database client
            fetch_hours: Hours to look back for tweets
            max_tweets: Maximum tweets to fetch
            filter_threshold: Default threshold for authors
            favorite_threshold_offset: How much lower for starred authors
            muted_threshold_offset: How much higher for muted authors
            starred_author_max_tweets: Max tweets per starred author timeline
            embedding_manager: Optional EmbeddingManager for RAG
            rag_enabled: Whether to attach voted neighbours to items for scoring
            neighbors_per_item: Nearest voted neighbours attached to each item
            exploration_per_run: Random below-threshold items sent per run
                (marked 🎲) so recall below the threshold can be measured
        """
        self.twitter = twitter
        self.claude = claude
        self.telegram = telegram
        self.db = db
        self.embedding_manager = embedding_manager
        self.fetch_hours = fetch_hours
        self.max_tweets = max_tweets
        self.starred_author_max_tweets = starred_author_max_tweets
        self.default_threshold = filter_threshold
        self.favorite_author_threshold = filter_threshold - favorite_threshold_offset
        self.muted_author_threshold = filter_threshold + muted_threshold_offset
        # Claude floor = lowest tier so all potentially relevant tweets get scored
        self.filter_threshold = self.favorite_author_threshold
        self.rag_enabled = rag_enabled
        self.neighbors_per_item = neighbors_per_item
        self.exploration_per_run = exploration_per_run
        logger.info(
            f"DailyCurator initialized (thresholds: favorite={self.favorite_author_threshold}, "
            f"default={self.default_threshold}, muted={self.muted_author_threshold})"
        )

    async def run_daily_curation(self) -> dict:
        """Run the full curation workflow.

        Workflow:
        1. Fetch tweets from Twitter timeline
        2. Filter tweets with Claude
        3. Save tweets to database
        4. Send filtered tweets to Telegram
        5. Return summary stats

        Returns:
            Dict with workflow statistics
        """
        stats = {
            "fetched": 0,
            "filtered": 0,
            "sent": 0,
            "errors": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
        }

        try:
            # Step 1: Fetch tweets from Twitter
            logger.info("Step 1: Fetching tweets from Twitter...")
            tweets = self.twitter.fetch_timeline(
                max_results=self.max_tweets,
                hours=self.fetch_hours,
            )
            stats["fetched"] = len(tweets)
            logger.info(f"Fetched {len(tweets)} tweets from home timeline")

            # Step 1a: Fetch tweets from starred authors' timelines
            favorite_authors = list(self.db.get_favorite_authors())
            if favorite_authors:
                logger.info(
                    f"Step 1a: Fetching tweets from {len(favorite_authors)} starred authors..."
                )
                starred_tweets = self.twitter.fetch_user_tweets(
                    usernames=favorite_authors,
                    max_per_user=self.starred_author_max_tweets,
                    hours=self.fetch_hours,
                )
                stats["starred_fetched"] = len(starred_tweets)

                # Merge and deduplicate by tweet_id
                seen_ids = {t["tweet_id"] for t in tweets}
                new_from_starred = 0
                for st in starred_tweets:
                    if st["tweet_id"] not in seen_ids:
                        tweets.append(st)
                        seen_ids.add(st["tweet_id"])
                        new_from_starred += 1

                stats["starred_new"] = new_from_starred
                logger.info(
                    f"Starred authors: {len(starred_tweets)} total, "
                    f"{new_from_starred} new (not in home timeline)"
                )

            if not tweets:
                logger.info("No tweets to process")
                return stats

            stats["fetched"] = len(tweets)

            # Step 1b: Deduplicate - remove tweets already processed
            new_tweets = self._deduplicate_tweets(tweets)
            stats["new"] = len(new_tweets)
            stats["skipped_duplicates"] = len(tweets) - len(new_tweets)

            if stats["skipped_duplicates"] > 0:
                logger.info(
                    f"Skipped {stats['skipped_duplicates']} already-processed tweets"
                )

            if not new_tweets:
                logger.info("No new tweets to process")
                return stats

            # Step 1c: Load author tiers
            if not favorite_authors:
                favorite_authors = list(self.db.get_favorite_authors())
            favorite_authors_set = set(favorite_authors)
            muted_authors = set(self.db.get_muted_authors())

            # Skip retweets from non-favorite authors
            tweets_for_filtering = []
            skipped_retweets = 0
            for tweet in new_tweets:
                if tweet.get("is_retweet") and tweet["author_username"].lower() not in favorite_authors_set:
                    skipped_retweets += 1
                else:
                    tweets_for_filtering.append(tweet)

            stats["skipped_retweets"] = skipped_retweets
            if skipped_retweets > 0:
                logger.info(
                    f"Skipped {skipped_retweets} retweets from non-favorite authors"
                )

            if not tweets_for_filtering:
                logger.info("No tweets remaining after retweet filter")
                return stats

            # Step 1c2: Apply keyword blocklist (pre-LLM, favorite authors exempt)
            blocked_keywords = self.db.get_blocked_keywords()
            if blocked_keywords:
                kept, blocked = filter_blocked_keywords(
                    tweets_for_filtering,
                    blocked_keywords,
                    exempt_authors=favorite_authors_set,
                )
                stats["skipped_blocked_keywords"] = len(blocked)
                if blocked:
                    logger.info(
                        f"Skipped {len(blocked)} items by keyword blocklist "
                        f"({len(blocked_keywords)} keywords active)"
                    )
                tweets_for_filtering = kept

            if not tweets_for_filtering:
                logger.info("No tweets remaining after keyword blocklist")
                return stats

            # Step 1d: Per-item nearest voted neighbours (RAG)
            neighbors: dict[str, list[dict]] = {}
            if self.rag_enabled and self.embedding_manager and self.embedding_manager.enabled:
                logger.info("Step 1d: Finding voted neighbours for each item...")
                try:
                    neighbors = self.embedding_manager.find_neighbors(
                        tweets_for_filtering, k=self.neighbors_per_item
                    )
                    stats["rag_items_with_neighbors"] = len(neighbors)
                    logger.info(
                        f"Neighbours found for {len(neighbors)}/{len(tweets_for_filtering)} items"
                    )
                except Exception as e:
                    logger.warning(f"Neighbour lookup failed, continuing without: {e}")

            # Step 2: Score tweets with Claude (floor threshold gets all 50+ tweets)
            logger.info("Step 2: Scoring tweets with Claude...")
            scored_tweets = self.claude.filter_tweets(
                tweets_for_filtering,
                threshold=self.filter_threshold,
                neighbors=neighbors or None,
            )

            # Step 2b: Apply per-author thresholds
            filtered_tweets = []
            tier_counts = {"favorite": 0, "muted": 0, "default": 0}
            for tweet in scored_tweets:
                author = tweet["author_username"].lower()
                score = tweet.get("filter_score", 0)

                if author in muted_authors:
                    tier = "muted"
                    threshold = self.muted_author_threshold
                elif author in favorite_authors_set:
                    tier = "favorite"
                    threshold = self.favorite_author_threshold
                else:
                    tier = "default"
                    threshold = self.default_threshold

                passed = score >= threshold
                tweet["filtered"] = passed
                tier_counts[tier] += 1

                if passed:
                    filtered_tweets.append(tweet)
                    logger.info(
                        f"[{tier}] @{author} score={score} threshold={threshold} → PASS"
                    )
                else:
                    logger.info(
                        f"[{tier}] @{author} score={score} threshold={threshold} → SKIP"
                    )

            # Step 2c: Exploration — a few random below-threshold items,
            # marked so the user knows, to measure recall below the threshold.
            explored = self._pick_exploration(tweets_for_filtering, filtered_tweets)
            for tweet in explored:
                tweet["exploration"] = True
                tweet["filtered"] = True
                filtered_tweets.append(tweet)
            stats["exploration"] = len(explored)

            stats["filtered"] = len(filtered_tweets)
            stats["tier_breakdown"] = tier_counts
            logger.info(
                f"Filtered to {len(filtered_tweets)} tweets "
                f"(favorite={tier_counts['favorite']}, default={tier_counts['default']}, muted={tier_counts['muted']})"
            )

            # Step 3: Save all tweets to database (including non-filtered)
            logger.info("Step 3: Saving tweets to database...")
            # Update original tweets with filter results
            tweet_map = {t["tweet_id"]: t for t in new_tweets}
            for ft in filtered_tweets:
                tweet_map[ft["tweet_id"]] = ft

            self.db.save_tweets(list(tweet_map.values()))
            logger.info(f"Saved {len(tweet_map)} tweets to database")

            # Step 4: Group by thread and send filtered tweets to Telegram
            if filtered_tweets:
                logger.info("Step 4: Sending filtered tweets to Telegram...")
                tweet_groups = self._group_by_thread(filtered_tweets)
                thread_count = sum(1 for g in tweet_groups if len(g) > 1)
                if thread_count:
                    logger.info(
                        f"Grouped into {len(tweet_groups)} groups "
                        f"({thread_count} threads)"
                    )
                sent_pairs = await self.telegram.send_daily_digest(tweet_groups)
                stats["sent"] = len(sent_pairs)

                # Mark tweets as sent
                for tweet, msg_id in sent_pairs:
                    if msg_id:
                        self.db.mark_tweet_sent(tweet["tweet_id"], msg_id)
            else:
                logger.info("No filtered tweets to send")

            stats["completed_at"] = datetime.now(timezone.utc).isoformat()
            logger.info(f"Curation complete: {stats}")
            return stats

        except Exception as e:
            error_msg = f"Curation error: {str(e)}"
            logger.error(error_msg, exc_info=True)
            stats["errors"].append(error_msg)

            # Try to notify user of error
            try:
                await self.telegram.send_error_notification(error_msg)
            except Exception as notify_error:
                logger.error(f"Failed to send error notification: {notify_error}")

            return stats

    def _pick_exploration(
        self, scored: list[dict], passed: list[dict]
    ) -> list[dict]:
        """Pick random below-threshold items to send as exploration."""
        if self.exploration_per_run <= 0:
            return []
        passed_ids = {t["tweet_id"] for t in passed}
        candidates = [
            t for t in scored
            if t["tweet_id"] not in passed_ids
            and t.get("filter_score") is not None
            and t.get("filter_reason") != "Not scored by Claude"
        ]
        if not candidates:
            return []
        return random.sample(candidates, min(self.exploration_per_run, len(candidates)))

    @staticmethod
    def _group_by_thread(tweets: list[dict]) -> list[list[dict]]:
        """Group tweets by conversation_id for thread-aware delivery.

        Tweets sharing a conversation_id are grouped together and sorted
        chronologically.  A tweet whose conversation_id equals its own
        tweet_id and has no siblings in the batch stays as a single-item group.

        Args:
            tweets: Filtered tweet list (each must have raw_data with
                    conversation_id, and created_at).

        Returns:
            List of groups (each group is a list of 1+ tweets).
        """
        from collections import OrderedDict

        groups: OrderedDict[str, list[dict]] = OrderedDict()
        for tweet in tweets:
            conv_id = tweet.get("raw_data", {}).get("conversation_id", tweet["tweet_id"])
            groups.setdefault(conv_id, []).append(tweet)

        result: list[list[dict]] = []
        for conv_id, group in groups.items():
            # Sort chronologically within the group
            group.sort(key=lambda t: t.get("created_at", ""))
            result.append(group)

        return result

    def _deduplicate_tweets(self, tweets: list[dict]) -> list[dict]:
        """Drop tweets that already have a filter score in the database.

        One batched ``IN`` query instead of a round trip per tweet.
        """
        if not tweets:
            return []
        scored_ids = self.db.get_scored_ids([t["tweet_id"] for t in tweets])
        return [t for t in tweets if t["tweet_id"] not in scored_ids]

    def schedule_daily(
        self,
        hour: int = 9,
        minute: int = 0,
        timezone: Optional[str] = None,
        skip_weekdays: Optional[list[str]] = None,
    ) -> None:
        """Schedule daily curation run.

        Args:
            hour: Hour to run (24h format)
            minute: Minute to run
            timezone: IANA tz name (e.g. "Asia/Taipei"). If None, uses the
                host's local time — which on Railway is UTC unless the TZ
                env var is set.
            skip_weekdays: Lowercased weekday names to skip
                (e.g. ["sunday"]). Decision is made in the timezone arg.
        """
        time_str = f"{hour:02d}:{minute:02d}"
        skip_set = {d.strip().lower() for d in (skip_weekdays or []) if d.strip()}
        weekday_names = [
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        ]

        def _launch() -> None:
            if skip_set:
                if timezone:
                    from zoneinfo import ZoneInfo
                    today = datetime.now(ZoneInfo(timezone)).weekday()
                else:
                    today = datetime.now().weekday()
                if weekday_names[today] in skip_set:
                    logger.info(
                        f"Skipping scheduled run today ({weekday_names[today]} "
                        f"is in SCHEDULE_SKIP_WEEKDAYS)"
                    )
                    return
            task = asyncio.create_task(self.run_daily_curation())

            def _log_exception(t: asyncio.Task) -> None:
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:
                    logger.error(
                        "Scheduled daily curation failed", exc_info=exc,
                    )

            task.add_done_callback(_log_exception)

        job = schedule.every().day.at(time_str, timezone) if timezone \
            else schedule.every().day.at(time_str)
        job.do(_launch)
        tz_label = f" ({timezone})" if timezone else " (host local time)"
        skip_label = f", skipping {sorted(skip_set)}" if skip_set else ""
        logger.info(f"Scheduled daily curation at {time_str}{tz_label}{skip_label}")

    async def run_scheduled(self) -> None:
        """Run the scheduler loop."""
        logger.info("Starting scheduler loop...")

        while True:
            schedule.run_pending()
            await asyncio.sleep(60)  # Check every minute


async def feedback_handler(
    db: DatabaseClient,
    tweet_id: str,
    vote: str,
    telegram_message_id: int,
    notes: str = None,
    embedding_manager: Optional[EmbeddingManager] = None,
    strength: int = 1,
    source: str = "button",
) -> None:
    """Handle a vote from Telegram (button, reaction or /like).

    Writes the legacy ``feedback`` row, appends a ``signals`` row, and
    embeds the item for future neighbour lookups.

    Args:
        db: Database client
        tweet_id: Item ID
        vote: 'up' or 'down'
        telegram_message_id: Telegram message ID
        notes: Optional reason for the vote
        embedding_manager: Optional EmbeddingManager to embed voted item
        strength: 1 normal, 2 strong (🔥)
        source: 'button' | 'reaction' | 'like_cmd'
    """
    try:
        db.save_feedback(
            tweet_id=tweet_id,
            vote=vote,
            telegram_message_id=telegram_message_id,
            notes=notes,
        )
        logger.info(
            f"Saved feedback: tweet={tweet_id}, vote={vote}, strength={strength}, "
            f"source={source}, notes={notes}"
        )
    except Exception as e:
        logger.error(f"Error saving feedback: {e}")
        return

    try:
        db.save_signal(
            item_id=tweet_id,
            kind="vote",
            value=vote,
            strength=strength,
            reason=notes,
            source=source,
            telegram_message_id=telegram_message_id,
        )
    except Exception as e:
        logger.warning(f"Signal save failed (feedback row kept): {e}")

    # Embed the item for RAG (skip if already embedded)
    if embedding_manager and embedding_manager.enabled:
        try:
            if not db.has_embedding(tweet_id):
                tweet = db.get_tweet_by_id(tweet_id)
                if tweet and tweet.get("text"):
                    embedding = embedding_manager.generate_embedding(tweet["text"])
                    if embedding:
                        db.save_embedding(tweet_id, embedding)
                        logger.info(f"Embedded voted tweet {tweet_id} for RAG")
        except Exception as e:
            logger.warning(f"Failed to embed voted tweet {tweet_id}: {e}")
