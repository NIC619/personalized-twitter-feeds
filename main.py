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
from src.embeddings import EmbeddingManager
from src.scheduler import DailyCurator, feedback_handler
from src.blog_fetcher import BlogFetcher
from src.error_logger import attach_db_error_handler

# Configure logging with colored console output
class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.WARNING: "\033[33m",  # yellow
        logging.ERROR: "\033[31m",    # red
        logging.CRITICAL: "\033[1;31m",  # bold red
    }
    RESET = "\033[0m"

    def format(self, record):
        msg = super().format(record)
        color = self.COLORS.get(record.levelno)
        return f"{color}{msg}{self.RESET}" if color else msg

_fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
_console = logging.StreamHandler()
_console.setFormatter(ColorFormatter(_fmt))

logging.basicConfig(
    level=logging.INFO,
    format=_fmt,
    handlers=[
        logging.FileHandler("curator.log"),
        _console,
    ],
)
logger = logging.getLogger(__name__)

# Silence noisy polling logs
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


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
    parser.add_argument(
        "--ab-report",
        type=str,
        default=None,
        metavar="EXPERIMENT_ID",
        help="Generate A/B test report for a given experiment ID",
    )
    parser.add_argument(
        "--threshold",
        type=int,
        default=70,
        help="Score threshold for A/B report precision/recall (default 70)",
    )
    parser.add_argument(
        "--error-report",
        type=str,
        default=None,
        metavar="YYYY-MM",
        help="Generate error log report for a month (e.g. 2026-03). "
             "Use 'last' for the previous calendar month.",
    )
    parser.add_argument(
        "-n", "--num-tweets",
        type=int,
        default=None,
        help="Number of tweets to fetch (overrides MAX_TWEETS from .env)",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help="Hours to look back (overrides FETCH_HOURS from .env)",
    )
    return parser.parse_args()


def init_components(settings, num_tweets=None, hours=None):
    """Initialize all components.

    Args:
        settings: Application settings
        num_tweets: Override for max tweets to fetch
        hours: Override for hours to look back

    Returns:
        Tuple of (twitter, claude, telegram, db, curator, blog_fetcher)
    """
    logger.info("Initializing components...")

    # Apply overrides
    max_tweets = num_tweets if num_tweets is not None else settings.max_tweets
    fetch_hours = hours if hours is not None else settings.fetch_hours

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

    # Persist WARNING+ log records to Supabase `error_log` for monthly review.
    # Separate sink — console and file handlers are untouched.
    attach_db_error_handler(db, level=logging.WARNING)

    # Initialize embedding manager
    embedding_manager = EmbeddingManager(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
        db_client=db,
    )

    # Create feedback callback
    async def on_feedback(tweet_id: str, vote: str, telegram_message_id: int, notes: str = None):
        await feedback_handler(
            db, tweet_id, vote, telegram_message_id,
            notes=notes, embedding_manager=embedding_manager,
        )

    # Create favorite author callback (toggle: muted→default, default→favorite)
    async def on_favorite_author(username: str) -> str:
        return db.toggle_favorite(username)

    # Create mute author callback (toggle: favorite→default, default→muted)
    async def on_mute_author(username: str) -> str:
        return db.toggle_mute(username)

    # Create stats callback
    async def on_stats() -> list[dict]:
        return db.get_author_stats()

    # Create list starred callback
    async def on_list_starred() -> list[str]:
        return db.get_favorite_authors()

    # Create like tweet callback
    async def on_like_tweet(tweet_id: str) -> dict | None:
        tweet = db.get_tweet_by_id(tweet_id)
        if tweet:
            return tweet
        tweet = twitter.fetch_tweet(tweet_id)
        if tweet:
            db.save_tweets([tweet])
        return tweet

    # Create thread callback
    async def on_fetch_thread(tweet_id: str) -> list[dict] | None:
        tweets = twitter.fetch_thread(tweet_id)
        if tweets:
            db.save_tweets(tweets)
        return tweets

    # Build A/B test config (needed by blog callbacks below and curator)
    ab_test_config = {
        "enabled": settings.ab_test_enabled,
        "experiment_id": settings.ab_test_experiment_id,
        "challenger_prompt": settings.ab_test_challenger_prompt,
    }

    # Initialize blog fetcher
    blog_fetcher = BlogFetcher()

    # RAG setup
    rag_enabled = settings.rag_enabled and embedding_manager.enabled

    def _build_rag_context(content: list[dict]) -> str | None:
        """Build RAG context from similar voted content, if RAG is enabled."""
        if not rag_enabled:
            return None
        try:
            similar = embedding_manager.find_similar_voted_tweets(content)
            if similar:
                from src.scheduler import DailyCurator
                return DailyCurator._format_rag_context(similar)
        except Exception as e:
            logger.warning(f"RAG context generation failed: {e}")
        return None

    # Create blog post like callback
    async def on_like_blog(url: str) -> dict | None:
        post = blog_fetcher.fetch_blog_post(url)
        if not post:
            return None
        # Build RAG context and score with Claude
        rag_context = _build_rag_context([post])
        control_key = "V2" if rag_context else "V1"
        scored = claude.filter_tweets([post], threshold=0, rag_context=rag_context)
        if scored:
            post = scored[0]
        # Save to DB first (A/B test scores have FK to tweets table)
        db.save_tweets([post])
        # A/B test scoring
        if ab_test_config["enabled"]:
            try:
                control_scores = [{
                    "tweet_id": post["tweet_id"],
                    "score": post.get("filter_score", 0),
                    "reason": post.get("filter_reason", ""),
                }]
                challenger_scores = claude.score_tweets_with_prompt(
                    [post], ab_test_config["challenger_prompt"],
                    rag_context=rag_context,
                )
                db.save_ab_test_scores(
                    ab_test_config["experiment_id"],
                    control_scores, control_key,
                    challenger_scores, ab_test_config["challenger_prompt"],
                )
            except Exception as e:
                logger.warning(f"A/B test scoring failed for blog post: {e}")
        return post

    # Create newsletter callback
    async def on_newsletter(url: str, ignored_sections: list[str] | None = None) -> list[dict]:
        posts = blog_fetcher.parse_newsletter(url, ignored_sections=ignored_sections)
        if not posts:
            return []
        # Build RAG context and score all posts with Claude
        rag_context = _build_rag_context(posts)
        control_key = "V2" if rag_context else "V1"
        scored = claude.filter_tweets(posts, threshold=0, rag_context=rag_context)
        if scored:
            posts = scored
        # Save to DB first (A/B test scores have FK to tweets table)
        db.save_tweets(posts)
        # A/B test scoring
        if ab_test_config["enabled"]:
            try:
                control_scores = [
                    {
                        "tweet_id": p["tweet_id"],
                        "score": p.get("filter_score", 0),
                        "reason": p.get("filter_reason", ""),
                    }
                    for p in posts
                ]
                challenger_scores = claude.score_tweets_with_prompt(
                    posts, ab_test_config["challenger_prompt"],
                    rag_context=rag_context,
                )
                db.save_ab_test_scores(
                    ab_test_config["experiment_id"],
                    control_scores, control_key,
                    challenger_scores, ab_test_config["challenger_prompt"],
                )
            except Exception as e:
                logger.warning(f"A/B test scoring failed for newsletter: {e}")
        return posts

    # Create newsletter preferences callbacks
    async def on_get_newsletter_prefs(domain: str) -> dict | None:
        return db.get_newsletter_preferences(domain)

    async def on_save_newsletter_prefs(
        domain: str, ignored_sections: list[str], all_sections: list[str],
    ) -> dict:
        return db.save_newsletter_preferences(domain, ignored_sections, all_sections)

    async def on_extract_sections(url: str) -> list[str]:
        return blog_fetcher.extract_sections(url)

    # Blocked keyword callbacks
    async def on_add_blocked_keyword(keyword: str) -> dict:
        return db.save_blocked_keyword(keyword)

    async def on_list_blocked_keywords() -> list[str]:
        return db.get_blocked_keywords()

    async def on_remove_blocked_keyword(keyword: str) -> None:
        db.remove_blocked_keyword(keyword)

    # A/B report callback (runs blocking report generation in a worker thread)
    async def on_ab_report(experiment_id: str, threshold: int) -> str:
        from scripts.ab_test_report import build_ab_report
        return await asyncio.to_thread(build_ab_report, db, experiment_id, threshold)

    # Initialize Telegram bot
    telegram = TelegramCurator(
        bot_token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
        feedback_callback=on_feedback,
        favorite_author_callback=on_favorite_author,
        mute_author_callback=on_mute_author,
        stats_callback=on_stats,
        list_starred_callback=on_list_starred,
        like_tweet_callback=on_like_tweet,
        thread_callback=on_fetch_thread,
        like_blog_callback=on_like_blog,
        newsletter_callback=on_newsletter,
        get_newsletter_prefs_callback=on_get_newsletter_prefs,
        save_newsletter_prefs_callback=on_save_newsletter_prefs,
        extract_sections_callback=on_extract_sections,
        add_blocked_keyword_callback=on_add_blocked_keyword,
        list_blocked_keywords_callback=on_list_blocked_keywords,
        remove_blocked_keyword_callback=on_remove_blocked_keyword,
        ab_report_callback=on_ab_report,
    )

    # Initialize curator
    curator = DailyCurator(
        twitter=twitter,
        claude=claude,
        telegram=telegram,
        db=db,
        fetch_hours=fetch_hours,
        max_tweets=max_tweets,
        filter_threshold=settings.filter_threshold,
        favorite_threshold_offset=settings.favorite_threshold_offset,
        muted_threshold_offset=settings.muted_threshold_offset,
        starred_author_max_tweets=settings.starred_author_max_tweets,
        embedding_manager=embedding_manager,
        ab_test_config=ab_test_config,
        rag_enabled=settings.rag_enabled,
    )

    logger.info(f"All components initialized (max_tweets={max_tweets}, fetch_hours={fetch_hours})")
    return twitter, claude, telegram, db, curator, blog_fetcher


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
        timezone=settings.schedule_timezone,
    )

    # Run initial curation, then start scheduler + polling concurrently.
    # If initial curation fails, log and continue so the bot stays reachable.
    logger.info("Running initial curation...")
    try:
        await curator.run_daily_curation()
    except Exception as e:
        logger.error(f"Initial curation failed: {e}", exc_info=True)

    await asyncio.gather(
        curator.run_scheduled(),
        telegram.run_polling(),
    )


async def run_bot_only(telegram: TelegramCurator) -> None:
    """Run only the Telegram bot for receiving feedback.

    Args:
        telegram: TelegramCurator instance
    """
    logger.info("Running Telegram bot only...")

    await telegram.initialize()
    await telegram.run_polling()


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
        if args.ab_report:
            from scripts.ab_test_report import run_ab_report
            db = DatabaseClient(url=settings.supabase_url, key=settings.supabase_key)
            run_ab_report(db, args.ab_report, threshold=args.threshold)
            return 0
        elif args.error_report:
            from scripts.error_report import run_error_report
            db = DatabaseClient(url=settings.supabase_url, key=settings.supabase_key)
            run_error_report(db, args.error_report)
            return 0
        elif args.test:
            asyncio.run(run_test(settings))
        elif args.once:
            _, _, telegram, _, curator, blog_fetcher = init_components(
                settings, num_tweets=args.num_tweets, hours=args.hours
            )
            try:
                asyncio.run(run_once(curator, telegram))
            finally:
                blog_fetcher.close()
        elif args.schedule:
            _, _, telegram, _, curator, blog_fetcher = init_components(settings)
            try:
                asyncio.run(run_scheduled(curator, telegram, settings))
            finally:
                blog_fetcher.close()
        elif args.bot_only:
            _, _, telegram, db, _, blog_fetcher = init_components(settings)
            try:
                asyncio.run(run_bot_only(telegram))
            finally:
                blog_fetcher.close()
        else:
            # Default: run once
            _, _, telegram, _, curator, blog_fetcher = init_components(
                settings, num_tweets=args.num_tweets, hours=args.hours
            )
            try:
                asyncio.run(run_once(curator, telegram))
            finally:
                blog_fetcher.close()

        return 0

    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 0
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
