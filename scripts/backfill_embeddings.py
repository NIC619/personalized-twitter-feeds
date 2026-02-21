#!/usr/bin/env python3
"""Backfill embeddings for tweets that have feedback but no embedding.

One-time migration script to bootstrap the RAG corpus from existing feedback.

Usage:
    python scripts/backfill_embeddings.py
    python scripts/backfill_embeddings.py --dry-run
    python scripts/backfill_embeddings.py --batch-size 50
"""

import argparse
import logging
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import get_settings
from src.database import DatabaseClient
from src.embeddings import EmbeddingManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def get_tweets_needing_embeddings(db: DatabaseClient) -> list[dict]:
    """Find tweets that have feedback but no embedding."""
    try:
        # Get all tweets with feedback
        feedback_result = (
            db.client.table("feedback")
            .select("tweet_id, tweets(tweet_id, text)")
            .execute()
        )

        # Get all tweet_ids that already have embeddings
        embedding_result = (
            db.client.table("tweet_embeddings")
            .select("tweet_id")
            .execute()
        )
        embedded_ids = {r["tweet_id"] for r in embedding_result.data}

        # Filter to tweets needing embeddings
        tweets = []
        seen = set()
        for row in feedback_result.data:
            tweet = row.get("tweets")
            if not tweet or not tweet.get("text"):
                continue
            tid = tweet["tweet_id"]
            if tid not in embedded_ids and tid not in seen:
                seen.add(tid)
                tweets.append(tweet)

        return tweets
    except Exception as e:
        logger.error(f"Error querying tweets needing embeddings: {e}")
        raise


def main():
    parser = argparse.ArgumentParser(description="Backfill embeddings for voted tweets")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without doing it")
    parser.add_argument("--batch-size", type=int, default=100, help="Batch size for OpenAI API calls")
    args = parser.parse_args()

    settings = get_settings()

    if not settings.openai_api_key:
        logger.error("OPENAI_API_KEY not set in environment")
        return 1

    db = DatabaseClient(url=settings.supabase_url, key=settings.supabase_key)
    embedding_manager = EmbeddingManager(
        api_key=settings.openai_api_key,
        model=settings.embedding_model,
        db_client=db,
    )

    tweets = get_tweets_needing_embeddings(db)
    logger.info(f"Found {len(tweets)} tweets needing embeddings")

    if not tweets:
        logger.info("Nothing to backfill")
        return 0

    if args.dry_run:
        for t in tweets:
            logger.info(f"  Would embed: {t['tweet_id']} — {t['text'][:60]}...")
        logger.info(f"Dry run: {len(tweets)} tweets would be embedded")
        return 0

    # Process in batches
    embedded = 0
    for i in range(0, len(tweets), args.batch_size):
        batch = tweets[i : i + args.batch_size]
        logger.info(f"Processing batch {i // args.batch_size + 1} ({len(batch)} tweets)...")

        embeddings = embedding_manager.embed_tweet_batch(batch)
        for tweet_id, embedding in embeddings.items():
            db.save_embedding(tweet_id, embedding)
            embedded += 1

        logger.info(f"Embedded {embedded}/{len(tweets)} tweets so far")

    logger.info(f"Backfill complete: {embedded} tweets embedded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
