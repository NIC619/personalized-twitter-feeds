"""Tests for TwitterClient."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.twitter_client import TwitterClient


@pytest.fixture
def twitter_client():
    """TwitterClient with mocked Tweepy client."""
    with patch("src.twitter_client.tweepy.Client"):
        tc = TwitterClient(
            api_key="k",
            api_secret="s",
            access_token="at",
            access_secret="as",
            bearer_token="bt",
        )
    return tc


def _make_tweet_obj(
    tweet_id=1,
    text="hello",
    author_id=100,
    created_at=None,
    public_metrics=None,
    referenced_tweets=None,
    entities=None,
    conversation_id=None,
):
    """Create a mock tweet object that behaves like tweepy Tweet."""
    return SimpleNamespace(
        id=tweet_id,
        text=text,
        author_id=author_id,
        created_at=created_at,
        public_metrics=public_metrics or {},
        referenced_tweets=referenced_tweets,
        entities=entities,
        conversation_id=conversation_id,
    )


def _make_user_obj(user_id=100, username="testuser", name="Test User"):
    return SimpleNamespace(id=user_id, username=username, name=name)


# --- _normalize_tweet ---

class TestNormalizeTweet:
    def test_basic_field_mapping(self, twitter_client):
        from datetime import datetime, timezone
        created = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        tweet = _make_tweet_obj(
            tweet_id=42,
            text="Test tweet",
            author_id=100,
            created_at=created,
            public_metrics={"like_count": 10, "retweet_count": 5, "reply_count": 2, "impression_count": 1000},
        )
        author = _make_user_obj(user_id=100, username="alice", name="Alice")

        result = twitter_client._normalize_tweet(tweet, author)

        assert result["tweet_id"] == "42"
        assert result["author_username"] == "alice"
        assert result["author_name"] == "Alice"
        assert result["text"] == "Test tweet"
        assert result["created_at"] == "2025-01-15T10:00:00+00:00"
        assert result["is_retweet"] is False
        assert result["metrics"]["likes"] == 10
        assert result["metrics"]["retweets"] == 5
        assert result["metrics"]["replies"] == 2
        assert result["metrics"]["views"] == 1000
        assert result["url"] == "https://twitter.com/alice/status/42"

    def test_retweet_detection(self, twitter_client):
        tweet = _make_tweet_obj(
            referenced_tweets=[{"type": "retweeted", "id": "999"}],
        )
        author = _make_user_obj()

        result = twitter_client._normalize_tweet(tweet, author)
        assert result["is_retweet"] is True

    def test_non_retweet_reference(self, twitter_client):
        tweet = _make_tweet_obj(
            referenced_tweets=[{"type": "quoted", "id": "999"}],
        )
        author = _make_user_obj()

        result = twitter_client._normalize_tweet(tweet, author)
        assert result["is_retweet"] is False

    def test_no_referenced_tweets(self, twitter_client):
        tweet = _make_tweet_obj(referenced_tweets=None)
        author = _make_user_obj()

        result = twitter_client._normalize_tweet(tweet, author)
        assert result["is_retweet"] is False

    def test_missing_metrics_default_zero(self, twitter_client):
        tweet = _make_tweet_obj(public_metrics=None)
        author = _make_user_obj()

        result = twitter_client._normalize_tweet(tweet, author)
        assert result["metrics"]["likes"] == 0
        assert result["metrics"]["retweets"] == 0

    def test_none_created_at(self, twitter_client):
        tweet = _make_tweet_obj(created_at=None)
        author = _make_user_obj()

        result = twitter_client._normalize_tweet(tweet, author)
        assert result["created_at"] is None


# --- get_tweet_url ---

class TestGetTweetUrl:
    def test_url_generation(self):
        url = TwitterClient.get_tweet_url("12345", "vitalik")
        assert url == "https://twitter.com/vitalik/status/12345"


# --- fetch_timeline ---

class TestFetchTimeline:
    def test_empty_response(self, twitter_client):
        twitter_client._fetch_with_retry = MagicMock(return_value=None)

        result = twitter_client.fetch_timeline(max_results=10)
        assert result == []

    def test_no_data_in_response(self, twitter_client):
        mock_resp = SimpleNamespace(data=None, includes=None, meta=None)
        twitter_client._fetch_with_retry = MagicMock(return_value=mock_resp)

        result = twitter_client.fetch_timeline(max_results=10)
        assert result == []

    def test_single_page(self, twitter_client):
        from datetime import datetime, timezone
        created = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        tweet = _make_tweet_obj(tweet_id=1, text="hello", author_id=100, created_at=created)
        user = _make_user_obj(user_id=100, username="alice", name="Alice")

        mock_resp = SimpleNamespace(
            data=[tweet],
            includes={"users": [user]},
            meta={"result_count": 1},
        )
        twitter_client._fetch_with_retry = MagicMock(return_value=mock_resp)

        result = twitter_client.fetch_timeline(max_results=10)

        assert len(result) == 1
        assert result[0]["tweet_id"] == "1"
        assert result[0]["author_username"] == "alice"

    def test_pagination(self, twitter_client):
        from datetime import datetime, timezone
        created = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)

        tweet1 = _make_tweet_obj(tweet_id=1, text="first", author_id=100, created_at=created)
        tweet2 = _make_tweet_obj(tweet_id=2, text="second", author_id=100, created_at=created)
        user = _make_user_obj(user_id=100, username="alice", name="Alice")

        page1 = SimpleNamespace(
            data=[tweet1],
            includes={"users": [user]},
            meta={"result_count": 1, "next_token": "page2"},
        )
        page2 = SimpleNamespace(
            data=[tweet2],
            includes={"users": [user]},
            meta={"result_count": 1},
        )

        twitter_client._fetch_with_retry = MagicMock(side_effect=[page1, page2])

        result = twitter_client.fetch_timeline(max_results=10)

        assert len(result) == 2
        assert result[0]["tweet_id"] == "1"
        assert result[1]["tweet_id"] == "2"

    def test_skips_tweet_without_author(self, twitter_client):
        from datetime import datetime, timezone
        created = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        tweet = _make_tweet_obj(tweet_id=1, text="hello", author_id=999, created_at=created)
        user = _make_user_obj(user_id=100, username="alice", name="Alice")  # different id

        mock_resp = SimpleNamespace(
            data=[tweet],
            includes={"users": [user]},
            meta={"result_count": 1},
        )
        twitter_client._fetch_with_retry = MagicMock(return_value=mock_resp)

        result = twitter_client.fetch_timeline(max_results=10)
        assert result == []
