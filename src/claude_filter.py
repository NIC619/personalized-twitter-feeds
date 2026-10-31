"""Claude scoring for curated content.

One prompt template with three slots:

* a preference profile (static baseline today, LLM-compiled per domain later),
* the items to score, each optionally carrying its own nearest voted
  neighbours (``similar_past_votes``) so the model sees per-item feedback
  instead of a handful of global examples,
* a strict tool-use schema for the output, so there is no JSON parsing.
"""

import json
import logging
from typing import Optional

from anthropic import Anthropic
import httpx

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-5-5"

# Baseline preference profile. This is the interest list that won exp_003
# (V4). Phase 3 replaces it with a profile compiled from recorded signals.
DEFAULT_PROFILE = """Ethereum protocol researcher. Interests, highest to lowest priority:

Must-see (90-100):
- Based rollups, preconfirmations, sequencer design
- TEE-based proving, L1↔L2 synchronous composability
- Puffer Finance, UniFi ecosystem

High interest (75-89):
- MEV, OFA, PBS, block building
- Account Abstraction (ERC-4337, ERC-7702)
- Censorship resistance, force inclusion
- ZK proofs, Data Availability, blob markets
- L2 architecture (OP Stack, Arbitrum, StarkNet, ZKsync)
- Ethereum protocol changes, EIPs, hard forks
- Smart contract security, audits, exploits
- Rollup economics, security models

Some interest (50-74):
- General Ethereum ecosystem news
- Crypto governance, DAOs
- Developer tooling, infrastructure

Skip (0-49):
- Price talk, trading, market commentary
- NFTs, meme coins, celebrity takes
- Engagement farming, giveaways, "gm" posts
- Marketing without technical substance
- Drama, gossip"""


# Topic domains the model tags every item with. Profiles are compiled per
# domain, so keep this list short and stable.
DOMAINS = [
    "ethereum-protocol",  # L1/L2 research, MEV, AA, DA, ZK, security
    "crypto",             # the rest of crypto: markets, products, governance
    "software",           # engineering, tooling, systems, open source
    "ai",                 # models, agents, ML research and products
    "science",            # physics, biology, math, hard science
    "economics",          # macro, mechanism design, finance (non-crypto)
    "politics",           # politics, policy, geopolitics, society
    "culture",            # film, books, music, art, sport
    "life",               # health, productivity, career, personal growth
    "other",
]

SCORING_PROMPT = """You score content for one specific reader. Score each item 0-100 for how much this reader would want to read it, and tag its topic domain.

Domains: {domains}

## Reader profile
{profile}

## How to use past votes
Some items include `similar_past_votes`: the reader's most similar past votes, with cosine similarity. Treat them as the strongest evidence you have. An item close to upvoted content should score high even if the profile is vague about it; an item close to downvoted content should score low even if it keyword-matches the profile. Ignore neighbours with similarity below 0.5.

## Items
{items_json}

Score every item exactly once using the submit_scores tool. Keep each reason to one short sentence naming the deciding factor. Pick the single closest domain for each item."""


SCORE_TOOL = {
    "name": "submit_scores",
    "description": "Submit a relevance score for every item.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string"},
                        "score": {"type": "integer"},
                        "reason": {"type": "string"},
                        "domain": {"type": "string", "enum": DOMAINS},
                    },
                    "required": ["item_id", "score", "reason", "domain"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["scores"],
        "additionalProperties": False,
    },
}


REASON_PROMPT = """A reader just voted {verb} on the item below. Guess, in at most 12 words, WHY — phrased as a reusable preference about the content ("deep technical dive on preconfirmation economics", "event promotion without substance"), never a summary of the item and never starting with "the reader".

## Reader profile
{profile}

## Reasons the reader gave on similar items
{past_reasons}

## Item
{item_json}

Call propose_reason with your best single guess."""


REASON_TOOL = {
    "name": "propose_reason",
    "description": "Propose the reader's reason for the vote.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {"reason": {"type": "string"}},
        "required": ["reason"],
        "additionalProperties": False,
    },
}


EXTRACT_PROMPT = """Below is the visible text of a newsletter issue or link digest. Extract every distinct linked item (article, paper, thread, talk, release) it points the reader to. Skip navigation, sponsor blocks, subscribe prompts, footers and the newsletter's own self-links. Links appear inline as `anchor text <url>`.

For each item return: title (as the newsletter names it, cleaned), url (the real destination; prefer non-tracking URLs), summary (the newsletter's one or two sentence description or your own from the surrounding text), author (if named), section (the heading it sits under, if any).

Page title: {page_title}

{page_text}

Call extract_entries with the list, in page order."""


EXTRACT_TOOL = {
    "name": "extract_entries",
    "description": "Return the linked items found in a newsletter page.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "url": {"type": "string"},
                        "summary": {"type": "string"},
                        "author": {"type": ["string", "null"]},
                        "section": {"type": ["string", "null"]},
                    },
                    "required": ["title", "url", "summary", "author", "section"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["entries"],
        "additionalProperties": False,
    },
}


class NoToolCallError(RuntimeError):
    """The model answered twice without calling the required tool."""


class ClaudeFilter:
    """Claude-based content scorer."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        batch_size: int = 25,
        profile: Optional[str] = None,
        max_tokens: int = 8192,
        effort: Optional[str] = "medium",
    ):
        """Initialize Anthropic client.

        Args:
            api_key: Anthropic API key
            model: Claude model to use
            batch_size: Max items per API call
            profile: Preference profile text (defaults to DEFAULT_PROFILE)
            max_tokens: Output budget per call. 25 items with one-sentence
                reasons use well under 4k tokens, so 8k never truncates.
            effort: ``output_config.effort`` (low/medium/high). None omits it.
        """
        self.client = Anthropic(
            api_key=api_key,
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        self.model = model
        self.batch_size = batch_size
        self.profile = profile or DEFAULT_PROFILE
        self.max_tokens = max_tokens
        self.effort = effort
        logger.info(f"Claude filter initialized with model: {model}")

    def _call_tool(self, prompt: str, tool: dict, max_tokens: int):
        """One request that asks the model to answer via ``tool``.

        Portable across model generations: forced ``tool_choice`` and
        non-default ``temperature`` are rejected by Claude 5.5+, so we use
        ``auto`` (single call) with a strict schema and an instruction in
        the prompt naming the tool.
        """
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            tools=[tool],
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
            messages=[{"role": "user", "content": prompt}],
        )
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        response = self.client.messages.create(**kwargs)
        if self._has_tool_call(response, tool["name"]):
            return response
        # With tool_choice=auto the model occasionally answers in prose
        # instead of calling the tool (seen in ~2% of Sonnet 5.5 batches).
        # One retry with an explicit nudge recovers nearly all of them.
        logger.warning(f"No {tool['name']} call in response; retrying once")
        kwargs["messages"] = [{
            "role": "user",
            "content": prompt + f"\n\nRespond only by calling the {tool['name']} tool.",
        }]
        response = self.client.messages.create(**kwargs)
        if not self._has_tool_call(response, tool["name"]):
            raise NoToolCallError(f"model returned no {tool['name']} call after retry")
        return response

    @staticmethod
    def _has_tool_call(response, name: str) -> bool:
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == name:
                return True
        return False

    # ------------------------------------------------------------------ public

    def filter_tweets(
        self,
        tweets: list[dict],
        threshold: int = 70,
        neighbors: Optional[dict[str, list[dict]]] = None,
        profile: Optional[str] = None,
    ) -> list[dict]:
        """Score items and return the ones at or above ``threshold``.

        Every input dict is annotated in place with ``filter_score``,
        ``filter_reason`` and ``filtered``. Items the model did not score
        get ``filter_score=None`` so they are not persisted as scored and
        are picked up again next run.

        Args:
            tweets: Item dicts (tweets or blog posts)
            threshold: Minimum score to pass
            neighbors: Optional map item_id -> list of similar voted items
                (dicts with text, author_username, vote, similarity)
            profile: Override the profile for this call
        """
        if not tweets:
            logger.warning("No items to filter")
            return []

        scores = self.score_tweets(tweets, neighbors=neighbors, profile=profile)
        score_map = {s["item_id"]: s for s in scores}

        passed = []
        for tweet in tweets:
            score_data = score_map.get(tweet["item_id"])
            if score_data:
                tweet["filter_score"] = score_data["score"]
                tweet["filter_reason"] = score_data["reason"]
                if score_data.get("domain"):
                    tweet["domain"] = score_data["domain"]
            else:
                tweet["filter_score"] = None
                tweet["filter_reason"] = "Not scored by Claude"
            tweet["filtered"] = (tweet["filter_score"] or 0) >= threshold and tweet["filter_score"] is not None
            if tweet["filtered"]:
                passed.append(tweet)

        self._log_scores(tweets)
        logger.info(
            f"Filtered {len(passed)}/{len(tweets)} items (threshold: {threshold})"
        )
        return passed

    def score_tweets(
        self,
        tweets: list[dict],
        neighbors: Optional[dict[str, list[dict]]] = None,
        profile: Optional[str] = None,
    ) -> list[dict]:
        """Score items without thresholding.

        Returns:
            List of dicts with item_id, score, reason (one per scored item;
            items the model skipped are absent).
        """
        if not tweets:
            return []

        neighbors = neighbors or {}
        profile = profile or self.profile
        all_scores: list[dict] = []
        total_batches = (len(tweets) + self.batch_size - 1) // self.batch_size
        for i in range(0, len(tweets), self.batch_size):
            batch = tweets[i : i + self.batch_size]
            if total_batches > 1:
                logger.info(
                    f"Scoring batch {i // self.batch_size + 1}/{total_batches} "
                    f"({len(batch)} items)..."
                )
            all_scores.extend(self._score_batch(batch, neighbors, profile))
        return all_scores

    def propose_reason(
        self,
        item: dict,
        vote: str,
        strength: int = 1,
        neighbors: Optional[list[dict]] = None,
        past_reasons: Optional[list[str]] = None,
        profile: Optional[str] = None,
    ) -> Optional[str]:
        """Guess why the reader voted this way (one short phrase, or None)."""
        verb = "DOWN (skip)" if vote == "down" else ("UP, strongly (🔥)" if strength >= 2 else "UP")
        reasons = [r for r in (past_reasons or []) if r]
        prompt = REASON_PROMPT.format(
            verb=verb,
            profile=profile or self.profile,
            past_reasons="\n".join(f"- {r}" for r in reasons[:15]) or "(none yet)",
            item_json=json.dumps(self.build_item_entry(item, neighbors), indent=2, ensure_ascii=False),
        )
        try:
            response = self._call_tool(prompt, REASON_TOOL, 1024)
        except Exception as e:
            logger.warning(f"Reason proposal failed: {e}")
            return None
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "tool_use" and block.name == "propose_reason":
                reason = str(block.input.get("reason", "")).strip().rstrip(".")
                return reason[:160] or None
        return None

    def extract_newsletter_entries(self, page_title: str, page_text: str) -> list[dict]:
        """Extract linked items from a newsletter page via one model call.

        Returns:
            List of {title, url, summary, author, section} with http(s) URLs.
        """
        prompt = EXTRACT_PROMPT.format(page_title=page_title, page_text=page_text)
        try:
            response = self._call_tool(prompt, EXTRACT_TOOL, 16000)
        except Exception as e:
            logger.error(f"Newsletter extraction failed: {e}")
            raise
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "tool_use" and block.name == "extract_entries":
                entries = []
                seen: set[str] = set()
                for e in block.input.get("entries", []) or []:
                    url = str(e.get("url") or "").strip()
                    if not url.startswith(("http://", "https://")) or url in seen:
                        continue
                    seen.add(url)
                    entries.append({
                        "title": str(e.get("title") or url).strip(),
                        "url": url,
                        "summary": str(e.get("summary") or "").strip(),
                        "author": (str(e.get("author")).strip() or None) if e.get("author") else None,
                        "section": (str(e.get("section")).strip() or None) if e.get("section") else None,
                    })
                return entries
        return []

    # ----------------------------------------------------------------- helpers

    @staticmethod
    def build_item_entry(
        tweet: dict, neighbors: Optional[list[dict]] = None
    ) -> dict:
        """Build the JSON entry the model sees for one item."""
        entry = {
            "item_id": tweet["item_id"],
            "author": tweet.get("author_username", "unknown"),
            "text": tweet.get("text", ""),
            "likes": tweet.get("metrics", {}).get("likes", 0),
            "retweets": tweet.get("metrics", {}).get("retweets", 0),
        }
        if tweet.get("content_type") and tweet["content_type"] != "tweet":
            entry["type"] = tweet["content_type"]
        if tweet.get("quoted_tweet"):
            entry["quoted_tweet"] = {
                "author": tweet["quoted_tweet"]["author_username"],
                "text": tweet["quoted_tweet"]["text"],
            }
        if tweet.get("article"):
            entry["article"] = {
                "title": tweet["article"]["title"],
                "body": tweet["article"].get("body", ""),
            }
        if neighbors:
            entry["similar_past_votes"] = [
                {
                    "vote": n["vote"],
                    "similarity": round(float(n.get("similarity", 0)), 2),
                    "author": n.get("author_username", ""),
                    "text": (n.get("text") or "")[:200],
                    **({"reason": n["reason"]} if n.get("reason") else {}),
                }
                for n in neighbors
            ]
        return entry

    def build_prompt(
        self,
        tweets: list[dict],
        neighbors: Optional[dict[str, list[dict]]] = None,
        profile: Optional[str] = None,
    ) -> str:
        """Render the scoring prompt for a batch (exposed for eval/debugging)."""
        neighbors = neighbors or {}
        entries = [
            self.build_item_entry(t, neighbors.get(t["item_id"])) for t in tweets
        ]
        return SCORING_PROMPT.format(
            domains=", ".join(DOMAINS),
            profile=profile or self.profile,
            items_json=json.dumps(entries, indent=2, ensure_ascii=False),
        )

    def _score_batch(
        self, tweets: list[dict], neighbors: dict[str, list[dict]], profile: str
    ) -> list[dict]:
        prompt = self.build_prompt(tweets, neighbors, profile)
        try:
            response = self._call_tool(prompt, SCORE_TOOL, self.max_tokens)
        except NoToolCallError as e:
            # Leave this batch unscored (retried next run); other batches continue
            logger.error(f"Batch left unscored: {e}")
            return []
        except Exception as e:
            logger.error(f"Error scoring items: {e}")
            raise

        valid_ids = {t["item_id"] for t in tweets}
        scores = self._extract_scores(response, valid_ids)
        missing = valid_ids - {s["item_id"] for s in scores}
        if missing:
            logger.warning(f"{len(missing)} items not scored by model: {sorted(missing)[:5]}...")
        return scores

    @staticmethod
    def _extract_scores(response, valid_ids: set[str]) -> list[dict]:
        """Pull validated scores out of the tool_use block."""
        tool_input = None
        for block in getattr(response, "content", []) or []:
            if getattr(block, "type", None) == "tool_use" and block.name == "submit_scores":
                tool_input = block.input
                break
        if tool_input is None:
            logger.error("Model response contained no submit_scores tool call")
            return []

        seen: set[str] = set()
        validated = []
        for item in tool_input.get("scores", []) or []:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("item_id", ""))
            if item_id not in valid_ids or item_id in seen:
                continue
            try:
                score = int(item.get("score", 0))
            except (TypeError, ValueError):
                continue
            seen.add(item_id)
            domain = item.get("domain")
            validated.append({
                "item_id": item_id,
                "score": max(0, min(100, score)),
                "reason": str(item.get("reason") or "No reason provided"),
                "domain": domain if domain in DOMAINS else "other",
            })
        return validated

    @staticmethod
    def _log_scores(tweets: list[dict]) -> None:
        logger.info("--- Item Scores ---")
        for tweet in tweets:
            status = "PASS" if tweet.get("filtered") else "SKIP"
            author = tweet.get("author_username", "unknown")
            preview = tweet.get("text", "")[:60].replace("\n", " ")
            logger.info(
                f"[{status}] Score {(tweet.get('filter_score') or 0):3d} | @{author}: {preview}..."
            )
            logger.info(f"         Reason: {tweet.get('filter_reason', '')}")
        logger.info("--- End Scores ---")
