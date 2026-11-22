"""Tests for the pure feedback-flow helpers."""

from types import SimpleNamespace

from src.content import parent_item_id, reply_to_preview
from src.feedback_flow import (
    PARENT_LABEL,
    Vote,
    parse_vote_callback,
    quoted_item_id,
    reason_done_text,
    reason_prompt_buttons,
    reason_prompt_text,
    vote_buttons,
    vote_from_reactions,
    vote_label,
    voted_buttons,
)


def emoji(*e):
    return [SimpleNamespace(emoji=x) for x in e]


class TestVoteFromReactions:
    def test_thumbs_up(self):
        vote, retracted = vote_from_reactions(emoji("👍"), [])
        assert vote == Vote("up", 1) and not retracted

    def test_fire_is_strong(self):
        vote, _ = vote_from_reactions(emoji("🔥"), [])
        assert vote == Vote("up", 2)

    def test_strongest_wins(self):
        vote, _ = vote_from_reactions(emoji("👍", "🔥"), [])
        assert vote.strength == 2

    def test_thumbs_down(self):
        vote, _ = vote_from_reactions(emoji("👎"), [])
        assert vote == Vote("down", 1)

    def test_removal_retracts(self):
        vote, retracted = vote_from_reactions([], emoji("👍"))
        assert vote is None and retracted

    def test_unknown_emoji_ignored(self):
        vote, retracted = vote_from_reactions(emoji("🤔"), [])
        assert vote is None and not retracted

    def test_swapping_unknown_for_known_is_not_retraction(self):
        vote, retracted = vote_from_reactions(emoji("🤔"), emoji("🤔"))
        assert vote is None and not retracted

    def test_custom_emoji_without_attr(self):
        vote, _ = vote_from_reactions([SimpleNamespace(custom_emoji_id="1")], [])
        assert vote is None


class TestParseVoteCallback:
    def test_with_strength(self):
        assert parse_vote_callback("vote:42:up:2") == ("42", Vote("up", 2))

    def test_legacy_without_strength(self):
        assert parse_vote_callback("vote:42:down") == ("42", Vote("down", 1))

    def test_down_strength_clamped(self):
        assert parse_vote_callback("vote:42:down:2") == ("42", Vote("down", 1))

    def test_invalid(self):
        assert parse_vote_callback("vote:42") is None
        assert parse_vote_callback("vote:42:sideways") is None
        assert parse_vote_callback("vote:42:up:x") is None
        assert parse_vote_callback("fav:a:1") is None


class TestKeyboards:
    def test_vote_buttons_with_author(self):
        kb = vote_buttons("42", "alice").inline_keyboard
        assert [b.callback_data for b in kb[0]] == ["vote:42:up:2", "vote:42:up:1", "vote:42:down:1", "vote:42:skip:1"]
        assert [b.callback_data for b in kb[1]] == ["fav:alice:42", "mute:alice:42"]

    def test_vote_buttons_without_author(self):
        kb = vote_buttons("blog_1", None).inline_keyboard
        assert len(kb) == 1

    def test_voted_buttons_undo(self):
        kb = voted_buttons("42", Vote("up", 2), undo=True).inline_keyboard[0]
        assert kb[0].text == "🔥"
        assert kb[1].callback_data == "undo:42"

    def test_voted_buttons_final(self):
        kb = voted_buttons("42", Vote("down", 1), undo=False).inline_keyboard[0]
        assert [b.text for b in kb] == ["👎"]

    def test_reason_prompt_buttons(self):
        kb = reason_prompt_buttons("42").inline_keyboard[0]
        assert [b.callback_data for b in kb] == ["rsn_ok:42", "rsn_edit:42", "rsn_skip:42"]


class TestTexts:
    def test_labels(self):
        assert vote_label(Vote("up", 1)) == "👍"
        assert vote_label(Vote("up", 2)) == "🔥"
        assert vote_label(Vote("down", 1)) == "👎"

    def test_prompt_text(self):
        assert reason_prompt_text(Vote("up", 2), "x") == "🔥 Loved because: <i>x</i>?"
        assert reason_prompt_text(Vote("down", 1), "y") == "👎 Skipped because: <i>y</i>?"

    def test_done_text(self):
        assert reason_done_text(Vote("up", 1), "z") == "👍 ✅ <i>z</i>"


class TestQuotedItemId:
    def test_from_fresh_tweet(self):
        assert quoted_item_id({"quoted_tweet": {"item_id": "888", "text": "x"}}) == "888"

    def test_from_db_row_referenced_tweets(self):
        row = {"quoted_tweet": None, "raw_data": {"referenced_tweets": [
            {"type": "replied_to", "id": "1"}, {"type": "quoted", "id": "999"},
        ]}}
        assert quoted_item_id(row) == "999"

    def test_retweet_is_not_a_quote(self):
        row = {"raw_data": {"referenced_tweets": [{"type": "retweeted", "id": "7"}]}}
        assert quoted_item_id(row) is None

    def test_plain_tweet(self):
        assert quoted_item_id({"quoted_tweet": None, "raw_data": None}) is None

    def test_vote_buttons_quoted_row(self):
        kb = vote_buttons("42", "alice", quoted_id="888").inline_keyboard
        assert len(kb) == 3
        assert [b.callback_data for b in kb[2]] == ["qt:888"]
        assert vote_buttons("42", "alice").inline_keyboard[-1][1].callback_data == "mute:alice:42"


class TestParentItemId:
    def test_from_fresh_reply_to(self):
        assert parent_item_id({"reply_to": {"item_id": "7", "author_username": "bob"}}) == "7"

    def test_from_raw_data_reply_to(self):
        item = {"raw_data": {"reply_to": {"item_id": "7"}}}
        assert parent_item_id(item) == "7"
        assert reply_to_preview(item) == {"item_id": "7"}

    def test_from_referenced_tweets_without_preview(self):
        item = {"raw_data": {"referenced_tweets": [
            {"type": "quoted", "id": "5"}, {"type": "replied_to", "id": "7"},
        ]}}
        assert parent_item_id(item) == "7"
        assert reply_to_preview(item) is None

    def test_none(self):
        assert parent_item_id({"reply_to": None, "raw_data": {}}) is None
        assert parent_item_id({}) is None


class TestParentButton:
    def test_parent_row_uses_qt_callback(self):
        rows = vote_buttons("1", "alice", quoted_id="5", parent_id="7").inline_keyboard
        assert rows[-2][0].callback_data == "qt:5"
        assert rows[-1][0].callback_data == "qt:7"
        assert rows[-1][0].text == PARENT_LABEL

    def test_no_parent_row_without_parent(self):
        rows = vote_buttons("1", "alice").inline_keyboard
        assert len(rows) == 2


class TestSkip:
    def test_parse_skip_callback(self):
        from src.feedback_flow import parse_vote_callback, Vote
        assert parse_vote_callback("vote:42:skip:1") == ("42", Vote("skip", 1))
        assert parse_vote_callback("vote:42:skip:2") == ("42", Vote("skip", 1))  # never strong

    def test_default_keyboard_has_skip_button(self):
        from src.feedback_flow import vote_buttons
        row = vote_buttons("42", None).inline_keyboard[0]
        assert [b.text for b in row] == ["🔥", "👍", "👎", "😐"]
        assert row[3].callback_data == "vote:42:skip:1"

    def test_skip_reactions_and_label(self):
        from types import SimpleNamespace
        from src.feedback_flow import Vote, vote_from_reaction_set, vote_label
        assert vote_from_reaction_set([SimpleNamespace(emoji="🤷")]) == Vote("skip", 1)
        # 👍 beats 😐 on the same message? No: equal strength, first wins, so 😐 is a real vote
        assert vote_label(Vote("skip", 1)) == "😐"
