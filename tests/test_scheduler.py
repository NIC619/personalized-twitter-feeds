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
    db.get_scored_ids.return_value = set()
    return twitter, claude, telegram, db


@pytest.fixture
def mock_embedding_manager():
    """Mocked EmbeddingManager."""
    mgr = MagicMock()
    mgr.enabled = True
    mgr.find_neighbors.return_value = {}
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
        curator.db.get_scored_ids.return_value = {"1"}
        assert curator._deduplicate_tweets([{"tweet_id": "1", "text": "old"}]) == []

    def test_keep_new_tweet(self, curator):
        curator.db.get_scored_ids.return_value = set()
        result = curator._deduplicate_tweets([{"tweet_id": "1", "text": "new"}])
        assert [t["tweet_id"] for t in result] == ["1"]

    def test_mixed_new_and_old_single_query(self, curator):
        curator.db.get_scored_ids.return_value = {"old"}
        tweets = [
            {"tweet_id": "old", "text": "already scored"},
            {"tweet_id": "new", "text": "fresh"},
        ]
        result = curator._deduplicate_tweets(tweets)
        assert [t["tweet_id"] for t in result] == ["new"]
        curator.db.get_scored_ids.assert_called_once_with(["old", "new"])

    def test_empty(self, curator):
        assert curator._deduplicate_tweets([]) == []
        curator.db.get_scored_ids.assert_not_called()


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

        # send_daily_digest returns list of (tweet, message_id) tuples
        async def mock_digest(groups, **kw):
            return [(t, 101 + i) for i, g in enumerate(groups) for t in g]
        curator.telegram.send_daily_digest.side_effect = mock_digest

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

        async def mock_digest(groups, **kw):
            return [(t, 201 + i) for i, g in enumerate(groups) for t in g]
        curator.telegram.send_daily_digest.side_effect = mock_digest

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
        curator.db.get_scored_ids.return_value = {"1"}

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


class TestPickExploration:
    def _curator(self, components, n):
        twitter, claude, telegram, db = components
        return DailyCurator(
            twitter=twitter, claude=claude, telegram=telegram, db=db,
            filter_threshold=70, favorite_threshold_offset=20,
            muted_threshold_offset=15, exploration_per_run=n,
        )

    def test_picks_only_scored_non_passed(self, components):
        curator = self._curator(components, 5)
        scored = [
            {"tweet_id": "pass", "filter_score": 90, "filter_reason": "x"},
            {"tweet_id": "low", "filter_score": 30, "filter_reason": "x"},
            {"tweet_id": "unscored", "filter_score": 0, "filter_reason": "Not scored by Claude"},
            {"tweet_id": "none"},
        ]
        picked = curator._pick_exploration(scored, [scored[0]])
        assert [t["tweet_id"] for t in picked] == ["low"]

    def test_respects_limit(self, components):
        curator = self._curator(components, 2)
        scored = [{"tweet_id": str(i), "filter_score": 10, "filter_reason": "x"} for i in range(10)]
        assert len(curator._pick_exploration(scored, [])) == 2

    def test_disabled(self, components):
        curator = self._curator(components, 0)
        scored = [{"tweet_id": "1", "filter_score": 10, "filter_reason": "x"}]
        assert curator._pick_exploration(scored, []) == []

    @pytest.mark.asyncio
    async def test_exploration_items_sent_and_flagged(self, components):
        twitter, claude, telegram, db = components
        curator = self._curator(components, 1)
        tweets = [
            {"tweet_id": "1", "author_username": "dev", "text": "good", "is_retweet": False},
            {"tweet_id": "2", "author_username": "dev", "text": "meh", "is_retweet": False},
        ]
        twitter.fetch_timeline.return_value = tweets
        twitter.fetch_user_tweets.return_value = []
        db.get_favorite_authors.return_value = []
        db.get_muted_authors.return_value = []

        def fake_filter(items, threshold, **kw):
            items[0].update(filter_score=90, filter_reason="great", filtered=True)
            items[1].update(filter_score=20, filter_reason="meh", filtered=False)
            return [items[0]]
        claude.filter_tweets.side_effect = fake_filter

        async def mock_digest(groups, **kw):
            return [(t, 101 + i) for i, g in enumerate(groups) for t in g]
        telegram.send_daily_digest.side_effect = mock_digest

        stats = await curator.run_daily_curation()

        assert stats["exploration"] == 1
        assert stats["filtered"] == 2
        sent = [t for g in telegram.send_daily_digest.call_args[0][0] for t in g]
        flagged = [t for t in sent if t.get("exploration")]
        assert [t["tweet_id"] for t in flagged] == ["2"]
        saved = db.save_tweets.call_args[0][0]
        assert any(t.get("exploration") for t in saved if t["tweet_id"] == "2")


class TestRagInPipeline:
    @pytest.mark.asyncio
    async def test_neighbors_passed_to_claude(self, components, mock_embedding_manager):
        twitter, claude, telegram, db = components

        curator = DailyCurator(
            twitter=twitter, claude=claude, telegram=telegram, db=db,
            filter_threshold=70, favorite_threshold_offset=20,
            muted_threshold_offset=15, embedding_manager=mock_embedding_manager,
            neighbors_per_item=4,
        )

        tweets = [
            {"tweet_id": "1", "author_username": "dev", "text": "rollup stuff", "is_retweet": False},
        ]
        twitter.fetch_timeline.return_value = tweets
        twitter.fetch_user_tweets.return_value = []
        db.get_favorite_authors.return_value = []
        db.get_muted_authors.return_value = []

        mock_embedding_manager.find_neighbors.return_value = {
            "1": [{"tweet_id": "x", "text": "liked tweet", "author_username": "a", "vote": "up", "similarity": 0.9}],
        }

        scored = [dict(tweets[0], filter_score=80, filter_reason="good", filtered=True)]
        claude.filter_tweets.return_value = scored
        async def mock_digest(groups, **kw):
            return [(t, 101 + i) for i, g in enumerate(groups) for t in g]
        telegram.send_daily_digest.side_effect = mock_digest

        stats = await curator.run_daily_curation()

        mock_embedding_manager.find_neighbors.assert_called_once_with(tweets, k=4)
        call_args = claude.filter_tweets.call_args
        assert call_args[1]["neighbors"] == mock_embedding_manager.find_neighbors.return_value
        assert stats.get("rag_items_with_neighbors") == 1

    @pytest.mark.asyncio
    async def test_neighbor_failure_is_non_fatal(self, components, mock_embedding_manager):
        twitter, claude, telegram, db = components
        curator = DailyCurator(
            twitter=twitter, claude=claude, telegram=telegram, db=db,
            filter_threshold=70, favorite_threshold_offset=20,
            muted_threshold_offset=15, embedding_manager=mock_embedding_manager,
        )
        tweets = [{"tweet_id": "1", "author_username": "dev", "text": "x", "is_retweet": False}]
        twitter.fetch_timeline.return_value = tweets
        twitter.fetch_user_tweets.return_value = []
        db.get_favorite_authors.return_value = []
        db.get_muted_authors.return_value = []
        mock_embedding_manager.find_neighbors.side_effect = RuntimeError("pgvector down")
        claude.filter_tweets.return_value = []

        stats = await curator.run_daily_curation()

        assert stats["errors"] == []
        assert claude.filter_tweets.call_args[1]["neighbors"] is None

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
        db.get_favorite_authors.return_value = []
        db.get_muted_authors.return_value = []

        scored = [dict(tweets[0], filter_score=80, filter_reason="good", filtered=True)]
        claude.filter_tweets.return_value = scored
        async def mock_digest(groups, **kw):
            return [(t, 101 + i) for i, g in enumerate(groups) for t in g]
        telegram.send_daily_digest.side_effect = mock_digest

        stats = await curator.run_daily_curation()

        # Should work fine with no RAG
        call_args = claude.filter_tweets.call_args
        assert call_args[1].get("neighbors") is None
