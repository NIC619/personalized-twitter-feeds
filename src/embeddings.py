"""Embeddings module for RAG with OpenAI embeddings and pgvector."""

import logging
import math
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from openai import OpenAI

logger = logging.getLogger(__name__)

# OpenAI accepts up to 2048 inputs per embeddings call; stay well under.
EMBED_BATCH = 500


class EmbeddingManager:
    """Embedding manager for item similarity using OpenAI embeddings."""

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
        response = self._client.embeddings.create(model=self.model, input=text)
        return response.data[0].embedding

    def embed_tweet_batch(self, tweets: list[dict]) -> dict[str, list[float]]:
        """Embed many items, returning item_id -> vector."""
        if not self._client or not tweets:
            return {}

        result: dict[str, list[float]] = {}
        for i in range(0, len(tweets), EMBED_BATCH):
            chunk = tweets[i : i + EMBED_BATCH]
            texts = [self._embed_text(t) for t in chunk]
            response = self._client.embeddings.create(model=self.model, input=texts)
            for t, emb in zip(chunk, response.data):
                result[t["item_id"]] = emb.embedding
        return result

    @staticmethod
    def _embed_text(tweet: dict) -> str:
        """Text used for embedding: tweet text plus quoted/article text if present."""
        parts = [tweet.get("text") or ""]
        quoted = tweet.get("quoted_tweet")
        if quoted and quoted.get("text"):
            parts.append(quoted["text"])
        article = tweet.get("article")
        if article and article.get("title"):
            parts.append(article["title"])
        text = "\n".join(p for p in parts if p).strip()
        return text or "(empty)"

    def find_neighbors(
        self,
        tweets: list[dict],
        k: int = 3,
        exclude_self: bool = True,
        workers: int = 8,
        embeddings: Optional[dict[str, list[float]]] = None,
    ) -> dict[str, list[dict]]:
        """Find the k most similar voted items for *each* input item.

        Args:
            embeddings: Optional precomputed item_id -> vector map (e.g. read
                back from the database). When given, no OpenAI call is made.

        Returns:
            Map item_id -> list of dicts with item_id, text, author_username,
            vote, similarity (descending). Items with no matches are absent.
        """
        if not self.db or not tweets or k <= 0:
            return {}

        if embeddings is None:
            if not self.enabled:
                return {}
            embeddings = self.embed_tweet_batch(tweets)
        if not embeddings:
            return {}

        # Ask for one extra so excluding the item itself still yields k.
        fetch = k + 1 if exclude_self else k

        def _lookup(item):
            item_id, embedding = item
            matches = self.db.find_similar_voted(embedding, limit=fetch)
            if exclude_self:
                matches = [m for m in matches if m.get("item_id") != item_id]
            return item_id, matches[:k]

        neighbors: dict[str, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for item_id, matches in pool.map(_lookup, embeddings.items()):
                if matches:
                    neighbors[item_id] = matches
        return neighbors

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
