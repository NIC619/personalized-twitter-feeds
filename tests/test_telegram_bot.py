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


# --- _extract_tweet_id ---

class TestExtractTweetId:
    def test_twitter_url(self):
        assert TelegramCurator._extract_tweet_id(
            "https://twitter.com/alice/status/123456789"
        ) == "123456789"

    def test_x_url(self):
        assert TelegramCurator._extract_tweet_id(
            "https://x.com/bob_dev/status/987654321"
        ) == "987654321"

    def test_www_prefix(self):
        assert TelegramCurator._extract_tweet_id(
            "https://www.twitter.com/alice/status/111"
        ) == "111"

    def test_raw_numeric_id(self):
        assert TelegramCurator._extract_tweet_id("123456789") == "123456789"

    def test_invalid_input_returns_none(self):
        assert TelegramCurator._extract_tweet_id("not_a_tweet") is None

    def test_profile_url_returns_none(self):
        assert TelegramCurator._extract_tweet_id("https://twitter.com/alice") is None

    def test_empty_string_returns_none(self):
        assert TelegramCurator._extract_tweet_id("") is None


# --- like reason callback ---

class TestLikeReasonCallback:
    @pytest.fixture
    def bot_with_feedback(self):
        async def fake_feedback(**kwargs):
            pass
        return TelegramCurator(
            bot_token="fake:token",
            chat_id="12345",
            feedback_callback=fake_feedback,
        )

    @pytest.mark.asyncio
    async def test_like_reason_shows_undo_and_schedules_save(self, bot_with_feedback):
        from unittest.mock import AsyncMock, MagicMock

        feedback_mock = AsyncMock()
        bot_with_feedback.feedback_callback = feedback_mock

        query = AsyncMock()
        query.data = "like_reason:42:tech"
        query.message.message_id = 100
        query.answer = AsyncMock()
        query.edit_message_reply_markup = AsyncMock()

        update = MagicMock()
        update.callback_query = query
        context = MagicMock()

        await bot_with_feedback._handle_feedback(update, context)

        # Feedback should NOT be called immediately (delayed by 10s)
        feedback_mock.assert_not_awaited()

        # Should show confirmation + undo button
        query.edit_message_reply_markup.assert_awaited_once()
        call_kwargs = query.edit_message_reply_markup.call_args[1]
        buttons = call_kwargs["reply_markup"].inline_keyboard[0]
        assert buttons[0].text == "👍 Tech content"
        assert buttons[1].text == "↩ Undo"
        assert buttons[1].callback_data == "like_undo:42"

        # Pending feedback should be tracked
        assert "42" in bot_with_feedback._pending_feedback

        # Clean up the scheduled task
        bot_with_feedback._pending_feedback["42"]["task"].cancel()

    @pytest.mark.asyncio
    async def test_like_undo_cancels_pending_feedback(self, bot_with_feedback):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        # First, trigger a like reason to create pending feedback
        feedback_mock = AsyncMock()
        bot_with_feedback.feedback_callback = feedback_mock

        query1 = AsyncMock()
        query1.data = "like_reason:42:tech"
        query1.message.message_id = 100
        query1.answer = AsyncMock()
        query1.edit_message_reply_markup = AsyncMock()

        update1 = MagicMock()
        update1.callback_query = query1
        context = MagicMock()

        await bot_with_feedback._handle_feedback(update1, context)
        assert "42" in bot_with_feedback._pending_feedback

        # Now trigger undo
        query2 = AsyncMock()
        query2.data = "like_undo:42"
        query2.message.message_id = 100
        query2.answer = AsyncMock()
        query2.edit_message_reply_markup = AsyncMock()

        update2 = MagicMock()
        update2.callback_query = query2

        await bot_with_feedback._handle_feedback(update2, context)

        # Pending feedback should be removed
        assert "42" not in bot_with_feedback._pending_feedback

        # Should restore reason buttons
        query2.edit_message_reply_markup.assert_awaited_once()
        call_kwargs = query2.edit_message_reply_markup.call_args[1]
        rows = call_kwargs["reply_markup"].inline_keyboard
        assert len(rows) == 2
        assert rows[0][0].callback_data == "like_reason:42:tech"
        assert rows[0][1].callback_data == "like_reason:42:non_tech"
        assert rows[1][0].callback_data == "like_reason:42:soft_skills"
        assert rows[1][1].callback_data == "like_reason:42:life_wisdom"

        # Feedback should never have been saved
        feedback_mock.assert_not_awaited()


# --- _format_thread_message ---

class TestFormatThreadMessage:
    def _make_thread_tweets(self, n):
        """Generate n tweet dicts for a thread."""
        return [
            {
                "tweet_id": str(i),
                "author_username": "alice",
                "author_name": "Alice",
                "text": f"Tweet number {i}",
                "created_at": f"2025-01-15T10:{i:02d}:00+00:00",
                "is_retweet": False,
                "quoted_tweet": None,
                "metrics": {"likes": 0, "retweets": 0, "replies": 0, "views": 0},
                "url": f"https://twitter.com/alice/status/{i}",
                "raw_data": {},
            }
            for i in range(1, n + 1)
        ]

    def test_header_shows_author_and_count(self, bot):
        tweets = self._make_thread_tweets(3)
        msg = bot._format_thread_message(tweets)

        assert "Thread by @alice (3 tweets)" in msg

    def test_tweets_numbered(self, bot):
        tweets = self._make_thread_tweets(3)
        msg = bot._format_thread_message(tweets)

        assert "[1/3]" in msg
        assert "[2/3]" in msg
        assert "[3/3]" in msg

    def test_tweet_text_included(self, bot):
        tweets = self._make_thread_tweets(2)
        msg = bot._format_thread_message(tweets)

        assert "Tweet number 1" in msg
        assert "Tweet number 2" in msg

    def test_footer_link_to_last_tweet(self, bot):
        tweets = self._make_thread_tweets(3)
        msg = bot._format_thread_message(tweets)

        assert "https://twitter.com/alice/status/3" in msg
        assert "View on Twitter" in msg

    def test_html_escaping(self, bot):
        tweets = self._make_thread_tweets(1)
        tweets[0]["text"] = "x < y & y > z"
        msg = bot._format_thread_message(tweets)

        assert "x &lt; y &amp; y &gt; z" in msg

    def test_single_tweet_thread(self, bot):
        tweets = self._make_thread_tweets(1)
        msg = bot._format_thread_message(tweets)

        assert "1 tweets" in msg
        assert "[1/1]" in msg


# --- _parse_ab_report_args ---

class TestParseAbReportArgs:
    def test_no_args_uses_default_experiment(self):
        exp, threshold = TelegramCurator._parse_ab_report_args([], "exp_003")
        assert exp == "exp_003"
        assert threshold == 70

    def test_explicit_experiment(self):
        exp, threshold = TelegramCurator._parse_ab_report_args(["exp_001"], "exp_003")
        assert exp == "exp_001"
        assert threshold == 70

    def test_experiment_and_threshold(self):
        exp, threshold = TelegramCurator._parse_ab_report_args(
            ["exp_001", "50"], "exp_003"
        )
        assert exp == "exp_001"
        assert threshold == 50

    def test_bare_threshold_uses_default_experiment(self):
        exp, threshold = TelegramCurator._parse_ab_report_args(["60"], "exp_003")
        assert exp == "exp_003"
        assert threshold == 60

    def test_no_args_no_default_raises(self):
        with pytest.raises(ValueError, match="No experiment configured"):
            TelegramCurator._parse_ab_report_args([], "")

    def test_bad_threshold_raises(self):
        with pytest.raises(ValueError, match="Invalid threshold"):
            TelegramCurator._parse_ab_report_args(["exp_001", "abc"], "exp_003")


# --- _format_ab_info_message ---

class TestFormatAbInfoMessage:
    CONFIG = {
        "enabled": True,
        "experiment_id": "exp_003",
        "challenger_prompt": "V4",
    }
    EXPERIMENTS = [
        {
            "experiment_id": "exp_003",
            "control_prompt": "V1/V2",
            "challenger_prompt": "V4",
            "pairs": 42,
            "first_scored": "2026-06-01T08:00:00+00:00",
            "last_scored": "2026-07-15T08:00:00+00:00",
        },
        {
            "experiment_id": "exp_002",
            "control_prompt": "V1",
            "challenger_prompt": "V3",
            "pairs": 30,
            "first_scored": "2026-04-01T08:00:00+00:00",
            "last_scored": "2026-05-20T08:00:00+00:00",
        },
    ]

    def test_current_config_shown(self):
        msg = TelegramCurator._format_ab_info_message(self.CONFIG, self.EXPERIMENTS)
        assert "exp_003" in msg
        assert "Challenger: V4" in msg

    def test_experiments_listed_with_pairs_and_dates(self):
        msg = TelegramCurator._format_ab_info_message(self.CONFIG, self.EXPERIMENTS)
        assert "exp_002" in msg
        assert "42 pairs" in msg
        assert "2026-06-01 → 2026-07-15" in msg

    def test_prompt_descriptions_included(self):
        msg = TelegramCurator._format_ab_info_message(self.CONFIG, self.EXPERIMENTS)
        assert "V4: Interests-only + RAG context" in msg

    def test_disabled_config(self):
        msg = TelegramCurator._format_ab_info_message({"enabled": False}, [])
        assert "disabled" in msg
        assert "No A/B test scores recorded yet." in msg


class TestFormatAbInfoRagAndControl:
    def test_pinned_control_shown(self):
        config = {
            "enabled": True,
            "experiment_id": "exp_004",
            "challenger_prompt": "V6",
            "control_prompt": "V5",
            "rag_enabled": True,
        }
        msg = TelegramCurator._format_ab_info_message(config, [])
        assert "Control: V5" in msg
        assert "set via CONTROL_PROMPT" in msg

    def test_rag_enabled_shown(self):
        config = {"enabled": False, "rag_enabled": True}
        msg = TelegramCurator._format_ab_info_message(config, [])
        assert "RAG: enabled" in msg
        assert "⚠️" not in msg

    def test_rag_disabled_warns_for_rag_challenger(self):
        config = {
            "enabled": True,
            "experiment_id": "exp_004",
            "challenger_prompt": "V5",
            "rag_enabled": False,
        }
        msg = TelegramCurator._format_ab_info_message(config, [])
        assert "RAG: disabled" in msg
        assert "⚠️ V5 expects RAG context" in msg

    def test_rag_disabled_warns_for_pinned_rag_control(self):
        config = {
            "enabled": False,
            "control_prompt": "V2",
            "rag_enabled": False,
        }
        msg = TelegramCurator._format_ab_info_message(config, [])
        assert "⚠️ V2 expects RAG context" in msg

    def test_rag_disabled_no_warning_for_non_rag_prompts(self):
        config = {
            "enabled": True,
            "experiment_id": "exp_004",
            "challenger_prompt": "V3",
            "control_prompt": "V1",
            "rag_enabled": False,
        }
        msg = TelegramCurator._format_ab_info_message(config, [])
        assert "⚠️" not in msg
