"""Message and keyboard formatting for the Telegram bot.

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


class FormattingMixin:
    def _format_item_message(self, item: dict) -> str:
        """Format any item (tweet, article, forwarded post, note) for Telegram."""
        if item.get("content_type") == "tweet" and item.get("metrics"):
            return self._format_like_message(item)
        if item.get("article") or item.get("content_type") == "blog_post":
            return self._format_blog_like_message(item)
        author = self._escape_html(item.get("author_name") or item.get("author_username") or "note")
        header = f"<b>{author}</b>"
        if item.get("url"):
            header += f" | <a href=\"{self._link(item)}\">source</a>"
        domain = item.get("domain")
        if domain:
            header += f"  <i>#{self._escape_html(domain)}</i>"
        body = self._escape_html((item.get("text") or "")[:1200])
        return f"{header}\n\n{body}"

    def _format_thread_message(self, tweets: list[dict]) -> str:
        """Format a list of thread tweets into a single compiled message.

        Args:
            tweets: List of normalized tweet dicts, sorted chronologically

        Returns:
            Formatted HTML message string
        """
        n = len(tweets)
        author = tweets[0]["author_username"]
        last_tweet = tweets[-1]

        lines = [f"<b>Thread by @{author} ({n} tweets)</b>\n"]

        for i, tweet in enumerate(tweets, 1):
            text = self._escape_html(tweet["text"])
            lines.append(f"<b>[{i}/{n}]</b> {text}\n")

        lines.append(
            f"<a href=\"{last_tweet['url']}\">View on Twitter</a>"
        )

        return "\n".join(lines)

    def _format_blog_like_message(self, post: dict) -> str:
        """Format a blog post for like message (no score/reason header)."""
        article = post.get("article") or {}
        title = self._escape_html(article.get("title") or (post.get("text") or "")[:100])
        url = post["url"]
        source = post["author_username"]
        author_name = post.get("author_name", source)

        message = (
            f"<b>{self._escape_html(author_name)}</b> | "
            f"<a href=\"{self._link(post)}\">{source}</a>\n\n"
            f"<b>{title}</b>\n"
        )

        body = article.get("body", "")
        if body:
            preview = self._escape_html(body[:300])
            if len(body) > 300:
                preview += "..."
            message += f"\n{preview}"

        return message

    def _format_blog_scored_message(
        self,
        post: dict,
        position: Optional[tuple[int, int]] = None,
    ) -> str:
        """Format a blog post with score for newsletter digest."""
        article = post.get("article") or {}
        title = self._escape_html(article.get("title") or post.get("title") or (post.get("text") or "")[:100])
        source = post["author_username"]
        author_name = post.get("author_name", source)

        prefix = f"<b>[{position[0]}/{position[1]}]</b>\n" if position else ""
        if post.get("exploration"):
            prefix += "🎲 <i>Exploration: scored below threshold. Vote to tell me if I missed it.</i>\n"
        feed_title = (post.get("raw_data") or {}).get("feed_title")
        via = f" · via {self._escape_html(feed_title)}" if feed_title else ""
        message = (
            f"{prefix}"
            f"<b>{self._escape_html(author_name)}</b> | "
            f"<a href=\"{self._link(post)}\">{source}</a>{via}\n\n"
            f"<b>{title}</b>\n"
        )

        body = article.get("body", "")
        if body:
            preview = self._escape_html(body[:300])
            if len(body) > 300:
                preview += "..."
            message += f"\n{preview}"

        return message

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

        header = self._retweet_header(tweet) + self._quote_header(tweet)
        message = (
            f"<b>@{tweet['author_username']}</b> | "
            f"<a href=\"{self._link(tweet)}\">View Tweet</a>\n\n"
            f"{header}{text}"
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

    @staticmethod
    def _make_blockword_buttons(keywords: list[str]) -> InlineKeyboardMarkup:
        """Build one-button-per-keyword inline keyboard keyed by index."""
        buttons = [
            [InlineKeyboardButton(f"✖ {kw}", callback_data=f"bkrm:{i}")]
            for i, kw in enumerate(keywords)
        ]
        return InlineKeyboardMarkup(buttons)

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

    @staticmethod
    def _make_tweet_buttons(
        item_id: str,
        username: Optional[str],
        fav_label: str = "⭐ Author",
        mute_label: str = "🔇 Mute",
    ) -> InlineKeyboardMarkup:
        """Vote row (🔥 👍 👎) plus author controls when a username is known."""
        return vote_buttons(item_id, username, fav_label=fav_label, mute_label=mute_label)

    def _link(self, item: dict) -> str:
        """Tracking link for an item when configured, else its real URL."""
        return redirect_url(self.redirect_base_url, item["item_id"]) or item["url"]

    def _format_tweet_message(
        self,
        tweet: dict,
        position: Optional[tuple[int, int]] = None,
    ) -> str:
        """Format tweet for Telegram message.

        Args:
            tweet: Tweet dictionary
            position: Optional (index, total) for digest numbering, 1-indexed.

        Returns:
            Formatted message string (HTML)
        """
        metrics = tweet.get("metrics", {})

        # Escape HTML special characters in text
        text = self._escape_html(tweet["text"])

        likes = metrics.get("likes", 0)
        retweets = metrics.get("retweets", 0)
        replies = metrics.get("replies", 0)

        # Format engagement numbers
        likes_str = self._format_number(likes)
        retweets_str = self._format_number(retweets)
        replies_str = self._format_number(replies)

        prefix = f"<b>[{position[0]}/{position[1]}]</b>\n" if position else ""
        if tweet.get("exploration"):
            prefix += "🎲 <i>Exploration: scored below threshold. Vote to tell me if I missed it.</i>\n"
        message = (
            f"{prefix}"
            f"<b>@{tweet['author_username']}</b> | "
            f"<a href=\"{self._link(tweet)}\">View Tweet</a>\n\n"
            f"{self._retweet_header(tweet)}{self._quote_header(tweet)}"
        )

        # Show article info for X Articles
        article = tweet.get("article")
        if article:
            title = self._escape_html(article["title"])
            article_url = article.get("url")
            if article_url:
                message += f"<b>[Article]</b> <a href=\"{article_url}\">{title}</a>\n"
            else:
                message += f"<b>[Article]</b> {title}\n"
            body = article.get("body", "")
            if body:
                preview = self._escape_html(body[:300])
                if len(body) > 300:
                    preview += "..."
                message += f"{preview}\n"
        else:
            message += text

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

    def _retweet_header(self, tweet: dict) -> str:
        """Render a "🔁 via @original_author" header for retweets, else empty.

        Falls back to raw_data.retweeted_from for DB-retrieved tweets where the
        top-level field isn't reconstructed.
        """
        if not tweet.get("is_retweet"):
            return ""
        retweeted_from = tweet.get("retweeted_from")
        if not retweeted_from:
            raw = tweet.get("raw_data") or {}
            retweeted_from = raw.get("retweeted_from")
        if not retweeted_from:
            return "🔁 <i>retweet</i>\n\n"
        original_author = self._escape_html(retweeted_from.get("author_username", ""))
        return f"🔁 via <b>@{original_author}</b>\n\n"

    def _quote_header(self, tweet: dict) -> str:
        """Render a "💬 quoting @quoted_author" header for quote tweets, else empty."""
        quoted = tweet.get("quoted_tweet")
        if not quoted:
            return ""
        quoted_author = self._escape_html(quoted.get("author_username", ""))
        return f"💬 quoting <b>@{quoted_author}</b>\n\n"

    @staticmethod
    def _escape_html(text: str) -> str:
        """Escape HTML special characters.

        Args:
            text: Raw text

        Returns:
            HTML-escaped text
        """
        text = html.unescape(text)
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
