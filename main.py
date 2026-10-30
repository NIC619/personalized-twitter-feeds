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
from src.feeds import FeedFetcher
from src.error_logger import attach_db_error_handler
from src.capture import minimal_url_item, text_item
from src.profile_compiler import render_preferences_md, scoring_profile

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
        "--run-once-on-startup",
        action="store_true",
        help="With --schedule, run one curation immediately before entering the "
             "scheduler loop. Default: wait for the next scheduled tick.",
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
        "--compile-profile",
        action="store_true",
        help="Recompile preference profiles from recorded signals and exit",
    )
    parser.add_argument(
        "--export-preferences",
        type=str,
        default=None,
        metavar="PATH",
        help="Write the latest compiled profiles as Markdown to PATH (e.g. PREFERENCES.md) and exit",
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
    claude = ClaudeFilter(api_key=settings.anthropic_api_key, model=settings.claude_model)

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
    async def on_feedback(
        item_id: str, vote: str, telegram_message_id: int, notes: str = None,
        strength: int = 1, source: str = "button",
    ):
        await feedback_handler(
            db, item_id, vote, telegram_message_id,
            notes=notes, embedding_manager=embedding_manager,
            strength=strength, source=source,
        )

    # Model-proposed reason for a vote (runs blocking calls in a thread)
    async def on_reason(item_id: str, vote: str, strength: int) -> str | None:
        def _propose():
            item = db.get_item(item_id)
            if not item:
                return None
            neighbors = None
            past_reasons: list[str] = []
            if settings.rag_enabled and embedding_manager.enabled:
                try:
                    found = embedding_manager.find_neighbors(
                        [item], k=settings.rag_similarity_limit, exclude_self=True
                    )
                    neighbors = found.get(item_id)
                    if neighbors:
                        past_reasons = [
                            r["reason"] for r in db.get_recent_reasons(
                                [n["item_id"] for n in neighbors]
                            ) if r.get("reason")
                        ]
                except Exception as e:
                    logger.warning(f"Neighbour lookup for reason failed: {e}")
            return claude.propose_reason(
                item, vote, strength, neighbors=neighbors, past_reasons=past_reasons,
            )
        return await asyncio.to_thread(_propose)

    async def on_reason_confirm(item_id: str, reason: str) -> None:
        await asyncio.to_thread(db.update_latest_vote_reason, item_id, reason)

    async def on_retract_vote(item_id: str) -> None:
        await asyncio.to_thread(db.delete_latest_vote, item_id)

    async def on_note(item_id: str, text: str, telegram_message_id: int) -> None:
        await asyncio.to_thread(
            db.save_signal, item_id, "note", None, 1, text, "reply", telegram_message_id,
        )

    async def on_open(item_id: str) -> str | None:
        def _resolve():
            item = db.get_item(item_id)
            if not item or not item.get("url"):
                return None
            try:
                db.save_signal(item_id=item_id, kind="open", source="redirect")
            except Exception as e:
                logger.warning(f"Open signal failed for {item_id}: {e}")
            return item["url"]
        return await asyncio.to_thread(_resolve)

    async def on_lookup_message(telegram_message_id: int) -> dict | None:
        return await asyncio.to_thread(db.get_item_by_message_id, telegram_message_id)

    redirect_base_url = settings.redirect_base_url
    if redirect_base_url is None and not settings.development_mode:
        redirect_base_url = settings.webhook_url

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

    # Create like tweet callback.
    # Runs the blocking Twitter HTTP call in a worker thread so a slow/hung
    # request can't freeze the Telegram bot's event loop.
    async def on_like_tweet(item_id: str) -> dict | None:
        tweet = db.get_item(item_id)
        if tweet:
            return tweet
        tweet = await asyncio.to_thread(twitter.fetch_tweet, item_id)
        if tweet:
            db.save_items([tweet])
        return tweet

    # Create thread callback
    async def on_fetch_thread(item_id: str) -> list[dict] | None:
        tweets = await asyncio.to_thread(twitter.fetch_thread, item_id)
        if tweets:
            db.save_items(tweets)
        return tweets

    # Initialize blog + feed fetchers
    blog_fetcher = BlogFetcher()
    feed_fetcher = FeedFetcher(blog_fetcher=blog_fetcher)

    # RAG setup
    rag_enabled = settings.rag_enabled and embedding_manager.enabled

    def _neighbors_for(content: list[dict]) -> dict[str, list[dict]] | None:
        """Per-item voted neighbours, if RAG is enabled."""
        if not rag_enabled:
            return None
        try:
            return embedding_manager.find_neighbors(
                content, k=settings.rag_similarity_limit
            ) or None
        except Exception as e:
            logger.warning(f"Neighbour lookup failed: {e}")
            return None

    def _score_and_save(content: list[dict]) -> list[dict]:
        """Score items (no threshold) with their neighbours and persist them."""
        neighbors = _neighbors_for(content)
        claude.filter_tweets(
            content, threshold=0, neighbors=neighbors,
            profile=curator_profile(),
        )
        db.save_items(content)
        return content

    def curator_profile() -> str | None:
        """Compiled profile text (or None → baseline), same as the daily run."""
        if settings.profile_source != "compiled":
            return None
        try:
            profiles = db.get_latest_profiles()
        except Exception:
            return None
        return scoring_profile(profiles, claude.profile) if profiles else None

    # Create blog post like callback
    async def on_like_blog(url: str) -> dict | None:
        post = await asyncio.to_thread(blog_fetcher.fetch_blog_post, url)
        if not post:
            return None
        await asyncio.to_thread(_score_and_save, [post])
        return post

    # Capture: any URL or text the user sends becomes an item (+ implied like)
    async def on_capture_url(url: str) -> dict | None:
        def _capture():
            existing = db.get_item_by_url(url)
            if existing:
                return existing
            post = blog_fetcher.fetch_blog_post(url)
            if not post:
                post = minimal_url_item(url)
            post["source"] = "telegram"
            return _score_and_save([post])[0]
        return await asyncio.to_thread(_capture)

    async def on_capture_text(text: str, origin: dict | None = None) -> dict | None:
        def _capture():
            item = text_item(text, origin)
            return _score_and_save([item])[0]
        return await asyncio.to_thread(_capture)

    # Newsletter: LLM extracts the linked items, they are scored like anything else
    async def on_newsletter(url: str) -> dict:
        def _run():
            page = blog_fetcher.fetch_page(url)
            if not page:
                return {"entries": 0, "items": []}
            entries = claude.extract_newsletter_entries(page["title"], page["text"])
            if not entries:
                return {"entries": 0, "items": []}
            items = []
            for e in entries:
                post = blog_fetcher.build_post(
                    url=e["url"], title=e["title"], body=e.get("summary") or "",
                    author=e.get("author"), source="newsletter",
                    original_url=url,
                    extra_raw={"newsletter_url": url, "newsletter_title": page["title"],
                               "section": e.get("section")},
                )
                items.append(post)
            # Skip ones already scored (same link in several issues)
            already = db.get_scored_ids([i["item_id"] for i in items])
            fresh = [i for i in items if i["item_id"] not in already]
            if fresh:
                _score_and_save(fresh)
            scored = {i["item_id"]: i for i in fresh}
            known = db.get_items([i["item_id"] for i in items if i["item_id"] in already])
            ordered = [scored.get(i["item_id"]) or known.get(i["item_id"]) for i in items]
            good = [
                i for i in ordered
                if i and (i.get("filter_score") or 0) >= settings.filter_threshold
            ]
            return {"entries": len(entries), "items": good}
        return await asyncio.to_thread(_run)

    # Feeds
    async def on_feed_add(url: str) -> dict | None:
        def _add():
            found = feed_fetcher.discover_feed(url)
            if not found:
                return None
            row = db.save_feed(found["feed_url"], title=found.get("title"), site_url=found.get("site_url"))
            row["entries"] = len(found.get("entries") or [])
            return row
        return await asyncio.to_thread(_add)

    async def on_feed_list() -> list[dict]:
        return await asyncio.to_thread(db.get_feeds)

    async def on_feed_remove(url: str) -> None:
        await asyncio.to_thread(db.remove_feed, url)

    # Blocked keyword callbacks
    async def on_add_blocked_keyword(keyword: str) -> dict:
        return db.save_blocked_keyword(keyword)

    async def on_list_blocked_keywords() -> list[str]:
        return db.get_blocked_keywords()

    async def on_remove_blocked_keyword(keyword: str) -> None:
        db.remove_blocked_keyword(keyword)

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
        feed_add_callback=on_feed_add,
        feed_list_callback=on_feed_list,
        feed_remove_callback=on_feed_remove,
        add_blocked_keyword_callback=on_add_blocked_keyword,
        list_blocked_keywords_callback=on_list_blocked_keywords,
        remove_blocked_keyword_callback=on_remove_blocked_keyword,
        capture_url_callback=on_capture_url,
        capture_text_callback=on_capture_text,
        reason_callback=on_reason,
        reason_confirm_callback=on_reason_confirm,
        retract_vote_callback=on_retract_vote,
        note_callback=on_note,
        open_callback=on_open,
        lookup_message_callback=on_lookup_message,
        redirect_base_url=redirect_base_url,
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
        rag_enabled=settings.rag_enabled,
        neighbors_per_item=settings.rag_similarity_limit,
        exploration_per_run=settings.exploration_per_run,
        profile_source=settings.profile_source,
        feed_fetcher=feed_fetcher,
        feed_max_entries=settings.feed_max_entries_per_run,
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
    run_once_on_startup: bool = False,
) -> None:
    """Run in scheduled mode with Telegram bot.

    Args:
        curator: DailyCurator instance
        telegram: TelegramCurator instance
        settings: Application settings
        run_once_on_startup: If True, run one curation immediately before
            entering the scheduler loop. Otherwise wait for the next scheduled tick.
    """
    logger.info("Starting scheduled mode...")

    # Initialize Telegram
    await telegram.initialize()

    # Schedule daily curation
    curator.schedule_daily(
        hour=settings.schedule_hour,
        minute=0,
        timezone=settings.schedule_timezone,
        skip_weekdays=[
            d.strip() for d in settings.schedule_skip_weekdays.split(",") if d.strip()
        ],
    )
    curator.schedule_weekly_profile(
        settings.profile_compile_weekday,
        hour=settings.schedule_hour,
        minute=30,
        timezone=settings.schedule_timezone,
    )

    if run_once_on_startup:
        # If initial curation fails, log and continue so the bot stays reachable.
        logger.info("Running initial curation...")
        try:
            await curator.run_daily_curation()
        except Exception as e:
            logger.error(f"Initial curation failed: {e}", exc_info=True)
    else:
        logger.info(
            "Skipping initial curation (pass --run-once-on-startup to enable). "
            f"Next run at {settings.schedule_hour:02d}:00 {settings.schedule_timezone}."
        )

    await asyncio.gather(
        curator.run_scheduled(),
        _run_telegram(telegram, settings),
    )


async def _run_telegram(telegram: TelegramCurator, settings) -> None:
    """Run Telegram in polling (dev) or webhook (prod) mode."""
    if settings.development_mode:
        logger.info("DEVELOPMENT_MODE=true → using long-polling")
        await telegram.run_polling()
    else:
        if not settings.webhook_url:
            raise RuntimeError(
                "webhook_url is required when development_mode=false. "
                "Set WEBHOOK_URL env var to the deployment's public HTTPS URL, "
                "or set DEVELOPMENT_MODE=true for local long-polling."
            )
        logger.info("Production mode → using webhook")
        await telegram.run_webhook(
            webhook_url=settings.webhook_url,
            port=settings.port,
        )


async def run_bot_only(telegram: TelegramCurator, settings) -> None:
    """Run only the Telegram bot for receiving feedback.

    Args:
        telegram: TelegramCurator instance
        settings: Application settings
    """
    logger.info("Running Telegram bot only...")

    await telegram.initialize()
    await _run_telegram(telegram, settings)


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
        claude = ClaudeFilter(api_key=settings.anthropic_api_key, model=settings.claude_model)
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
        if args.compile_profile:
            _, _, telegram, db, curator, blog_fetcher = init_components(settings)
            try:
                async def _compile():
                    await telegram.initialize()
                    try:
                        written = await curator.compile_profiles()
                    finally:
                        await telegram.shutdown()
                    return written
                written = asyncio.run(_compile())
                logger.info(f"Compiled: {', '.join(written) if written else 'nothing'}")
            finally:
                blog_fetcher.close()
            return 0
        elif args.export_preferences:
            db = DatabaseClient(url=settings.supabase_url, key=settings.supabase_key)
            profiles = db.get_latest_profiles()
            if not profiles:
                logger.error("No compiled profiles yet. Run --compile-profile first.")
                return 1
            with open(args.export_preferences, "w") as fh:
                fh.write(render_preferences_md(profiles))
            logger.info(f"Wrote {args.export_preferences} ({len(profiles)} sections)")
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
                asyncio.run(run_scheduled(
                    curator, telegram, settings,
                    run_once_on_startup=args.run_once_on_startup,
                ))
            finally:
                blog_fetcher.close()
        elif args.bot_only:
            _, _, telegram, db, _, blog_fetcher = init_components(settings)
            try:
                asyncio.run(run_bot_only(telegram, settings))
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
