"""Telegram bot for sending curated tweets and collecting feedback."""

import asyncio
import logging
from typing import Callable, Optional

import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

logger = logging.getLogger(__name__)


class TelegramCurator:
    """Telegram bot for tweet curation."""

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        feedback_callback: Optional[Callable] = None,
        favorite_author_callback: Optional[Callable] = None,
        mute_author_callback: Optional[Callable] = None,
    ):
        """Initialize Telegram bot.

        Args:
            bot_token: Telegram bot token from BotFather
            chat_id: Target chat ID to send messages to
            feedback_callback: Async callback function(tweet_id, vote, message_id)
            favorite_author_callback: Async callback function(username) → state string
            mute_author_callback: Async callback function(username) → state string
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.feedback_callback = feedback_callback
        self.favorite_author_callback = favorite_author_callback
        self.mute_author_callback = mute_author_callback
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
        logger.info("Telegram application initialized")

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
            "- I send you filtered tweets daily\n"
            "- Use the buttons to tell me what you like\n"
            "- Your feedback helps improve future curation"
        )

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
            await query.edit_message_reply_markup(
                reply_markup=self._make_tweet_buttons(tweet_id, username, fav_label=label)
            )

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
            await query.edit_message_reply_markup(
                reply_markup=self._make_tweet_buttons(tweet_id, username, mute_label=label)
            )

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

        return (
            f"<b>Score:</b> {score}/100\n"
            f"<b>Why:</b> {reason}\n\n"
            f"<b>@{tweet['author_username']}</b> | "
            f"<a href=\"{tweet['url']}\">View Tweet</a>\n\n"
            f"{text}\n\n"
            f"❤️ {likes_str}  🔁 {retweets_str}  💬 {replies_str}"
        )

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
