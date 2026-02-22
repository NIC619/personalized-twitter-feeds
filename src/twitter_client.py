"""Twitter API v2 client for fetching timeline."""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import tweepy

logger = logging.getLogger(__name__)


class TwitterClient:
    """Twitter API v2 client using OAuth 1.0a User Context."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        access_token: str,
        access_secret: str,
        bearer_token: str,
    ):
        """Initialize Tweepy client with OAuth 1.0a credentials.

        Args:
            api_key: Twitter API key
            api_secret: Twitter API secret
            access_token: User's access token
            access_secret: User's access token secret
            bearer_token: App bearer token
        """
        self.client = tweepy.Client(
            bearer_token=bearer_token,
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_secret,
            wait_on_rate_limit=True,
        )
        logger.info("Twitter client initialized")

    def fetch_timeline(
        self, max_results: int = 100, hours: int = 24
    ) -> list[dict]:
        """Fetch tweets from home timeline from past N hours.

        Args:
            max_results: Maximum tweets to fetch (default 100)
            hours: Look back period (default 24)

        Returns:
            List of normalized tweet objects
        """
        start_time = datetime.now(timezone.utc) - timedelta(hours=hours)

        tweets = []
        pagination_token = None
        fetched_count = 0

        # Tweet fields to request
        tweet_fields = [
            "created_at",
            "public_metrics",
            "entities",
            "author_id",
            "conversation_id",
            "referenced_tweets",
        ]
        user_fields = ["username", "name", "profile_image_url"]
        expansions = ["author_id", "referenced_tweets.id", "referenced_tweets.id.author_id"]

        while fetched_count < max_results:
            batch_size = min(100, max_results - fetched_count)

            try:
                response = self._fetch_with_retry(
                    batch_size=batch_size,
                    start_time=start_time,
                    pagination_token=pagination_token,
                    tweet_fields=tweet_fields,
                    user_fields=user_fields,
                    expansions=expansions,
                )

                if not response or not response.data:
                    logger.info("No more tweets to fetch")
                    break

                # Build user lookup from includes
                users = {}
                if response.includes and "users" in response.includes:
                    for user in response.includes["users"]:
                        users[user.id] = user

                # Build referenced tweets lookup from includes
                ref_tweets_map = {}
                if response.includes and "tweets" in response.includes:
                    for ref_tweet in response.includes["tweets"]:
                        ref_tweets_map[ref_tweet.id] = ref_tweet

                # Process tweets
                for tweet in response.data:
                    author = users.get(tweet.author_id)
                    if not author:
                        logger.warning(f"No author found for tweet {tweet.id}")
                        continue

                    normalized = self._normalize_tweet(tweet, author, ref_tweets_map, users)
                    tweets.append(normalized)
                    fetched_count += 1

                # Check for more pages
                if response.meta and response.meta.get("next_token"):
                    pagination_token = response.meta["next_token"]
                else:
                    break

            except tweepy.TweepyException as e:
                logger.error(f"Twitter API error: {e}")
                raise

        logger.info(f"Fetched {len(tweets)} tweets from timeline")
        return tweets

    def _fetch_with_retry(
        self,
        batch_size: int,
        start_time: datetime,
        pagination_token: Optional[str],
        tweet_fields: list[str],
        user_fields: list[str],
        expansions: list[str],
        max_retries: int = 3,
    ):
        """Fetch timeline with exponential backoff retry.

        Args:
            batch_size: Number of tweets to fetch
            start_time: Start time for tweet filter
            pagination_token: Token for pagination
            tweet_fields: Tweet fields to request
            user_fields: User fields to request
            expansions: Expansions to request
            max_retries: Maximum retry attempts

        Returns:
            Tweepy response object
        """
        for attempt in range(max_retries):
            try:
                return self.client.get_home_timeline(
                    max_results=batch_size,
                    start_time=start_time,
                    pagination_token=pagination_token,
                    tweet_fields=tweet_fields,
                    user_fields=user_fields,
                    expansions=expansions,
                )
            except tweepy.TweepyException as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** (attempt + 1)
                    logger.warning(
                        f"Twitter API error (attempt {attempt + 1}): {e}. "
                        f"Retrying in {wait_time}s..."
                    )
                    time.sleep(wait_time)
                else:
                    raise

    def _normalize_tweet(
        self, tweet, author, referenced_tweets_map=None, users=None
    ) -> dict:
        """Normalize tweet data into standard format.

        Args:
            tweet: Tweepy tweet object
            author: Tweepy user object
            referenced_tweets_map: Optional dict of tweet_id → tweepy tweet objects
                from response.includes["tweets"]
            users: Optional dict of user_id → tweepy user objects
                from response.includes["users"]

        Returns:
            Normalized tweet dictionary
        """
        metrics = tweet.public_metrics or {}
        referenced_tweets_map = referenced_tweets_map or {}
        users = users or {}

        # Detect retweets via referenced_tweets
        referenced = getattr(tweet, "referenced_tweets", None) or []
        is_retweet = any(ref["type"] == "retweeted" for ref in referenced)

        # Look up quoted/retweeted tweet from includes
        quoted_tweet = None
        for ref in referenced:
            if ref["type"] in ("quoted", "retweeted"):
                ref_id = ref["id"] if isinstance(ref["id"], int) else int(ref["id"])
                ref_tweet = referenced_tweets_map.get(ref_id)
                if ref_tweet:
                    ref_author = users.get(ref_tweet.author_id)
                    quoted_tweet = {
                        "author_username": ref_author.username if ref_author else "unknown",
                        "author_name": ref_author.name if ref_author else "Unknown",
                        "text": ref_tweet.text,
                        "tweet_id": str(ref_tweet.id),
                    }
                break

        return {
            "tweet_id": str(tweet.id),
            "author_username": author.username,
            "author_name": author.name,
            "text": tweet.text,
            "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
            "is_retweet": is_retweet,
            "quoted_tweet": quoted_tweet,
            "metrics": {
                "likes": metrics.get("like_count", 0),
                "retweets": metrics.get("retweet_count", 0),
                "replies": metrics.get("reply_count", 0),
                "views": metrics.get("impression_count", 0),
            },
            "url": self.get_tweet_url(str(tweet.id), author.username),
            "raw_data": {
                "id": str(tweet.id),
                "text": tweet.text,
                "author_id": str(tweet.author_id),
                "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
                "entities": tweet.entities if hasattr(tweet, "entities") else None,
                "conversation_id": str(tweet.conversation_id) if tweet.conversation_id else None,
            },
        }

    def fetch_user_tweets(
        self, usernames: list[str], max_per_user: int = 10, hours: int = 24
    ) -> list[dict]:
        """Fetch recent tweets from specific users' timelines.

        Args:
            usernames: List of Twitter usernames to fetch tweets from
            max_per_user: Maximum tweets per user (default 10)
            hours: Look back period (default 24)

        Returns:
            List of normalized tweet objects from all users
        """
        start_time = datetime.now(timezone.utc) - timedelta(hours=hours)
        tweet_fields = [
            "created_at",
            "public_metrics",
            "entities",
            "author_id",
            "conversation_id",
            "referenced_tweets",
        ]
        user_fields = ["username", "name", "profile_image_url"]
        expansions = ["author_id", "referenced_tweets.id", "referenced_tweets.id.author_id"]

        all_tweets = []
        for username in usernames:
            try:
                # Resolve username to user ID
                user_response = self._get_user_with_retry(username)
                if not user_response or not user_response.data:
                    logger.warning(f"Could not resolve user: @{username}")
                    continue

                user = user_response.data
                user_id = user.id

                # Fetch user's recent tweets
                response = self._get_user_tweets_with_retry(
                    user_id=user_id,
                    max_results=max(max_per_user, 5),  # API minimum is 5
                    start_time=start_time,
                    tweet_fields=tweet_fields,
                    user_fields=user_fields,
                    expansions=expansions,
                )

                if not response or not response.data:
                    logger.info(f"No recent tweets from @{username}")
                    continue

                # Build user lookup from includes
                users = {}
                if response.includes and "users" in response.includes:
                    for u in response.includes["users"]:
                        users[u.id] = u

                # Build referenced tweets lookup from includes
                ref_tweets_map = {}
                if response.includes and "tweets" in response.includes:
                    for ref_tweet in response.includes["tweets"]:
                        ref_tweets_map[ref_tweet.id] = ref_tweet

                count = 0
                for tweet in response.data:
                    if count >= max_per_user:
                        break
                    author = users.get(tweet.author_id, user)
                    all_tweets.append(self._normalize_tweet(tweet, author, ref_tweets_map, users))
                    count += 1

                logger.info(f"Fetched {count} tweets from @{username}")

            except tweepy.TweepyException as e:
                logger.error(f"Error fetching tweets for @{username}: {e}")
                continue

        logger.info(
            f"Fetched {len(all_tweets)} total tweets from {len(usernames)} starred authors"
        )
        return all_tweets

    def _get_user_with_retry(self, username: str, max_retries: int = 3):
        """Resolve username to user object with retry."""
        for attempt in range(max_retries):
            try:
                return self.client.get_user(username=username)
            except tweepy.TweepyException as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** (attempt + 1)
                    logger.warning(
                        f"Error resolving @{username} (attempt {attempt + 1}): {e}. "
                        f"Retrying in {wait_time}s..."
                    )
                    time.sleep(wait_time)
                else:
                    raise

    def _get_user_tweets_with_retry(
        self,
        user_id: int,
        max_results: int,
        start_time: datetime,
        tweet_fields: list[str],
        user_fields: list[str],
        expansions: list[str],
        max_retries: int = 3,
    ):
        """Fetch user tweets with retry."""
        for attempt in range(max_retries):
            try:
                return self.client.get_users_tweets(
                    id=user_id,
                    max_results=max_results,
                    start_time=start_time,
                    tweet_fields=tweet_fields,
                    user_fields=user_fields,
                    expansions=expansions,
                )
            except tweepy.TweepyException as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** (attempt + 1)
                    logger.warning(
                        f"Error fetching tweets for user {user_id} (attempt {attempt + 1}): {e}. "
                        f"Retrying in {wait_time}s..."
                    )
                    time.sleep(wait_time)
                else:
                    raise

    @staticmethod
    def get_tweet_url(tweet_id: str, username: str) -> str:
        """Generate tweet URL.

        Args:
            tweet_id: Twitter ID of the tweet
            username: Author's username

        Returns:
            Full URL to the tweet
        """
        return f"https://twitter.com/{username}/status/{tweet_id}"
