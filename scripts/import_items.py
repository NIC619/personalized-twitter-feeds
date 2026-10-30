#!/usr/bin/env python3
"""Import things you already liked elsewhere into the preference store.

Every imported row becomes an item plus an upvote signal
(source = import:<name>) and, when OPENAI_API_KEY is set, an embedding so it
immediately acts as a neighbour for scoring.

Sources:

    # Generic CSV with a url column (title, note/excerpt, date optional)
    python scripts/import_items.py csv likes.csv --name pocket

    # Raindrop.io export (Settings → Backups → CSV)
    python scripts/import_items.py raindrop raindrop.csv

    # Readwise Reader export CSV
    python scripts/import_items.py readwise reader.csv

    # Twitter/X data archive: data/like.js (liked tweets with text)
    python scripts/import_items.py twitter-archive like.js --username yourhandle

Options:
    --dry-run        parse and report, write nothing
    --tag-domains    run the scorer to assign domains (costs Claude calls)
    --limit N        import at most N rows
"""

import argparse
import csv
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.content import generate_blog_id  # noqa: E402

logger = logging.getLogger("import")

URL_KEYS = ("url", "URL", "link", "Link", "href", "Source URL")
TITLE_KEYS = ("title", "Title", "name", "Name")
NOTE_KEYS = ("note", "Note", "notes", "Notes", "excerpt", "Excerpt", "highlight", "Highlights", "Document tags")
DATE_KEYS = ("created", "Created", "date", "Date", "created_at", "Saved date", "time_added", "Last Highlighted")


def _first(row: dict, keys) -> str:
    for k in keys:
        v = row.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return ""


def _iso(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return datetime.now(timezone.utc).isoformat()
    if value.isdigit():  # unix seconds (Pocket)
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return datetime.now(timezone.utc).isoformat()


def parse_csv(path: Path, name: str) -> list[dict]:
    """Generic CSV / Raindrop / Readwise: any file with a url-ish column."""
    items = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            url = _first(row, URL_KEYS)
            if not url.startswith(("http://", "https://")):
                continue
            title = _first(row, TITLE_KEYS)
            note = _first(row, NOTE_KEYS)
            text = title or url
            if note and note != title:
                text = f"{text}\n\n{note[:500]}"
            items.append({
                "item_id": generate_blog_id(url),
                "source": f"import:{name}",
                "content_type": "blog_post",
                "url": url,
                "title": title or None,
                "author_username": _domain(url),
                "author_name": _first(row, ("author", "Author")) or _domain(url),
                "text": text,
                "created_at": _iso(_first(row, DATE_KEYS)),
                "metrics": {},
                "raw_data": {"source": f"import:{name}", "row": {k: v for k, v in row.items() if v}},
                "_note": note or None,
            })
    return items


def parse_twitter_archive(path: Path, username: str) -> list[dict]:
    """data/like.js from a Twitter/X archive: window.YTD.like.part0 = [{like:{tweetId, fullText, expandedUrl}}]."""
    raw = path.read_text(encoding="utf-8")
    raw = re.sub(r"^window\.YTD\.like\.part\d+\s*=\s*", "", raw.strip())
    data = json.loads(raw)
    items = []
    for entry in data:
        like = entry.get("like") or {}
        tweet_id = like.get("tweetId")
        if not tweet_id:
            continue
        text = like.get("fullText") or ""
        url = like.get("expandedUrl") or f"https://x.com/i/web/status/{tweet_id}"
        items.append({
            "item_id": str(tweet_id),
            "source": "import:twitter-likes",
            "content_type": "tweet",
            "url": url,
            "title": None,
            "author_username": "unknown",   # like.js has no author; the URL is /i/web/status/<id>
            "author_name": "unknown",
            "text": text or url,
            "created_at": None,
            "metrics": {},
            "raw_data": {"source": "import:twitter-likes", "liked_by": username},
            "_note": None,
        })
    return items


def _domain(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1) if m else "unknown"


def run(args) -> int:
    from config.settings import get_settings
    from src.database import DatabaseClient
    from src.embeddings import EmbeddingManager

    path = Path(args.file)
    if args.source == "twitter-archive":
        items = parse_twitter_archive(path, args.username or "me")
    else:
        name = args.name or args.source
        items = parse_csv(path, name)
    if args.limit:
        items = items[: args.limit]
    logger.info(f"Parsed {len(items)} items from {path}")
    if not items:
        return 1
    if args.dry_run:
        for it in items[:20]:
            logger.info(f"  {it['item_id']}  {(it.get('title') or it['text'])[:70]}")
        logger.info("Dry run: nothing written")
        return 0

    settings = get_settings()
    db = DatabaseClient(url=settings.supabase_url, key=settings.supabase_key)

    # Skip items already present (keeps re-runs idempotent for votes too)
    existing = set(db.get_items([i["item_id"] for i in items]).keys())
    new_items = [i for i in items if i["item_id"] not in existing]
    logger.info(f"{len(existing)} already in store, importing {len(new_items)}")
    if not new_items:
        return 0

    if args.tag_domains and settings.anthropic_api_key:
        from src.claude_filter import ClaudeFilter
        claude = ClaudeFilter(api_key=settings.anthropic_api_key, model=settings.claude_model)
        claude.filter_tweets(new_items, threshold=0)

    for i in range(0, len(new_items), 200):
        batch = new_items[i : i + 200]
        db.save_items([{k: v for k, v in it.items() if not k.startswith("_")} for it in batch])
        db.save_signals([
            {
                "item_id": it["item_id"],
                "kind": "vote",
                "value": "up",
                "strength": 1,
                "reason": it.get("_note"),
                "source": it["source"],
                "created_at": it.get("created_at"),
            }
            for it in batch
        ])
        logger.info(f"Wrote {min(i + 200, len(new_items))}/{len(new_items)}")

    emb = EmbeddingManager(api_key=settings.openai_api_key, model=settings.embedding_model, db_client=db)
    if emb.enabled:
        logger.info("Embedding imported items...")
        vectors = emb.embed_tweet_batch(new_items)
        db.save_embeddings(vectors)
        logger.info(f"Embedded {len(vectors)} items")
    else:
        logger.warning("OPENAI_API_KEY unset: run scripts/backfill_embeddings.py later")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source", choices=["csv", "raindrop", "readwise", "twitter-archive"])
    parser.add_argument("file")
    parser.add_argument("--name", help="Source label for csv imports (default: source)")
    parser.add_argument("--username", help="Your handle, recorded with twitter-archive imports")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tag-domains", action="store_true", help="Score items to assign domains (Claude calls)")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
