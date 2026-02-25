"""Tests for TwitterClient."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

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

    def test_quoted_tweet_attached(self, twitter_client):
        tweet = _make_tweet_obj(
            tweet_id=42,
            text="Great thread!",
            author_id=100,
            referenced_tweets=[{"type": "quoted", "id": 999}],
        )
        author = _make_user_obj(user_id=100, username="alice", name="Alice")
        ref_tweet = _make_tweet_obj(
            tweet_id=999, text="Original content here", author_id=200
        )
        ref_author = _make_user_obj(user_id=200, username="bob", name="Bob")
        ref_map = {999: ref_tweet}
        users = {100: author, 200: ref_author}

        result = twitter_client._normalize_tweet(tweet, author, ref_map, users)

        assert result["quoted_tweet"] is not None
        assert result["quoted_tweet"]["author_username"] == "bob"
        assert result["quoted_tweet"]["text"] == "Original content here"
        assert result["quoted_tweet"]["tweet_id"] == "999"
        assert result["is_retweet"] is False

    def test_retweeted_tweet_attached(self, twitter_client):
        tweet = _make_tweet_obj(
            tweet_id=42,
            text="RT @bob: Original content",
            author_id=100,
            referenced_tweets=[{"type": "retweeted", "id": 999}],
        )
        author = _make_user_obj(user_id=100, username="alice", name="Alice")
        ref_tweet = _make_tweet_obj(
            tweet_id=999, text="Original content", author_id=200
        )
        ref_author = _make_user_obj(user_id=200, username="bob", name="Bob")
        ref_map = {999: ref_tweet}
        users = {100: author, 200: ref_author}

        result = twitter_client._normalize_tweet(tweet, author, ref_map, users)

        assert result["is_retweet"] is True
        assert result["quoted_tweet"] is not None
        assert result["quoted_tweet"]["author_username"] == "bob"

    def test_referenced_tweet_not_in_includes(self, twitter_client):
        tweet = _make_tweet_obj(
            tweet_id=42,
            text="Quote tweet",
            author_id=100,
            referenced_tweets=[{"type": "quoted", "id": 999}],
        )
        author = _make_user_obj(user_id=100, username="alice", name="Alice")

        result = twitter_client._normalize_tweet(tweet, author, {}, {})

        assert result["quoted_tweet"] is None

    def test_no_ref_map_defaults_none(self, twitter_client):
        tweet = _make_tweet_obj(
            tweet_id=42,
            text="Normal tweet",
            author_id=100,
        )
        author = _make_user_obj(user_id=100, username="alice", name="Alice")

        result = twitter_client._normalize_tweet(tweet, author)

        assert result["quoted_tweet"] is None

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
        assert result[0]["quoted_tweet"] is None

    def test_single_page_with_quoted_tweet(self, twitter_client):
        from datetime import datetime, timezone
        created = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        tweet = _make_tweet_obj(
            tweet_id=1, text="Great!", author_id=100, created_at=created,
            referenced_tweets=[{"type": "quoted", "id": 50}],
        )
        ref_tweet = _make_tweet_obj(tweet_id=50, text="Original post", author_id=200)
        user = _make_user_obj(user_id=100, username="alice", name="Alice")
        ref_user = _make_user_obj(user_id=200, username="bob", name="Bob")

        mock_resp = SimpleNamespace(
            data=[tweet],
            includes={"users": [user, ref_user], "tweets": [ref_tweet]},
            meta={"result_count": 1},
        )
        twitter_client._fetch_with_retry = MagicMock(return_value=mock_resp)

        result = twitter_client.fetch_timeline(max_results=10)

        assert len(result) == 1
        assert result[0]["quoted_tweet"] is not None
        assert result[0]["quoted_tweet"]["author_username"] == "bob"
        assert result[0]["quoted_tweet"]["text"] == "Original post"

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


# --- fetch_tweet ---

class TestFetchTweet:
    def test_returns_normalized_tweet(self, twitter_client):
        from datetime import datetime, timezone
        created = datetime(2025, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        tweet = _make_tweet_obj(tweet_id=42, text="Hello world", author_id=100, created_at=created)
        user = _make_user_obj(user_id=100, username="alice", name="Alice")

        mock_resp = SimpleNamespace(
            data=tweet,
            includes={"users": [user]},
        )
        twitter_client.client.get_tweet = MagicMock(return_value=mock_resp)

        result = twitter_client.fetch_tweet("42")

        assert result is not None
        assert result["tweet_id"] == "42"
        assert result["author_username"] == "alice"
        assert result["text"] == "Hello world"
        assert result["url"] == "https://twitter.com/alice/status/42"

    def test_returns_none_when_no_data(self, twitter_client):
        mock_resp = SimpleNamespace(data=None, includes=None)
        twitter_client.client.get_tweet = MagicMock(return_value=mock_resp)

        result = twitter_client.fetch_tweet("999")
        assert result is None

    def test_returns_none_when_response_is_none(self, twitter_client):
        twitter_client.client.get_tweet = MagicMock(return_value=None)

        result = twitter_client.fetch_tweet("999")
        assert result is None

    def test_returns_none_when_no_author(self, twitter_client):
        tweet = _make_tweet_obj(tweet_id=42, author_id=999)
        user = _make_user_obj(user_id=100, username="alice", name="Alice")

        mock_resp = SimpleNamespace(
            data=tweet,
            includes={"users": [user]},
        )
        twitter_client.client.get_tweet = MagicMock(return_value=mock_resp)

        result = twitter_client.fetch_tweet("42")
        assert result is None


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


# --- fetch_thread ---

class TestFetchThread:
    def _make_normalized_tweet(self, tweet_id, text="hello", author="alice", replied_to_id=None):
        """Create a normalized tweet dict with optional replied_to reference."""
        ref_tweets = None
        if replied_to_id:
            ref_tweets = [{"type": "replied_to", "id": str(replied_to_id)}]
        return {
            "tweet_id": str(tweet_id),
            "author_username": author,
            "author_name": author.title(),
            "text": text,
            "created_at": "2025-01-15T10:00:00+00:00",
            "is_retweet": False,
            "quoted_tweet": None,
            "metrics": {"likes": 0, "retweets": 0, "replies": 0, "views": 0},
            "url": f"https://twitter.com/{author}/status/{tweet_id}",
            "raw_data": {
                "id": str(tweet_id),
                "text": text,
                "author_id": "100",
                "created_at": "2025-01-15T10:00:00+00:00",
                "entities": None,
                "conversation_id": None,
                "referenced_tweets": ref_tweets,
            },
        }

    def test_walks_reply_chain_chronological(self, twitter_client):
        """Thread of 3 tweets should be returned oldest-first."""
        tweet3 = self._make_normalized_tweet(3, "third", replied_to_id=2)
        tweet2 = self._make_normalized_tweet(2, "second", replied_to_id=1)
        tweet1 = self._make_normalized_tweet(1, "first")

        twitter_client.fetch_tweet = MagicMock(side_effect=[tweet3, tweet2, tweet1])

        result = twitter_client.fetch_thread("3")

        assert result is not None
        assert len(result) == 3
        assert result[0]["tweet_id"] == "1"
        assert result[1]["tweet_id"] == "2"
        assert result[2]["tweet_id"] == "3"

    def test_stops_at_root(self, twitter_client):
        """Should stop when reaching a tweet with no replied_to."""
        tweet2 = self._make_normalized_tweet(2, "reply", replied_to_id=1)
        tweet1 = self._make_normalized_tweet(1, "root")

        twitter_client.fetch_tweet = MagicMock(side_effect=[tweet2, tweet1])

        result = twitter_client.fetch_thread("2")

        assert result is not None
        assert len(result) == 2
        assert result[0]["tweet_id"] == "1"
        assert result[1]["tweet_id"] == "2"

    def test_returns_none_when_start_not_found(self, twitter_client):
        """Should return None if starting tweet doesn't exist."""
        twitter_client.fetch_tweet = MagicMock(return_value=None)

        result = twitter_client.fetch_thread("999")

        assert result is None

    def test_single_tweet_thread(self, twitter_client):
        """A single tweet with no replied_to should return a list of one."""
        tweet = self._make_normalized_tweet(1, "solo tweet")
        twitter_client.fetch_tweet = MagicMock(return_value=tweet)

        result = twitter_client.fetch_thread("1")

        assert result is not None
        assert len(result) == 1
        assert result[0]["tweet_id"] == "1"

    def test_stops_at_max_tweets(self, twitter_client):
        """Should respect the max_tweets safety cap."""
        def make_tweet_with_parent(tweet_id_str):
            tid = int(tweet_id_str)
            if tid > 1:
                return self._make_normalized_tweet(tid, f"tweet {tid}", replied_to_id=tid - 1)
            return self._make_normalized_tweet(tid, f"tweet {tid}")

        twitter_client.fetch_tweet = MagicMock(side_effect=make_tweet_with_parent)

        result = twitter_client.fetch_thread("100", max_tweets=5)

        assert result is not None
        assert len(result) == 5
