"""Tests for the bot callbacks wired in main.init_components, built against mocks."""

import pytest
from unittest.mock import MagicMock, patch

import main


def _callbacks():
    """Run init_components with every component mocked; return (db, twitter, bot kwargs)."""
    names = [
        "TwitterClient", "ClaudeFilter", "DatabaseClient", "EmbeddingManager",
        "attach_db_error_handler", "BlogFetcher", "FeedFetcher", "MailboxFetcher",
        "NewsletterProcessor", "TelegramCurator", "DailyCurator",
    ]
    with patch.multiple(main, **{n: MagicMock() for n in names}):
        settings = MagicMock()
        settings.redirect_base_url = None
        settings.development_mode = True
        main.init_components(settings)
        db = main.DatabaseClient.return_value
        twitter = main.TwitterClient.return_value
        kwargs = main.TelegramCurator.call_args[1]
    return db, twitter, kwargs


@pytest.mark.asyncio
async def test_retweet_link_does_not_overwrite_stored_original():
    db, twitter, kwargs = _callbacks()
    stored = {"item_id": "999", "filter_score": 80, "filter_reason": "good", "domain": "eth"}
    db.get_item.side_effect = lambda i: stored if i == "999" else None
    twitter.fetch_tweet.return_value = {"item_id": "999", "is_retweet": True, "text": "orig"}

    result = await kwargs["like_tweet_callback"]("42")  # 42 = the retweet's id

    assert result is stored
    db.save_items.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_original_is_saved():
    db, twitter, kwargs = _callbacks()
    db.get_item.return_value = None
    fetched = {"item_id": "999", "is_retweet": True, "text": "orig"}
    twitter.fetch_tweet.return_value = fetched

    result = await kwargs["like_tweet_callback"]("42")

    assert result is fetched
    db.save_items.assert_called_once_with([fetched])


@pytest.mark.asyncio
async def test_stored_tweet_returned_without_fetch():
    db, twitter, kwargs = _callbacks()
    db.get_item.return_value = {"item_id": "42"}
    assert (await kwargs["like_tweet_callback"]("42"))["item_id"] == "42"
    twitter.fetch_tweet.assert_not_called()


@pytest.mark.asyncio
async def test_thread_fetch_saves_only_unknown_tweets():
    db, twitter, kwargs = _callbacks()
    t1, t2 = {"item_id": "1", "text": "a"}, {"item_id": "2", "text": "b"}
    twitter.fetch_thread.return_value = [t1, t2]
    db.get_items.return_value = {"1": {"item_id": "1", "filter_score": 80}}

    result = await kwargs["thread_callback"]("2")

    assert result == [t1, t2]
    db.save_items.assert_called_once_with([t2])


@pytest.mark.asyncio
async def test_thread_fetch_not_found():
    db, twitter, kwargs = _callbacks()
    twitter.fetch_thread.return_value = None
    assert await kwargs["thread_callback"]("2") is None
    db.save_items.assert_not_called()
