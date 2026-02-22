"""Shared fixtures for tests."""

import pytest


@pytest.fixture
def sample_tweet():
    """A single normalized tweet dict as produced by TwitterClient._normalize_tweet."""
    return {
        "tweet_id": "123456789",
        "author_username": "vitalikbuterin",
        "author_name": "Vitalik Buterin",
        "text": "New EIP proposal for blob fee market adjustments",
        "created_at": "2025-01-15T10:30:00+00:00",
        "is_retweet": False,
        "quoted_tweet": None,
        "metrics": {
            "likes": 500,
            "retweets": 120,
            "replies": 45,
            "views": 25000,
        },
        "url": "https://twitter.com/vitalikbuterin/status/123456789",
        "raw_data": {
            "id": "123456789",
            "text": "New EIP proposal for blob fee market adjustments",
            "author_id": "111",
            "created_at": "2025-01-15T10:30:00+00:00",
            "entities": None,
            "conversation_id": None,
        },
    }


@pytest.fixture
def sample_tweets(sample_tweet):
    """Multiple tweet dicts for batch operations."""
    tweet2 = {
        "tweet_id": "987654321",
        "author_username": "protolambda",
        "author_name": "protolambda",
        "text": "Based rollup sequencer design exploration thread",
        "created_at": "2025-01-15T11:00:00+00:00",
        "is_retweet": False,
        "quoted_tweet": None,
        "metrics": {"likes": 200, "retweets": 80, "replies": 30, "views": 12000},
        "url": "https://twitter.com/protolambda/status/987654321",
        "raw_data": {"id": "987654321", "text": "Based rollup sequencer design exploration thread", "author_id": "222", "created_at": "2025-01-15T11:00:00+00:00", "entities": None, "conversation_id": None},
    }
    tweet3 = {
        "tweet_id": "111222333",
        "author_username": "memecoinshiller",
        "author_name": "Meme Lord",
        "text": "BUY $DOGE TO THE MOON 🚀🚀🚀",
        "created_at": "2025-01-15T12:00:00+00:00",
        "is_retweet": False,
        "quoted_tweet": None,
        "metrics": {"likes": 5000, "retweets": 2000, "replies": 800, "views": 100000},
        "url": "https://twitter.com/memecoinshiller/status/111222333",
        "raw_data": {"id": "111222333", "text": "BUY $DOGE TO THE MOON", "author_id": "333", "created_at": "2025-01-15T12:00:00+00:00", "entities": None, "conversation_id": None},
    }
    return [sample_tweet, tweet2, tweet3]


@pytest.fixture
def sample_retweet():
    """A retweet dict."""
    return {
        "tweet_id": "444555666",
        "author_username": "someuser",
        "author_name": "Some User",
        "text": "RT @vitalikbuterin: New EIP proposal...",
        "created_at": "2025-01-15T13:00:00+00:00",
        "is_retweet": True,
        "quoted_tweet": None,
        "metrics": {"likes": 0, "retweets": 0, "replies": 0, "views": 0},
        "url": "https://twitter.com/someuser/status/444555666",
        "raw_data": {"id": "444555666", "text": "RT @vitalikbuterin: New EIP proposal...", "author_id": "444", "created_at": "2025-01-15T13:00:00+00:00", "entities": None, "conversation_id": None},
    }


@pytest.fixture
def sample_quote_tweet():
    """A tweet that quotes another tweet."""
    return {
        "tweet_id": "555666777",
        "author_username": "researcher",
        "author_name": "Researcher",
        "text": "This is a great analysis!",
        "created_at": "2025-01-15T14:00:00+00:00",
        "is_retweet": False,
        "quoted_tweet": {
            "author_username": "vitalikbuterin",
            "author_name": "Vitalik Buterin",
            "text": "Deep dive into blob fee market dynamics and EIP-4844 implications",
            "tweet_id": "888999000",
        },
        "metrics": {"likes": 300, "retweets": 50, "replies": 20, "views": 15000},
        "url": "https://twitter.com/researcher/status/555666777",
        "raw_data": {
            "id": "555666777",
            "text": "This is a great analysis!",
            "author_id": "555",
            "created_at": "2025-01-15T14:00:00+00:00",
            "entities": None,
            "conversation_id": None,
        },
    }


@pytest.fixture
def claude_scores():
    """Sample Claude scoring response (parsed)."""
    return [
        {"tweet_id": "123456789", "score": 85, "reason": "Relevant EIP discussion"},
        {"tweet_id": "987654321", "score": 92, "reason": "Based rollup design — core interest"},
        {"tweet_id": "111222333", "score": 10, "reason": "Meme coin promotion"},
    ]
