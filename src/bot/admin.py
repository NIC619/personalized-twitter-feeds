"""Author stars/mutes, keyword blocklist, thread fetching, stats.

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
from src.capture import classify_message, forward_origin
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


class AdminMixin:
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

    async def _handle_thread(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle /thread command to fetch and display a thread.

        If called with args, processes immediately.
        If called without args, prompts and waits for input.
        """
        if not self.thread_callback:
            await update.message.reply_text("Thread feature not available.")
            return ConversationHandler.END

        if not context.args:
            await update.message.reply_text(
                "Send me a tweet URL or ID (the last tweet in the thread).\n\n"
                "/cancel to abort."
            )
            return THREAD_AWAITING_INPUT

        await self._fetch_and_send_thread(update, context.args[0])
        return ConversationHandler.END

    async def _handle_thread_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle follow-up message with tweet URL/ID after /thread prompt."""
        arg = update.message.text.strip().split()[0] if update.message.text.strip() else ""
        if not arg:
            await update.message.reply_text("No input received. Try again or /cancel.")
            return THREAD_AWAITING_INPUT

        await self._fetch_and_send_thread(update, arg)
        return ConversationHandler.END

    async def _handle_thread_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle /cancel during thread conversation."""
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    async def _fetch_and_send_thread(self, update: Update, arg: str) -> None:
        """Fetch a thread and send it as a compiled message."""
        item_id = self._extract_item_id(arg)
        if not item_id:
            await update.message.reply_text(f"Could not parse tweet ID from: {arg}")
            return

        try:
            tweets = await self.thread_callback(item_id)
        except Exception as e:
            logger.error(f"Error fetching thread for {item_id}: {e}")
            await update.message.reply_text(f"Error fetching thread for tweet {item_id}.")
            return

        if not tweets:
            await update.message.reply_text(f"Could not find thread for tweet {item_id}.")
            return

        message = self._format_thread_message(tweets)

        try:
            await self.application.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"Error sending thread message for {item_id}: {e}")
            await update.message.reply_text(f"Error sending thread for tweet {item_id}.")

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

    async def _handle_blockword(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle /blockword command to add keyword(s) to the blocklist."""
        if not self.add_blocked_keyword_callback:
            await update.message.reply_text("Keyword blocking not available.")
            return ConversationHandler.END

        if context.args:
            await self._add_blocked_keywords(update, " ".join(context.args))
            return ConversationHandler.END

        await update.message.reply_text(
            "Send me the keyword(s) to block. One per line or comma-separated.\n"
            "Matching is whole-word, case-insensitive.\n"
            "Content from starred authors is exempt.\n\n"
            "/cancel to abort."
        )
        return BLOCKWORD_AWAITING_INPUT

    async def _handle_blockword_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle follow-up message with keyword(s) after /blockword prompt."""
        text = update.message.text.strip()
        if not text:
            await update.message.reply_text("No input received. Try again or /cancel.")
            return BLOCKWORD_AWAITING_INPUT

        await self._add_blocked_keywords(update, text)
        return ConversationHandler.END

    async def _handle_blockword_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle /cancel during blockword conversation."""
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    async def _add_blocked_keywords(self, update: Update, text: str) -> None:
        """Parse keyword input (one per line or comma-separated) and save each."""
        raw_parts = re.split(r"[\n,]", text)
        keywords = []
        seen = set()
        for part in raw_parts:
            kw = part.strip().lower()
            if kw and kw not in seen:
                keywords.append(kw)
                seen.add(kw)

        if not keywords:
            await update.message.reply_text("No valid keywords found.")
            return

        results = []
        for kw in keywords:
            try:
                await self.add_blocked_keyword_callback(kw)
                results.append(f"🚫 '{kw}' blocked")
            except Exception as e:
                logger.error(f"Error saving blocked keyword '{kw}': {e}")
                results.append(f"❌ '{kw}' — error")

        await update.message.reply_text("\n".join(results))

    async def _handle_blockwords(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /blockwords command to list blocked keywords."""
        if not self.list_blocked_keywords_callback:
            await update.message.reply_text("Keyword blocking not available.")
            return

        try:
            keywords = await self.list_blocked_keywords_callback()
        except Exception as e:
            logger.error(f"Error fetching blocked keywords: {e}")
            await update.message.reply_text("Error fetching blocked keywords.")
            return

        if not keywords:
            await update.message.reply_text(
                "No blocked keywords yet.\nUse /blockword to add some."
            )
            return

        chat_id = str(update.effective_chat.id)
        self._blockword_list[chat_id] = keywords
        await update.message.reply_text(
            f"🚫 <b>Blocked keywords</b> ({len(keywords)})\n"
            "Tap to remove.",
            reply_markup=self._make_blockword_buttons(keywords),
            parse_mode="HTML",
        )

    async def _handle_blockword_remove(self, query, data: str) -> None:
        """Handle tap on a blocked-keyword button: remove it and re-render."""
        chat_id = str(query.message.chat_id)
        keywords = self._blockword_list.get(chat_id)
        if keywords is None:
            await query.edit_message_text("Blocked keyword list expired. Run /blockwords again.")
            return

        try:
            idx = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            return
        if idx < 0 or idx >= len(keywords):
            return

        kw = keywords[idx]
        if self.remove_blocked_keyword_callback:
            try:
                await self.remove_blocked_keyword_callback(kw)
            except Exception as e:
                logger.error(f"Error removing blocked keyword '{kw}': {e}")
                return

        remaining = [k for k in keywords if k != kw]
        self._blockword_list[chat_id] = remaining

        if not remaining:
            del self._blockword_list[chat_id]
            await query.edit_message_text("All blocked keywords removed.")
            return

        try:
            await query.edit_message_reply_markup(
                reply_markup=self._make_blockword_buttons(remaining),
            )
        except telegram.error.BadRequest:
            pass
