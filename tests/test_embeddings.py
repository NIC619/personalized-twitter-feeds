"""Tests for EmbeddingManager."""

import math
from unittest.mock import MagicMock, patch

import pytest

from src.embeddings import EmbeddingManager


@pytest.fixture
def manager():
    return EmbeddingManager()


@pytest.fixture
def mock_openai_manager():
    """EmbeddingManager with mocked OpenAI client."""
    with patch("src.embeddings.OpenAI") as MockOpenAI:
        mock_client = MagicMock()
        MockOpenAI.return_value = mock_client
        mgr = EmbeddingManager(api_key="fake-key", model="text-embedding-3-small")
    mgr._client = mock_client
    return mgr


class TestCosineSimilarity:
    def test_identical_vectors(self, manager):
        vec = [1.0, 2.0, 3.0]
        assert manager.cosine_similarity(vec, vec) == pytest.approx(1.0)

    def test_orthogonal_vectors(self, manager):
        vec1 = [1.0, 0.0]
        vec2 = [0.0, 1.0]
        assert manager.cosine_similarity(vec1, vec2) == pytest.approx(0.0)

    def test_opposite_vectors(self, manager):
        vec1 = [1.0, 0.0]
        vec2 = [-1.0, 0.0]
        assert manager.cosine_similarity(vec1, vec2) == pytest.approx(-1.0)

    def test_empty_vec1(self, manager):
        assert manager.cosine_similarity([], [1.0, 2.0]) == 0.0

    def test_empty_vec2(self, manager):
        assert manager.cosine_similarity([1.0, 2.0], []) == 0.0

    def test_both_empty(self, manager):
        assert manager.cosine_similarity([], []) == 0.0

    def test_zero_norm_vec1(self, manager):
        assert manager.cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0

    def test_zero_norm_vec2(self, manager):
        assert manager.cosine_similarity([1.0, 2.0], [0.0, 0.0]) == 0.0

    def test_known_angle(self, manager):
        # 45-degree angle: cos(45) ~ 0.7071
        vec1 = [1.0, 0.0]
        vec2 = [1.0, 1.0]
        expected = 1.0 / math.sqrt(2)
        assert manager.cosine_similarity(vec1, vec2) == pytest.approx(expected)


class TestEnabled:
    def test_disabled_without_api_key(self):
        mgr = EmbeddingManager()
        assert mgr.enabled is False

    def test_enabled_with_api_key(self, mock_openai_manager):
        assert mock_openai_manager.enabled is True


class TestGenerateEmbedding:
    def test_returns_empty_when_disabled(self, manager):
        assert manager.generate_embedding("hello") == []

    def test_calls_openai_api(self, mock_openai_manager):
        mock_embedding = MagicMock()
        mock_embedding.embedding = [0.1, 0.2, 0.3]
        mock_response = MagicMock()
        mock_response.data = [mock_embedding]
        mock_openai_manager._client.embeddings.create.return_value = mock_response

        result = mock_openai_manager.generate_embedding("test text")

        assert result == [0.1, 0.2, 0.3]
        mock_openai_manager._client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-small",
            input="test text",
        )


class TestEmbedTweetBatch:
    def test_returns_empty_when_disabled(self, manager):
        assert manager.embed_tweet_batch([{"tweet_id": "1", "text": "hi"}]) == {}

    def test_returns_empty_for_empty_input(self, mock_openai_manager):
        assert mock_openai_manager.embed_tweet_batch([]) == {}

    def test_batch_embeds_tweets(self, mock_openai_manager):
        emb1 = MagicMock()
        emb1.embedding = [0.1, 0.2]
        emb2 = MagicMock()
        emb2.embedding = [0.3, 0.4]
        mock_response = MagicMock()
        mock_response.data = [emb1, emb2]
        mock_openai_manager._client.embeddings.create.return_value = mock_response

        tweets = [
            {"tweet_id": "a", "text": "first"},
            {"tweet_id": "b", "text": "second"},
        ]
        result = mock_openai_manager.embed_tweet_batch(tweets)

        assert result == {"a": [0.1, 0.2], "b": [0.3, 0.4]}
        mock_openai_manager._client.embeddings.create.assert_called_once_with(
            model="text-embedding-3-small",
            input=["first", "second"],
        )


class TestFindSimilarVotedTweets:
    def test_returns_empty_when_disabled(self, manager):
        assert manager.find_similar_voted_tweets([{"tweet_id": "1", "text": "hi"}]) == []

    def test_returns_empty_for_empty_input(self, mock_openai_manager):
        assert mock_openai_manager.find_similar_voted_tweets([]) == []

    def test_queries_db_and_deduplicates(self, mock_openai_manager):
        mock_openai_manager.db = MagicMock()

        # Mock embed_tweet_batch
        emb1 = MagicMock()
        emb1.embedding = [0.1]
        emb2 = MagicMock()
        emb2.embedding = [0.2]
        mock_response = MagicMock()
        mock_response.data = [emb1, emb2]
        mock_openai_manager._client.embeddings.create.return_value = mock_response

        # DB returns overlapping results for the two queries
        mock_openai_manager.db.find_similar_tweets.side_effect = [
            [
                {"tweet_id": "x", "text": "liked", "author_username": "a", "vote": "up", "similarity": 0.9},
                {"tweet_id": "y", "text": "disliked", "author_username": "b", "vote": "down", "similarity": 0.8},
            ],
            [
                {"tweet_id": "x", "text": "liked", "author_username": "a", "vote": "up", "similarity": 0.85},
                {"tweet_id": "z", "text": "another", "author_username": "c", "vote": "up", "similarity": 0.7},
            ],
        ]

        tweets = [
            {"tweet_id": "1", "text": "new tweet 1"},
            {"tweet_id": "2", "text": "new tweet 2"},
        ]
        result = mock_openai_manager.find_similar_voted_tweets(tweets, limit=5)

        # Should deduplicate tweet "x" and sort by similarity
        assert len(result) == 3
        assert result[0]["tweet_id"] == "x"
        assert result[0]["similarity"] == 0.9
        assert result[1]["tweet_id"] == "y"
        assert result[2]["tweet_id"] == "z"
