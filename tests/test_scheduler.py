"""Tests for DailyCurator and feedback_handler."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.scheduler import DailyCurator, feedback_handler


@pytest.fixture
def components():
    """Create mocked component dependencies."""
    twitter = MagicMock()
    claude = MagicMock()
    telegram = AsyncMock()
    db = MagicMock()
    return twitter, claude, telegram, db


@pytest.fixture
def curator(components):
    """DailyCurator with all mocked dependencies."""
    twitter, claude, telegram, db = components
    return DailyCurator(
        twitter=twitter,
        claude=claude,
        telegram=telegram,
        db=db,
        filter_threshold=70,
        favorite_threshold_offset=20,
        muted_threshold_offset=15,
    )


# --- _deduplicate_tweets ---

class TestDeduplicateTweets:
    def test_skip_already_scored(self, curator):
        curator.db.get_tweet_by_id.return_value = {
            "tweet_id": "1",
            "filter_score": 80,
        }

        tweets = [{"tweet_id": "1", "text": "old"}]
        result = curator._deduplicate_tweets(tweets)

        assert result == []

    def test_keep_new_tweet(self, curator):
        curator.db.get_tweet_by_id.return_value = None

        tweets = [{"tweet_id": "1", "text": "new"}]
        result = curator._deduplicate_tweets(tweets)

        assert len(result) == 1
        assert result[0]["tweet_id"] == "1"

    def test_keep_existing_without_score(self, curator):
        curator.db.get_tweet_by_id.return_value = {
            "tweet_id": "1",
            "filter_score": None,
        }

        tweets = [{"tweet_id": "1", "text": "unscored"}]
        result = curator._deduplicate_tweets(tweets)

        assert len(result) == 1

    def test_mixed_new_and_old(self, curator):
        def mock_get(tweet_id):
            if tweet_id == "old":
                return {"tweet_id": "old", "filter_score": 50}
            return None

        curator.db.get_tweet_by_id.side_effect = mock_get

        tweets = [
            {"tweet_id": "old", "text": "already scored"},
            {"tweet_id": "new", "text": "fresh"},
        ]
        result = curator._deduplicate_tweets(tweets)

        assert len(result) == 1
        assert result[0]["tweet_id"] == "new"


# --- run_daily_curation ---

class TestRunDailyCuration:
    @pytest.mark.asyncio
    async def test_empty_timeline(self, curator):
        curator.twitter.fetch_timeline.return_value = []
        curator.db.get_favorite_authors.return_value = []

        stats = await curator.run_daily_curation()

        assert stats["fetched"] == 0

    @pytest.mark.asyncio
    async def test_full_workflow_with_author_tiers(self, curator):
        tweets = [
            {"tweet_id": "1", "author_username": "fav_author", "text": "a", "is_retweet": False},
            {"tweet_id": "2", "author_username": "normal_author", "text": "b", "is_retweet": False},
            {"tweet_id": "3", "author_username": "muted_author", "text": "c", "is_retweet": False},
        ]
        curator.twitter.fetch_timeline.return_value = tweets
        curator.twitter.fetch_user_tweets.return_value = []
        curator.db.get_tweet_by_id.return_value = None  # all new
        curator.db.get_favorite_authors.return_value = ["fav_author"]
        curator.db.get_muted_authors.return_value = ["muted_author"]

        # Claude scores all at 60 — below default 70 but above favorite 50
        scored_tweets = []
        for t in tweets:
            t_copy = dict(t)
            t_copy["filter_score"] = 60
            t_copy["filter_reason"] = "moderate"
            t_copy["filtered"] = True
            scored_tweets.append(t_copy)
        curator.claude.filter_tweets.return_value = scored_tweets

        curator.telegram.send_daily_digest.return_value = [101]

        stats = await curator.run_daily_curation()

        # fav_author threshold = 50 → 60 passes
        # normal_author threshold = 70 → 60 fails
        # muted_author threshold = 85 → 60 fails
        assert stats["filtered"] == 1

    @pytest.mark.asyncio
    async def test_retweet_skipping(self, curator):
        tweets = [
            {"tweet_id": "1", "author_username": "normal", "text": "RT something", "is_retweet": True},
            {"tweet_id": "2", "author_username": "fav", "text": "RT something else", "is_retweet": True},
            {"tweet_id": "3", "author_username": "normal", "text": "original", "is_retweet": False},
        ]
        curator.twitter.fetch_timeline.return_value = tweets
        curator.twitter.fetch_user_tweets.return_value = []
        curator.db.get_tweet_by_id.return_value = None
        curator.db.get_favorite_authors.return_value = ["fav"]
        curator.db.get_muted_authors.return_value = []

        scored = []
        for t in [tweets[1], tweets[2]]:  # only non-skipped tweets
            t_copy = dict(t)
            t_copy["filter_score"] = 80
            t_copy["filter_reason"] = "good"
            t_copy["filtered"] = True
            scored.append(t_copy)
        curator.claude.filter_tweets.return_value = scored

        curator.telegram.send_daily_digest.return_value = [201, 202]

        stats = await curator.run_daily_curation()

        assert stats["skipped_retweets"] == 1
        # Claude should only receive 2 tweets (fav retweet + normal original)
        call_args = curator.claude.filter_tweets.call_args
        assert len(call_args[0][0]) == 2

    @pytest.mark.asyncio
    async def test_all_duplicates(self, curator):
        tweets = [{"tweet_id": "1", "author_username": "a", "text": "x", "is_retweet": False}]
        curator.twitter.fetch_timeline.return_value = tweets
        curator.twitter.fetch_user_tweets.return_value = []
        curator.db.get_favorite_authors.return_value = []
        curator.db.get_tweet_by_id.return_value = {"tweet_id": "1", "filter_score": 80}

        stats = await curator.run_daily_curation()

        assert stats["new"] == 0
        curator.claude.filter_tweets.assert_not_called()


# --- feedback_handler ---

class TestFeedbackHandler:
    @pytest.mark.asyncio
    async def test_delegates_to_db(self):
        db = MagicMock()

        await feedback_handler(
            db=db,
            tweet_id="123",
            vote="up",
            telegram_message_id=42,
            notes="great content",
        )

        db.save_feedback.assert_called_once_with(
            tweet_id="123",
            vote="up",
            telegram_message_id=42,
            notes="great content",
        )

    @pytest.mark.asyncio
    async def test_error_does_not_raise(self):
        db = MagicMock()
        db.save_feedback.side_effect = Exception("DB error")

        # Should not raise — just logs
        await feedback_handler(
            db=db,
            tweet_id="123",
            vote="up",
            telegram_message_id=42,
        )
