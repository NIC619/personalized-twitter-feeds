"""Embeddings module for RAG with OpenAI embeddings and pgvector."""

import logging
import math
from typing import Optional

from openai import OpenAI

logger = logging.getLogger(__name__)


class EmbeddingManager:
    """Embedding manager for tweet similarity using OpenAI embeddings."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "text-embedding-3-small",
        db_client=None,
    ):
        self.api_key = api_key
        self.model = model
        self.db = db_client
        self._client = None
        if api_key:
            self._client = OpenAI(api_key=api_key)
            logger.info(f"EmbeddingManager initialized with model: {model}")
        else:
            logger.warning("EmbeddingManager initialized without API key — embeddings disabled")

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def generate_embedding(self, text: str) -> list[float]:
        """Generate embedding for a single text."""
        if not self._client:
            return []
        response = self._client.embeddings.create(
            model=self.model,
            input=text,
        )
        return response.data[0].embedding

    def embed_tweet_batch(self, tweets: list[dict]) -> dict[str, list[float]]:
        """Batch embed multiple tweets. OpenAI supports batch in a single call."""
        if not self._client or not tweets:
            return {}

        texts = [t["text"] for t in tweets]
        tweet_ids = [t["tweet_id"] for t in tweets]

        response = self._client.embeddings.create(
            model=self.model,
            input=texts,
        )

        result = {}
        for i, embedding_obj in enumerate(response.data):
            result[tweet_ids[i]] = embedding_obj.embedding
        return result

    def find_similar_voted_tweets(
        self,
        tweets: list[dict],
        limit: int = 5,
    ) -> list[dict]:
        """Find similar voted tweets for a batch of new tweets.

        Embeds new tweets, queries pgvector for similar tweets that have feedback.

        Returns:
            List of dicts with: tweet_id, text, author_username, vote, similarity
        """
        if not self.enabled or not self.db or not tweets:
            return []

        embeddings = self.embed_tweet_batch(tweets)
        if not embeddings:
            return []

        all_similar = []
        seen_ids = set()
        for tweet_id, embedding in embeddings.items():
            matches = self.db.find_similar_tweets(embedding, limit=limit)
            for match in matches:
                if match["tweet_id"] not in seen_ids:
                    seen_ids.add(match["tweet_id"])
                    all_similar.append(match)

        # Sort by similarity descending and cap at limit
        all_similar.sort(key=lambda x: x["similarity"], reverse=True)
        return all_similar[:limit]

    def cosine_similarity(self, vec1: list[float], vec2: list[float]) -> float:
        """Calculate cosine similarity between two vectors."""
        if not vec1 or not vec2:
            return 0.0

        dot_product = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = math.sqrt(sum(a * a for a in vec1))
        norm2 = math.sqrt(sum(b * b for b in vec2))

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot_product / (norm1 * norm2)
