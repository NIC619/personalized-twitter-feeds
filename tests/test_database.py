"""Tests for DatabaseClient."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.database import DatabaseClient


@pytest.fixture
def db():
    """DatabaseClient with mocked Supabase client."""
    with patch("src.database.create_client") as mock_create:
        mock_client = MagicMock()
        mock_create.return_value = mock_client
        client = DatabaseClient(url="https://fake.supabase.co", key="fake-key")
    return client


# --- get_signals_for_profile (paginated) ---

class TestSignalsForProfile:
    def test_paginates_past_1000_rows(self, db):
        pages = [[{"id": i} for i in range(1000)], [{"id": i} for i in range(1000, 1500)]]
        q = db.client.table.return_value.select.return_value.in_.return_value.order.return_value
        q.range.return_value.execute.side_effect = [MagicMock(data=p) for p in pages]

        rows = db.get_signals_for_profile(limit=4000)

        assert len(rows) == 1500
        assert [c[0] for c in q.range.call_args_list] == [(0, 999), (1000, 1999)]

    def test_honours_limit(self, db):
        q = db.client.table.return_value.select.return_value.in_.return_value.order.return_value
        q.range.return_value.execute.return_value = MagicMock(data=[{"id": i} for i in range(300)])
        assert len(db.get_signals_for_profile(limit=300)) == 300
        assert q.range.call_args_list[0][0] == (0, 299)

    def test_get_voted_ids(self, db):
        q = db.client.table.return_value.select.return_value.eq.return_value.in_.return_value
        q.execute.return_value = MagicMock(data=[{"item_id": "a"}])
        assert db.get_voted_ids(["a", "b"]) == {"a"}
        assert db.get_voted_ids([]) == set()


# --- save_items ---

class TestSaveTweets:
    def test_empty_input(self, db):
        result = db.save_items([])
        assert result == []

    def test_record_construction(self, db, sample_tweet):
        mock_result = MagicMock()
        mock_result.data = [{"item_id": "123456789"}]
        db.client.table.return_value.upsert.return_value.execute.return_value = mock_result

        db.save_items([sample_tweet])

        call_args = db.client.table.return_value.upsert.call_args
        records = call_args[0][0]
        assert len(records) == 1
        rec = records[0]
        assert rec["item_id"] == "123456789"
        assert rec["author_username"] == "vitalikbuterin"
        assert rec["author_name"] == "Vitalik Buterin"
        assert rec["text"] == "New EIP proposal for blob fee market adjustments"
        assert rec["is_retweet"] is False
        assert rec["metrics"] == sample_tweet["metrics"]

    def test_optional_fields_default(self, db):
        minimal = {
            "item_id": "1",
            "author_username": "a",
            "author_name": "A",
            "text": "hi",
            "created_at": "2025-01-01T00:00:00",
            "url": "https://twitter.com/a/status/1",
        }
        mock_result = MagicMock()
        mock_result.data = [{"item_id": "1"}]
        db.client.table.return_value.upsert.return_value.execute.return_value = mock_result

        db.save_items([minimal])

        call_args = db.client.table.return_value.upsert.call_args
        rec = call_args[0][0][0]
        assert rec["is_retweet"] is False
        assert rec["filtered"] is False
        assert rec["filter_score"] is None
        assert rec["filter_reason"] is None


# --- save_feedback ---

class TestSignals:
    def test_save_signal_record(self, db):
        mock_result = MagicMock()
        mock_result.data = [{"id": 1}]
        db.client.table.return_value.insert.return_value.execute.return_value = mock_result

        db.save_signal("123", "vote", value="up", strength=2, reason="why", source="reaction", telegram_message_id=42)

        db.client.table.assert_called_with("signals")
        rec = db.client.table.return_value.insert.call_args[0][0]
        assert rec == {
            "item_id": "123", "kind": "vote", "value": "up", "strength": 2,
            "reason": "why", "source": "reaction", "telegram_message_id": 42,
        }

    def test_save_signal_with_created_at(self, db):
        db.client.table.return_value.insert.return_value.execute.return_value = MagicMock(data=[])
        db.save_signal("1", "vote", value="up", created_at="2026-01-01T00:00:00+00:00")
        rec = db.client.table.return_value.insert.call_args[0][0]
        assert rec["created_at"] == "2026-01-01T00:00:00+00:00"

    def test_update_latest_vote_reason_none(self, db):
        chain = db.client.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value
        chain.execute.return_value = MagicMock(data=[])
        assert db.update_latest_vote_reason("1", "r") is False
        db.client.table.return_value.update.assert_not_called()

    def test_update_latest_vote_reason_updates(self, db):
        chain = db.client.table.return_value.select.return_value.eq.return_value.eq.return_value.order.return_value.limit.return_value
        chain.execute.return_value = MagicMock(data=[{"id": 9}])
        assert db.update_latest_vote_reason("1", "r") is True
        db.client.table.return_value.update.assert_called_once_with({"reason": "r"})
        db.client.table.return_value.update.return_value.eq.assert_called_once_with("id", 9)


# --- toggle_favorite ---

class TestToggleFavorite:
    def test_muted_to_unmuted(self, db):
        db.is_muted_author = MagicMock(return_value=True)
        db.remove_muted_author = MagicMock()

        result = db.toggle_favorite("SomeUser")

        assert result == "unmuted"
        db.remove_muted_author.assert_called_once_with("someuser")

    def test_default_to_favorited(self, db):
        db.is_muted_author = MagicMock(return_value=False)
        db.save_favorite_author = MagicMock()

        result = db.toggle_favorite("SomeUser")

        assert result == "favorited"
        db.save_favorite_author.assert_called_once_with("someuser")

    def test_strips_at_prefix(self, db):
        db.is_muted_author = MagicMock(return_value=False)
        db.save_favorite_author = MagicMock()

        db.toggle_favorite("@SomeUser")

        db.save_favorite_author.assert_called_once_with("someuser")


# --- toggle_mute ---

class TestToggleMute:
    def test_favorited_to_unfavorited(self, db):
        db.is_favorite_author = MagicMock(return_value=True)
        db.remove_favorite_author = MagicMock()

        result = db.toggle_mute("SomeUser")

        assert result == "unfavorited"
        db.remove_favorite_author.assert_called_once_with("someuser")

    def test_default_to_muted(self, db):
        db.is_favorite_author = MagicMock(return_value=False)
        db.save_muted_author = MagicMock()

        result = db.toggle_mute("SomeUser")

        assert result == "muted"
        db.save_muted_author.assert_called_once_with("someuser")


# --- get_author_stats ---

class TestGetAuthorStats:
    def _setup(self, db, rows, favorites=None, muted=None):
        """Wire mocked vote rows (signals with embedded item) and author lists."""
        db._vote_rows = MagicMock(return_value=rows)
        db.get_favorite_authors = MagicMock(return_value=favorites or [])
        db.get_muted_authors = MagicMock(return_value=muted or [])

    def test_weighted_score_calculation(self, db):
        rows = [
            {"id": 1, "value": "up", "items": {"author_username": "alice", "is_retweet": False, "filter_score": 80}},
            {"id": 2, "value": "down", "items": {"author_username": "alice", "is_retweet": False, "filter_score": 80}},
        ]
        self._setup(db, rows)

        stats = db.get_author_stats()

        assert len(stats) == 1
        s = stats[0]
        assert s["author_username"] == "alice"
        assert s["up"] == 1 and s["down"] == 1
        assert s["weighted_up"] == 1.0 and s["weighted_down"] == 1.0
        assert s["weighted_score"] == pytest.approx(0.5)
        assert s["avg_filter_score"] == 80.0

    def test_retweet_half_weight(self, db):
        rows = [{"id": 1, "value": "up", "items": {"author_username": "bob", "is_retweet": True, "filter_score": 70}}]
        self._setup(db, rows)
        s = db.get_author_stats()[0]
        assert s["weighted_up"] == 0.5
        assert s["weighted_score"] == pytest.approx(1.0)

    def test_favorite_and_muted_flags(self, db):
        rows = [
            {"id": 1, "value": "up", "items": {"author_username": "fav_user", "is_retweet": False, "filter_score": 90}},
            {"id": 2, "value": "down", "items": {"author_username": "muted_user", "is_retweet": False, "filter_score": 30}},
        ]
        self._setup(db, rows, favorites=["fav_user"], muted=["muted_user"])
        by_name = {s["author_username"]: s for s in db.get_author_stats()}
        assert by_name["fav_user"]["is_favorite"] is True
        assert by_name["fav_user"]["is_muted"] is False
        assert by_name["muted_user"]["is_muted"] is True

    def test_orphaned_vote_skipped(self, db):
        rows = [
            {"id": 1, "value": "up", "items": None},
            {"id": 2, "value": "up", "items": {"author_username": "alice", "is_retweet": False, "filter_score": 80}},
        ]
        self._setup(db, rows)
        assert len(db.get_author_stats()) == 1

    def test_sorted_by_weighted_score_desc(self, db):
        rows = [
            {"id": 1, "value": "up", "items": {"author_username": "low", "is_retweet": False, "filter_score": 50}},
            {"id": 2, "value": "down", "items": {"author_username": "low", "is_retweet": False, "filter_score": 50}},
            {"id": 3, "value": "up", "items": {"author_username": "high", "is_retweet": False, "filter_score": 90}},
        ]
        self._setup(db, rows)
        stats = db.get_author_stats()
        assert [s["author_username"] for s in stats] == ["high", "low"]


class TestSaveItemsRecord:
    def test_blog_post_source_and_title(self, db):
        db.client.table.return_value.upsert.return_value.execute.return_value = MagicMock(data=[{}])
        db.save_items([{
            "item_id": "blog_1", "author_username": "example.com", "author_name": "Ex",
            "text": "T\n\nbody", "created_at": "2026-01-01", "url": "https://example.com/p",
            "content_type": "blog_post", "raw_data": {"source": "newsletter"},
            "article": {"title": "T", "body": "body"}, "domain": "ai",
        }])
        rec = db.client.table.return_value.upsert.call_args[0][0][0]
        assert rec["source"] == "newsletter" and rec["title"] == "T" and rec["domain"] == "ai"
        db.client.table.assert_called_with("items")

    def test_tweet_defaults(self, db, sample_tweet):
        db.client.table.return_value.upsert.return_value.execute.return_value = MagicMock(data=[{}])
        db.save_items([sample_tweet])
        rec = db.client.table.return_value.upsert.call_args[0][0][0]
        assert rec["source"] == "twitter" and rec["content_type"] == "tweet"
        assert rec["title"] is None and rec["exploration"] is False


# --- blocked_keywords ---

class TestBlockedKeywords:
    def test_save_normalizes_case_and_whitespace(self, db):
        mock_result = MagicMock()
        mock_result.data = [{"keyword": "client diversity"}]
        db.client.table.return_value.upsert.return_value.execute.return_value = mock_result

        db.save_blocked_keyword("  Client Diversity  ")

        call_args = db.client.table.return_value.upsert.call_args
        record = call_args[0][0]
        assert record == {"keyword": "client diversity"}
        assert call_args.kwargs["on_conflict"] == "keyword"

    def test_save_empty_raises(self, db):
        with pytest.raises(ValueError, match="cannot be empty"):
            db.save_blocked_keyword("   ")

    def test_get_returns_keyword_list(self, db):
        mock_result = MagicMock()
        mock_result.data = [{"keyword": "client diversity"}, {"keyword": "staking marketshare"}]
        db.client.table.return_value.select.return_value.order.return_value.execute.return_value = mock_result

        result = db.get_blocked_keywords()

        assert result == ["client diversity", "staking marketshare"]

    def test_remove_normalizes_case(self, db):
        db.remove_blocked_keyword("  Client Diversity  ")

        eq_call = db.client.table.return_value.delete.return_value.eq
        eq_call.assert_called_once_with("keyword", "client diversity")


# --- error_log ---

class TestErrorLog:
    def test_save_error_log_inserts_record(self, db):
        db.save_error_log(
            source="src.embeddings",
            level="WARNING",
            error_type="HTTPError",
            message="supabase 502",
        )

        insert_call = db.client.table.return_value.insert
        insert_call.assert_called_once()
        record = insert_call.call_args[0][0]
        assert record == {
            "source": "src.embeddings",
            "level": "WARNING",
            "error_type": "HTTPError",
            "message": "supabase 502",
        }

    def test_get_error_logs_in_range_uses_iso_bounds(self, db):
        mock_result = MagicMock()
        mock_result.data = [{"id": 1, "level": "WARNING"}]
        chain = (
            db.client.table.return_value.select.return_value
            .gte.return_value.lt.return_value.order.return_value
        )
        chain.execute.return_value = mock_result

        start = datetime(2026, 3, 1, tzinfo=timezone.utc)
        end = datetime(2026, 4, 1, tzinfo=timezone.utc)
        rows = db.get_error_logs_in_range(start, end)

        assert rows == mock_result.data
        db.client.table.return_value.select.return_value.gte.assert_called_once_with(
            "logged_at", start.isoformat()
        )
        db.client.table.return_value.select.return_value.gte.return_value.lt.assert_called_once_with(
            "logged_at", end.isoformat()
        )


# --- A/B experiment trimming ---


class TestGetScoredIds:
    def test_empty_input(self, db):
        assert db.get_scored_ids([]) == set()
        db.client.table.assert_not_called()

    def test_returns_only_scored_ids(self, db):
        chain = db.client.table.return_value.select.return_value.in_.return_value.not_.is_.return_value
        chain.execute.return_value = MagicMock(data=[{"item_id": "1"}, {"item_id": "3"}])

        result = db.get_scored_ids(["1", "2", "3"])

        assert result == {"1", "3"}
        db.client.table.return_value.select.return_value.in_.assert_called_once_with(
            "item_id", ["1", "2", "3"]
        )

    def test_chunks_large_input(self, db):
        chain = db.client.table.return_value.select.return_value.in_.return_value.not_.is_.return_value
        chain.execute.return_value = MagicMock(data=[])

        db.get_scored_ids([str(i) for i in range(450)], chunk_size=200)

        assert db.client.table.return_value.select.return_value.in_.call_count == 3

    def test_deduplicates_input_ids(self, db):
        chain = db.client.table.return_value.select.return_value.in_.return_value.not_.is_.return_value
        chain.execute.return_value = MagicMock(data=[])

        db.get_scored_ids(["1", "1", "2"])

        db.client.table.return_value.select.return_value.in_.assert_called_once_with(
            "item_id", ["1", "2"]
        )
