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
def mock_embedding_manager():
    """Mocked EmbeddingManager."""
    mgr = MagicMock()
    mgr.enabled = True
    mgr.find_similar_voted_tweets.return_value = []
    mgr.generate_embedding.return_value = [0.1] * 1536
    return mgr


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

    @pytest.mark.asyncio
    async def test_embeds_tweet_on_feedback(self, mock_embedding_manager):
        db = MagicMock()
        db.has_embedding.return_value = False
        db.get_tweet_by_id.return_value = {"tweet_id": "123", "text": "great content"}

        await feedback_handler(
            db=db,
            tweet_id="123",
            vote="up",
            telegram_message_id=42,
            embedding_manager=mock_embedding_manager,
        )

        db.save_feedback.assert_called_once()
        mock_embedding_manager.generate_embedding.assert_called_once_with("great content")
        db.save_embedding.assert_called_once_with("123", [0.1] * 1536)

    @pytest.mark.asyncio
    async def test_skips_embedding_if_already_exists(self, mock_embedding_manager):
        db = MagicMock()
        db.has_embedding.return_value = True

        await feedback_handler(
            db=db,
            tweet_id="123",
            vote="up",
            telegram_message_id=42,
            embedding_manager=mock_embedding_manager,
        )

        mock_embedding_manager.generate_embedding.assert_not_called()
        db.save_embedding.assert_not_called()

    @pytest.mark.asyncio
    async def test_embedding_failure_does_not_block_feedback(self, mock_embedding_manager):
        db = MagicMock()
        db.has_embedding.side_effect = Exception("DB error")

        # Feedback should still be saved
        await feedback_handler(
            db=db,
            tweet_id="123",
            vote="up",
            telegram_message_id=42,
            embedding_manager=mock_embedding_manager,
        )

        db.save_feedback.assert_called_once()


class TestFormatRagContext:
    def test_format_liked_and_disliked(self):
        similar = [
            {"tweet_id": "1", "text": "Great technical content about rollups", "author_username": "vitalik", "vote": "up", "similarity": 0.92},
            {"tweet_id": "2", "text": "Buy this meme coin now", "author_username": "spammer", "vote": "down", "similarity": 0.85},
        ]
        result = DailyCurator._format_rag_context(similar)

        assert "Liked tweets" in result
        assert "Disliked tweets" in result
        assert "@vitalik" in result
        assert "@spammer" in result
        assert "0.92" in result
        assert "0.85" in result

    def test_format_only_liked(self):
        similar = [
            {"tweet_id": "1", "text": "Good stuff", "author_username": "dev", "vote": "up", "similarity": 0.9},
        ]
        result = DailyCurator._format_rag_context(similar)
        assert "Liked tweets" in result
        assert "Disliked tweets" not in result

    def test_format_only_disliked(self):
        similar = [
            {"tweet_id": "1", "text": "Bad stuff", "author_username": "troll", "vote": "down", "similarity": 0.8},
        ]
        result = DailyCurator._format_rag_context(similar)
        assert "Liked tweets" not in result
        assert "Disliked tweets" in result


class TestRagInPipeline:
    @pytest.mark.asyncio
    async def test_rag_context_passed_to_claude(self, components, mock_embedding_manager):
        twitter, claude, telegram, db = components

        curator = DailyCurator(
            twitter=twitter, claude=claude, telegram=telegram, db=db,
            filter_threshold=70, favorite_threshold_offset=20,
            muted_threshold_offset=15, embedding_manager=mock_embedding_manager,
        )

        tweets = [
            {"tweet_id": "1", "author_username": "dev", "text": "rollup stuff", "is_retweet": False},
        ]
        twitter.fetch_timeline.return_value = tweets
        twitter.fetch_user_tweets.return_value = []
        db.get_tweet_by_id.return_value = None
        db.get_favorite_authors.return_value = []
        db.get_muted_authors.return_value = []

        mock_embedding_manager.find_similar_voted_tweets.return_value = [
            {"tweet_id": "x", "text": "liked tweet", "author_username": "a", "vote": "up", "similarity": 0.9},
        ]

        scored = [dict(tweets[0], filter_score=80, filter_reason="good", filtered=True)]
        claude.filter_tweets.return_value = scored
        telegram.send_daily_digest.return_value = [101]

        stats = await curator.run_daily_curation()

        # Verify rag_context was passed to Claude
        call_args = claude.filter_tweets.call_args
        assert call_args[1].get("rag_context") is not None
        assert "liked tweet" in call_args[1]["rag_context"]
        assert stats.get("rag_matches") == 1

    @pytest.mark.asyncio
    async def test_pipeline_works_without_embedding_manager(self, components):
        twitter, claude, telegram, db = components

        curator = DailyCurator(
            twitter=twitter, claude=claude, telegram=telegram, db=db,
            filter_threshold=70, favorite_threshold_offset=20,
            muted_threshold_offset=15,
        )

        tweets = [
            {"tweet_id": "1", "author_username": "dev", "text": "stuff", "is_retweet": False},
        ]
        twitter.fetch_timeline.return_value = tweets
        twitter.fetch_user_tweets.return_value = []
        db.get_tweet_by_id.return_value = None
        db.get_favorite_authors.return_value = []
        db.get_muted_authors.return_value = []

        scored = [dict(tweets[0], filter_score=80, filter_reason="good", filtered=True)]
        claude.filter_tweets.return_value = scored
        telegram.send_daily_digest.return_value = [101]

        stats = await curator.run_daily_curation()

        # Should work fine with no RAG
        call_args = claude.filter_tweets.call_args
        assert call_args[1].get("rag_context") is None
