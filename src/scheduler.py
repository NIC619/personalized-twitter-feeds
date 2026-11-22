"""Scheduler for daily tweet curation."""

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import schedule

from src.twitter_client import TwitterClient
from src.claude_filter import ClaudeFilter
from src.telegram_bot import TelegramCurator
from src.content import parent_item_id
from src.database import DatabaseClient
from src.embeddings import EmbeddingManager
from src.skips import pick_skips, skip_signal
from src.keyword_filter import filter_blocked_keywords
from src.profile_compiler import ProfileCompiler, scoring_profile

logger = logging.getLogger(__name__)



def _prefer_direct(tweets: list[dict]) -> list[dict]:
    """One copy per item_id, order preserved, a direct copy beating a retweet copy.

    Retweets are stored under the original's id, so the same original can
    arrive directly and via a retweet in one batch; the upsert must never
    hit an id twice and the direct copy carries the author's own context.
    """
    unique: dict[str, dict] = {}
    for t in tweets:
        prev = unique.get(t["item_id"])
        if prev is None or (prev.get("is_retweet") and not t.get("is_retweet")):
            unique[t["item_id"]] = t
    return list(unique.values())


class DailyCurator:
    """Orchestrates the daily tweet curation workflow."""

    def __init__(
        self,
        twitter: TwitterClient,
        claude: ClaudeFilter,
        telegram: TelegramCurator,
        db: DatabaseClient,
        fetch_hours: int = 24,
        max_tweets: int = 100,
        filter_threshold: int = 70,
        favorite_threshold_offset: int = 20,
        muted_threshold_offset: int = 15,
        starred_author_max_tweets: int = 10,
        embedding_manager: Optional[EmbeddingManager] = None,
        rag_enabled: bool = True,
        neighbors_per_item: int = 3,
        exploration_per_run: int = 2,
        skip_after_days: int = 3,
        skip_window_days: int = 10,
        reply_parent_depth: int = 5,
        profile_source: str = "compiled",
        feed_fetcher=None,
        feed_max_entries: int = 10,
        newsletter=None,
        mailbox=None,
        email_max_per_run: int = 10,
    ):
        """Initialize daily curator with all components.

        Args:
            twitter: Twitter API client
            claude: Claude filter
            telegram: Telegram bot
            db: Database client
            fetch_hours: Hours to look back for tweets
            max_tweets: Maximum tweets to fetch
            filter_threshold: Default threshold for authors
            favorite_threshold_offset: How much lower for starred authors
            muted_threshold_offset: How much higher for muted authors
            starred_author_max_tweets: Max tweets per starred author timeline
            embedding_manager: Optional EmbeddingManager for RAG
            rag_enabled: Whether to attach voted neighbours to items for scoring
            neighbors_per_item: Nearest voted neighbours attached to each item
            exploration_per_run: Random below-threshold items sent per run
            skip_after_days: Days after delivery before an unvoted, unopened
                item on an engaged digest day gets a ``skip`` signal (0 = off)
            skip_window_days: How far back skip detection looks
                (marked 🎲) so recall below the threshold can be measured
            reply_parent_depth: How many levels of replied-to tweets to pull
                into the batch as items of their own (0 disables)
            profile_source: 'compiled' loads the latest compiled profiles
                for the scoring prompt, 'static' uses the built-in baseline
            feed_fetcher: Optional FeedFetcher; subscribed RSS/Atom feeds are
                polled each run and their new entries scored like tweets
            feed_max_entries: Newest entries fetched per feed per run
            newsletter: Optional NewsletterProcessor (issue text → scored items)
            mailbox: Optional MailboxFetcher; unseen emails are processed as
                newsletter issues after the digest (and on demand)
            email_max_per_run: Max emails processed per run
        """
        self.twitter = twitter
        self.claude = claude
        self.telegram = telegram
        self.db = db
        self.embedding_manager = embedding_manager
        self.fetch_hours = fetch_hours
        self.max_tweets = max_tweets
        self.starred_author_max_tweets = starred_author_max_tweets
        self.default_threshold = filter_threshold
        self.favorite_author_threshold = filter_threshold - favorite_threshold_offset
        self.muted_author_threshold = filter_threshold + muted_threshold_offset
        # Claude floor = lowest tier so all potentially relevant tweets get scored
        self.filter_threshold = self.favorite_author_threshold
        self.rag_enabled = rag_enabled
        self.neighbors_per_item = neighbors_per_item
        self.exploration_per_run = exploration_per_run
        self.skip_after_days = skip_after_days
        self.skip_window_days = skip_window_days
        self.reply_parent_depth = reply_parent_depth
        self.profile_source = profile_source
        self.feed_fetcher = feed_fetcher
        self.feed_max_entries = feed_max_entries
        self.newsletter = newsletter
        self.mailbox = mailbox
        self.email_max_per_run = email_max_per_run
        self._inbox_lock = asyncio.Lock()
        logger.info(
            f"DailyCurator initialized (thresholds: favorite={self.favorite_author_threshold}, "
            f"default={self.default_threshold}, muted={self.muted_author_threshold})"
        )

    async def run_daily_curation(self) -> dict:
        """Run the full curation workflow.

        Workflow:
        1. Fetch tweets from Twitter timeline
        2. Filter tweets with Claude
        3. Save tweets to database
        4. Send filtered tweets to Telegram
        5. Return summary stats

        Returns:
            Dict with workflow statistics
        """
        started = datetime.now(timezone.utc)
        stats = {
            "fetched": 0,
            "filtered": 0,
            "sent": 0,
            "errors": [],
            "started_at": started.isoformat(),
        }

        # Step 0: silence on earlier digests becomes weak negative votes
        try:
            stats["skips_recorded"] = self.record_skips()
        except Exception as e:
            logger.error(f"Skip detection failed: {e}", exc_info=True)
            stats["errors"].append(f"Skip detection error: {e}")

        try:
            await self._curate_twitter(stats)
        except Exception as e:
            error_msg = f"Curation error: {str(e)}"
            logger.error(error_msg, exc_info=True)
            stats["errors"].append(error_msg)

            # Try to notify user of error
            try:
                await self.telegram.send_error_notification(error_msg)
            except Exception as notify_error:
                logger.error(f"Failed to send error notification: {notify_error}")

        # Step 5: newsletters that arrived by email, each sent as its own digest
        if self.mailbox and self.newsletter:
            try:
                stats["inbox"] = await self.process_inbox()
            except Exception as e:
                logger.error(f"Inbox processing failed: {e}", exc_info=True)
                stats["errors"].append(f"Inbox error: {e}")

        # Step 6: one footer with "rest are meh" covering everything sent this run
        if stats["sent"] or (stats.get("inbox") or {}).get("sent"):
            try:
                await self.telegram.send_digest_footer(started, datetime.now(timezone.utc))
            except Exception as e:
                logger.warning(f"Digest footer failed: {e}")

        stats["completed_at"] = datetime.now(timezone.utc).isoformat()
        logger.info(f"Curation complete: {stats}")
        return stats

    def record_skips(self) -> int:
        """Mark delivered items the reader never voted on or opened as ``skip``.

        Looks at items sent between ``skip_window_days`` and
        ``skip_after_days`` ago, on days the reader engaged with the digest
        (see :mod:`src.skips`). Returns the number of signals written.
        """
        if self.skip_after_days <= 0:
            return 0
        now = datetime.now(timezone.utc)
        until = (now - timedelta(days=self.skip_after_days)).isoformat()
        since = (now - timedelta(days=self.skip_window_days)).isoformat()
        return self._write_skips(since, until, source="ignored", include_exploration=False)

    def mark_rest_meh(self, since: datetime, until: datetime) -> int:
        """"Rest are meh": skip every unvoted, unopened item sent in the window."""
        return self._write_skips(
            since.isoformat(), until.isoformat(), source="meh", include_exploration=True,
            require_engaged_day=False,
        )

    def _write_skips(
        self, since: str, until: str, source: str,
        include_exploration: bool, require_engaged_day: bool = True,
    ) -> int:
        sent = self.db.get_sent_items(since, until)
        if not sent:
            return 0
        engaged = self.db.get_engaged_ids([i["item_id"] for i in sent])
        to_mark = pick_skips(
            sent, engaged, require_engaged_day=require_engaged_day,
            include_exploration=include_exploration,
        )
        written = 0
        for i in range(0, len(to_mark), 200):
            batch = to_mark[i : i + 200]
            written += self.db.save_signals([skip_signal(it, source) for it in batch])
        if written:
            logger.info(f"Recorded {written} skip signals ({source}) for {since[:10]}..{until[:10]}")
        return written

    async def _curate_twitter(self, stats: dict) -> None:
        """Steps 1-4: fetch, filter, score, save, send. Mutates ``stats``."""
        # Step 1: Fetch tweets from Twitter
        logger.info("Step 1: Fetching tweets from Twitter...")
        tweets = self.twitter.fetch_timeline(
            max_results=self.max_tweets,
            hours=self.fetch_hours,
        )
        stats["fetched"] = len(tweets)
        logger.info(f"Fetched {len(tweets)} tweets from home timeline")

        # Step 1a: Fetch tweets from starred authors' timelines
        favorite_authors = list(self.db.get_favorite_authors())
        if favorite_authors:
            logger.info(
                f"Step 1a: Fetching tweets from {len(favorite_authors)} starred authors..."
            )
            starred_tweets = self.twitter.fetch_user_tweets(
                usernames=favorite_authors,
                max_per_user=self.starred_author_max_tweets,
                hours=self.fetch_hours,
            )
            stats["starred_fetched"] = len(starred_tweets)

            # Merge and deduplicate by item_id. A starred author's own tweet
            # beats the home timeline's retweet copy of it.
            home_ids = {t["item_id"] for t in tweets}
            new_from_starred = sum(1 for st in starred_tweets if st["item_id"] not in home_ids)
            tweets = _prefer_direct(tweets + starred_tweets)

            stats["starred_new"] = new_from_starred
            logger.info(
                f"Starred authors: {len(starred_tweets)} total, "
                f"{new_from_starred} new (not in home timeline)"
            )

        # Step 1a2: Replies bring the tweets they answer into the batch as
        # items of their own, so a thin reply can score low while the
        # parent it answers scores (and is voted) on its own merits.
        if self.reply_parent_depth > 0 and tweets:
            parents = self._expand_reply_parents(tweets, self.reply_parent_depth)
            if parents:
                stats["reply_parents"] = len(parents)
                tweets.extend(parents)
                logger.info(f"Step 1a2: Added {len(parents)} replied-to tweets")

        # Step 1b: New entries from subscribed feeds join the batch here,
        # before the dedup / retweet / keyword steps, so they are blocked
        # by the keyword list like anything else and are still processed
        # on days when Twitter yields nothing new.
        feed_items = self._fetch_feed_items()
        if feed_items:
            stats["feed_items"] = len(feed_items)
            tweets.extend(feed_items)

        if not tweets:
            logger.info("No tweets to process")
            return

        stats["fetched"] = len(tweets)

        # Step 1c: Deduplicate - remove tweets already processed
        new_tweets = self._deduplicate_tweets(tweets)
        stats["new"] = len(new_tweets)
        stats["skipped_duplicates"] = len(tweets) - len(new_tweets)

        if stats["skipped_duplicates"] > 0:
            logger.info(
                f"Skipped {stats['skipped_duplicates']} already-processed tweets"
            )

        if not new_tweets:
            logger.info("No new tweets to process")
            return

        # Step 1d: Load author tiers
        if not favorite_authors:
            favorite_authors = list(self.db.get_favorite_authors())
        favorite_authors_set = set(favorite_authors)
        muted_authors = set(self.db.get_muted_authors())

        # Retweets are not filtered out: they are stored as the original tweet
        # and scored like anything else (the model sees who retweeted them).
        tweets_for_filtering = list(new_tweets)
        stats["retweets"] = sum(1 for t in new_tweets if t.get("is_retweet"))

        # Step 1e: Apply keyword blocklist (pre-LLM, favorite authors exempt)
        blocked_keywords = self.db.get_blocked_keywords()
        if blocked_keywords:
            kept, blocked = filter_blocked_keywords(
                tweets_for_filtering,
                blocked_keywords,
                exempt_authors=favorite_authors_set,
            )
            stats["skipped_blocked_keywords"] = len(blocked)
            if blocked:
                logger.info(
                    f"Skipped {len(blocked)} items by keyword blocklist "
                    f"({len(blocked_keywords)} keywords active)"
                )
            tweets_for_filtering = kept

        if not tweets_for_filtering:
            logger.info("No tweets remaining after keyword blocklist")
            return

        # Step 1f: Per-item nearest voted neighbours (RAG)
        neighbors: dict[str, list[dict]] = {}
        if self.rag_enabled and self.embedding_manager and self.embedding_manager.enabled:
            logger.info("Step 1f: Finding voted neighbours for each item...")
            try:
                neighbors = self.embedding_manager.find_neighbors(
                    tweets_for_filtering, k=self.neighbors_per_item
                )
                stats["rag_items_with_neighbors"] = len(neighbors)
                logger.info(
                    f"Neighbours found for {len(neighbors)}/{len(tweets_for_filtering)} items"
                )
            except Exception as e:
                logger.warning(f"Neighbour lookup failed, continuing without: {e}")

        # Step 2: Score tweets with Claude (floor threshold gets all 50+ tweets)
        logger.info("Step 2: Scoring tweets with Claude...")
        profile = self.load_scoring_profile()
        scored_tweets = self.claude.filter_tweets(
            tweets_for_filtering,
            threshold=self.filter_threshold,
            neighbors=neighbors or None,
            profile=profile,
        )

        # Step 2b: Apply per-author thresholds
        filtered_tweets = []
        tier_counts = {"favorite": 0, "muted": 0, "default": 0}
        for tweet in scored_tweets:
            author = tweet["author_username"].lower()
            score = tweet.get("filter_score")
            if score is None:
                tweet["filtered"] = False
                logger.info(f"@{author} not scored; will be retried next run")
                continue

            if author in muted_authors:
                tier = "muted"
                threshold = self.muted_author_threshold
            elif author in favorite_authors_set:
                tier = "favorite"
                threshold = self.favorite_author_threshold
            else:
                tier = "default"
                threshold = self.default_threshold

            passed = score >= threshold
            tweet["filtered"] = passed
            tier_counts[tier] += 1

            if passed:
                filtered_tweets.append(tweet)
                logger.info(
                    f"[{tier}] @{author} score={score} threshold={threshold} → PASS"
                )
            else:
                logger.info(
                    f"[{tier}] @{author} score={score} threshold={threshold} → SKIP"
                )

        # Step 2c: Exploration — a few random below-threshold items,
        # marked so the user knows, to measure recall below the threshold.
        explored = self._pick_exploration(tweets_for_filtering, filtered_tweets)
        for tweet in explored:
            tweet["exploration"] = True
            tweet["filtered"] = True
            filtered_tweets.append(tweet)
        stats["exploration"] = len(explored)
        stats["exploration_ids"] = [t["item_id"] for t in explored]

        stats["filtered"] = len(filtered_tweets)
        stats["tier_breakdown"] = tier_counts
        logger.info(
            f"Filtered to {len(filtered_tweets)} tweets "
            f"(favorite={tier_counts['favorite']}, default={tier_counts['default']}, muted={tier_counts['muted']})"
        )

        # Step 3: Save all tweets to database (including non-filtered)
        logger.info("Step 3: Saving tweets to database...")
        # Update original tweets with filter results
        tweet_map = {t["item_id"]: t for t in new_tweets}
        for ft in filtered_tweets:
            tweet_map[ft["item_id"]] = ft

        self.db.save_items(list(tweet_map.values()))
        logger.info(f"Saved {len(tweet_map)} tweets to database")

        # Step 4: Group by thread and send filtered tweets to Telegram
        if filtered_tweets:
            logger.info("Step 4: Sending filtered tweets to Telegram...")
            tweet_groups = self._group_by_thread(filtered_tweets)
            thread_count = sum(1 for g in tweet_groups if len(g) > 1)
            if thread_count:
                logger.info(
                    f"Grouped into {len(tweet_groups)} groups "
                    f"({thread_count} threads)"
                )
            sent_pairs = await self.telegram.send_daily_digest(tweet_groups)
            stats["sent"] = len(sent_pairs)

            # Mark tweets as sent
            for tweet, msg_id in sent_pairs:
                if msg_id:
                    self.db.mark_item_sent(tweet["item_id"], msg_id)
        else:
            logger.info("No filtered tweets to send")

    async def resend_unsent(self, hours: int = 48) -> int:
        """Deliver items that passed the filter but never reached Telegram
        (a run that failed mid-send). Returns the number sent."""
        items = self.db.get_unsent_filtered(hours=hours)
        if not items:
            logger.info("No unsent filtered items")
            return 0
        logger.info(f"Resending {len(items)} unsent items from the last {hours}h")
        groups = self._group_by_thread(items)
        sent_pairs = await self.telegram.send_daily_digest(groups)
        for item, msg_id in sent_pairs:
            if msg_id:
                self.db.mark_item_sent(item["item_id"], msg_id)
        return sum(1 for _, m in sent_pairs if m)

    async def process_inbox(self) -> dict:
        """Read unseen newsletter emails, extract and score their links, send
        the ones above the default threshold (one digest per issue).

        An email is acknowledged (marked seen / deleted) only once every item
        above the threshold was delivered; otherwise it stays unseen and the
        next run re-sends just the items that did not go out. Runs are
        serialised so /inbox cannot overlap the scheduled job.

        Returns:
            {"emails": n, "entries": n, "sent": n} (+ "busy": True if skipped)
        """
        summary = {"emails": 0, "entries": 0, "sent": 0}
        if not (self.mailbox and self.newsletter):
            return summary
        if self._inbox_lock.locked():
            logger.info("Inbox already being processed; skipping")
            return {**summary, "busy": True}
        async with self._inbox_lock:
            emails = await asyncio.to_thread(self.mailbox.fetch_unseen, self.email_max_per_run)
            for mail in emails:
                try:
                    result = await asyncio.to_thread(self.newsletter.from_email, mail)
                except Exception as e:
                    logger.error(f"Newsletter email '{mail.get('subject')}' failed: {e}", exc_info=True)
                    continue  # stays unseen → retried next run
                summary["emails"] += 1
                summary["entries"] += result["entries"]
                unscored = [i for i in result["items"] if i.get("filter_score") is None]
                good = [
                    i for i in result["items"]
                    if i.get("filter_score") is not None
                    and i["filter_score"] >= self.default_threshold
                ]
                pending = [i for i in good if not i.get("sent_to_telegram")]
                if pending:
                    sent_pairs = await self.telegram.send_issue_digest(
                        result["title"], pending, total=result["entries"],
                    )
                    for item, msg_id in sent_pairs:
                        try:
                            self.db.mark_item_sent(item["item_id"], msg_id)
                        except Exception as e:
                            logger.warning(f"mark_item_sent failed for {item['item_id']}: {e}")
                    summary["sent"] += len(sent_pairs)
                    if len(sent_pairs) < len(pending):
                        logger.warning(
                            f"'{result['title']}': {len(pending) - len(sent_pairs)} items not "
                            "delivered; email left unseen for retry"
                        )
                        continue
                if unscored:
                    # A scoring batch failed; the unscored links are retried
                    # with the email next run (scored ones are not re-sent).
                    logger.warning(
                        f"'{result['title']}': {len(unscored)} links unscored; email left unseen for retry"
                    )
                    continue
                await asyncio.to_thread(self.mailbox.acknowledge, mail["uid"])
        if summary["emails"]:
            logger.info(f"Inbox: {summary}")
        return summary

    def score_items(self, items: list[dict]) -> list[dict]:
        """Score and persist items outside the daily batch (newsletters,
        captures): neighbours + compiled profile, no threshold."""
        neighbors = None
        if self.rag_enabled and self.embedding_manager and self.embedding_manager.enabled:
            try:
                neighbors = self.embedding_manager.find_neighbors(items, k=self.neighbors_per_item) or None
            except Exception as e:
                logger.warning(f"Neighbour lookup failed: {e}")
        self.claude.filter_tweets(
            items, threshold=0, neighbors=neighbors, profile=self.load_scoring_profile(),
        )
        self.db.save_items(items)
        return items

    def _fetch_feed_items(self) -> list[dict]:
        """Unscored entries from active feeds, as pipeline items."""
        if not self.feed_fetcher:
            return []
        try:
            feeds = self.db.get_feeds()
        except Exception as e:
            logger.warning(f"Could not load feeds: {e}")
            return []
        if not feeds:
            return []
        logger.info(f"Step 1b: Polling {len(feeds)} feeds...")
        items: list[dict] = []
        for feed in feeds:
            try:
                entries = self.feed_fetcher.fetch_entries(feed["url"])
                candidates = [e for e in entries if e.get("url")]
                ids = [self.feed_fetcher.blog_fetcher.build_post(
                    url=e["url"], title=e.get("title") or e["url"])["item_id"] for e in candidates]
                scored = self.db.get_scored_ids(ids)
                fresh = [e for e, iid in zip(candidates, ids) if iid not in scored]
                new_items = self.feed_fetcher.entries_to_items(
                    feed, fresh, max_items=self.feed_max_entries
                )
                items.extend(new_items)
                self.db.update_feed_state(feed["url"])
                logger.info(f"  {feed.get('title') or feed['url']}: {len(new_items)} new")
            except Exception as e:
                logger.warning(f"Feed {feed['url']} failed: {e}")
                self.db.update_feed_state(feed["url"], error=str(e))
        return items

    def load_scoring_profile(self) -> Optional[str]:
        """Profile text for the scoring prompt (compiled when available)."""
        if self.profile_source != "compiled":
            return None
        try:
            profiles = self.db.get_latest_profiles()
        except Exception as e:
            logger.warning(f"Could not load compiled profiles, using baseline: {e}")
            return None
        if not isinstance(profiles, dict) or not profiles:
            return None
        text = scoring_profile(profiles, self.claude.profile)
        logger.info(
            f"Using compiled profile ({len(profiles)} sections, {len(text)} chars) for scoring"
        )
        return text

    async def compile_profiles(self) -> dict[str, str]:
        """Recompile preference profiles from signals (weekly job / CLI)."""
        compiler = ProfileCompiler(self.claude.client, self.claude.model, self.db)
        try:
            written = await asyncio.to_thread(compiler.compile_all)
        except Exception as e:
            logger.error(f"Profile compilation failed: {e}", exc_info=True)
            try:
                await self.telegram.send_error_notification(f"Profile compilation failed: {e}")
            except Exception:
                pass
            return {}
        if written:
            logger.info(f"Compiled profiles: {', '.join(written)}")
            try:
                await self.telegram.send_profile_update(written)
            except Exception as e:
                logger.warning(f"Could not announce profile update: {e}")
        else:
            logger.info("Profile compilation wrote nothing (not enough votes per domain)")
        return written

    def _pick_exploration(
        self, scored: list[dict], passed: list[dict]
    ) -> list[dict]:
        """Pick random below-threshold items to send as exploration."""
        if self.exploration_per_run <= 0:
            return []
        passed_ids = {t["item_id"] for t in passed}
        candidates = [
            t for t in scored
            if t["item_id"] not in passed_ids
            and t.get("filter_score") is not None
            and t.get("filter_reason") != "Not scored by Claude"
        ]
        if not candidates:
            return []
        return random.sample(candidates, min(self.exploration_per_run, len(candidates)))

    def _expand_reply_parents(self, tweets: list[dict], max_depth: int) -> list[dict]:
        """Walk reply chains upward and return the parent tweets not already
        in ``tweets``, up to ``max_depth`` levels.

        Each level is one batched lookup for the whole batch. Parents already
        stored are taken from the database (and still walked through, so a
        chain is not cut short by a known tweet); the scored ones are then
        dropped by the usual dedup. Each parent gets a transient
        ``replied_by`` list naming the batch authors who answered it, for the
        scorer. Lookup failures are logged and leave the batch as it was.
        """
        pool = {t["item_id"]: t for t in tweets}
        added: list[dict] = []
        frontier = list(tweets)
        for _ in range(max_depth):
            wanted: dict[str, list[dict]] = {}
            for t in frontier:
                pid = parent_item_id(t)
                if pid and pid != t["item_id"]:
                    wanted.setdefault(pid, []).append(t)
            # Parents already in the batch still get their repliers noted.
            for pid in wanted:
                if pid in pool:
                    self._note_repliers(pool[pid], wanted[pid])
            missing = [pid for pid in wanted if pid not in pool]
            if not missing:
                break
            try:
                known = self.db.get_items(missing)
                to_fetch = [pid for pid in missing if pid not in known]
                fetched = self.twitter.fetch_tweets(to_fetch) if to_fetch else []
            except Exception as e:
                logger.warning(f"Reply parent lookup failed, continuing without: {e}")
                break
            level = [known[pid] for pid in missing if pid in known]
            level += [t for t in fetched if t["item_id"] not in pool and t["item_id"] in wanted]
            for parent in level:
                self._note_repliers(parent, wanted[parent["item_id"]])
                pool[parent["item_id"]] = parent
                added.append(parent)
            frontier = level
        return added

    @staticmethod
    def _note_repliers(parent: dict, replies: list[dict]) -> None:
        """Merge the authors of ``replies`` into ``parent["replied_by"]`` (sorted, unique)."""
        names = set(parent.get("replied_by") or [])
        names.update(c["author_username"] for c in replies if c.get("author_username"))
        parent["replied_by"] = sorted(names)

    @staticmethod
    def _group_by_thread(tweets: list[dict]) -> list[list[dict]]:
        """Group tweets by conversation_id for thread-aware delivery.

        Tweets sharing a conversation_id are grouped together and sorted
        chronologically.  A tweet whose conversation_id equals its own
        item_id and has no siblings in the batch stays as a single-item group.

        Args:
            tweets: Filtered tweet list (each must have raw_data with
                    conversation_id, and created_at).

        Returns:
            List of groups (each group is a list of 1+ tweets).
        """
        from collections import OrderedDict

        groups: OrderedDict[str, list[dict]] = OrderedDict()
        for tweet in tweets:
            conv_id = tweet.get("raw_data", {}).get("conversation_id", tweet["item_id"])
            groups.setdefault(conv_id, []).append(tweet)

        result: list[list[dict]] = []
        for conv_id, group in groups.items():
            # Sort chronologically within the group
            group.sort(key=lambda t: t.get("created_at", ""))
            result.append(group)

        return result

    def _deduplicate_tweets(self, tweets: list[dict]) -> list[dict]:
        """Drop tweets that already have a filter score in the database.

        One batched ``IN`` query instead of a round trip per tweet.
        """
        if not tweets:
            return []
        tweets = _prefer_direct(tweets)
        scored_ids = self.db.get_scored_ids([t["item_id"] for t in tweets])
        return [t for t in tweets if t["item_id"] not in scored_ids]

    def schedule_weekly_profile(
        self, weekday: str, hour: int = 9, minute: int = 30,
        timezone: Optional[str] = None,
    ) -> None:
        """Recompile profiles once a week (after that day's digest)."""
        weekday = (weekday or "").strip().lower()
        if not weekday:
            logger.info("Weekly profile compilation disabled")
            return
        every = getattr(schedule.every(), weekday, None)
        if every is None:
            raise ValueError(f"Unknown weekday for profile compilation: {weekday!r}")
        time_str = f"{hour:02d}:{minute:02d}"

        def _launch() -> None:
            task = asyncio.create_task(self.compile_profiles())
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

        job = every.at(time_str, timezone) if timezone else every.at(time_str)
        job.do(_launch)
        logger.info(f"Scheduled weekly profile compilation on {weekday} at {time_str}")

    def schedule_daily(
        self,
        hour: int = 9,
        minute: int = 0,
        timezone: Optional[str] = None,
        skip_weekdays: Optional[list[str]] = None,
    ) -> None:
        """Schedule daily curation run.

        Args:
            hour: Hour to run (24h format)
            minute: Minute to run
            timezone: IANA tz name (e.g. "Asia/Taipei"). If None, uses the
                host's local time — which on Railway is UTC unless the TZ
                env var is set.
            skip_weekdays: Lowercased weekday names to skip
                (e.g. ["sunday"]). Decision is made in the timezone arg.
        """
        time_str = f"{hour:02d}:{minute:02d}"
        skip_set = {d.strip().lower() for d in (skip_weekdays or []) if d.strip()}
        weekday_names = [
            "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday",
        ]

        def _launch() -> None:
            if skip_set:
                if timezone:
                    from zoneinfo import ZoneInfo
                    today = datetime.now(ZoneInfo(timezone)).weekday()
                else:
                    today = datetime.now().weekday()
                if weekday_names[today] in skip_set:
                    logger.info(
                        f"Skipping scheduled run today ({weekday_names[today]} "
                        f"is in SCHEDULE_SKIP_WEEKDAYS)"
                    )
                    return
            task = asyncio.create_task(self.run_daily_curation())

            def _log_exception(t: asyncio.Task) -> None:
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:
                    logger.error(
                        "Scheduled daily curation failed", exc_info=exc,
                    )

            task.add_done_callback(_log_exception)

        job = schedule.every().day.at(time_str, timezone) if timezone \
            else schedule.every().day.at(time_str)
        job.do(_launch)
        tz_label = f" ({timezone})" if timezone else " (host local time)"
        skip_label = f", skipping {sorted(skip_set)}" if skip_set else ""
        logger.info(f"Scheduled daily curation at {time_str}{tz_label}{skip_label}")

    async def run_scheduled(self) -> None:
        """Run the scheduler loop."""
        logger.info("Starting scheduler loop...")

        while True:
            schedule.run_pending()
            await asyncio.sleep(60)  # Check every minute


async def feedback_handler(
    db: DatabaseClient,
    item_id: str,
    vote: str,
    telegram_message_id: int,
    notes: str = None,
    embedding_manager: Optional[EmbeddingManager] = None,
    strength: int = 1,
    source: str = "button",
) -> None:
    """Handle a vote from Telegram (button, reaction or /like).

    Appends a vote signal and embeds the item for future neighbour lookups.

    Args:
        db: Database client
        item_id: Item ID
        vote: 'up' or 'down'
        telegram_message_id: Telegram message ID
        notes: Optional reason for the vote
        embedding_manager: Optional EmbeddingManager to embed voted item
        strength: 1 normal, 2 strong (🔥)
        source: 'button' | 'reaction' | 'like_cmd'
    """
    try:
        db.save_signal(
            item_id=item_id,
            kind="vote",
            value=vote,
            strength=strength,
            reason=notes,
            source=source,
            telegram_message_id=telegram_message_id,
        )
        logger.info(
            f"Saved vote: item={item_id}, vote={vote}, strength={strength}, "
            f"source={source}, notes={notes}"
        )
    except Exception as e:
        logger.error(f"Error saving vote: {e}")
        return

    # Embed the item for RAG (skip if already embedded)
    if embedding_manager and embedding_manager.enabled:
        try:
            if not db.has_embedding(item_id):
                item = db.get_item(item_id)
                if item and item.get("text"):
                    embedding = embedding_manager.generate_embedding(item["text"])
                    if embedding:
                        db.save_embedding(item_id, embedding)
                        logger.info(f"Embedded voted item {item_id} for RAG")
        except Exception as e:
            logger.warning(f"Failed to embed voted item {item_id}: {e}")
