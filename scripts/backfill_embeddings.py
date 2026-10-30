#!/usr/bin/env python3
"""Backfill embeddings for voted items that have none.

Run after imports done without OPENAI_API_KEY, or after any outage.

Usage:
    python scripts/backfill_embeddings.py
    python scripts/backfill_embeddings.py --dry-run
    python scripts/backfill_embeddings.py --batch-size 50
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import get_settings
from src.database import DatabaseClient
from src.embeddings import EmbeddingManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Backfill embeddings for voted items")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without doing it")
    parser.add_argument("--batch-size", type=int, default=100, help="Items per OpenAI call")
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

    items = db.get_voted_items_without_embeddings()
    logger.info(f"Found {len(items)} voted items needing embeddings")
    if not items:
        return 0

    if args.dry_run:
        for it in items[:50]:
            logger.info(f"  Would embed: {it['item_id']} — {(it.get('text') or '')[:60]}...")
        logger.info(f"Dry run: {len(items)} items would be embedded")
        return 0

    embedded = 0
    for i in range(0, len(items), args.batch_size):
        batch = items[i : i + args.batch_size]
        vectors = embedding_manager.embed_tweet_batch(batch)
        embedded += db.save_embeddings(vectors)
        logger.info(f"Embedded {embedded}/{len(items)} items so far")

    logger.info(f"Backfill complete: {embedded} items embedded")
    return 0


if __name__ == "__main__":
    sys.exit(main())
