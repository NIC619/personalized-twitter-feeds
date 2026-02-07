"""Telegram bot for sending curated tweets and collecting feedback."""

import asyncio
import logging
from typing import Callable, Optional

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
    ):
        """Initialize Telegram bot.

        Args:
            bot_token: Telegram bot token from BotFather
            chat_id: Target chat ID to send messages to
            feedback_callback: Async callback function(tweet_id, vote, message_id)
            favorite_author_callback: Async callback function(username)
        """
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.feedback_callback = feedback_callback
        self.favorite_author_callback = favorite_author_callback
        self.application: Optional[Application] = None
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
        await query.answer()

        data = query.data

        # Handle vote: "vote:{tweet_id}:{up|down}"
        if data.startswith("vote:"):
            parts = data.split(":")
            if len(parts) != 3:
                return

            _, tweet_id, vote = parts

            # Update message to show vote recorded
            vote_emoji = "👍" if vote == "up" else "👎"
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"Voted: {vote_emoji}", callback_data="voted")]
                ])
            )

            # Call feedback callback if provided
            if self.feedback_callback:
                try:
                    await self.feedback_callback(
                        tweet_id=tweet_id,
                        vote=vote,
                        telegram_message_id=query.message.message_id,
                    )
                    logger.info(f"Feedback recorded: tweet={tweet_id}, vote={vote}")
                except Exception as e:
                    logger.error(f"Error recording feedback: {e}")

        # Handle favorite author: "fav:{username}:{tweet_id}"
        elif data.startswith("fav:"):
            parts = data.split(":")
            if len(parts) != 3:
                return

            _, username, tweet_id = parts

            # Update button to show author favorited
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "👍",
                            callback_data=f"vote:{tweet_id}:up"
                        ),
                        InlineKeyboardButton(
                            "👎",
                            callback_data=f"vote:{tweet_id}:down"
                        ),
                        InlineKeyboardButton(
                            f"⭐ @{username}",
                            callback_data="favorited"
                        ),
                    ]
                ])
            )

            # Call favorite author callback if provided
            if self.favorite_author_callback:
                try:
                    await self.favorite_author_callback(username=username)
                    logger.info(f"Favorite author recorded: @{username}")
                except Exception as e:
                    logger.error(f"Error recording favorite author: {e}")

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

        # Create inline keyboard with feedback buttons
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "👍",
                    callback_data=f"vote:{tweet['tweet_id']}:up"
                ),
                InlineKeyboardButton(
                    "👎",
                    callback_data=f"vote:{tweet['tweet_id']}:down"
                ),
                InlineKeyboardButton(
                    "⭐ Author",
                    callback_data=f"fav:{tweet['author_username']}:{tweet['tweet_id']}"
                ),
            ]
        ])

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
        """Start the bot in polling mode."""
        if not self.application:
            await self.initialize()

        logger.info("Starting Telegram bot polling...")
        await self.application.run_polling()

    async def shutdown(self) -> None:
        """Shutdown the bot gracefully."""
        if self.application:
            await self.application.shutdown()
            logger.info("Telegram bot shut down")
