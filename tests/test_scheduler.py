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
    db.get_latest_profiles.return_value = {}
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
        assert curator._deduplicate_tweets([{"item_id": "1", "text": "old"}]) == []

    def test_keep_new_tweet(self, curator):
        curator.db.get_scored_ids.return_value = set()
        result = curator._deduplicate_tweets([{"item_id": "1", "text": "new"}])
        assert [t["item_id"] for t in result] == ["1"]

    def test_mixed_new_and_old_single_query(self, curator):
        curator.db.get_scored_ids.return_value = {"old"}
        tweets = [
            {"item_id": "old", "text": "already scored"},
            {"item_id": "new", "text": "fresh"},
        ]
        result = curator._deduplicate_tweets(tweets)
        assert [t["item_id"] for t in result] == ["new"]
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
            {"item_id": "1", "author_username": "fav_author", "text": "a", "is_retweet": False},
            {"item_id": "2", "author_username": "normal_author", "text": "b", "is_retweet": False},
            {"item_id": "3", "author_username": "muted_author", "text": "c", "is_retweet": False},
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
            {"item_id": "1", "author_username": "normal", "text": "RT something", "is_retweet": True},
            {"item_id": "2", "author_username": "fav", "text": "RT something else", "is_retweet": True},
            {"item_id": "3", "author_username": "normal", "text": "original", "is_retweet": False},
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
        tweets = [{"item_id": "1", "author_username": "a", "text": "x", "is_retweet": False}]
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
    async def test_writes_vote_signal(self):
        db = MagicMock()

        await feedback_handler(
            db=db, item_id="123", vote="up", telegram_message_id=42, notes="great content",
        )

        db.save_signal.assert_called_once_with(
            item_id="123", kind="vote", value="up", strength=1,
            reason="great content", source="button", telegram_message_id=42,
        )

    @pytest.mark.asyncio
    async def test_strength_and_source_passed_to_signal(self):
        db = MagicMock()
        await feedback_handler(
            db=db, item_id="1", vote="up", telegram_message_id=7,
            strength=2, source="reaction",
        )
        kwargs = db.save_signal.call_args[1]
        assert kwargs["strength"] == 2 and kwargs["source"] == "reaction"

    @pytest.mark.asyncio
    async def test_error_does_not_raise(self, mock_embedding_manager):
        db = MagicMock()
        db.save_signal.side_effect = Exception("DB error")

        await feedback_handler(
            db=db, item_id="123", vote="up", telegram_message_id=42,
            embedding_manager=mock_embedding_manager,
        )
        # No embedding attempted when the vote itself failed
        mock_embedding_manager.generate_embedding.assert_not_called()

    @pytest.mark.asyncio
    async def test_embeds_item_on_feedback(self, mock_embedding_manager):
        db = MagicMock()
        db.has_embedding.return_value = False
        db.get_item.return_value = {"item_id": "123", "text": "great content"}

        await feedback_handler(
            db=db, item_id="123", vote="up", telegram_message_id=42,
            embedding_manager=mock_embedding_manager,
        )

        db.save_signal.assert_called_once()
        mock_embedding_manager.generate_embedding.assert_called_once_with("great content")
        db.save_embedding.assert_called_once_with("123", [0.1] * 1536)

    @pytest.mark.asyncio
    async def test_skips_embedding_if_already_exists(self, mock_embedding_manager):
        db = MagicMock()
        db.has_embedding.return_value = True

        await feedback_handler(
            db=db, item_id="123", vote="up", telegram_message_id=42,
            embedding_manager=mock_embedding_manager,
        )

        mock_embedding_manager.generate_embedding.assert_not_called()
        db.save_embedding.assert_not_called()

    @pytest.mark.asyncio
    async def test_embedding_failure_does_not_block_feedback(self, mock_embedding_manager):
        db = MagicMock()
        db.has_embedding.side_effect = Exception("DB error")

        await feedback_handler(
            db=db, item_id="123", vote="up", telegram_message_id=42,
            embedding_manager=mock_embedding_manager,
        )

        db.save_signal.assert_called_once()


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
            {"item_id": "pass", "filter_score": 90, "filter_reason": "x"},
            {"item_id": "low", "filter_score": 30, "filter_reason": "x"},
            {"item_id": "unscored", "filter_score": 0, "filter_reason": "Not scored by Claude"},
            {"item_id": "none"},
        ]
        picked = curator._pick_exploration(scored, [scored[0]])
        assert [t["item_id"] for t in picked] == ["low"]

    def test_respects_limit(self, components):
        curator = self._curator(components, 2)
        scored = [{"item_id": str(i), "filter_score": 10, "filter_reason": "x"} for i in range(10)]
        assert len(curator._pick_exploration(scored, [])) == 2

    def test_disabled(self, components):
        curator = self._curator(components, 0)
        scored = [{"item_id": "1", "filter_score": 10, "filter_reason": "x"}]
        assert curator._pick_exploration(scored, []) == []

    @pytest.mark.asyncio
    async def test_exploration_items_sent_and_flagged(self, components):
        twitter, claude, telegram, db = components
        curator = self._curator(components, 1)
        tweets = [
            {"item_id": "1", "author_username": "dev", "text": "good", "is_retweet": False},
            {"item_id": "2", "author_username": "dev", "text": "meh", "is_retweet": False},
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
        assert [t["item_id"] for t in flagged] == ["2"]
        saved = db.save_items.call_args[0][0]
        assert any(t.get("exploration") for t in saved if t["item_id"] == "2")


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
            {"item_id": "1", "author_username": "dev", "text": "rollup stuff", "is_retweet": False},
        ]
        twitter.fetch_timeline.return_value = tweets
        twitter.fetch_user_tweets.return_value = []
        db.get_favorite_authors.return_value = []
        db.get_muted_authors.return_value = []

        mock_embedding_manager.find_neighbors.return_value = {
            "1": [{"item_id": "x", "text": "liked tweet", "author_username": "a", "vote": "up", "similarity": 0.9}],
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
        tweets = [{"item_id": "1", "author_username": "dev", "text": "x", "is_retweet": False}]
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
            {"item_id": "1", "author_username": "dev", "text": "stuff", "is_retweet": False},
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


# --- feeds in the daily run ---

class TestFeedItems:
    def _curator(self, components, feed_fetcher):
        twitter, claude, telegram, db = components
        return DailyCurator(
            twitter=twitter, claude=claude, telegram=telegram, db=db,
            filter_threshold=70, favorite_threshold_offset=20, muted_threshold_offset=15,
            feed_fetcher=feed_fetcher, feed_max_entries=5, exploration_per_run=0,
        )

    def test_no_fetcher(self, components):
        twitter, claude, telegram, db = components
        curator = DailyCurator(twitter=twitter, claude=claude, telegram=telegram, db=db)
        assert curator._fetch_feed_items() == []

    def test_only_unscored_entries_become_items(self, components):
        from src.blog_fetcher import BlogFetcher
        twitter, claude, telegram, db = components
        fetcher = MagicMock()
        fetcher.blog_fetcher.build_post = BlogFetcher.build_post
        fetcher.fetch_entries.return_value = [
            {"url": "https://x.example/new", "title": "New"},
            {"url": "https://x.example/old", "title": "Old"},
        ]
        old_id = BlogFetcher.build_post(url="https://x.example/old", title="Old")["item_id"]
        db.get_feeds.return_value = [{"url": "https://x.example/feed", "title": "X"}]
        db.get_scored_ids.return_value = {old_id}
        fetcher.entries_to_items.side_effect = lambda feed, entries, max_items: [
            {"item_id": "n", "url": e["url"]} for e in entries
        ]
        curator = self._curator(components, fetcher)

        items = curator._fetch_feed_items()

        assert [i["url"] for i in items] == ["https://x.example/new"]
        fetcher.entries_to_items.assert_called_once()
        assert fetcher.entries_to_items.call_args[1]["max_items"] == 5
        db.update_feed_state.assert_called_once_with("https://x.example/feed")

    def test_feed_error_recorded_and_non_fatal(self, components):
        twitter, claude, telegram, db = components
        fetcher = MagicMock()
        fetcher.fetch_entries.side_effect = RuntimeError("timeout")
        db.get_feeds.return_value = [{"url": "https://bad.example/feed"}]
        curator = self._curator(components, fetcher)

        assert curator._fetch_feed_items() == []
        db.update_feed_state.assert_called_once_with("https://bad.example/feed", error="timeout")

    @pytest.mark.asyncio
    async def test_feed_items_join_scoring_batch(self, components):
        twitter, claude, telegram, db = components
        tweets = [{"item_id": "1", "author_username": "dev", "text": "t", "is_retweet": False}]
        twitter.fetch_timeline.return_value = tweets
        twitter.fetch_user_tweets.return_value = []
        db.get_favorite_authors.return_value = []
        db.get_muted_authors.return_value = []
        fetcher = MagicMock()
        curator = self._curator(components, fetcher)
        curator._fetch_feed_items = MagicMock(return_value=[
            {"item_id": "blog_1", "author_username": "x.example", "text": "post", "is_retweet": False},
        ])
        claude.filter_tweets.return_value = []

        stats = await curator.run_daily_curation()

        scored_input = claude.filter_tweets.call_args[0][0]
        assert [i["item_id"] for i in scored_input] == ["1", "blog_1"]
        assert stats["feed_items"] == 1
        saved_ids = {i["item_id"] for i in db.save_items.call_args[0][0]}
        assert saved_ids == {"1", "blog_1"}

    @pytest.mark.asyncio
    async def test_feeds_processed_when_twitter_is_empty(self, components):
        twitter, claude, telegram, db = components
        twitter.fetch_timeline.return_value = []
        db.get_favorite_authors.return_value = []
        db.get_muted_authors.return_value = []
        db.get_blocked_keywords.return_value = []
        curator = self._curator(components, MagicMock())
        curator._fetch_feed_items = MagicMock(return_value=[
            {"item_id": "blog_1", "author_username": "x.example", "text": "post", "is_retweet": False},
        ])
        claude.filter_tweets.return_value = []

        stats = await curator.run_daily_curation()

        assert stats["feed_items"] == 1
        assert [i["item_id"] for i in claude.filter_tweets.call_args[0][0]] == ["blog_1"]

    @pytest.mark.asyncio
    async def test_feed_items_respect_keyword_blocklist(self, components):
        twitter, claude, telegram, db = components
        twitter.fetch_timeline.return_value = []
        db.get_favorite_authors.return_value = []
        db.get_muted_authors.return_value = []
        db.get_blocked_keywords.return_value = ["airdrop"]
        curator = self._curator(components, MagicMock())
        curator._fetch_feed_items = MagicMock(return_value=[
            {"item_id": "blog_1", "author_username": "x.example", "text": "Huge airdrop guide", "is_retweet": False},
            {"item_id": "blog_2", "author_username": "x.example", "text": "Consensus deep dive", "is_retweet": False},
        ])
        claude.filter_tweets.return_value = []

        stats = await curator.run_daily_curation()

        assert stats["skipped_blocked_keywords"] == 1
        assert [i["item_id"] for i in claude.filter_tweets.call_args[0][0]] == ["blog_2"]


# --- newsletters by email ---

class TestInbox:
    def _curator(self, components, mailbox, newsletter):
        twitter, claude, telegram, db = components
        return DailyCurator(
            twitter=twitter, claude=claude, telegram=telegram, db=db,
            filter_threshold=50, favorite_threshold_offset=20, muted_threshold_offset=15,
            newsletter=newsletter, mailbox=mailbox, email_max_per_run=5, exploration_per_run=0,
        )

    @pytest.mark.asyncio
    async def test_not_configured(self, components):
        curator = self._curator(components, None, None)
        assert await curator.process_inbox() == {"emails": 0, "entries": 0, "sent": 0}

    @pytest.mark.asyncio
    async def test_each_issue_sent_and_marked_seen(self, components):
        twitter, claude, telegram, db = components
        mailbox = MagicMock()
        mailbox.fetch_unseen.return_value = [
            {"uid": "7", "subject": "ETH Daily #7", "sender": "a@b", "text": "..."},
            {"uid": "8", "subject": "Broken", "sender": "a@b", "text": "..."},
        ]
        newsletter = MagicMock()
        good = {"item_id": "n1", "filter_score": 80, "author_username": "x.io"}
        bad = {"item_id": "n2", "filter_score": 20, "author_username": "x.io"}

        def from_email(mail):
            if mail["uid"] == "8":
                raise RuntimeError("extraction failed")
            return {"title": mail["subject"], "entries": 2, "items": [good, bad]}
        newsletter.from_email.side_effect = from_email
        telegram.send_issue_digest = AsyncMock(return_value=[(good, 501)])
        curator = self._curator(components, mailbox, newsletter)

        summary = await curator.process_inbox()

        assert summary == {"emails": 1, "entries": 2, "sent": 1}
        telegram.send_issue_digest.assert_awaited_once_with("ETH Daily #7", [good], total=2)
        mailbox.fetch_unseen.assert_called_once_with(5)
        mailbox.acknowledge.assert_called_once_with("7")  # the failed one stays unseen
        db.mark_item_sent.assert_called_once_with("n1", 501)

    @pytest.mark.asyncio
    async def test_delivery_failure_leaves_email_unseen_and_retries_only_unsent(self, components):
        twitter, claude, telegram, db = components
        mailbox = MagicMock()
        mailbox.fetch_unseen.return_value = [{"uid": "9", "subject": "Issue", "sender": "a@b", "text": "..."}]
        a = {"item_id": "a", "filter_score": 90, "author_username": "x"}
        b = {"item_id": "b", "filter_score": 90, "author_username": "x"}
        newsletter = MagicMock()
        newsletter.from_email.return_value = {"title": "Issue", "entries": 2, "items": [a, b]}
        telegram.send_issue_digest = AsyncMock(return_value=[(a, 1)])  # b failed
        curator = self._curator(components, mailbox, newsletter)

        summary = await curator.process_inbox()
        assert summary["sent"] == 1
        mailbox.acknowledge.assert_not_called()

        # Next run: a is known as sent, only b goes out, then the email is acknowledged
        a_sent = {**a, "sent_to_telegram": "2026-10-31T00:00:00+00:00"}
        newsletter.from_email.return_value = {"title": "Issue", "entries": 2, "items": [a_sent, b]}
        telegram.send_issue_digest = AsyncMock(return_value=[(b, 2)])
        await curator.process_inbox()
        telegram.send_issue_digest.assert_awaited_once_with("Issue", [b], total=2)
        mailbox.acknowledge.assert_called_once_with("9")

    @pytest.mark.asyncio
    async def test_unscored_items_keep_email_unseen(self, components):
        twitter, claude, telegram, db = components
        mailbox = MagicMock()
        mailbox.fetch_unseen.return_value = [{"uid": "4", "subject": "Issue", "sender": "a@b", "text": "..."}]
        newsletter = MagicMock()
        newsletter.from_email.return_value = {"title": "Issue", "entries": 2, "items": [
            {"item_id": "a", "filter_score": 90, "author_username": "x"},
            {"item_id": "b", "filter_score": None, "author_username": "x"},
        ]}
        telegram.send_issue_digest = AsyncMock(return_value=[({"item_id": "a"}, 1)])
        curator = self._curator(components, mailbox, newsletter)
        await curator.process_inbox()
        telegram.send_issue_digest.assert_awaited_once()  # scored items still go out
        mailbox.acknowledge.assert_not_called()

    @pytest.mark.asyncio
    async def test_nothing_above_threshold_still_acknowledges(self, components):
        twitter, claude, telegram, db = components
        mailbox = MagicMock()
        mailbox.fetch_unseen.return_value = [{"uid": "3", "subject": "Issue", "sender": "a@b", "text": "..."}]
        newsletter = MagicMock()
        newsletter.from_email.return_value = {"title": "Issue", "entries": 1, "items": [{"item_id": "a", "filter_score": 10}]}
        curator = self._curator(components, mailbox, newsletter)
        await curator.process_inbox()
        telegram.send_issue_digest.assert_not_awaited()
        mailbox.acknowledge.assert_called_once_with("3")

    @pytest.mark.asyncio
    async def test_overlapping_runs_are_serialised(self, components):
        import asyncio
        twitter, claude, telegram, db = components
        mailbox = MagicMock()
        release = asyncio.Event()
        loop = asyncio.get_running_loop()

        def slow_fetch(limit):
            # Runs in a worker thread; block until the test releases it
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
            return []
        mailbox.fetch_unseen.side_effect = slow_fetch
        curator = self._curator(components, mailbox, MagicMock())

        first = asyncio.create_task(curator.process_inbox())
        await asyncio.sleep(0.05)
        second = await curator.process_inbox()
        assert second.get("busy") is True
        release.set()
        assert (await first)["emails"] == 0
        mailbox.fetch_unseen.assert_called_once()

    @pytest.mark.asyncio
    async def test_daily_run_processes_inbox_after_digest(self, components):
        twitter, claude, telegram, db = components
        twitter.fetch_timeline.return_value = []
        db.get_favorite_authors.return_value = []
        mailbox = MagicMock()
        mailbox.fetch_unseen.return_value = []
        curator = self._curator(components, mailbox, MagicMock())

        stats = await curator.run_daily_curation()

        assert stats["inbox"] == {"emails": 0, "entries": 0, "sent": 0}

    def test_score_items_uses_profile_and_saves(self, components, mock_embedding_manager):
        twitter, claude, telegram, db = components
        curator = self._curator(components, None, None)
        curator.embedding_manager = mock_embedding_manager
        items = [{"item_id": "n1", "text": "t"}]
        curator.score_items(items)
        claude.filter_tweets.assert_called_once()
        assert claude.filter_tweets.call_args[1]["threshold"] == 0
        db.save_items.assert_called_once_with(items)
