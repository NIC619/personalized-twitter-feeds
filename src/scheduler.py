"""Scheduler for daily tweet curation."""

import asyncio
import logging
from datetime import datetime
from typing import Optional

import schedule

from src.twitter_client import TwitterClient
from src.claude_filter import ClaudeFilter
from src.telegram_bot import TelegramCurator
from src.database import DatabaseClient
from src.embeddings import EmbeddingManager

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
        ab_test_config: Optional[dict] = None,
        rag_enabled: bool = True,
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
            ab_test_config: Optional dict with keys: enabled, experiment_id, challenger_prompt
            rag_enabled: Whether to use RAG context in Claude prompts
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
        self.ab_test_config = ab_test_config or {}
        self.rag_enabled = rag_enabled
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
            "started_at": datetime.utcnow().isoformat(),
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

            # Step 1d: Build RAG context from similar voted tweets
            rag_context = None
            if self.rag_enabled and self.embedding_manager and self.embedding_manager.enabled:
                logger.info("Step 1d: Building RAG context from similar voted tweets...")
                try:
                    similar = self.embedding_manager.find_similar_voted_tweets(
                        tweets_for_filtering
                    )
                    if similar:
                        rag_context = self._format_rag_context(similar)
                        stats["rag_matches"] = len(similar)
                        logger.info(f"RAG context built with {len(similar)} similar voted tweets")
                    else:
                        logger.info("No similar voted tweets found for RAG context")
                except Exception as e:
                    logger.warning(f"RAG context generation failed, continuing without: {e}")

            # Step 2: Score tweets with Claude (floor threshold gets all 50+ tweets)
            logger.info("Step 2: Scoring tweets with Claude...")
            scored_tweets = self.claude.filter_tweets(
                tweets_for_filtering,
                threshold=self.filter_threshold,
                rag_context=rag_context,
            )

            # Step 2a: A/B test shadow scoring
            if self.ab_test_config.get("enabled"):
                try:
                    experiment_id = self.ab_test_config["experiment_id"]
                    challenger_key = self.ab_test_config["challenger_prompt"]
                    control_key = "V2" if rag_context else "V1"

                    logger.info(
                        f"A/B test '{experiment_id}': control={control_key} vs challenger={challenger_key} "
                        f"(RAG={'on' if rag_context else 'off'})"
                    )
                    challenger_scores = self.claude.score_tweets_with_prompt(
                        tweets_for_filtering, challenger_key, rag_context=rag_context
                    )

                    # Build control scores from the scored_tweets (already scored above)
                    control_scores = []
                    for tweet in tweets_for_filtering:
                        if tweet.get("filter_score") is not None:
                            control_scores.append({
                                "tweet_id": tweet["tweet_id"],
                                "score": tweet["filter_score"],
                                "reason": tweet.get("filter_reason", ""),
                            })

                    self.db.save_ab_test_scores(
                        experiment_id=experiment_id,
                        control_scores=control_scores,
                        control_key=control_key,
                        challenger_scores=challenger_scores,
                        challenger_key=challenger_key,
                    )
                    stats["ab_test_control_scores"] = len(control_scores)
                    stats["ab_test_challenger_scores"] = len(challenger_scores)
                    logger.info(
                        f"A/B test '{experiment_id}': saved {len(control_scores)} control({control_key}) + "
                        f"{len(challenger_scores)} challenger({challenger_key}) scores"
                    )
                except Exception as e:
                    logger.warning(f"A/B test shadow scoring failed (non-fatal): {e}")
                    stats["ab_test_error"] = str(e)

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

            # Step 4: Send filtered tweets to Telegram
            if filtered_tweets:
                logger.info("Step 4: Sending filtered tweets to Telegram...")
                message_ids = await self.telegram.send_daily_digest(filtered_tweets)
                stats["sent"] = len(message_ids)

                # Mark tweets as sent
                for tweet, msg_id in zip(filtered_tweets, message_ids):
                    if msg_id:
                        self.db.mark_tweet_sent(tweet["tweet_id"], msg_id)
            else:
                logger.info("No filtered tweets to send")

            stats["completed_at"] = datetime.utcnow().isoformat()
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

    @staticmethod
    def _format_rag_context(similar_tweets: list[dict]) -> str:
        """Format similar voted tweets into RAG context string for Claude."""
        liked = [t for t in similar_tweets if t["vote"] == "up"]
        disliked = [t for t in similar_tweets if t["vote"] == "down"]

        lines = []
        if liked:
            lines.append("Liked tweets (boost similar content):")
            for t in liked:
                text_preview = t["text"][:120].replace("\n", " ")
                lines.append(
                    f'- @{t["author_username"]}: "{text_preview}" (similarity: {t["similarity"]:.2f})'
                )
            lines.append("")

        if disliked:
            lines.append("Disliked tweets (penalize similar content):")
            for t in disliked:
                text_preview = t["text"][:120].replace("\n", " ")
                lines.append(
                    f'- @{t["author_username"]}: "{text_preview}" (similarity: {t["similarity"]:.2f})'
                )

        return "\n".join(lines)

    def _deduplicate_tweets(self, tweets: list[dict]) -> list[dict]:
        """Remove tweets that are already in the database.

        Skips tweets that have already been:
        - Sent to Telegram, OR
        - Already scored by Claude (filtered or not)

        Args:
            tweets: List of tweet dictionaries

        Returns:
            List of new tweets not yet processed
        """
        new_tweets = []
        for tweet in tweets:
            existing = self.db.get_tweet_by_id(tweet["tweet_id"])
            if existing and existing.get("filter_score") is not None:
                continue
            new_tweets.append(tweet)
        return new_tweets

    def schedule_daily(self, hour: int = 9, minute: int = 0) -> None:
        """Schedule daily curation run.

        Args:
            hour: Hour to run (24h format)
            minute: Minute to run
        """
        time_str = f"{hour:02d}:{minute:02d}"
        schedule.every().day.at(time_str).do(
            lambda: asyncio.create_task(self.run_daily_curation())
        )
        logger.info(f"Scheduled daily curation at {time_str}")

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
) -> None:
    """Handle feedback from Telegram buttons.

    Saves feedback and embeds the tweet for future RAG context.

    Args:
        db: Database client
        tweet_id: Twitter ID of the tweet
        vote: 'up' or 'down'
        telegram_message_id: Telegram message ID
        notes: Optional reason for the vote
        embedding_manager: Optional EmbeddingManager to embed voted tweet
    """
    try:
        db.save_feedback(
            tweet_id=tweet_id,
            vote=vote,
            telegram_message_id=telegram_message_id,
            notes=notes,
        )
        logger.info(f"Saved feedback: tweet={tweet_id}, vote={vote}, notes={notes}")
    except Exception as e:
        logger.error(f"Error saving feedback: {e}")
        return

    # Embed the tweet for RAG (skip if already embedded)
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
