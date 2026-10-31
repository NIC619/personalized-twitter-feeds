"""Newsletter extraction and RSS feed subscriptions.

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


class FeedsMixin:
    async def _handle_newsletter(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """Handle /newsletter: extract the links in an issue, score, send the good ones."""
        if not self.newsletter_callback:
            await update.message.reply_text("Newsletter feature not available.")
            return ConversationHandler.END
        if not context.args:
            await update.message.reply_text(
                "Send me the newsletter issue URL.\n\n/cancel to abort."
            )
            return NEWSLETTER_AWAITING_INPUT
        await self._process_newsletter(update, context.args[0])
        return ConversationHandler.END

    async def _handle_newsletter_input(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        arg = update.message.text.strip().split()[0] if update.message.text.strip() else ""
        if not arg:
            await update.message.reply_text("No input received. Try again or /cancel.")
            return NEWSLETTER_AWAITING_INPUT
        await self._process_newsletter(update, arg)
        return ConversationHandler.END

    async def _handle_newsletter_cancel(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        await update.message.reply_text("Cancelled.")
        return ConversationHandler.END

    async def _process_newsletter(self, update: Update, url: str) -> None:
        if not is_http_url(url):
            await update.message.reply_text(f"Not a URL: {url}")
            return
        await update.message.reply_text("Reading the issue and scoring its links…")
        try:
            result = await self.newsletter_callback(url)
        except Exception as e:
            logger.error(f"Error processing newsletter {url}: {e}", exc_info=True)
            await update.message.reply_text(f"Error processing newsletter: {e}")
            return
        if not result or not result.get("entries"):
            await update.message.reply_text("No linked items found in that page.")
            return
        items = result.get("items") or []
        await update.message.reply_text(
            f"Found {result['entries']} links; {len(items)} worth your time."
            + ("" if items else " Nothing passed the threshold.")
        )
        await self._send_scored_items(items)

    async def _send_item(self, item: dict, position: Optional[tuple[int, int]] = None) -> Optional[int]:
        """Send any scored item with the vote keyboard (articles use the article layout)."""
        if item.get("content_type") == "tweet":
            return await self.send_tweet(item, position=position)
        message = self._format_blog_scored_message(item, position=position)
        try:
            sent = await self.application.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                reply_markup=self._make_tweet_buttons(item["item_id"], item.get("author_username")),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"Error sending item {item['item_id']}: {e}")
            return None
        self._message_items[sent.message_id] = item["item_id"]
        return sent.message_id

    async def _send_scored_items(self, items: list[dict]) -> list[tuple[dict, int]]:
        """Send on-demand scored items (newsletter, inbox); persist message ids."""
        sent: list[tuple[dict, int]] = []
        for i, item in enumerate(items, 1):
            self._tweet_authors[item["item_id"]] = item.get("author_username")
            msg_id = await self._send_item(item, position=(i, len(items)))
            if msg_id:
                sent.append((item, msg_id))
                await self._remember_sent(item["item_id"], msg_id)
            if msg_id and i < len(items):
                await asyncio.sleep(0.5)
        return sent

    async def send_issue_digest(
        self, title: str, items: list[dict], total: int,
    ) -> list[tuple[dict, int]]:
        """One newsletter issue: a header line, then its items above threshold."""
        if not self.application:
            return []
        header = f"📧 <b>{self._escape_html(title)}</b>\n{len(items)} of {total} links worth your time."
        try:
            await self.application.bot.send_message(
                chat_id=self.chat_id, text=header, parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.error(f"Error sending issue header: {e}")
        return await self._send_scored_items(items)

    async def _handle_inbox(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """/inbox: process newsletter emails now instead of waiting for the daily run."""
        if not self.inbox_callback:
            await update.message.reply_text(
                "Email ingestion is not configured (set EMAIL_IMAP_HOST / USER / PASSWORD)."
            )
            return
        await update.message.reply_text("Checking the mailbox…")
        try:
            summary = await self.inbox_callback()
        except Exception as e:
            logger.error(f"Inbox check failed: {e}", exc_info=True)
            await update.message.reply_text(f"Inbox check failed: {e}")
            return
        if not summary.get("emails"):
            await update.message.reply_text("No new newsletter emails.")
            return
        await update.message.reply_text(
            f"Processed {summary['emails']} issue(s): {summary['entries']} links, "
            f"{summary['sent']} sent."
        )

    async def _handle_feed(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """/feed add URL | list | remove N-or-URL."""
        args = context.args or []
        sub = args[0].lower() if args else "list"
        if sub == "add" and len(args) >= 2:
            if not self.feed_add_callback:
                await update.message.reply_text("Feeds not available.")
                return
            for url in args[1:]:
                await update.message.reply_text(f"Looking for a feed at {url}…")
                try:
                    feed = await self.feed_add_callback(url)
                except Exception as e:
                    logger.error(f"Error adding feed {url}: {e}")
                    await update.message.reply_text(f"Error adding feed: {e}")
                    continue
                if not feed:
                    await update.message.reply_text(
                        f"No RSS/Atom feed found for {url}. Send me the feed URL directly if you know it."
                    )
                    continue
                await update.message.reply_text(
                    f"📡 Subscribed: <b>{self._escape_html(feed.get('title') or feed['url'])}</b>\n"
                    f"{self._escape_html(feed['url'])}\n"
                    f"{feed.get('entries', 0)} entries seen; new ones arrive with the daily digest.",
                    parse_mode="HTML", disable_web_page_preview=True,
                )
            return
        if sub == "remove" and len(args) >= 2:
            if not self.feed_remove_callback:
                await update.message.reply_text("Feeds not available.")
                return
            target = args[1]
            if target.isdigit():
                shown = self._feed_list.get(str(update.message.chat_id)) or []
                idx = int(target) - 1
                if idx < 0 or idx >= len(shown):
                    await update.message.reply_text("Run /feed list first, then /feed remove N.")
                    return
                target = shown[idx]["url"]
            try:
                await self.feed_remove_callback(target)
            except Exception as e:
                await update.message.reply_text(f"Error removing feed: {e}")
                return
            await update.message.reply_text(f"Removed {target}")
            return
        if sub in ("list", "ls") or not args:
            if not self.feed_list_callback:
                await update.message.reply_text("Feeds not available.")
                return
            feeds = await self.feed_list_callback()
            self._feed_list[str(update.message.chat_id)] = feeds
            if not feeds:
                await update.message.reply_text("No feeds yet. /feed add <site or feed URL>")
                return
            lines = [f"📡 <b>Feeds</b> ({len(feeds)})"]
            for i, f in enumerate(feeds, 1):
                err = f" ⚠️ {f['error_count']} errors" if f.get("error_count") else ""
                lines.append(f"{i}. {self._escape_html(f.get('title') or f['url'])}{err}\n   {self._escape_html(f['url'])}")
            lines.append("\n/feed add URL · /feed remove N")
            await update.message.reply_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
            return
        await update.message.reply_text("Usage: /feed add URL | /feed list | /feed remove N")
