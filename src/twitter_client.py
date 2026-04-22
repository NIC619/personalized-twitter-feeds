"""X API v2 client for fetching timeline using official XDK."""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from xdk import Client as XdkClient
from xdk.oauth1_auth import OAuth1

logger = logging.getLogger(__name__)


def _full_tweet_text(tweet: dict) -> str:
    """Return the fullest available text for a tweet (note_tweet preferred)."""
    note_tweet = tweet.get("note_tweet")
    if isinstance(note_tweet, dict):
        nt_text = note_tweet.get("text")
        if nt_text:
            return nt_text
    return tweet.get("text", "")


class TwitterClient:
    """X API v2 client using OAuth 1.0a User Context via official XDK."""

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        access_token: str,
        access_secret: str,
        bearer_token: str,
    ):
        """Initialize XDK client with OAuth 1.0a credentials.

        Args:
            api_key: X API consumer key
            api_secret: X API consumer secret
            access_token: User's access token
            access_secret: User's access token secret
            bearer_token: App bearer token
        """
        oauth1 = OAuth1(
            api_key=api_key,
            api_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_secret,
            callback="http://localhost:8080/callback",
        )
        self.client = XdkClient(
            bearer_token=bearer_token,
            auth=oauth1,
        )
        # xdk does not pass a timeout to requests, so a dead connection can hang
        # indefinitely and block the caller (and any asyncio event loop it's on).
        # Inject a default (connect, read) timeout via session.request.
        _orig_request = self.client.session.request

        def _request_with_timeout(method, url, **kwargs):
            kwargs.setdefault("timeout", (10, 60))
            return _orig_request(method, url, **kwargs)

        self.client.session.request = _request_with_timeout
        # Resolve authenticated user ID (required for timeline endpoint)
        me = self.client.users.get_me()
        self.user_id = me.data["id"]
        logger.info("X client initialized (user_id=%s)", self.user_id)

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

        # Tweet fields to request
        tweet_fields = [
            "created_at",
            "public_metrics",
            "entities",
            "author_id",
            "conversation_id",
            "referenced_tweets",
            "note_tweet",
        ]
        user_fields = ["username", "name", "profile_image_url"]
        expansions = ["author_id", "referenced_tweets.id", "referenced_tweets.id.author_id"]

        for page in self._fetch_timeline_with_retry(
            max_results=min(100, max_results),
            start_time=self._format_time(start_time),
            tweet_fields=tweet_fields,
            user_fields=user_fields,
            expansions=expansions,
        ):
            if not page.data:
                break

            # Build user lookup from includes
            users = {}
            if page.includes and "users" in page.includes:
                for user in page.includes["users"]:
                    users[user["id"]] = user

            # Build referenced tweets lookup from includes
            ref_tweets_map = {}
            if page.includes and "tweets" in page.includes:
                for ref_tweet in page.includes["tweets"]:
                    ref_tweets_map[ref_tweet["id"]] = ref_tweet

            # Process tweets
            for tweet in page.data:
                author = users.get(tweet.get("author_id"))
                if not author:
                    logger.warning("No author found for tweet %s", tweet.get("id"))
                    continue

                normalized = self._normalize_tweet(tweet, author, ref_tweets_map, users)
                tweets.append(normalized)

            if len(tweets) >= max_results:
                tweets = tweets[:max_results]
                break

        logger.info("Fetched %d tweets from timeline", len(tweets))
        return tweets

    def _fetch_timeline_with_retry(
        self,
        max_results: int,
        start_time: str,
        tweet_fields: list[str],
        user_fields: list[str],
        expansions: list[str],
        max_retries: int = 3,
    ):
        """Fetch timeline pages with exponential backoff retry.

        Yields pages from the XDK auto-pagination generator.
        Retries the entire generator on transient errors.
        """
        for attempt in range(max_retries):
            try:
                yield from self.client.users.get_timeline(
                    id=self.user_id,
                    max_results=max_results,
                    start_time=start_time,
                    tweet_fields=tweet_fields,
                    user_fields=user_fields,
                    expansions=expansions,
                )
                return
            except requests.exceptions.HTTPError as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** (attempt + 1)
                    logger.warning(
                        "X API error (attempt %d): %s. Retrying in %ds...",
                        attempt + 1, e, wait_time,
                    )
                    time.sleep(wait_time)
                else:
                    raise

    def _normalize_tweet(
        self, tweet: dict, author: dict, referenced_tweets_map=None, users=None
    ) -> dict:
        """Normalize tweet data into standard format.

        Args:
            tweet: Tweet dict from XDK response
            author: User dict from XDK response includes
            referenced_tweets_map: Optional dict of tweet_id → tweet dicts
                from response.includes["tweets"]
            users: Optional dict of user_id → user dicts
                from response.includes["users"]

        Returns:
            Normalized tweet dictionary
        """
        metrics = tweet.get("public_metrics") or {}
        referenced_tweets_map = referenced_tweets_map or {}
        users = users or {}

        # Detect retweets via referenced_tweets
        referenced = tweet.get("referenced_tweets") or []
        is_retweet = any(ref["type"] == "retweeted" for ref in referenced)

        # Look up quoted/retweeted tweet from includes.
        # For pure retweets we treat the original's content as the subject — text is
        # replaced with the original below, quoted_tweet stays None, and retweeted_from
        # preserves the original author for display/attribution.
        quoted_tweet = None
        retweeted_from = None
        ref_tweet_for_retweet = None
        for ref in referenced:
            if ref["type"] not in ("quoted", "retweeted"):
                continue
            ref_id = str(ref["id"])
            ref_tweet = referenced_tweets_map.get(ref_id)
            if ref_tweet:
                ref_author = users.get(ref_tweet.get("author_id"))
                ref_username = ref_author["username"] if ref_author else "unknown"
                ref_name = ref_author["name"] if ref_author else "Unknown"
                if ref["type"] == "retweeted":
                    retweeted_from = {
                        "author_username": ref_username,
                        "author_name": ref_name,
                        "tweet_id": str(ref_tweet["id"]),
                    }
                    ref_tweet_for_retweet = ref_tweet
                else:
                    quoted_tweet = {
                        "author_username": ref_username,
                        "author_name": ref_name,
                        "text": _full_tweet_text(ref_tweet),
                        "tweet_id": str(ref_tweet["id"]),
                    }
            break

        # For retweets, the outer tweet's text is a truncated "RT @user: ..." preview.
        # Use the original's full content so downstream scoring and embeddings see what
        # the user is actually judging.
        if is_retweet and ref_tweet_for_retweet is not None:
            full_text = _full_tweet_text(ref_tweet_for_retweet)
        else:
            full_text = _full_tweet_text(tweet)

        # Extract article info if present — check outer tweet first, then referenced tweet
        article = self._extract_article(tweet)
        if not article:
            for ref in referenced:
                if ref["type"] in ("quoted", "retweeted"):
                    ref_id = str(ref["id"])
                    ref_tweet = referenced_tweets_map.get(ref_id)
                    if ref_tweet:
                        article = self._extract_article(ref_tweet)
                        if article:
                            break

        created_at = tweet.get("created_at")

        return {
            "tweet_id": str(tweet["id"]),
            "author_username": author["username"],
            "author_name": author["name"],
            "text": full_text,
            "created_at": created_at,
            "is_retweet": is_retweet,
            "quoted_tweet": quoted_tweet,
            "retweeted_from": retweeted_from,
            "article": article,
            "metrics": {
                "likes": metrics.get("like_count", 0),
                "retweets": metrics.get("retweet_count", 0),
                "replies": metrics.get("reply_count", 0),
                "views": metrics.get("impression_count", 0),
            },
            "url": self.get_tweet_url(str(tweet["id"]), author["username"]),
            "raw_data": {
                "id": str(tweet["id"]),
                "text": full_text,
                "author_id": str(tweet.get("author_id", "")),
                "created_at": created_at,
                "entities": tweet.get("entities"),
                "conversation_id": str(tweet["conversation_id"]) if tweet.get("conversation_id") else None,
                "referenced_tweets": [
                    {"type": ref["type"], "id": str(ref["id"])}
                    for ref in referenced
                ] if referenced else None,
                "retweeted_from": retweeted_from,
            },
        }

    def fetch_user_tweets(
        self, usernames: list[str], max_per_user: int = 10, hours: int = 24
    ) -> list[dict]:
        """Fetch recent tweets from specific users' timelines.

        Args:
            usernames: List of X usernames to fetch tweets from
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
            "note_tweet",
        ]
        user_fields = ["username", "name", "profile_image_url"]
        expansions = ["author_id", "referenced_tweets.id", "referenced_tweets.id.author_id"]

        all_tweets = []
        for username in usernames:
            try:
                # Resolve username to user ID
                user_response = self._get_user_with_retry(username)
                if not user_response or not user_response.data:
                    logger.warning("Could not resolve user: @%s", username)
                    continue

                user = user_response.data
                user_id = user["id"]

                # Fetch user's recent tweets (first page only)
                count = 0
                for page in self._get_user_tweets_with_retry(
                    user_id=user_id,
                    max_results=max(max_per_user, 5),  # API minimum is 5
                    start_time=self._format_time(start_time),
                    tweet_fields=tweet_fields,
                    user_fields=user_fields,
                    expansions=expansions,
                ):
                    if not page.data:
                        break

                    # Build user lookup from includes
                    users = {}
                    if page.includes and "users" in page.includes:
                        for u in page.includes["users"]:
                            users[u["id"]] = u

                    # Build referenced tweets lookup from includes
                    ref_tweets_map = {}
                    if page.includes and "tweets" in page.includes:
                        for ref_tweet in page.includes["tweets"]:
                            ref_tweets_map[ref_tweet["id"]] = ref_tweet

                    for tweet in page.data:
                        if count >= max_per_user:
                            break
                        author = users.get(tweet.get("author_id"), user)
                        all_tweets.append(self._normalize_tweet(tweet, author, ref_tweets_map, users))
                        count += 1

                    break  # only need first page per user

                logger.info("Fetched %d tweets from @%s", count, username)

            except requests.exceptions.HTTPError as e:
                logger.error("Error fetching tweets for @%s: %s", username, e)
                continue

        logger.info(
            "Fetched %d total tweets from %d starred authors",
            len(all_tweets), len(usernames),
        )
        return all_tweets

    def _get_user_with_retry(self, username: str, max_retries: int = 3):
        """Resolve username to user object with retry."""
        for attempt in range(max_retries):
            try:
                return self.client.users.get_by_username(username=username)
            except requests.exceptions.HTTPError as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** (attempt + 1)
                    logger.warning(
                        "Error resolving @%s (attempt %d): %s. Retrying in %ds...",
                        username, attempt + 1, e, wait_time,
                    )
                    time.sleep(wait_time)
                else:
                    raise

    def _get_user_tweets_with_retry(
        self,
        user_id: str,
        max_results: int,
        start_time: str,
        tweet_fields: list[str],
        user_fields: list[str],
        expansions: list[str],
        max_retries: int = 3,
    ):
        """Fetch user tweets with retry. Yields pages."""
        for attempt in range(max_retries):
            try:
                yield from self.client.users.get_posts(
                    id=user_id,
                    max_results=max_results,
                    start_time=start_time,
                    tweet_fields=tweet_fields,
                    user_fields=user_fields,
                    expansions=expansions,
                )
                return
            except requests.exceptions.HTTPError as e:
                if attempt < max_retries - 1:
                    wait_time = 2 ** (attempt + 1)
                    logger.warning(
                        "Error fetching tweets for user %s (attempt %d): %s. Retrying in %ds...",
                        user_id, attempt + 1, e, wait_time,
                    )
                    time.sleep(wait_time)
                else:
                    raise

    def fetch_tweet(self, tweet_id: str) -> dict | None:
        """Fetch a single tweet by ID.

        Args:
            tweet_id: Tweet ID

        Returns:
            Normalized tweet dict, or None if not found
        """
        tweet_fields = [
            "created_at",
            "public_metrics",
            "entities",
            "author_id",
            "conversation_id",
            "referenced_tweets",
            "note_tweet",
        ]
        user_fields = ["username", "name", "profile_image_url"]
        expansions = ["author_id", "referenced_tweets.id", "referenced_tweets.id.author_id"]

        for attempt in range(3):
            try:
                response = self.client.posts.get_by_id(
                    id=tweet_id,
                    tweet_fields=tweet_fields,
                    user_fields=user_fields,
                    expansions=expansions,
                )
                break
            except requests.exceptions.HTTPError as e:
                if attempt < 2:
                    wait_time = 2 ** (attempt + 1)
                    logger.warning(
                        "Error fetching tweet %s (attempt %d): %s. Retrying in %ds...",
                        tweet_id, attempt + 1, e, wait_time,
                    )
                    time.sleep(wait_time)
                else:
                    raise

        if not response or not response.data:
            return None

        tweet = response.data

        # Build user lookup from includes
        users = {}
        if response.includes and "users" in response.includes:
            for user in response.includes["users"]:
                users[user["id"]] = user

        # Build referenced tweets lookup from includes
        ref_tweets_map = {}
        if response.includes and "tweets" in response.includes:
            for ref_tweet in response.includes["tweets"]:
                ref_tweets_map[ref_tweet["id"]] = ref_tweet

        author = users.get(tweet.get("author_id"))
        if not author:
            logger.warning("No author found for tweet %s", tweet_id)
            return None

        return self._normalize_tweet(tweet, author, ref_tweets_map, users)

    def fetch_thread(self, tweet_id: str, max_tweets: int = 50) -> list[dict] | None:
        """Fetch a full thread by walking the reply chain backwards from a tweet.

        Starting from the given tweet, follows replied_to references upward
        until reaching the root tweet (no more replied_to references).

        Args:
            tweet_id: Tweet ID of the last tweet in the thread
            max_tweets: Safety cap to avoid runaway chains (default 50)

        Returns:
            List of normalized tweet dicts sorted chronologically (oldest first),
            or None if the starting tweet was not found
        """
        tweets = []
        current_id = tweet_id

        for _ in range(max_tweets):
            tweet = self.fetch_tweet(current_id)
            if not tweet:
                if not tweets:
                    return None
                break

            tweets.append(tweet)

            # Look for replied_to reference to walk up the chain
            ref_tweets = tweet.get("raw_data", {}).get("referenced_tweets") or []
            parent_id = None
            for ref in ref_tweets:
                if ref["type"] == "replied_to":
                    parent_id = ref["id"]
                    break

            if not parent_id:
                break

            current_id = parent_id

        # Return in chronological order (oldest first)
        tweets.reverse()
        return tweets

    @staticmethod
    def _extract_article(tweet: dict) -> dict | None:
        """Extract article info from a tweet dict if present."""
        article_data = tweet.get("article")
        if not article_data or not isinstance(article_data, dict):
            return None

        article_title = article_data.get("title")
        if not article_title:
            return None

        article_body = article_data.get("plain_text")
        article_url = None
        entities = tweet.get("entities")
        if entities and "urls" in entities:
            for url_entity in entities["urls"]:
                expanded = url_entity.get("expanded_url") or url_entity.get("unwound_url", "")
                if "/article/" in expanded:
                    article_url = expanded
                    break

        return {
            "title": article_title,
            "url": article_url,
            "body": article_body,
        }

    @staticmethod
    def _format_time(dt: datetime) -> str:
        """Format datetime as RFC 3339 for X API (no microseconds, Z suffix)."""
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def get_tweet_url(tweet_id: str, username: str) -> str:
        """Generate tweet URL.

        Args:
            tweet_id: Tweet ID
            username: Author's username

        Returns:
            Full URL to the tweet
        """
        return f"https://twitter.com/{username}/status/{tweet_id}"
