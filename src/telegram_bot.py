"""Telegram bot for sending curated tweets and collecting feedback."""

import asyncio
import html
import logging
import re
from typing import Callable, Optional

import telegram.error
from datetime import datetime

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    MessageReactionHandler,
    filters,
)

from src.content import parent_item_id
from src.feedback_flow import Vote, quoted_item_id
from src.webhook_server import make_app
from src.bot.admin import AdminMixin
from src.bot.capture import CaptureMixin
from src.bot.constants import (
    BLOCKWORD_AWAITING_INPUT,
    LIKE_AWAITING_INPUT,
    NEWSLETTER_AWAITING_INPUT,
    STAR_AWAITING_INPUT,
    THREAD_AWAITING_INPUT,
    VOTE_GRACE_SECONDS,
)
from src.bot.feedback import FeedbackMixin
from src.bot.feeds import FeedsMixin
from src.bot.formatting import FormattingMixin


logger = logging.getLogger(__name__)


class TelegramCurator(
    FeedbackMixin, CaptureMixin, FeedsMixin, AdminMixin, FormattingMixin
):
    """Telegram bot for tweet curation."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        feedback_callback: Optional[Callable] = None,
        favorite_author_callback: Optional[Callable] = None,
        mute_author_callback: Optional[Callable] = None,
        stats_callback: Optional[Callable] = None,
        list_starred_callback: Optional[Callable] = None,
        like_tweet_callback: Optional[Callable] = None,
        thread_callback: Optional[Callable] = None,
        like_blog_callback: Optional[Callable] = None,
        newsletter_callback: Optional[Callable] = None,
        feed_add_callback: Optional[Callable] = None,
        feed_list_callback: Optional[Callable] = None,
        feed_remove_callback: Optional[Callable] = None,
        inbox_callback: Optional[Callable] = None,
        add_blocked_keyword_callback: Optional[Callable] = None,
        list_blocked_keywords_callback: Optional[Callable] = None,
        remove_blocked_keyword_callback: Optional[Callable] = None,
        capture_url_callback: Optional[Callable] = None,
        capture_text_callback: Optional[Callable] = None,
        reason_callback: Optional[Callable] = None,
        reason_confirm_callback: Optional[Callable] = None,
        retract_vote_callback: Optional[Callable] = None,
        note_callback: Optional[Callable] = None,
        open_callback: Optional[Callable] = None,
        lookup_message_callback: Optional[Callable] = None,
        mark_sent_callback: Optional[Callable] = None,
        meh_callback: Optional[Callable] = None,
        redirect_base_url: Optional[str] = None,
    ):
        """Initialize Telegram bot.

        Args:
            bot_token: Telegram bot token from BotFather
            chat_id: Target chat ID to send messages to
            feedback_callback: Async callback function(item_id, vote, message_id)
            favorite_author_callback: Async callback function(username) → state string
            mute_author_callback: Async callback function(username) → state string
            stats_callback: Async callback function() → list of author stat dicts
            list_starred_callback: Async callback function() → list of starred usernames
            like_tweet_callback: Async callback function(item_id) → tweet dict or None
            thread_callback: Async callback function(item_id) → list of tweet dicts or None
            like_blog_callback: Async callback function(url) → blog post dict or None
            newsletter_callback: Async function(url) → {"entries": n, "items": [scored item dicts]}
            feed_add_callback: Async function(url) → feed dict (discovered + saved) or None
            feed_list_callback: Async function() → list of feed dicts
            feed_remove_callback: Async function(url) → None
            inbox_callback: Async function() → {"emails", "entries", "sent"}; processes
                newsletter emails now (None when email ingestion is not configured)
            add_blocked_keyword_callback: Async callback function(keyword) → saved keyword
            list_blocked_keywords_callback: Async callback function() → list of keywords
            remove_blocked_keyword_callback: Async callback function(keyword) → None
            capture_url_callback: Async function(url) → item dict (fetched, scored, saved) or None
            capture_text_callback: Async function(text, origin) → item dict or None
            reason_callback: Async function(item_id, vote, strength) → proposed reason text or None
            reason_confirm_callback: Async function(item_id, reason) → None
            retract_vote_callback: Async function(item_id) → None (reaction removed)
            note_callback: Async function(item_id, text, telegram_message_id) → None
            open_callback: Async function(item_id) → destination URL or None; records the open
            lookup_message_callback: Async function(telegram_message_id) → item dict or None
            meh_callback: Async function(since, until) → int; marks every unvoted,
                unopened item sent in that window as skipped ("rest are meh")
            mark_sent_callback: Async function(item_id, telegram_message_id) → None; persists
                the message id for items sent outside the daily digest
            redirect_base_url: Public base URL for /r/<item_id> tracking links (None = direct links)
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.feedback_callback = feedback_callback
        self.favorite_author_callback = favorite_author_callback
        self.mute_author_callback = mute_author_callback
        self.stats_callback = stats_callback
        self.list_starred_callback = list_starred_callback
        self.like_tweet_callback = like_tweet_callback
        self.thread_callback = thread_callback
        self.like_blog_callback = like_blog_callback
        self.newsletter_callback = newsletter_callback
        self.feed_add_callback = feed_add_callback
        self.feed_list_callback = feed_list_callback
        self.feed_remove_callback = feed_remove_callback
        self.inbox_callback = inbox_callback
        self.add_blocked_keyword_callback = add_blocked_keyword_callback
        self.list_blocked_keywords_callback = list_blocked_keywords_callback
        self.remove_blocked_keyword_callback = remove_blocked_keyword_callback
        self.capture_url_callback = capture_url_callback
        self.capture_text_callback = capture_text_callback
        self.reason_callback = reason_callback
        self.reason_confirm_callback = reason_confirm_callback
        self.retract_vote_callback = retract_vote_callback
        self.note_callback = note_callback
        self.open_callback = open_callback
        self.lookup_message_callback = lookup_message_callback
        self.mark_sent_callback = mark_sent_callback
        self.meh_callback = meh_callback
        self.redirect_base_url = redirect_base_url
        self.application: Optional[Application] = None
        self._pending_feedback: dict[str, dict] = {}  # item_id → pending save info
        self._tweet_authors: dict[str, str] = {}  # item_id → username
        self._message_items: dict[int, str] = {}  # telegram message_id → item_id
        self._item_votes: dict[str, Vote] = {}  # item_id → latest vote
        self._reason_prompts: dict[int, str] = {}  # proposal message_id → item_id
        self._item_prompts: dict[str, tuple[int, str]] = {}  # item_id → (prompt msg id, proposal)
        self._pending_reasons: dict[str, str] = {}  # item_id → reason confirmed before save
        self._feed_list: dict[str, list[dict]] = {}  # chat_id → feeds shown by /feed list
        self._blockword_list: dict[str, list[str]] = {}  # chat_id → keyword list shown
        self._tasks: set[asyncio.Task] = set()  # background tasks (reason proposals)
        self._vote_generation: dict[str, int] = {}  # item_id → bumps on every (re)vote
        self._quoted_items: dict[str, str] = {}  # item_id → id of the tweet it quotes
        self._parent_items: dict[str, str] = {}  # item_id → id of the tweet it replies to
        self._links_known: set[str] = set()  # item_ids whose quoted/parent links were recorded
        logger.info("Telegram bot initialized")

    async def initialize(self) -> None:
        """Initialize the bot application."""
        self.application = (
            Application.builder()
            .token(self.bot_token)
            .connect_timeout(30.0)
            .read_timeout(30.0)
            .write_timeout(30.0)
            .build()
        )
        self.setup_handlers()
        await self.application.initialize()
        await self._set_commands_menu()
        logger.info("Telegram application initialized")

    async def _set_commands_menu(self) -> None:
        """Register bot commands so they appear in Telegram's command menu."""
        commands = [
            BotCommand("star", "Toggle starred status — /star username"),
            BotCommand("like", "Upvote a tweet or blog post — /like url"),
            BotCommand("newsletter", "Extract and score a newsletter issue — /newsletter url"),
            BotCommand("feed", "RSS subscriptions — /feed add url | list | remove n"),
            BotCommand("inbox", "Process newsletter emails now"),
            BotCommand("thread", "Fetch and display a thread — /thread tweet_url"),
            BotCommand("starred", "List all starred authors"),
            BotCommand("blockword", "Block keyword(s) from pipeline — paste one per line or comma-separated"),
            BotCommand("blockwords", "List blocked keywords — tap to remove"),
            BotCommand("stats", "Show author performance stats"),
            BotCommand("help", "Show help message"),
        ]
        await self.application.bot.set_my_commands(commands)
        logger.info("Bot command menu registered")

    def setup_handlers(self) -> None:
        """Set up message and callback handlers."""
        if not self.application:
            raise RuntimeError("Application not initialized")

        # Command handlers
        self.application.add_handler(
            CommandHandler("start", self._handle_start)
        )
        self.application.add_handler(
            CommandHandler("help", self._handle_help)
        )
        self.application.add_handler(
            CommandHandler("stats", self._handle_stats)
        )
        self.application.add_handler(
            ConversationHandler(
                entry_points=[CommandHandler("star", self._handle_star)],
                states={
                    STAR_AWAITING_INPUT: [
                        MessageHandler(
                            filters.TEXT & ~filters.COMMAND,
                            self._handle_star_input,
                        )
                    ],
                },
                fallbacks=[CommandHandler("cancel", self._handle_star_cancel)],
            )
        )
        self.application.add_handler(
            ConversationHandler(
                entry_points=[CommandHandler("like", self._handle_like)],
                states={
                    LIKE_AWAITING_INPUT: [
                        MessageHandler(
                            filters.TEXT & ~filters.COMMAND,
                            self._handle_like_input,
                        )
                    ],
                },
                fallbacks=[CommandHandler("cancel", self._handle_like_cancel)],
            )
        )
        self.application.add_handler(
            ConversationHandler(
                entry_points=[CommandHandler("thread", self._handle_thread)],
                states={
                    THREAD_AWAITING_INPUT: [
                        MessageHandler(
                            filters.TEXT & ~filters.COMMAND,
                            self._handle_thread_input,
                        )
                    ],
                },
                fallbacks=[CommandHandler("cancel", self._handle_thread_cancel)],
            )
        )
        self.application.add_handler(
            ConversationHandler(
                entry_points=[CommandHandler("newsletter", self._handle_newsletter)],
                states={
                    NEWSLETTER_AWAITING_INPUT: [
                        MessageHandler(
                            filters.TEXT & ~filters.COMMAND,
                            self._handle_newsletter_input,
                        )
                    ],
                },
                fallbacks=[CommandHandler("cancel", self._handle_newsletter_cancel)],
            )
        )
        self.application.add_handler(
            CommandHandler("starred", self._handle_starred)
        )
        self.application.add_handler(
            CommandHandler("feed", self._handle_feed)
        )
        self.application.add_handler(
            CommandHandler("inbox", self._handle_inbox)
        )
        self.application.add_handler(
            ConversationHandler(
                entry_points=[CommandHandler("blockword", self._handle_blockword)],
                states={
                    BLOCKWORD_AWAITING_INPUT: [
                        MessageHandler(
                            filters.TEXT & ~filters.COMMAND,
                            self._handle_blockword_input,
                        )
                    ],
                },
                fallbacks=[CommandHandler("cancel", self._handle_blockword_cancel)],
            )
        )
        self.application.add_handler(
            CommandHandler("blockwords", self._handle_blockwords)
        )

        # Callback handler for inline buttons
        self.application.add_handler(
            CallbackQueryHandler(self._handle_feedback)
        )

        # Emoji reactions on sent items count as votes
        self.application.add_handler(
            MessageReactionHandler(self._handle_reaction)
        )

        # Default handler: route plain messages to /like (tweets/blogs) or
        # /star (profiles/usernames) based on content. Registered last so
        # active ConversationHandlers take precedence.
        self.application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_default_message,
            )
        )

        logger.info("Handlers set up")

    async def _handle_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /start command."""
        await update.message.reply_text(
            "Welcome to Twitter Curator!\n\n"
            "I'll send you curated tweets based on your interests. "
            "Use the buttons to provide feedback."
        )

    async def _handle_help(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /help command."""
        await update.message.reply_text(
            "Content Curator Help:\n\n"
            "Commands:\n"
            "/star username or URL — toggle starred status for an author\n"
            "/like URL or tweet ID — upvote a tweet or blog post with a reason\n"
            "/newsletter URL — extract the links in a newsletter issue, score them, send the good ones\n"
            "/feed add URL | list | remove N — RSS/Atom subscriptions polled in the daily run\n"
            "/inbox — process forwarded newsletter emails now (also runs after each daily digest)\n"
            "/thread tweet URL or ID — fetch and display a full thread\n"
            "/starred — list all starred authors\n"
            "/blockword — block keyword(s) from the pipeline (one per line or comma-separated)\n"
            "/blockwords — list blocked keywords, tap to remove\n"
            "/stats — show author performance stats\n\n"
            "- I send you filtered tweets daily\n"
            "- Items marked 🎲 scored below the threshold and are sent at random "
            "so your vote tells me what the filter is missing\n"
            "- Vote with the buttons (🔥 strong like, 👍, 👎) or just react to the "
            "message with an emoji; remove the reaction to retract\n"
            "- After a vote I guess why; tap ✅, or ✏️ and reply with your own words\n"
            "- Reply to any item to attach a note\n"
            "\nSending me things (no command needed):\n"
            "- a URL (tweet, article, anything) → saved and upvoted; "
            "words around it are saved as your reason, e.g. "
            "'great read https://… the section on preconfs'\n"
            "- :like URL [why] · :dislike URL [why] · :star @handle · :note your thought\n"
            "- several items: one per line, each line keeps its own reason\n"
            "- a forwarded post → saved and upvoted\n"
            "- a reply to any item → note on it\n"
            "- Blocked keywords are matched whole-word, case-insensitive, before LLM scoring\n"
            "  (content from starred authors is exempt from the blocklist)"
        )

    async def send_tweet(
        self,
        tweet: dict,
        position: Optional[tuple[int, int]] = None,
        hide_parent: bool = False,
    ) -> Optional[int]:
        """Send a formatted tweet message with feedback buttons.

        Args:
            tweet: Tweet dictionary with:
                - item_id: Twitter ID
                - author_username: Author's username
                - author_name: Author's display name
                - text: Tweet text
                - url: Tweet URL
                - metrics: Dict with likes, retweets, replies
                - filter_score: Claude's score
                - filter_reason: Reason for score

        Returns:
            Telegram message ID or None if failed
        """
        if not self.application:
            raise RuntimeError("Application not initialized")

        # Format message
        message = self._format_tweet_message(
            tweet, position=position, hide_parent=hide_parent
        )

        # Store author mapping for undo functionality
        self._tweet_authors[tweet["item_id"]] = tweet["author_username"]

        # Create inline keyboard with feedback buttons (+ quoted / parent rows)
        quoted_id, parent_id = self._remember_links(tweet)
        keyboard = self._make_tweet_buttons(
            tweet["item_id"], tweet["author_username"],
            quoted_id=quoted_id, parent_id=parent_id,
        )

        try:
            sent_message = await self.application.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                reply_markup=keyboard,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            self._message_items[sent_message.message_id] = tweet["item_id"]
            logger.info(f"Sent tweet {tweet['item_id']} to Telegram")
            return sent_message.message_id
        except Exception as e:
            logger.error(f"Error sending tweet {tweet['item_id']} to Telegram: {e}")
            return None

    async def send_daily_digest(
        self,
        tweet_groups: list[list[dict]],
        delay_seconds: float = 1.0,
    ) -> list[tuple[dict, Optional[int]]]:
        """Send grouped tweets with thread headers and rate limiting.

        Args:
            tweet_groups: List of tweet groups (each group is a list of 1+
                         tweets sharing a conversation_id).
            delay_seconds: Delay between messages (default 1s)

        Returns:
            Flat list of (tweet, message_id) tuples for mark_item_sent.
        """
        total_tweets = sum(len(g) for g in tweet_groups)
        if not total_tweets:
            logger.warning("No tweets to send in digest")
            return []

        # Send digest header
        await self.application.bot.send_message(
            chat_id=self.chat_id,
            text=f"📰 <b>Daily Tweet Digest</b>\n\n"
                 f"Found {total_tweets} relevant tweets for you today.",
            parse_mode="HTML",
        )

        results: list[tuple[dict, Optional[int]]] = []
        flat_index = 0

        for group in tweet_groups:
            # Thread / conversation header for multi-tweet groups
            if len(group) > 1:
                await self.application.bot.send_message(
                    chat_id=self.chat_id,
                    text=self._group_header(group),
                    parse_mode="HTML",
                )
                await asyncio.sleep(delay_seconds)

            group_ids = {t["item_id"] for t in group}
            for tweet in group:
                flat_index += 1
                message_id = await self._send_item(
                    tweet, position=(flat_index, total_tweets),
                    hide_parent=parent_item_id(tweet) in group_ids,
                )
                results.append((tweet, message_id))

                # Rate limit between tweets
                if flat_index < total_tweets:
                    await asyncio.sleep(delay_seconds)

        sent_count = sum(1 for _, mid in results if mid)
        logger.info(f"Sent {sent_count} tweets in daily digest")
        return results

    async def send_digest_footer(self, since: datetime, until: datetime) -> None:
        """Close a run with a one-tap "rest are meh" for everything sent in it."""
        if not self.application or not self.meh_callback:
            return
        text = (
            "That's everything for this run. Items you don't vote on count as a weak "
            "skip after a few days; tap below to mark them now instead."
        )
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(
            "😐 Rest are meh",
            callback_data=f"meh:{int(since.timestamp())}:{int(until.timestamp())}",
        )]])
        await self.application.bot.send_message(
            chat_id=self.chat_id, text=text, reply_markup=keyboard,
        )

    def _group_header(self, group: list[dict]) -> str:
        """Header above a multi-tweet group: a thread when one author wrote
        it all, otherwise a conversation naming the authors in order."""
        authors = list(dict.fromkeys(t["author_username"] for t in group))
        if len(authors) == 1:
            title = f"🧵 <b>Thread by @{self._escape_html(authors[0])}</b>"
        else:
            names = ", ".join(f"@{self._escape_html(a)}" for a in authors)
            title = f"💬 <b>Conversation</b> {names}"
        return f"{title} ({len(group)} tweets in this batch)\n────────────────────"

    async def send_error_notification(self, error_message: str) -> None:
        """Send error notification to user.

        Args:
            error_message: Error message to send
        """
        if not self.application:
            return

        try:
            await self.application.bot.send_message(
                chat_id=self.chat_id,
                text=f"⚠️ <b>Curator Error</b>\n\n{self._escape_html(error_message)}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to send error notification: {e}")

    async def run_polling(self) -> None:
        """Start the bot in polling mode (works within existing event loop)."""
        if not self.application:
            await self.initialize()

        logger.info("Starting Telegram bot polling...")
        await self.application.start()
        await self.application.updater.start_polling(
            bootstrap_retries=3, allowed_updates=Update.ALL_TYPES,
        )

        # Keep running until interrupted
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.shutdown()

    async def run_webhook(self, webhook_url: str, port: int) -> None:
        """Start the bot in webhook mode.

        Runs our own HTTP server (see ``src/webhook_server.py``) so the same
        port serves Telegram updates at ``/<token>`` and click-tracking
        redirects at ``/r/<item_id>``.

        Args:
            webhook_url: Public HTTPS base URL Telegram will POST updates to
                (e.g. https://app.up.railway.app). The bot token is appended
                as the URL path so only Telegram can reach the endpoint.
            port: Local port to bind the HTTP server on.
        """
        if not self.application:
            await self.initialize()

        # Bot token as url_path acts as a shared secret between Telegram and us.
        url_path = self.bot_token
        full_url = f"{webhook_url.rstrip('/')}/{url_path}"

        logger.info(f"Starting webhook server on port {port}, url={webhook_url}/<token>")
        app = make_app(self.application, url_path, self.resolve_open)
        server = app.listen(port, address="0.0.0.0")
        await self.application.start()
        await self.application.bot.set_webhook(
            url=full_url, allowed_updates=Update.ALL_TYPES,
        )

        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            server.stop()
            await self.shutdown()

    async def shutdown(self) -> None:
        """Shutdown the bot gracefully."""
        if self.application:
            if self.application.updater.running:
                await self.application.updater.stop()
            if self.application.running:
                await self.application.stop()
            await self.application.shutdown()
            logger.info("Telegram bot shut down")
