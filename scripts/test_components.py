#!/usr/bin/env python3
"""Test individual components of Twitter Curator."""

import asyncio
import logging
import sys
import os

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def test_settings():
    """Test settings loading."""
    logger.info("Testing settings...")
    try:
        settings = get_settings()
        logger.info("Settings loaded successfully")
        logger.info(f"  - Twitter API Key: {settings.twitter_api_key[:8]}...")
        logger.info(f"  - Anthropic API Key: {settings.anthropic_api_key[:8]}...")
        logger.info(f"  - Telegram Bot Token: {settings.telegram_bot_token[:8]}...")
        logger.info(f"  - Supabase URL: {settings.supabase_url}")
        return settings
    except Exception as e:
        logger.error(f"Failed to load settings: {e}")
        raise


def test_database(settings):
    """Test database connection."""
    logger.info("\nTesting database connection...")
    from src.database import DatabaseClient

    try:
        db = DatabaseClient(url=settings.supabase_url, key=settings.supabase_key)

        # Try to query the tweets table
        result = db.client.table("tweets").select("count", count="exact").execute()
        count = result.count if hasattr(result, "count") else 0
        logger.info(f"Database connection OK - tweets table has {count} rows")
        return db
    except Exception as e:
        logger.error(f"Database connection failed: {e}")
        logger.error("Make sure you've run the setup_database.py SQL in Supabase")
        raise


def test_twitter(settings):
    """Test Twitter API connection."""
    logger.info("\nTesting Twitter API...")
    from src.twitter_client import TwitterClient

    try:
        twitter = TwitterClient(
            api_key=settings.twitter_api_key,
            api_secret=settings.twitter_api_secret,
            access_token=settings.twitter_access_token,
            access_secret=settings.twitter_access_secret,
            bearer_token=settings.twitter_bearer_token,
        )

        # Fetch a few tweets
        tweets = twitter.fetch_timeline(max_results=5, hours=24)
        logger.info(f"Twitter API OK - fetched {len(tweets)} tweets")

        if tweets:
            logger.info(f"  Sample tweet: @{tweets[0]['author_username']}")
            logger.info(f"    Text: {tweets[0]['text'][:100]}...")

        return tweets
    except Exception as e:
        logger.error(f"Twitter API failed: {e}")
        raise


def test_claude(settings, tweets):
    """Test Claude API filtering."""
    logger.info("\nTesting Claude API...")
    from src.claude_filter import ClaudeFilter

    if not tweets:
        logger.warning("No tweets to test Claude with, skipping")
        return []

    try:
        claude = ClaudeFilter(api_key=settings.anthropic_api_key)

        # Filter the tweets
        filtered = claude.filter_tweets(tweets[:3])
        logger.info(f"Claude API OK - filtered {len(filtered)} tweets")

        for tweet in filtered:
            logger.info(f"  Score {tweet['filter_score']}: {tweet['filter_reason']}")

        return filtered
    except Exception as e:
        logger.error(f"Claude API failed: {e}")
        raise


async def test_telegram(settings, filtered_tweets):
    """Test Telegram bot."""
    logger.info("\nTesting Telegram bot...")
    from src.telegram_bot import TelegramCurator

    try:
        telegram = TelegramCurator(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
        )
        await telegram.initialize()

        # Send a test message
        await telegram.application.bot.send_message(
            chat_id=settings.telegram_chat_id,
            text="Twitter Curator: Component test successful!",
        )
        logger.info("Telegram bot OK - test message sent")

        # Optionally send a sample tweet
        if filtered_tweets:
            logger.info("Sending sample filtered tweet...")
            msg_id = await telegram.send_tweet(filtered_tweets[0])
            if msg_id:
                logger.info(f"Sample tweet sent with message ID: {msg_id}")

        await telegram.shutdown()
    except Exception as e:
        logger.error(f"Telegram bot failed: {e}")
        raise


async def run_tests():
    """Run all component tests."""
    logger.info("=" * 60)
    logger.info("Twitter Curator Component Tests")
    logger.info("=" * 60)

    try:
        # Test settings
        settings = test_settings()

        # Test database
        test_database(settings)

        # Test Twitter
        tweets = test_twitter(settings)

        # Test Claude
        filtered = test_claude(settings, tweets)

        # Test Telegram
        await test_telegram(settings, filtered)

        logger.info("\n" + "=" * 60)
        logger.info("All tests passed!")
        logger.info("=" * 60)
        return 0

    except Exception as e:
        logger.error(f"\nTest failed: {e}")
        return 1


def main():
    """Main entry point."""
    return asyncio.run(run_tests())


if __name__ == "__main__":
    sys.exit(main())
