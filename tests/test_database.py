"""Tests for DatabaseClient."""

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


# --- save_tweets ---

class TestSaveTweets:
    def test_empty_input(self, db):
        result = db.save_tweets([])
        assert result == []

    def test_record_construction(self, db, sample_tweet):
        mock_result = MagicMock()
        mock_result.data = [{"tweet_id": "123456789"}]
        db.client.table.return_value.upsert.return_value.execute.return_value = mock_result

        db.save_tweets([sample_tweet])

        call_args = db.client.table.return_value.upsert.call_args
        records = call_args[0][0]
        assert len(records) == 1
        rec = records[0]
        assert rec["tweet_id"] == "123456789"
        assert rec["author_username"] == "vitalikbuterin"
        assert rec["author_name"] == "Vitalik Buterin"
        assert rec["text"] == "New EIP proposal for blob fee market adjustments"
        assert rec["is_retweet"] is False
        assert rec["metrics"] == sample_tweet["metrics"]

    def test_optional_fields_default(self, db):
        minimal = {
            "tweet_id": "1",
            "author_username": "a",
            "author_name": "A",
            "text": "hi",
            "created_at": "2025-01-01T00:00:00",
            "url": "https://twitter.com/a/status/1",
        }
        mock_result = MagicMock()
        mock_result.data = [{"tweet_id": "1"}]
        db.client.table.return_value.upsert.return_value.execute.return_value = mock_result

        db.save_tweets([minimal])

        call_args = db.client.table.return_value.upsert.call_args
        rec = call_args[0][0][0]
        assert rec["is_retweet"] is False
        assert rec["filtered"] is False
        assert rec["filter_score"] is None
        assert rec["filter_reason"] is None


# --- save_feedback ---

class TestSaveFeedback:
    def test_valid_upvote(self, db):
        mock_result = MagicMock()
        mock_result.data = [{"id": 1, "tweet_id": "123", "user_vote": "up"}]
        db.client.table.return_value.insert.return_value.execute.return_value = mock_result

        result = db.save_feedback(tweet_id="123", vote="up", telegram_message_id=42)

        assert result["user_vote"] == "up"
        call_args = db.client.table.return_value.insert.call_args
        rec = call_args[0][0]
        assert rec["tweet_id"] == "123"
        assert rec["user_vote"] == "up"
        assert rec["telegram_message_id"] == 42

    def test_valid_downvote(self, db):
        mock_result = MagicMock()
        mock_result.data = [{"id": 2, "tweet_id": "456", "user_vote": "down"}]
        db.client.table.return_value.insert.return_value.execute.return_value = mock_result

        result = db.save_feedback(tweet_id="456", vote="down")
        assert result["user_vote"] == "down"

    def test_invalid_vote_raises(self, db):
        with pytest.raises(ValueError, match="Invalid vote"):
            db.save_feedback(tweet_id="123", vote="sideways")


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
    def _setup_feedback_data(self, db, feedback_rows, favorites=None, muted=None):
        """Helper: wire up mocked feedback data and author lists."""
        mock_result = MagicMock()
        mock_result.data = feedback_rows
        db.client.table.return_value.select.return_value.execute.return_value = mock_result
        db.get_favorite_authors = MagicMock(return_value=favorites or [])
        db.get_muted_authors = MagicMock(return_value=muted or [])

    def test_weighted_score_calculation(self, db):
        rows = [
            {"id": 1, "user_vote": "up", "tweets": {"author_username": "alice", "is_retweet": False, "filter_score": 80}},
            {"id": 2, "user_vote": "down", "tweets": {"author_username": "alice", "is_retweet": False, "filter_score": 80}},
        ]
        self._setup_feedback_data(db, rows)

        stats = db.get_author_stats()

        assert len(stats) == 1
        s = stats[0]
        assert s["author_username"] == "alice"
        assert s["up"] == 1
        assert s["down"] == 1
        assert s["weighted_up"] == 1.0
        assert s["weighted_down"] == 1.0
        assert s["weighted_score"] == pytest.approx(0.5)
        assert s["avg_filter_score"] == 80.0

    def test_retweet_half_weight(self, db):
        rows = [
            {"id": 1, "user_vote": "up", "tweets": {"author_username": "bob", "is_retweet": True, "filter_score": 70}},
        ]
        self._setup_feedback_data(db, rows)

        stats = db.get_author_stats()

        s = stats[0]
        assert s["weighted_up"] == 0.5
        assert s["weighted_score"] == pytest.approx(1.0)  # 0.5 / (0.5 + 0) = 1.0

    def test_favorite_and_muted_flags(self, db):
        rows = [
            {"id": 1, "user_vote": "up", "tweets": {"author_username": "fav_user", "is_retweet": False, "filter_score": 90}},
            {"id": 2, "user_vote": "down", "tweets": {"author_username": "muted_user", "is_retweet": False, "filter_score": 30}},
        ]
        self._setup_feedback_data(db, rows, favorites=["fav_user"], muted=["muted_user"])

        stats = db.get_author_stats()

        by_name = {s["author_username"]: s for s in stats}
        assert by_name["fav_user"]["is_favorite"] is True
        assert by_name["fav_user"]["is_muted"] is False
        assert by_name["muted_user"]["is_favorite"] is False
        assert by_name["muted_user"]["is_muted"] is True

    def test_orphaned_feedback_skipped(self, db):
        rows = [
            {"id": 1, "user_vote": "up", "tweets": None},
            {"id": 2, "user_vote": "up", "tweets": {"author_username": "alice", "is_retweet": False, "filter_score": 80}},
        ]
        self._setup_feedback_data(db, rows)

        stats = db.get_author_stats()

        assert len(stats) == 1

    def test_sorted_by_weighted_score_desc(self, db):
        rows = [
            {"id": 1, "user_vote": "up", "tweets": {"author_username": "low", "is_retweet": False, "filter_score": 50}},
            {"id": 2, "user_vote": "down", "tweets": {"author_username": "low", "is_retweet": False, "filter_score": 50}},
            {"id": 3, "user_vote": "up", "tweets": {"author_username": "high", "is_retweet": False, "filter_score": 90}},
        ]
        self._setup_feedback_data(db, rows)

        stats = db.get_author_stats()

        assert stats[0]["author_username"] == "high"
        assert stats[1]["author_username"] == "low"
