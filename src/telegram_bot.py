"""Telegram bot for sending curated tweets and collecting feedback."""

import asyncio
import logging
import re
from typing import Callable, Optional

import telegram.error
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)


# Conversation states
STAR_AWAITING_INPUT = 0
LIKE_AWAITING_INPUT = 1


class TelegramCurator:
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
    ):
        """Initialize Telegram bot.

        Args:
            bot_token: Telegram bot token from BotFather
            chat_id: Target chat ID to send messages to
            feedback_callback: Async callback function(tweet_id, vote, message_id)
            favorite_author_callback: Async callback function(username) → state string
            mute_author_callback: Async callback function(username) → state string
            stats_callback: Async callback function() → list of author stat dicts
            list_starred_callback: Async callback function() → list of starred usernames
            like_tweet_callback: Async callback function(tweet_id) → tweet dict or None
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.feedback_callback = feedback_callback
        self.favorite_author_callback = favorite_author_callback
        self.mute_author_callback = mute_author_callback
        self.stats_callback = stats_callback
        self.list_starred_callback = list_starred_callback
        self.like_tweet_callback = like_tweet_callback
        self.application: Optional[Application] = None
        self._pending_feedback: dict[str, dict] = {}  # tweet_id → pending save info
        self._tweet_authors: dict[str, str] = {}  # tweet_id → username
        logger.info("Telegram bot initialized")

    async def initialize(self) -> None:
        """Initialize the bot application."""
        self.application = (
            Application.builder()
            .token(self.bot_token)
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
            BotCommand("like", "Upvote a tweet — /like tweet_url"),
            BotCommand("starred", "List all starred authors"),
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
            CommandHandler("starred", self._handle_starred)
        )

        # Callback handler for inline buttons
        self.application.add_handler(
            CallbackQueryHandler(self._handle_feedback)
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
            "Twitter Curator Help:\n\n"
            "Commands:\n"
            "/star username or URL — toggle starred status for an author\n"
            "/like tweet URL or ID — upvote a tweet with a reason\n"
            "/starred — list all starred authors\n"
            "/stats — show author performance stats\n\n"
            "- I send you filtered tweets daily\n"
            "- Use the buttons to tell me what you like\n"
            "- Your feedback helps improve future curation"
        )

    async def _handle_stats(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /stats command."""
        if not self.stats_callback:
            await update.message.reply_text("Stats not available.")
            return

        try:
            stats = await self.stats_callback()
        except Exception as e:
            logger.error(f"Error fetching stats: {e}")
            await update.message.reply_text("Error fetching stats.")
            return

        if not stats:
            await update.message.reply_text(
                "No feedback data yet. Vote on some tweets first!"
            )
            return

        # Parse page number
        page = 1
        if context.args:
            try:
                page = max(1, int(context.args[0]))
            except (ValueError, IndexError):
                page = 1

        message = self._format_stats_message(stats, page)
        await update.message.reply_text(message, parse_mode="HTML")

    @staticmethod
    def _extract_username(arg: str) -> str:
        """Extract a Twitter username from a URL, @mention, or plain username.

        Supports:
            - https://twitter.com/username/status/123
            - https://x.com/username
            - @username
            - username

        Returns:
            Lowercase username string
        """
        # Match twitter.com or x.com URLs
        match = re.match(
            r"https?://(?:www\.)?(?:twitter|x)\.com/([A-Za-z0-9_]+)",
            arg,
        )
        if match:
            return match.group(1).lower()
        # Plain username or @mention
        return arg.lower().lstrip("@")

    async def _handle_star(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle /star command to toggle starred status for an author.

        If called with args, processes immediately.
        If called without args, prompts and waits for input.
        """
        if not self.favorite_author_callback:
            await update.message.reply_text("Star feature not available.")
            return ConversationHandler.END

        if not context.args:
            await update.message.reply_text(
                "Send me a username, @mention, or tweet/profile URL to star.\n"
                "You can send multiple separated by spaces.\n\n"
                "/cancel to abort."
            )
            return STAR_AWAITING_INPUT

        await self._star_authors(update, context.args)
        return ConversationHandler.END

    async def _handle_star_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle the follow-up message with usernames/URLs after /star prompt."""
        args = update.message.text.strip().split()
        if not args:
            await update.message.reply_text("No input received. Try again or /cancel.")
            return STAR_AWAITING_INPUT

        await self._star_authors(update, args)
        return ConversationHandler.END

    async def _handle_star_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle /cancel during star conversation."""
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    async def _star_authors(self, update: Update, args: list[str]) -> None:
        """Process a list of username/URL args and toggle their starred status."""
        results = []
        for arg in args:
            username = self._extract_username(arg)
            try:
                state = await self.favorite_author_callback(username=username)
                if state == "favorited":
                    results.append(f"⭐ @{username} starred")
                elif state == "unmuted":
                    results.append(f"🔊 @{username} unmuted (was muted)")
                else:
                    results.append(f"@{username} → {state}")
            except Exception as e:
                logger.error(f"Error starring @{username}: {e}")
                results.append(f"❌ @{username} — error")

        await update.message.reply_text("\n".join(results))

    @staticmethod
    def _extract_tweet_id(arg: str) -> str | None:
        """Extract a tweet ID from a URL or raw numeric string.

        Supports:
            - https://twitter.com/user/status/123456
            - https://x.com/user/status/123456
            - 123456 (raw numeric ID)

        Returns:
            Tweet ID string, or None if unrecognizable
        """
        match = re.match(
            r"https?://(?:www\.)?(?:twitter|x)\.com/[A-Za-z0-9_]+/status/(\d+)",
            arg,
        )
        if match:
            return match.group(1)
        if arg.isdigit():
            return arg
        return None

    async def _handle_like(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle /like command to upvote a tweet.

        If called with args, processes immediately.
        If called without args, prompts and waits for input.
        """
        if not self.like_tweet_callback:
            await update.message.reply_text("Like feature not available.")
            return ConversationHandler.END

        if not context.args:
            await update.message.reply_text(
                "Send me a tweet URL or ID to like.\n"
                "You can send multiple separated by spaces.\n\n"
                "/cancel to abort."
            )
            return LIKE_AWAITING_INPUT

        await self._like_tweets(update, context.args)
        return ConversationHandler.END

    async def _handle_like_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle follow-up message with tweet URLs/IDs after /like prompt."""
        args = update.message.text.strip().split()
        if not args:
            await update.message.reply_text("No input received. Try again or /cancel.")
            return LIKE_AWAITING_INPUT

        await self._like_tweets(update, args)
        return ConversationHandler.END

    async def _handle_like_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle /cancel during like conversation."""
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    async def _like_tweets(self, update: Update, args: list[str]) -> None:
        """Process a list of tweet URL/ID args and send each with reason buttons."""
        for arg in args:
            tweet_id = self._extract_tweet_id(arg)
            if not tweet_id:
                await update.message.reply_text(f"Could not parse tweet ID from: {arg}")
                continue

            try:
                tweet = await self.like_tweet_callback(tweet_id)
            except Exception as e:
                logger.error(f"Error fetching tweet {tweet_id}: {e}")
                await update.message.reply_text(f"Error fetching tweet {tweet_id}.")
                continue

            if not tweet:
                await update.message.reply_text(f"Tweet {tweet_id} not found.")
                continue

            # Format message without score/reason
            message = self._format_like_message(tweet)

            # Store author mapping for undo functionality
            self._tweet_authors[tweet_id] = tweet["author_username"]

            # Send with reason category buttons
            keyboard = self._make_like_reason_buttons(tweet_id)

            try:
                await self.application.bot.send_message(
                    chat_id=self.chat_id,
                    text=message,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.error(f"Error sending like message for {tweet_id}: {e}")
                await update.message.reply_text(f"Error sending tweet {tweet_id}.")

    @staticmethod
    def _make_like_reason_buttons(tweet_id: str) -> InlineKeyboardMarkup:
        """Build the reason category buttons for a liked tweet."""
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "Tech content",
                    callback_data=f"like_reason:{tweet_id}:tech",
                ),
                InlineKeyboardButton(
                    "Non-tech insight",
                    callback_data=f"like_reason:{tweet_id}:non_tech",
                ),
            ],
            [
                InlineKeyboardButton(
                    "Soft skills",
                    callback_data=f"like_reason:{tweet_id}:soft_skills",
                ),
                InlineKeyboardButton(
                    "Life wisdom",
                    callback_data=f"like_reason:{tweet_id}:life_wisdom",
                ),
            ],
        ])

    def _format_like_message(self, tweet: dict) -> str:
        """Format tweet for like message (no score/reason header)."""
        metrics = tweet.get("metrics", {})
        text = self._escape_html(tweet["text"])

        likes = metrics.get("likes", 0)
        retweets = metrics.get("retweets", 0)
        replies = metrics.get("replies", 0)

        likes_str = self._format_number(likes)
        retweets_str = self._format_number(retweets)
        replies_str = self._format_number(replies)

        message = (
            f"<b>@{tweet['author_username']}</b> | "
            f"<a href=\"{tweet['url']}\">View Tweet</a>\n\n"
            f"{text}"
        )

        quoted = tweet.get("quoted_tweet")
        if quoted:
            qt_author = self._escape_html(quoted["author_username"])
            qt_text = self._escape_html(quoted["text"])
            qt_lines = qt_text.split("\n")
            qt_block = "\n".join(f"┃ {line}" for line in qt_lines)
            message += (
                f"\n\n┃ <b>@{qt_author}:</b>\n"
                f"{qt_block}"
            )

        message += f"\n\n❤️ {likes_str}  🔁 {retweets_str}  💬 {replies_str}"
        return message

    async def _handle_starred(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /starred command to list all starred authors."""
        if not self.list_starred_callback:
            await update.message.reply_text("Starred list not available.")
            return

        try:
            authors = await self.list_starred_callback()
        except Exception as e:
            logger.error(f"Error fetching starred authors: {e}")
            await update.message.reply_text("Error fetching starred authors.")
            return

        if not authors:
            await update.message.reply_text(
                "No starred authors yet.\n"
                "Use /star username to add one."
            )
            return

        lines = [f"⭐ <b>Starred Authors</b> ({len(authors)})\n"]
        for author in sorted(authors):
            lines.append(f"  @{author}")
        lines.append(f"\nUse /star username to add or remove.")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    @staticmethod
    def _format_stats_message(stats: list[dict], page: int = 1, per_page: int = 15) -> str:
        """Format author stats into a paginated table.

        Args:
            stats: List of author stat dicts from get_author_stats()
            page: Page number (1-indexed)
            per_page: Authors per page

        Returns:
            Formatted HTML message string
        """
        total_authors = len(stats)
        total_pages = max(1, (total_authors + per_page - 1) // per_page)
        page = min(page, total_pages)

        start = (page - 1) * per_page
        end = start + per_page
        page_stats = stats[start:end]

        total_votes = sum(s["total_votes"] for s in stats)

        lines = [f"Author Performance (Page {page}/{total_pages})\n"]
        lines.append(f" #  {'Author':<16} Score  Up Dn  Avg")
        lines.append("-" * 42)

        for i, s in enumerate(page_stats, start=start + 1):
            username = s["author_username"]
            prefix = ""
            if s["is_favorite"]:
                prefix = "\u2b50"
            elif s["is_muted"]:
                prefix = "\U0001f507"

            display = prefix + username
            if len(display) > 15:
                display = display[:14] + "\u2026"

            score = f"{s['weighted_score']:.2f}"
            avg = f"{s['avg_filter_score']:.0f}" if s["avg_filter_score"] else " -"

            lines.append(
                f"{i:>2}  {display:<16} {score}  {s['up']:>2} {s['down']:>2}  {avg:>3}"
            )

        lines.append("")
        lines.append("Score = weighted signal ratio (tweets 1.0x, RTs 0.5x)")
        lines.append(f"Total authors: {total_authors} | Total votes: {total_votes}")
        if total_pages > 1:
            lines.append(f"/stats {page + 1 if page < total_pages else 1} \u2192 {'next' if page < total_pages else 'first'} page")

        return "<pre>" + "\n".join(lines) + "</pre>"

    async def _handle_feedback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle feedback button press."""
        query = update.callback_query
        try:
            await query.answer()
        except telegram.error.BadRequest:
            logger.warning(f"Callback query expired, processing anyway: {query.data}")

        data = query.data

        # Handle vote: "vote:{tweet_id}:{up|down}"
        if data.startswith("vote:"):
            parts = data.split(":")
            if len(parts) != 3:
                return

            _, tweet_id, vote = parts

            if vote == "up":
                # Thumbs up - show category buttons
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "Tech content",
                                callback_data=f"reason:{tweet_id}:up:tech"
                            ),
                            InlineKeyboardButton(
                                "Non-tech insight",
                                callback_data=f"reason:{tweet_id}:up:non_tech"
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "Soft skills",
                                callback_data=f"reason:{tweet_id}:up:soft_skills"
                            ),
                            InlineKeyboardButton(
                                "Life wisdom",
                                callback_data=f"reason:{tweet_id}:up:life_wisdom"
                            ),
                        ],
                    ])
                )

            elif vote == "down":
                # Thumbs down - show reason buttons
                await query.edit_message_reply_markup(
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "No tech content",
                                callback_data=f"reason:{tweet_id}:down:no_tech"
                            ),
                            InlineKeyboardButton(
                                "Event/promo",
                                callback_data=f"reason:{tweet_id}:down:event_promo"
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                "Low quality",
                                callback_data=f"reason:{tweet_id}:down:low_quality"
                            ),
                            InlineKeyboardButton(
                                "Not relevant",
                                callback_data=f"reason:{tweet_id}:down:not_relevant"
                            ),
                        ],
                    ])
                )

        # Handle vote reason: "reason:{tweet_id}:{up|down}:{reason_code}"
        elif data.startswith("reason:"):
            parts = data.split(":")
            if len(parts) != 4:
                return

            _, tweet_id, vote, reason_code = parts

            reason_labels = {
                # Upvote reasons
                "tech": "Tech content",
                "non_tech": "Non-tech insight",
                "soft_skills": "Soft skills",
                "life_wisdom": "Life wisdom",
                # Downvote reasons
                "no_tech": "No tech content",
                "event_promo": "Event/promo",
                "low_quality": "Low quality",
                "not_relevant": "Not relevant",
            }
            reason = reason_labels.get(reason_code, reason_code)
            vote_emoji = "👍" if vote == "up" else "👎"
            message_id = query.message.message_id

            # Cancel any existing pending feedback for this tweet
            if tweet_id in self._pending_feedback:
                self._pending_feedback[tweet_id]["task"].cancel()

            # Show confirmation with undo button
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            f"{vote_emoji} {reason}", callback_data="voted"
                        ),
                        InlineKeyboardButton(
                            "↩ Undo", callback_data=f"undo:{tweet_id}"
                        ),
                    ]
                ])
            )

            # Schedule feedback save after 10 seconds
            async def _save_after_delay(
                t_id=tweet_id, v=vote, r=reason, m_id=message_id, emoji=vote_emoji
            ):
                await asyncio.sleep(10)
                if t_id not in self._pending_feedback:
                    return

                if self.feedback_callback:
                    try:
                        await self.feedback_callback(
                            tweet_id=t_id,
                            vote=v,
                            telegram_message_id=m_id,
                            notes=r,
                        )
                        logger.info(
                            f"Feedback recorded: tweet={t_id}, vote={v}, reason={r}"
                        )
                    except Exception as e:
                        logger.error(f"Error recording feedback: {e}")

                # Remove undo button
                try:
                    await self.application.bot.edit_message_reply_markup(
                        chat_id=self.chat_id,
                        message_id=m_id,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(
                                f"{emoji} {r}", callback_data="voted"
                            )]
                        ]),
                    )
                except Exception:
                    pass

                self._pending_feedback.pop(t_id, None)

            task = asyncio.create_task(_save_after_delay())
            self._pending_feedback[tweet_id] = {
                "task": task,
                "message_id": message_id,
            }

        # Handle like reason: "like_reason:{tweet_id}:{reason_code}"
        elif data.startswith("like_reason:"):
            parts = data.split(":")
            if len(parts) != 3:
                return

            _, tweet_id, reason_code = parts

            reason_labels = {
                "tech": "Tech content",
                "non_tech": "Non-tech insight",
                "soft_skills": "Soft skills",
                "life_wisdom": "Life wisdom",
            }
            reason = reason_labels.get(reason_code, reason_code)
            message_id = query.message.message_id

            # Cancel any existing pending feedback for this tweet
            if tweet_id in self._pending_feedback:
                self._pending_feedback[tweet_id]["task"].cancel()

            # Show confirmation with undo button
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            f"👍 {reason}", callback_data="voted"
                        ),
                        InlineKeyboardButton(
                            "↩ Undo", callback_data=f"like_undo:{tweet_id}"
                        ),
                    ]
                ])
            )

            # Schedule feedback save after 10 seconds
            async def _save_like_after_delay(
                t_id=tweet_id, r=reason, m_id=message_id
            ):
                await asyncio.sleep(10)
                if t_id not in self._pending_feedback:
                    return

                if self.feedback_callback:
                    try:
                        await self.feedback_callback(
                            tweet_id=t_id,
                            vote="up",
                            telegram_message_id=m_id,
                            notes=r,
                        )
                        logger.info(
                            f"Like feedback recorded: tweet={t_id}, reason={r}"
                        )
                    except Exception as e:
                        logger.error(f"Error recording like feedback: {e}")

                # Remove undo button
                try:
                    await self.application.bot.edit_message_reply_markup(
                        chat_id=self.chat_id,
                        message_id=m_id,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton(
                                f"👍 {r}", callback_data="voted"
                            )]
                        ]),
                    )
                except Exception:
                    pass

                self._pending_feedback.pop(t_id, None)

            task = asyncio.create_task(_save_like_after_delay())
            self._pending_feedback[tweet_id] = {
                "task": task,
                "message_id": message_id,
            }

        # Handle like undo: "like_undo:{tweet_id}"
        elif data.startswith("like_undo:"):
            parts = data.split(":")
            if len(parts) != 2:
                return

            _, tweet_id = parts

            if tweet_id not in self._pending_feedback:
                logger.info(f"Like undo too late: tweet={tweet_id}, already saved")
                return

            self._pending_feedback[tweet_id]["task"].cancel()
            del self._pending_feedback[tweet_id]

            await query.edit_message_reply_markup(
                reply_markup=self._make_like_reason_buttons(tweet_id)
            )
            logger.info(f"Like feedback undone: tweet={tweet_id}")

        # Handle undo: "undo:{tweet_id}"
        elif data.startswith("undo:"):
            parts = data.split(":")
            if len(parts) != 2:
                return

            _, tweet_id = parts

            if tweet_id not in self._pending_feedback:
                # Too late — feedback already saved
                logger.info(f"Undo too late: tweet={tweet_id}, already saved")
                return

            self._pending_feedback[tweet_id]["task"].cancel()
            del self._pending_feedback[tweet_id]

            username = self._tweet_authors.get(tweet_id, "unknown")
            await query.edit_message_reply_markup(
                reply_markup=self._make_tweet_buttons(tweet_id, username)
            )
            logger.info(f"Feedback undone: tweet={tweet_id}")

        # Handle favorite author: "fav:{username}:{tweet_id}"
        elif data.startswith("fav:"):
            parts = data.split(":")
            if len(parts) != 3:
                return

            _, username, tweet_id = parts

            state = None
            if self.favorite_author_callback:
                try:
                    state = await self.favorite_author_callback(username=username)
                    logger.info(f"Toggle favorite @{username} → {state}")
                except Exception as e:
                    logger.error(f"Error toggling favorite author: {e}")

            label = f"⭐ @{username}" if state == "favorited" else f"⭐ Author"
            try:
                await query.edit_message_reply_markup(
                    reply_markup=self._make_tweet_buttons(tweet_id, username, fav_label=label)
                )
            except telegram.error.BadRequest:
                pass

        # Handle mute author: "mute:{username}:{tweet_id}"
        elif data.startswith("mute:"):
            parts = data.split(":")
            if len(parts) != 3:
                return

            _, username, tweet_id = parts

            state = None
            if self.mute_author_callback:
                try:
                    state = await self.mute_author_callback(username=username)
                    logger.info(f"Toggle mute @{username} → {state}")
                except Exception as e:
                    logger.error(f"Error toggling mute author: {e}")

            label = f"🔇 @{username}" if state == "muted" else f"🔇 Mute"
            try:
                await query.edit_message_reply_markup(
                    reply_markup=self._make_tweet_buttons(tweet_id, username, mute_label=label)
                )
            except telegram.error.BadRequest:
                pass

    async def send_tweet(
        self,
        tweet: dict,
    ) -> Optional[int]:
        """Send a formatted tweet message with feedback buttons.

        Args:
            tweet: Tweet dictionary with:
                - tweet_id: Twitter ID
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
        message = self._format_tweet_message(tweet)

        # Store author mapping for undo functionality
        self._tweet_authors[tweet["tweet_id"]] = tweet["author_username"]

        # Create inline keyboard with feedback buttons
        keyboard = self._make_tweet_buttons(tweet["tweet_id"], tweet["author_username"])

        try:
            sent_message = await self.application.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                reply_markup=keyboard,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            logger.info(f"Sent tweet {tweet['tweet_id']} to Telegram")
            return sent_message.message_id
        except Exception as e:
            logger.error(f"Error sending tweet to Telegram: {e}")
            return None

    @staticmethod
    def _make_tweet_buttons(
        tweet_id: str,
        username: str,
        fav_label: str = "⭐ Author",
        mute_label: str = "🔇 Mute",
    ) -> InlineKeyboardMarkup:
        """Build the two-row inline keyboard for a tweet.

        Args:
            tweet_id: Twitter ID
            username: Author username
            fav_label: Label for favorite button
            mute_label: Label for mute button

        Returns:
            InlineKeyboardMarkup with two rows
        """
        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👍", callback_data=f"vote:{tweet_id}:up"),
                InlineKeyboardButton("👎", callback_data=f"vote:{tweet_id}:down"),
            ],
            [
                InlineKeyboardButton(fav_label, callback_data=f"fav:{username}:{tweet_id}"),
                InlineKeyboardButton(mute_label, callback_data=f"mute:{username}:{tweet_id}"),
            ],
        ])

    def _format_tweet_message(self, tweet: dict) -> str:
        """Format tweet for Telegram message.

        Args:
            tweet: Tweet dictionary

        Returns:
            Formatted message string (HTML)
        """
        score = tweet.get("filter_score", 0)
        reason = tweet.get("filter_reason", "")
        metrics = tweet.get("metrics", {})

        # Escape HTML special characters in text
        text = self._escape_html(tweet["text"])
        reason = self._escape_html(reason)

        likes = metrics.get("likes", 0)
        retweets = metrics.get("retweets", 0)
        replies = metrics.get("replies", 0)

        # Format engagement numbers
        likes_str = self._format_number(likes)
        retweets_str = self._format_number(retweets)
        replies_str = self._format_number(replies)

        message = (
            f"<b>Score:</b> {score}/100\n"
            f"<b>Why:</b> {reason}\n\n"
            f"<b>@{tweet['author_username']}</b> | "
            f"<a href=\"{tweet['url']}\">View Tweet</a>\n\n"
            f"{text}"
        )

        # Append quoted tweet block if present
        quoted = tweet.get("quoted_tweet")
        if quoted:
            qt_author = self._escape_html(quoted["author_username"])
            qt_text = self._escape_html(quoted["text"])
            qt_lines = qt_text.split("\n")
            qt_block = "\n".join(f"┃ {line}" for line in qt_lines)
            message += (
                f"\n\n┃ <b>@{qt_author}:</b>\n"
                f"{qt_block}"
            )

        message += f"\n\n❤️ {likes_str}  🔁 {retweets_str}  💬 {replies_str}"
        return message

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape HTML special characters.

        Args:
            text: Raw text

        Returns:
            HTML-escaped text
        """
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    @staticmethod
    def _format_number(num: int) -> str:
        """Format large numbers with K/M suffix.

        Args:
            num: Number to format

        Returns:
            Formatted string
        """
        if num >= 1_000_000:
            return f"{num / 1_000_000:.1f}M"
        elif num >= 1_000:
            return f"{num / 1_000:.1f}K"
        return str(num)

    async def send_daily_digest(
        self,
        tweets: list[dict],
        delay_seconds: float = 1.0,
    ) -> list[int]:
        """Send all filtered tweets with rate limiting.

        Args:
            tweets: List of filtered tweet dictionaries
            delay_seconds: Delay between messages (default 1s)

        Returns:
            List of sent Telegram message IDs
        """
        if not tweets:
            logger.warning("No tweets to send in digest")
            return []

        # Send header message
        await self.application.bot.send_message(
            chat_id=self.chat_id,
            text=f"📰 <b>Daily Tweet Digest</b>\n\n"
                 f"Found {len(tweets)} relevant tweets for you today.",
            parse_mode="HTML",
        )

        message_ids = []
        for i, tweet in enumerate(tweets):
            message_id = await self.send_tweet(tweet)
            if message_id:
                message_ids.append(message_id)

            # Rate limit
            if i < len(tweets) - 1:
                await asyncio.sleep(delay_seconds)

        logger.info(f"Sent {len(message_ids)} tweets in daily digest")
        return message_ids

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
        await self.application.updater.start_polling()

        # Keep running until interrupted
        try:
            while True:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
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
