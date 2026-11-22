"""Tests for the explicit skip helpers ("rest are meh")."""

from src.skips import pick_skips, skip_signal


def _item(item_id, msg=None, exploration=False):
    return {"item_id": item_id, "telegram_message_id": msg, "exploration": exploration}


class TestPickSkips:
    def test_returns_items_with_no_vote_and_no_open(self):
        sent = [_item("a"), _item("b"), _item("c")]
        assert pick_skips(sent, engaged_ids={"a"}) == [sent[1], sent[2]]

    def test_exploration_items_count_too(self):
        # "Rest are meh" is deliberate, so an unflagged 🎲 is a confirmed miss-not
        sent = [_item("x", exploration=True)]
        assert pick_skips(sent, engaged_ids=set()) == sent

    def test_rows_without_id_are_dropped(self):
        assert pick_skips([{"item_id": None}, {}], set()) == []


def test_skip_signal_shape():
    assert skip_signal(_item("a", msg=77), "meh") == {
        "item_id": "a", "kind": "vote", "value": "skip", "strength": 1,
        "reason": None, "source": "meh", "telegram_message_id": 77,
    }
