"""Capture flow: anything the user sends becomes an item; /like; notes and typed reasons.

Mixin for :class:`src.telegram_bot.TelegramCurator`; it relies on the
attributes that class sets up in ``__init__``.
"""

import asyncio
import html
import logging
import re
from typing import Optional

import telegram.error
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler

from src.bot.constants import (
    BLOCKWORD_AWAITING_INPUT,
    LIKE_AWAITING_INPUT,
    NEWSLETTER_AWAITING_INPUT,
    STAR_AWAITING_INPUT,
    THREAD_AWAITING_INPUT,
    VOTE_GRACE_SECONDS,
)
from src.capture import USAGE, classify_message, forward_origin
from src.content import is_blog_content, is_http_url, is_tweet_url, is_twitter_profile_url
from src.feedback_flow import (
    Vote,
    parse_vote_callback,
    reason_done_text,
    reason_prompt_buttons,
    reason_prompt_text,
    vote_buttons,
    vote_from_reactions,
    vote_label,
    voted_buttons,
)
from src.webhook_server import redirect_url

logger = logging.getLogger(__name__)


class CaptureMixin:
    @staticmethod
    def _extract_item_id(arg: str) -> str | None:
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

    async def _handle_text_signals(self, update: Update) -> bool:
        """Typed reasons and reply-notes. Returns True if the message was consumed."""
        message = update.message
        text = (message.text or "").strip()
        chat_id = str(message.chat_id)
        if not text:
            return False

        replied = message.reply_to_message
        replied_id = replied.message_id if replied else None

        # 1. Reply to a reason prompt, or a typed reason after ✏️
        item_id = None
        if replied_id is not None and replied_id in self._reason_prompts:
            item_id = self._reason_prompts[replied_id]
        elif chat_id in self._awaiting_reason:
            item_id = self._awaiting_reason[chat_id]
        if item_id:
            self._awaiting_reason.pop(chat_id, None)
            await self._confirm_reason(item_id, text)
            await message.reply_text("✅ Reason saved.")
            return True

        # 2. Reply to a sent item → note
        if replied_id is not None:
            item_id = await self._item_for_message(replied_id)
            if item_id and self.note_callback:
                try:
                    await self.note_callback(item_id, text, replied_id)
                    await message.reply_text("📝 Note saved.")
                except Exception as e:
                    logger.error(f"Error saving note for {item_id}: {e}")
                    await message.reply_text("Error saving note.")
                return True
        return False

    async def _handle_default_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if await self._handle_text_signals(update):
            return
        await self._route_plain_message(update, context)

    async def _route_plain_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Default handler for plain messages (grammar in ``src.capture``)."""
        message = update.message
        if not message or not message.text:
            return

        origin = forward_origin(message)
        classified = classify_message(message.text, forwarded=origin is not None)
        await self._act_on(update, classified, origin)

    async def _act_on(self, update: Update, classified, origin: Optional[dict] = None) -> None:
        """Carry out a parsed instruction."""
        vote = Vote(classified.vote, 1) if classified.vote else None
        if classified.tweet_refs:
            await self._like_tweets(update, classified.tweet_refs, vote=vote, reason=classified.comment)
        for url in classified.urls:
            await self._capture_url(update, url, vote=vote, reason=classified.comment)
        if classified.usernames and self.favorite_author_callback:
            await self._star_authors(update, classified.usernames)
        if classified.text:
            await self._capture_text(update, classified.text, origin)
        if classified.unknown:
            await update.message.reply_text(USAGE)

    async def _capture_url(
        self, update: Update, url: str, vote: Optional[Vote] = None, reason: Optional[str] = None,
    ) -> None:
        """Store a non-tweet URL as a voted item (upvote by default)."""
        if not self.capture_url_callback:
            await self._like_blog_post(update, url)
            return
        await update.message.reply_text("Saving…")
        try:
            item = await self.capture_url_callback(url)
        except Exception as e:
            logger.error(f"Error capturing {url}: {e}")
            await update.message.reply_text("Error saving that link.")
            return
        if not item:
            await update.message.reply_text("Could not save that link.")
            return
        self._tweet_authors[item["item_id"]] = item.get("author_username")
        if not await self._send_liked_item(
            item["item_id"], self._format_item_message(item), vote=vote, reason=reason,
        ):
            await update.message.reply_text("Error sending item.")

    async def _capture_text(self, update: Update, text: str, origin: Optional[dict]) -> None:
        """Store free text / a forwarded post as a liked item."""
        if not self.capture_text_callback:
            await update.message.reply_text("Text capture not available.")
            return
        try:
            item = await self.capture_text_callback(text, origin)
        except Exception as e:
            logger.error(f"Error capturing text: {e}")
            await update.message.reply_text("Error saving that.")
            return
        if not item:
            await update.message.reply_text("Could not save that.")
            return
        self._tweet_authors[item["item_id"]] = item.get("author_username")
        if not await self._send_liked_item(item["item_id"], self._format_item_message(item)):
            await update.message.reply_text("Error sending item.")

    async def send_profile_update(self, written: dict[str, str]) -> None:
        """Announce a recompiled profile with its overall section."""
        if not self.application:
            return
        overall = written.get("overall", "")
        domains = ", ".join(d for d in written if d != "overall")
        text = (
            f"🧭 <b>Preference profile updated</b> ({domains})\n\n"
            f"{self._escape_html(overall[:3000])}"
        )
        await self.application.bot.send_message(
            chat_id=self.chat_id, text=text, parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def _handle_like(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle /like command to upvote a tweet or blog post.

        If called with args, processes immediately.
        If called without args, prompts and waits for input.
        """
        if not self.like_tweet_callback and not self.like_blog_callback:
            await update.message.reply_text("Like feature not available.")
            return ConversationHandler.END

        if not context.args:
            await update.message.reply_text(
                "Send me a tweet URL, blog post URL, or tweet ID to like.\n"
                "Words after the URL are saved as your reason.\n\n"
                "/cancel to abort."
            )
            return LIKE_AWAITING_INPUT

        await self._act_on(update, classify_message(":like " + " ".join(context.args)))
        return ConversationHandler.END

    async def _handle_like_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle follow-up message with tweet URLs/IDs after /like prompt."""
        text = update.message.text.strip()
        if not text:
            await update.message.reply_text("No input received. Try again or /cancel.")
            return LIKE_AWAITING_INPUT

        await self._act_on(update, classify_message(":like " + text))
        return ConversationHandler.END

    async def _handle_like_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle /cancel during like conversation."""
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    async def _like_tweets(
        self,
        update: Update,
        args: list[str],
        vote: Optional[Vote] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Fetch each tweet (URL or id), send it and record the vote (upvote by default)."""
        if not self.like_tweet_callback:
            await update.message.reply_text("Tweet lookup not available.")
            return
        for arg in args:
            # Check if this is a blog post URL (not a tweet URL)
            if is_http_url(arg) and not is_tweet_url(arg):
                await self._capture_url(update, arg, vote=vote, reason=reason)
                continue

            item_id = self._extract_item_id(arg)
            if not item_id:
                await update.message.reply_text(f"Could not parse ID from: {arg}")
                continue

            try:
                tweet = await self.like_tweet_callback(item_id)
            except Exception as e:
                logger.error(f"Error fetching tweet {item_id}: {e}")
                await update.message.reply_text(f"Error fetching tweet {item_id}.")
                continue

            if not tweet:
                await update.message.reply_text(f"Tweet {item_id} not found.")
                continue

            # Format message without score/reason
            message = self._format_like_message(tweet)

            # Store author mapping for undo functionality
            self._tweet_authors[item_id] = tweet["author_username"]

            if not await self._send_liked_item(item_id, message, vote=vote, reason=reason):
                await update.message.reply_text(f"Error sending tweet {item_id}.")

    async def _like_blog_post(self, update: Update, url: str) -> None:
        """Process a blog post URL for liking."""
        if not self.like_blog_callback:
            await update.message.reply_text("Blog post liking not available.")
            return

        await update.message.reply_text("Fetching and scoring blog post...")

        try:
            post = await self.like_blog_callback(url)
        except Exception as e:
            logger.error(f"Error fetching blog post {url}: {e}")
            await update.message.reply_text(f"Error fetching blog post.")
            return

        if not post:
            await update.message.reply_text("Could not fetch blog post.")
            return

        content_id = post["item_id"]
        message = self._format_blog_like_message(post)

        # Store author mapping for undo functionality
        self._tweet_authors[content_id] = post["author_username"]

        if not await self._send_liked_item(content_id, message):
            await update.message.reply_text("Error sending blog post.")
