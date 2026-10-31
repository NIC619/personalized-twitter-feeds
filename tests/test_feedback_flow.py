"""Tests for the pure feedback-flow helpers."""

from types import SimpleNamespace

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
        assert [b.callback_data for b in kb[0]] == ["vote:42:up:2", "vote:42:up:1", "vote:42:down:1"]
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
