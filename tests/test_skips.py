"""Tests for skip detection (silence as a weak negative)."""

from datetime import datetime, timezone

from src.skips import pick_skips, skip_signal


def _item(item_id, day, exploration=False, msg=None):
    return {
        "item_id": item_id,
        "sent_to_telegram": f"2026-11-{day:02d}T01:00:00+00:00",
        "exploration": exploration,
        "telegram_message_id": msg,
    }


class TestPickSkips:
    def test_marks_unengaged_items_on_engaged_days_only(self):
        sent = [_item("a", 18), _item("b", 18), _item("c", 19), _item("d", 19)]
        # Day 18: "a" was voted on → "b" is a skip. Day 19: untouched → nothing.
        picked = pick_skips(sent, engaged_ids={"a"})
        assert [i["item_id"] for i in picked] == ["b"]

    def test_open_counts_as_engagement(self):
        sent = [_item("a", 18), _item("b", 18)]
        assert pick_skips(sent, engaged_ids={"b"}) == [sent[0]]

    def test_exploration_excluded_unless_asked(self):
        sent = [_item("a", 18), _item("x", 18, exploration=True)]
        assert pick_skips(sent, engaged_ids={"a"}) == []
        assert pick_skips(sent, engaged_ids={"a"}, include_exploration=True) == [sent[1]]

    def test_without_day_gate_everything_unengaged_is_marked(self):
        sent = [_item("a", 18), _item("b", 19)]
        picked = pick_skips(sent, engaged_ids=set(), require_engaged_day=False)
        assert [i["item_id"] for i in picked] == ["a", "b"]

    def test_datetime_sent_values_group_by_utc_day(self):
        a = {"item_id": "a", "sent_to_telegram": datetime(2026, 11, 18, 1, tzinfo=timezone.utc)}
        b = {"item_id": "b", "sent_to_telegram": datetime(2026, 11, 18, 23, tzinfo=timezone.utc)}
        assert pick_skips([a, b], engaged_ids={"a"}) == [b]

    def test_rows_without_delivery_time_are_ignored(self):
        assert pick_skips([{"item_id": "a"}, {"item_id": "b", "sent_to_telegram": None}], {"a"}) == []


def test_skip_signal_shape():
    sig = skip_signal(_item("a", 18, msg=77), "ignored")
    assert sig == {
        "item_id": "a", "kind": "vote", "value": "skip", "strength": 1,
        "reason": None, "source": "ignored", "telegram_message_id": 77,
    }
