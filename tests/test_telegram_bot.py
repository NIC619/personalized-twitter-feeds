"""Tests for TelegramCurator."""

import pytest

from src.telegram_bot import TelegramCurator


@pytest.fixture
def bot():
    """TelegramCurator without initializing the real Application."""
    return TelegramCurator(bot_token="fake:token", chat_id="12345")


# --- _escape_html ---

class TestEscapeHtml:
    def test_ampersand(self):
        assert TelegramCurator._escape_html("A & B") == "A &amp; B"

    def test_less_than(self):
        assert TelegramCurator._escape_html("a < b") == "a &lt; b"

    def test_greater_than(self):
        assert TelegramCurator._escape_html("a > b") == "a &gt; b"

    def test_all_together(self):
        assert TelegramCurator._escape_html("<b>A & B</b>") == "&lt;b&gt;A &amp; B&lt;/b&gt;"

    def test_no_special_chars(self):
        assert TelegramCurator._escape_html("hello world") == "hello world"


# --- _format_number ---

class TestFormatNumber:
    def test_small_number(self):
        assert TelegramCurator._format_number(42) == "42"

    def test_zero(self):
        assert TelegramCurator._format_number(0) == "0"

    def test_thousands(self):
        assert TelegramCurator._format_number(1500) == "1.5K"

    def test_exact_thousand(self):
        assert TelegramCurator._format_number(1000) == "1.0K"

    def test_millions(self):
        assert TelegramCurator._format_number(2_500_000) == "2.5M"

    def test_exact_million(self):
        assert TelegramCurator._format_number(1_000_000) == "1.0M"

    def test_below_thousand(self):
        assert TelegramCurator._format_number(999) == "999"


# --- _format_tweet_message ---

class TestFormatTweetMessage:
    def test_basic_formatting(self, bot, sample_tweet):
        msg = bot._format_tweet_message(sample_tweet)

        assert "<b>Score:</b> 0/100" in msg  # no filter_score set
        assert "@vitalikbuterin" in msg
        assert "View Tweet" in msg
        assert "New EIP proposal" in msg

    def test_html_escaping_in_text(self, bot):
        tweet = {
            "tweet_id": "1",
            "author_username": "test",
            "text": "x < y & y > z",
            "url": "https://twitter.com/test/status/1",
            "metrics": {"likes": 0, "retweets": 0, "replies": 0},
        }
        msg = bot._format_tweet_message(tweet)
        assert "x &lt; y &amp; y &gt; z" in msg

    def test_metric_display(self, bot, sample_tweet):
        msg = bot._format_tweet_message(sample_tweet)
        # 500 likes → "500", 120 retweets → "120", 45 replies → "45"
        assert "500" in msg
        assert "120" in msg
        assert "45" in msg

    def test_large_metric_formatting(self, bot):
        tweet = {
            "tweet_id": "1",
            "author_username": "test",
            "text": "hi",
            "url": "https://twitter.com/test/status/1",
            "metrics": {"likes": 15000, "retweets": 2_000_000, "replies": 50},
        }
        msg = bot._format_tweet_message(tweet)
        assert "15.0K" in msg
        assert "2.0M" in msg

    def test_quoted_tweet_displayed(self, bot, sample_quote_tweet):
        msg = bot._format_tweet_message(sample_quote_tweet)

        assert "┃" in msg
        assert "@vitalikbuterin" in msg
        assert "blob fee market" in msg
        assert "This is a great analysis!" in msg

    def test_no_quoted_tweet_no_quote_block(self, bot, sample_tweet):
        msg = bot._format_tweet_message(sample_tweet)

        assert "┃" not in msg

    def test_quoted_tweet_html_escaped(self, bot):
        tweet = {
            "tweet_id": "1",
            "author_username": "test",
            "text": "Quote this",
            "url": "https://twitter.com/test/status/1",
            "metrics": {"likes": 0, "retweets": 0, "replies": 0},
            "quoted_tweet": {
                "author_username": "original",
                "author_name": "Original",
                "text": "x < y & y > z",
                "tweet_id": "2",
            },
        }
        msg = bot._format_tweet_message(tweet)
        assert "x &lt; y &amp; y &gt; z" in msg

    def test_quoted_tweet_multiline_all_prefixed(self, bot):
        tweet = {
            "tweet_id": "1",
            "author_username": "test",
            "text": "Interesting",
            "url": "https://twitter.com/test/status/1",
            "metrics": {"likes": 0, "retweets": 0, "replies": 0},
            "quoted_tweet": {
                "author_username": "original",
                "author_name": "Original",
                "text": "Line one\nLine two\nLine three",
                "tweet_id": "2",
            },
        }
        msg = bot._format_tweet_message(tweet)
        assert "┃ Line one\n┃ Line two\n┃ Line three" in msg

    def test_score_and_reason(self, bot):
        tweet = {
            "tweet_id": "1",
            "author_username": "test",
            "text": "hello",
            "url": "https://twitter.com/test/status/1",
            "metrics": {},
            "filter_score": 88,
            "filter_reason": "Relevant EIP discussion",
        }
        msg = bot._format_tweet_message(tweet)
        assert "88/100" in msg
        assert "Relevant EIP discussion" in msg


# --- _format_stats_message ---

class TestFormatStatsMessage:
    def _make_stats(self, n):
        """Generate n dummy author stat entries."""
        return [
            {
                "author_username": f"author{i}",
                "up": i + 1,
                "down": 1,
                "weighted_up": float(i + 1),
                "weighted_down": 1.0,
                "weighted_score": (i + 1) / (i + 2),
                "avg_filter_score": 70.0 + i,
                "total_votes": i + 2,
                "is_favorite": i == 0,
                "is_muted": i == 1,
            }
            for i in range(n)
        ]

    def test_single_page(self):
        stats = self._make_stats(3)
        msg = TelegramCurator._format_stats_message(stats, page=1, per_page=15)

        assert "Page 1/1" in msg
        assert "<pre>" in msg
        assert "author0" in msg
        assert "author2" in msg

    def test_pagination(self):
        stats = self._make_stats(20)
        msg = TelegramCurator._format_stats_message(stats, page=2, per_page=10)

        assert "Page 2/2" in msg
        assert "author10" in msg
        assert "author0" not in msg

    def test_favorite_star_prefix(self):
        stats = self._make_stats(1)
        msg = TelegramCurator._format_stats_message(stats, page=1)
        assert "\u2b50" in msg  # star emoji

    def test_muted_icon_prefix(self):
        stats = self._make_stats(2)
        msg = TelegramCurator._format_stats_message(stats, page=1)
        assert "\U0001f507" in msg  # muted speaker emoji

    def test_total_votes_shown(self):
        stats = self._make_stats(2)
        total = sum(s["total_votes"] for s in stats)
        msg = TelegramCurator._format_stats_message(stats)
        assert f"Total votes: {total}" in msg


# --- _make_tweet_buttons ---

class TestMakeTweetButtons:
    def test_default_layout(self):
        markup = TelegramCurator._make_tweet_buttons("tweet123", "alice")

        rows = markup.inline_keyboard
        assert len(rows) == 2
        # Row 1: thumbs up, thumbs down
        assert rows[0][0].callback_data == "vote:tweet123:up"
        assert rows[0][1].callback_data == "vote:tweet123:down"
        # Row 2: fav, mute
        assert rows[1][0].callback_data == "fav:alice:tweet123"
        assert rows[1][1].callback_data == "mute:alice:tweet123"

    def test_custom_labels(self):
        markup = TelegramCurator._make_tweet_buttons(
            "t1", "bob", fav_label="Starred", mute_label="Silenced"
        )

        rows = markup.inline_keyboard
        assert rows[1][0].text == "Starred"
        assert rows[1][1].text == "Silenced"


# --- _extract_username ---

class TestExtractUsername:
    def test_plain_username(self):
        assert TelegramCurator._extract_username("vitalikbuterin") == "vitalikbuterin"

    def test_at_mention(self):
        assert TelegramCurator._extract_username("@VitalikButerin") == "vitalikbuterin"

    def test_twitter_profile_url(self):
        assert TelegramCurator._extract_username("https://twitter.com/VitalikButerin") == "vitalikbuterin"

    def test_x_profile_url(self):
        assert TelegramCurator._extract_username("https://x.com/elaboratequery") == "elaboratequery"

    def test_tweet_url(self):
        assert TelegramCurator._extract_username("https://twitter.com/alice/status/123456789") == "alice"

    def test_x_tweet_url(self):
        assert TelegramCurator._extract_username("https://x.com/Bob_dev/status/987654321") == "bob_dev"

    def test_www_prefix(self):
        assert TelegramCurator._extract_username("https://www.twitter.com/carol") == "carol"

    def test_uppercase_normalized(self):
        assert TelegramCurator._extract_username("ALICE") == "alice"
