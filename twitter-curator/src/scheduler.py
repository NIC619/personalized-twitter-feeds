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
    ):
        """Initialize daily curator with all components.

        Args:
            twitter: Twitter API client
            claude: Claude filter
            telegram: Telegram bot
            db: Database client
            fetch_hours: Hours to look back for tweets
            max_tweets: Maximum tweets to fetch
            filter_threshold: Minimum score to send
        """
        self.twitter = twitter
        self.claude = claude
        self.telegram = telegram
        self.db = db
        self.fetch_hours = fetch_hours
        self.max_tweets = max_tweets
        self.filter_threshold = filter_threshold
        logger.info("DailyCurator initialized")

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
            logger.info(f"Fetched {len(tweets)} tweets")

            if not tweets:
                logger.info("No tweets to process")
                return stats

            # Step 2: Filter tweets with Claude
            logger.info("Step 2: Filtering tweets with Claude...")
            filtered_tweets = self.claude.filter_tweets(
                tweets,
                threshold=self.filter_threshold,
            )
            stats["filtered"] = len(filtered_tweets)
            logger.info(f"Filtered to {len(filtered_tweets)} relevant tweets")

            # Step 3: Save all tweets to database (including non-filtered)
            logger.info("Step 3: Saving tweets to database...")
            # Update original tweets with filter results
            tweet_map = {t["tweet_id"]: t for t in tweets}
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
) -> None:
    """Handle feedback from Telegram buttons.

    Args:
        db: Database client
        tweet_id: Twitter ID of the tweet
        vote: 'up' or 'down'
        telegram_message_id: Telegram message ID
    """
    try:
        db.save_feedback(
            tweet_id=tweet_id,
            vote=vote,
            telegram_message_id=telegram_message_id,
        )
        logger.info(f"Saved feedback: tweet={tweet_id}, vote={vote}")
    except Exception as e:
        logger.error(f"Error saving feedback: {e}")
