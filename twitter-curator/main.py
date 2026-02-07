#!/usr/bin/env python3
"""Twitter Curator - Main entry point."""

import argparse
import asyncio
import logging
import sys
from functools import partial

from config.settings import get_settings
from src.twitter_client import TwitterClient
from src.claude_filter import ClaudeFilter
from src.telegram_bot import TelegramCurator
from src.database import DatabaseClient
from src.scheduler import DailyCurator, feedback_handler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("curator.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Twitter Curator - AI-powered tweet curation"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run curation once and exit",
    )
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="Run in scheduled mode (daily curation)",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Test components without running full curation",
    )
    parser.add_argument(
        "--bot-only",
        action="store_true",
        help="Run only the Telegram bot (for receiving feedback)",
    )
    return parser.parse_args()


def init_components(settings):
    """Initialize all components.

    Args:
        settings: Application settings

    Returns:
        Tuple of (twitter, claude, telegram, db, curator)
    """
    logger.info("Initializing components...")

    # Initialize Twitter client
    twitter = TwitterClient(
        api_key=settings.twitter_api_key,
        api_secret=settings.twitter_api_secret,
        access_token=settings.twitter_access_token,
        access_secret=settings.twitter_access_secret,
        bearer_token=settings.twitter_bearer_token,
    )

    # Initialize Claude filter
    claude = ClaudeFilter(api_key=settings.anthropic_api_key)

    # Initialize database
    db = DatabaseClient(
        url=settings.supabase_url,
        key=settings.supabase_key,
    )

    # Create feedback callback
    async def on_feedback(tweet_id: str, vote: str, telegram_message_id: int):
        await feedback_handler(db, tweet_id, vote, telegram_message_id)

    # Initialize Telegram bot
    telegram = TelegramCurator(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        feedback_callback=on_feedback,
    )

    # Initialize curator
    curator = DailyCurator(
        twitter=twitter,
        claude=claude,
        telegram=telegram,
        db=db,
        fetch_hours=settings.fetch_hours,
        max_tweets=settings.max_tweets,
        filter_threshold=settings.filter_threshold,
    )

    logger.info("All components initialized")
    return twitter, claude, telegram, db, curator


async def run_once(curator: DailyCurator, telegram: TelegramCurator) -> None:
    """Run curation once.

    Args:
        curator: DailyCurator instance
        telegram: TelegramCurator instance
    """
    logger.info("Running one-time curation...")

    # Initialize Telegram
    await telegram.initialize()

    try:
        stats = await curator.run_daily_curation()
        logger.info(f"Curation complete: {stats}")
    finally:
        await telegram.shutdown()


async def run_scheduled(
    curator: DailyCurator,
    telegram: TelegramCurator,
    settings,
) -> None:
    """Run in scheduled mode with Telegram bot.

    Args:
        curator: DailyCurator instance
        telegram: TelegramCurator instance
        settings: Application settings
    """
    logger.info("Starting scheduled mode...")

    # Initialize Telegram
    await telegram.initialize()

    # Schedule daily curation
    curator.schedule_daily(
        hour=settings.schedule_hour,
        minute=0,
    )

    # Run both the scheduler and the Telegram bot
    try:
        # Run initial curation
        logger.info("Running initial curation...")
        await curator.run_daily_curation()

        # Start polling for Telegram updates
        await telegram.run_polling()
    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down...")
    finally:
        await telegram.shutdown()


async def run_bot_only(telegram: TelegramCurator) -> None:
    """Run only the Telegram bot for receiving feedback.

    Args:
        telegram: TelegramCurator instance
    """
    logger.info("Running Telegram bot only...")

    await telegram.initialize()

    try:
        await telegram.run_polling()
    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down...")
    finally:
        await telegram.shutdown()


async def run_test(settings) -> None:
    """Test individual components.

    Args:
        settings: Application settings
    """
    logger.info("Testing components...")

    # Test database connection
    logger.info("Testing database connection...")
    db = DatabaseClient(url=settings.supabase_url, key=settings.supabase_key)
    logger.info("Database connection OK")

    # Test Twitter client
    logger.info("Testing Twitter client...")
    twitter = TwitterClient(
        api_key=settings.twitter_api_key,
        api_secret=settings.twitter_api_secret,
        access_token=settings.twitter_access_token,
        access_secret=settings.twitter_access_secret,
        bearer_token=settings.twitter_bearer_token,
    )
    tweets = twitter.fetch_timeline(max_results=5, hours=24)
    logger.info(f"Twitter client OK - fetched {len(tweets)} tweets")

    # Test Claude filter
    if tweets:
        logger.info("Testing Claude filter...")
        claude = ClaudeFilter(api_key=settings.anthropic_api_key)
        filtered = claude.filter_tweets(tweets[:3])
        logger.info(f"Claude filter OK - filtered {len(filtered)} tweets")

    # Test Telegram bot
    logger.info("Testing Telegram bot...")
    telegram = TelegramCurator(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )
    await telegram.initialize()
    await telegram.application.bot.send_message(
        chat_id=settings.telegram_chat_id,
        text="Twitter Curator test message",
    )
    await telegram.shutdown()
    logger.info("Telegram bot OK")

    logger.info("All component tests passed!")


def main() -> int:
    """Main entry point."""
    args = parse_args()

    try:
        settings = get_settings()
    except Exception as e:
        logger.error(f"Failed to load settings: {e}")
        logger.error("Make sure .env file exists with all required variables")
        return 1

    try:
        if args.test:
            asyncio.run(run_test(settings))
        elif args.once:
            _, _, telegram, _, curator = init_components(settings)
            asyncio.run(run_once(curator, telegram))
        elif args.schedule:
            _, _, telegram, _, curator = init_components(settings)
            asyncio.run(run_scheduled(curator, telegram, settings))
        elif args.bot_only:
            _, _, telegram, db, _ = init_components(settings)
            asyncio.run(run_bot_only(telegram))
        else:
            # Default: run once
            _, _, telegram, _, curator = init_components(settings)
            asyncio.run(run_once(curator, telegram))

        return 0

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 0
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
