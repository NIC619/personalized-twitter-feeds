#!/usr/bin/env python3
"""Count tweets in Twitter timeline for a specific day (fetches from Twitter directly)."""

import argparse
import sys
import os
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import get_settings
from src.twitter_client import TwitterClient


def parse_date(date_str: str) -> datetime:
    """Parse date string in yyyy/mm/dd format."""
    try:
        return datetime.strptime(date_str, "%Y/%m/%d")
    except ValueError:
        raise ValueError(f"Invalid date format: {date_str}. Use yyyy/mm/dd")


def count_timeline(date_str: str = None, max_tweets: int = 200, hours: int = None):
    """Count tweets in Twitter timeline.

    Args:
        date_str: Optional date in yyyy/mm/dd format (default: today)
        max_tweets: Maximum tweets to fetch
        hours: Hours to look back (overrides date if provided)
    """
    settings = get_settings()

    twitter = TwitterClient(
        api_key=settings.twitter_api_key,
        api_secret=settings.twitter_api_secret,
        access_token=settings.twitter_access_token,
        access_secret=settings.twitter_access_secret,
        bearer_token=settings.twitter_bearer_token,
    )

    # Determine hours to fetch
    if hours:
        fetch_hours = hours
    elif date_str:
        target_date = parse_date(date_str)
        now = datetime.now(timezone.utc)
        target_end = target_date.replace(tzinfo=timezone.utc) + timedelta(days=1)
        # Calculate hours from now to start of target date
        fetch_hours = int((now - target_date.replace(tzinfo=timezone.utc)).total_seconds() / 3600) + 24
    else:
        fetch_hours = 24

    print(f"Fetching up to {max_tweets} tweets from last {fetch_hours} hours...")
    print()

    tweets = twitter.fetch_timeline(max_results=max_tweets, hours=fetch_hours)

    if not tweets:
        print("No tweets found")
        return

    # Group by date
    by_date = defaultdict(list)
    for tweet in tweets:
        created = tweet.get("created_at", "")
        if created:
            date_key = created[:10]  # yyyy-mm-dd
            by_date[date_key].append(tweet)

    # Print summary
    print(f"Total tweets fetched: {len(tweets)}")
    print(f"{'─' * 50}")
    print()

    for date_key in sorted(by_date.keys(), reverse=True):
        day_tweets = by_date[date_key]
        authors = set(t["author_username"] for t in day_tweets)
        print(f"{date_key}: {len(day_tweets)} tweets from {len(authors)} authors")

    print()
    print(f"{'─' * 50}")

    # Recommendation
    if len(tweets) >= max_tweets:
        print(f"⚠️  Hit limit of {max_tweets} tweets!")
        print(f"   Consider increasing MAX_TWEETS to capture all tweets.")
        print(f"   Try: python scripts/count_twitter_timeline.py --max 300")
    else:
        print(f"✓  Fetched all available tweets ({len(tweets)} < {max_tweets} limit)")


def main():
    parser = argparse.ArgumentParser(
        description="Count tweets in Twitter timeline (fetches from Twitter directly)"
    )
    parser.add_argument(
        "date",
        nargs="?",
        default=None,
        help="Optional date in yyyy/mm/dd format"
    )
    parser.add_argument(
        "--max", "-m",
        type=int,
        default=200,
        help="Maximum tweets to fetch (default: 200)"
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=None,
        help="Hours to look back (overrides date)"
    )

    args = parser.parse_args()

    try:
        count_timeline(args.date, args.max, args.hours)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
