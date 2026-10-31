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

        assert "<b>Score:</b>" not in msg
        assert "@vitalikbuterin" in msg
        assert "View Tweet" in msg
        assert "New EIP proposal" in msg

    def test_html_escaping_in_text(self, bot):
        tweet = {
            "item_id": "1",
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
            "item_id": "1",
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
            "item_id": "1",
            "author_username": "test",
            "text": "Quote this",
            "url": "https://twitter.com/test/status/1",
            "metrics": {"likes": 0, "retweets": 0, "replies": 0},
            "quoted_tweet": {
                "author_username": "original",
                "author_name": "Original",
                "text": "x < y & y > z",
                "item_id": "2",
            },
        }
        msg = bot._format_tweet_message(tweet)
        assert "x &lt; y &amp; y &gt; z" in msg

    def test_quoted_tweet_multiline_all_prefixed(self, bot):
        tweet = {
            "item_id": "1",
            "author_username": "test",
            "text": "Interesting",
            "url": "https://twitter.com/test/status/1",
            "metrics": {"likes": 0, "retweets": 0, "replies": 0},
            "quoted_tweet": {
                "author_username": "original",
                "author_name": "Original",
                "text": "Line one\nLine two\nLine three",
                "item_id": "2",
            },
        }
        msg = bot._format_tweet_message(tweet)
        assert "┃ Line one\n┃ Line two\n┃ Line three" in msg

    def test_score_and_reason_omitted(self, bot):
        tweet = {
            "item_id": "1",
            "author_username": "test",
            "text": "hello",
            "url": "https://twitter.com/test/status/1",
            "metrics": {},
            "filter_score": 88,
            "filter_reason": "Relevant EIP discussion",
        }
        msg = bot._format_tweet_message(tweet)
        assert "88/100" not in msg
        assert "Relevant EIP discussion" not in msg


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
        # Row 1: strong like, like, dislike
        assert rows[0][0].callback_data == "vote:tweet123:up:2"
        assert rows[0][1].callback_data == "vote:tweet123:up:1"
        assert rows[0][2].callback_data == "vote:tweet123:down:1"
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

    def test_no_author_row_for_blog_posts(self):
        markup = TelegramCurator._make_tweet_buttons("blog_x", None)
        assert len(markup.inline_keyboard) == 1


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


# --- _extract_item_id ---

class TestExtractTweetId:
    def test_twitter_url(self):
        assert TelegramCurator._extract_item_id(
            "https://twitter.com/alice/status/123456789"
        ) == "123456789"

    def test_x_url(self):
        assert TelegramCurator._extract_item_id(
            "https://x.com/bob_dev/status/987654321"
        ) == "987654321"

    def test_www_prefix(self):
        assert TelegramCurator._extract_item_id(
            "https://www.twitter.com/alice/status/111"
        ) == "111"

    def test_raw_numeric_id(self):
        assert TelegramCurator._extract_item_id("123456789") == "123456789"

    def test_invalid_input_returns_none(self):
        assert TelegramCurator._extract_item_id("not_a_tweet") is None

    def test_profile_url_returns_none(self):
        assert TelegramCurator._extract_item_id("https://twitter.com/alice") is None

    def test_empty_string_returns_none(self):
        assert TelegramCurator._extract_item_id("") is None


# --- vote flow ---

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from src.feedback_flow import Vote


def _query(data, message_id=100, chat_id=12345):
    query = AsyncMock()
    query.data = data
    query.message.message_id = message_id
    query.message.chat_id = chat_id
    query.answer = AsyncMock()
    query.edit_message_reply_markup = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    return update, query


@pytest.fixture
def vote_bot():
    bot = TelegramCurator(
        bot_token="fake:token",
        chat_id="12345",
        feedback_callback=AsyncMock(),
        reason_callback=AsyncMock(return_value="deep dive on preconfs"),
        reason_confirm_callback=AsyncMock(),
        retract_vote_callback=AsyncMock(),
        note_callback=AsyncMock(),
        lookup_message_callback=AsyncMock(return_value=None),
    )
    bot.application = MagicMock()
    bot.application.bot = AsyncMock()
    bot.application.bot.send_message = AsyncMock(
        return_value=SimpleNamespace(message_id=555)
    )
    bot._tweet_authors["42"] = "alice"
    return bot


async def _settle(bot):
    """Let the bot's background tasks (reason proposals) finish."""
    import asyncio
    if bot._tasks:
        await asyncio.gather(*list(bot._tasks), return_exceptions=True)


class TestVoteButtons:
    @pytest.mark.asyncio
    async def test_vote_shows_undo_schedules_save_and_proposes_reason(self, vote_bot):
        update, query = _query("vote:42:up:2")

        await vote_bot._handle_feedback(update, MagicMock())
        vote_bot.reason_callback.assert_not_awaited()  # proposal runs in the background
        await _settle(vote_bot)

        vote_bot.feedback_callback.assert_not_awaited()  # grace period
        row = query.edit_message_reply_markup.call_args[1]["reply_markup"].inline_keyboard[0]
        assert row[0].text == "🔥"
        assert row[1].callback_data == "undo:42"
        assert "42" in vote_bot._pending_feedback

        vote_bot.reason_callback.assert_awaited_once_with("42", "up", 2)
        kwargs = vote_bot.application.bot.send_message.call_args[1]
        assert "deep dive on preconfs" in kwargs["text"]
        assert kwargs["reply_to_message_id"] == 100
        assert vote_bot._reason_prompts[555] == "42"

        vote_bot._pending_feedback["42"]["task"].cancel()

    @pytest.mark.asyncio
    async def test_undo_during_slow_proposal_is_not_blocked(self, vote_bot):
        import asyncio
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_reason(*_):
            started.set()
            await release.wait()
            return "late guess"
        vote_bot.reason_callback = AsyncMock(side_effect=slow_reason)

        update, _ = _query("vote:42:up:1")
        await vote_bot._handle_feedback(update, MagicMock())
        await started.wait()
        update2, _ = _query("undo:42")
        await vote_bot._handle_feedback(update2, MagicMock())  # returns while proposal still runs
        assert "42" not in vote_bot._pending_feedback
        release.set()
        await _settle(vote_bot)
        # The late proposal is dropped: no prompt for an undone vote
        vote_bot.application.bot.send_message.assert_not_awaited()
        vote_bot.feedback_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_undo_then_same_vote_drops_stale_proposal(self, vote_bot):
        import asyncio
        gate = asyncio.Event()
        calls = 0

        async def reason(*_):
            nonlocal calls
            calls += 1
            if calls == 1:
                await gate.wait()
                return "stale guess"
            return "fresh guess"
        vote_bot.reason_callback = AsyncMock(side_effect=reason)

        u1, _ = _query("vote:42:up:1")
        await vote_bot._handle_feedback(u1, MagicMock())
        await asyncio.sleep(0)
        u2, _ = _query("undo:42")
        await vote_bot._handle_feedback(u2, MagicMock())
        u3, _ = _query("vote:42:up:1")
        await vote_bot._handle_feedback(u3, MagicMock())
        gate.set()
        await _settle(vote_bot)

        texts = [c[1]["text"] for c in vote_bot.application.bot.send_message.call_args_list]
        assert any("fresh guess" in t for t in texts)
        assert not any("stale guess" in t for t in texts)
        assert vote_bot._item_prompts["42"][1] == "fresh guess"
        vote_bot._pending_feedback["42"]["task"].cancel()

    @pytest.mark.asyncio
    async def test_liked_item_message_id_is_persisted(self, vote_bot):
        vote_bot.mark_sent_callback = AsyncMock()
        assert await vote_bot._send_liked_item("42", "hello")
        vote_bot.mark_sent_callback.assert_awaited_once_with("42", 555)
        assert vote_bot._message_items[555] == "42"
        vote_bot._pending_feedback["42"]["task"].cancel()
        await _settle(vote_bot)

    @pytest.mark.asyncio
    async def test_liked_item_with_reason_skips_proposal(self, vote_bot):
        assert await vote_bot._send_liked_item("42", "hello", reason="great read")
        await _settle(vote_bot)
        vote_bot.reason_callback.assert_not_awaited()
        assert vote_bot._pending_reasons["42"] == "great read"
        vote_bot._pending_feedback["42"]["task"].cancel()

    @pytest.mark.asyncio
    async def test_undo_cancels_vote_and_deletes_prompt(self, vote_bot):
        update, _ = _query("vote:42:down:1")
        await vote_bot._handle_feedback(update, MagicMock())
        await _settle(vote_bot)

        update2, query2 = _query("undo:42")
        await vote_bot._handle_feedback(update2, MagicMock())

        assert "42" not in vote_bot._pending_feedback
        assert "42" not in vote_bot._item_prompts
        vote_bot.application.bot.delete_message.assert_awaited_once()
        rows = query2.edit_message_reply_markup.call_args[1]["reply_markup"].inline_keyboard
        assert rows[0][1].callback_data == "vote:42:up:1"
        vote_bot.feedback_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reason_ok_before_save_is_carried_into_save(self, vote_bot):
        update, _ = _query("vote:42:up:1")
        await vote_bot._handle_feedback(update, MagicMock())
        await _settle(vote_bot)

        ok_update, _ = _query("rsn_ok:42", message_id=555)
        await vote_bot._handle_feedback(ok_update, MagicMock())

        # Not saved yet, so the reason is parked for the delayed save
        assert vote_bot._pending_reasons["42"] == "deep dive on preconfs"
        vote_bot.reason_confirm_callback.assert_not_awaited()
        vote_bot.application.bot.edit_message_text.assert_awaited_once()
        vote_bot._pending_feedback["42"]["task"].cancel()

    @pytest.mark.asyncio
    async def test_reason_ok_after_save_updates_db(self, vote_bot):
        vote_bot._item_prompts["42"] = (555, "guess")
        vote_bot._reason_prompts[555] = "42"
        vote_bot._item_votes["42"] = Vote("up", 1)

        ok_update, _ = _query("rsn_ok:42", message_id=555)
        await vote_bot._handle_feedback(ok_update, MagicMock())

        vote_bot.reason_confirm_callback.assert_awaited_once_with("42", "guess")
        assert 555 not in vote_bot._reason_prompts

    @pytest.mark.asyncio
    async def test_reason_edit_then_typed_reply(self, vote_bot):
        vote_bot._item_prompts["42"] = (555, "guess")
        vote_bot._reason_prompts[555] = "42"

        edit_update, query = _query("rsn_edit:42", message_id=555)
        await vote_bot._handle_feedback(edit_update, MagicMock())
        assert vote_bot._awaiting_reason["12345"] == "42"

        msg = MagicMock()
        msg.text = "because it quantifies L1 inclusion delay"
        msg.chat_id = 12345
        msg.reply_to_message = None
        msg.reply_text = AsyncMock()
        update = MagicMock(); update.message = msg

        consumed = await vote_bot._handle_text_signals(update)

        assert consumed
        vote_bot.reason_confirm_callback.assert_awaited_once_with(
            "42", "because it quantifies L1 inclusion delay"
        )
        assert "12345" not in vote_bot._awaiting_reason

    @pytest.mark.asyncio
    async def test_reply_to_item_saves_note(self, vote_bot):
        vote_bot._message_items[100] = "42"
        msg = MagicMock()
        msg.text = "revisit when the spec lands"
        msg.chat_id = 12345
        msg.reply_to_message = SimpleNamespace(message_id=100)
        msg.reply_text = AsyncMock()
        update = MagicMock(); update.message = msg

        assert await vote_bot._handle_text_signals(update)
        vote_bot.note_callback.assert_awaited_once_with("42", "revisit when the spec lands", 100)

    @pytest.mark.asyncio
    async def test_plain_text_not_consumed(self, vote_bot):
        msg = MagicMock()
        msg.text = "@someone"
        msg.chat_id = 12345
        msg.reply_to_message = None
        update = MagicMock(); update.message = msg
        assert not await vote_bot._handle_text_signals(update)


class TestReactions:
    def _reaction_update(self, message_id, new, old, chat_id=12345):
        mr = SimpleNamespace(
            chat=SimpleNamespace(id=chat_id),
            message_id=message_id,
            new_reaction=[SimpleNamespace(emoji=e) for e in new],
            old_reaction=[SimpleNamespace(emoji=e) for e in old],
        )
        update = MagicMock(); update.message_reaction = mr
        return update

    @pytest.mark.asyncio
    async def test_reaction_votes_immediately(self, vote_bot):
        vote_bot._message_items[100] = "42"

        await vote_bot._handle_reaction(self._reaction_update(100, ["🔥"], []), MagicMock())
        await _settle(vote_bot)

        vote_bot.feedback_callback.assert_awaited_once()
        kwargs = vote_bot.feedback_callback.call_args[1]
        assert (kwargs["vote"], kwargs["strength"], kwargs["source"]) == ("up", 2, "reaction")
        assert "42" not in vote_bot._pending_feedback
        vote_bot.reason_callback.assert_awaited_once_with("42", "up", 2)

    @pytest.mark.asyncio
    async def test_reaction_removal_retracts(self, vote_bot):
        vote_bot._message_items[100] = "42"
        vote_bot._item_votes["42"] = Vote("up", 1)

        await vote_bot._handle_reaction(self._reaction_update(100, [], ["👍"]), MagicMock())

        vote_bot.retract_vote_callback.assert_awaited_once_with("42")
        assert "42" not in vote_bot._item_votes
        vote_bot.feedback_callback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reaction_change_retracts_then_votes(self, vote_bot):
        vote_bot._message_items[100] = "42"
        vote_bot._item_votes["42"] = Vote("up", 1)

        await vote_bot._handle_reaction(self._reaction_update(100, ["👎"], ["👍"]), MagicMock())

        vote_bot.retract_vote_callback.assert_awaited_once_with("42")
        assert vote_bot.feedback_callback.call_args[1]["vote"] == "down"

    @pytest.mark.asyncio
    async def test_change_after_restart_uses_old_reaction(self, vote_bot):
        """Persisted 👍, bot restarted (no in-memory vote), user switches to 👎."""
        vote_bot._message_items[100] = "42"
        assert "42" not in vote_bot._item_votes

        await vote_bot._handle_reaction(self._reaction_update(100, ["👎"], ["👍"]), MagicMock())

        vote_bot.retract_vote_callback.assert_awaited_once_with("42")
        assert vote_bot.feedback_callback.call_args[1]["vote"] == "down"

    @pytest.mark.asyncio
    async def test_unknown_message_falls_back_to_lookup(self, vote_bot):
        vote_bot.lookup_message_callback = AsyncMock(
            return_value={"item_id": "77", "author_username": "bob"}
        )
        await vote_bot._handle_reaction(self._reaction_update(900, ["👍"], []), MagicMock())
        vote_bot.lookup_message_callback.assert_awaited_once_with(900)
        assert vote_bot.feedback_callback.call_args[1]["item_id"] == "77"

    @pytest.mark.asyncio
    async def test_other_chat_ignored(self, vote_bot):
        vote_bot._message_items[100] = "42"
        await vote_bot._handle_reaction(
            self._reaction_update(100, ["👍"], [], chat_id=999), MagicMock()
        )
        vote_bot.feedback_callback.assert_not_awaited()


class TestPlainMessages:
    def _update(self, text, forward=None):
        msg = MagicMock()
        msg.text = text
        msg.chat_id = 12345
        msg.reply_to_message = None
        msg.forward_origin = forward
        msg.reply_text = AsyncMock()
        update = MagicMock(); update.message = msg
        return update

    @pytest.mark.asyncio
    async def test_url_with_words_is_liked_with_reason(self, vote_bot):
        vote_bot.capture_url_callback = AsyncMock(return_value={
            "item_id": "blog_1", "author_username": "example.com", "text": "Post", "url": "https://example.com/p",
            "content_type": "blog_post",
        })
        update = self._update("great read https://example.com/p the preconf section")
        await vote_bot._handle_default_message(update, MagicMock())
        await _settle(vote_bot)

        vote_bot.capture_url_callback.assert_awaited_once_with("https://example.com/p")
        assert vote_bot._pending_reasons["blog_1"] == "great read the preconf section"
        vote_bot.reason_callback.assert_not_awaited()
        vote_bot._pending_feedback["blog_1"]["task"].cancel()

    @pytest.mark.asyncio
    async def test_dislike_verb_records_downvote(self, vote_bot):
        vote_bot.capture_url_callback = AsyncMock(return_value={
            "item_id": "blog_2", "author_username": "example.com", "text": "Post", "url": "https://example.com/q",
            "content_type": "blog_post",
        })
        await vote_bot._handle_default_message(self._update(":dislike https://example.com/q shallow"), MagicMock())
        await _settle(vote_bot)
        assert vote_bot._item_votes["blog_2"] == Vote("down", 1)
        assert vote_bot._pending_reasons["blog_2"] == "shallow"
        vote_bot._pending_feedback["blog_2"]["task"].cancel()

    @pytest.mark.asyncio
    async def test_star_verb(self, vote_bot):
        vote_bot.favorite_author_callback = AsyncMock(return_value="favorited")
        update = self._update(":star @dankrad")
        await vote_bot._handle_default_message(update, MagicMock())
        vote_bot.favorite_author_callback.assert_awaited_once_with(username="dankrad")

    @pytest.mark.asyncio
    async def test_plain_text_gets_usage_not_capture(self, vote_bot):
        vote_bot.capture_text_callback = AsyncMock()
        update = self._update("Based rollups only make sense if L1 inclusion is cheap and fast.")
        await vote_bot._handle_default_message(update, MagicMock())
        vote_bot.capture_text_callback.assert_not_awaited()
        assert ":note" in update.message.reply_text.call_args[0][0]

    @pytest.mark.asyncio
    async def test_note_verb_captures_text(self, vote_bot):
        vote_bot.capture_text_callback = AsyncMock(return_value={
            "item_id": "note_1", "author_username": "me", "text": "A thought", "content_type": "note",
        })
        await vote_bot._handle_default_message(self._update(":note A thought"), MagicMock())
        await _settle(vote_bot)
        vote_bot.capture_text_callback.assert_awaited_once_with("A thought", None)
        vote_bot._pending_feedback["note_1"]["task"].cancel()


class TestTrackingLinks:
    def test_direct_link_without_redirect_base(self, bot):
        assert bot._link({"item_id": "42", "url": "https://x.com/a/status/42"}) == "https://x.com/a/status/42"

    def test_redirect_link_when_configured(self):
        tracked = TelegramCurator(
            bot_token="fake:token", chat_id="1", redirect_base_url="https://app.example"
        )
        assert tracked._link({"item_id": "42", "url": "https://x.com/a/status/42"}) == "https://app.example/r/42"


# --- _format_thread_message ---

class TestFormatThreadMessage:
    def _make_thread_tweets(self, n):
        """Generate n tweet dicts for a thread."""
        return [
            {
                "item_id": str(i),
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
