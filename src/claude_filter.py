"""Claude AI filter for tweet curation."""

import json
import logging
from typing import Optional

from anthropic import Anthropic

logger = logging.getLogger(__name__)

# Production prompt for Phase 1 (no RAG)
PRODUCTION_PROMPT_V1 = """You are curating a Twitter feed for Nic Lin, a protocol researcher and Project Lead at Puffer Finance working on UniFi Based Rollup (TEE proofs, L1↔L2 synchronous composability) and Preconf AVS.

Background: Former Senior Protocol Engineer at imToken Labs (Account Abstraction, OFA, rollup security) and Blockchain Engineer at Ethereum Foundation (Eth 2.0, ERC-2938). Ethereum Support Program grantee with 30+ technical articles. Speaker at Devcon and ETHTaipei.

Score each tweet 0-100 based on relevance to Nic's work and interests:

95-100: Directly about Nic's active work
  - Based rollups, preconfirmations, sequencer design
  - TEE-based proving, L1↔L2 composability
  - Puffer Finance or UniFi ecosystem updates

85-94: Core research areas
  - MEV, OFA (Order Flow Auctions), PBS, block building
  - Account Abstraction (ERC-4337, ERC-7702, wallet design)
  - Censorship resistance, force inclusion mechanisms
  - ZK proofs, Data Availability (DAS, EIP-4844, blob markets)

70-84: Adjacent technical content
  - L2 architecture deep-dives (OP Stack, Arbitrum, StarkNet, ZKsync)
  - Ethereum CL/EL protocol changes, EIPs, hard fork planning
  - Smart contract security, audit findings, exploit analysis
  - Rollup economics, security models, escape hatches
  - Developer tooling for protocol/infra engineers

50-69: Peripheral interest
  - General Ethereum ecosystem news (surface-level)
  - Crypto governance and DAO mechanics
  - Tangentially related L1/L2 announcements

0-49: Not relevant — skip
  - Price speculation, trading signals, market commentary
  - NFT drops, meme coins, celebrity opinions
  - Engagement farming, giveaways, generic "gm" posts
  - Product marketing without technical substance
  - Drama, gossip, influencer takes

Return JSON array:
[{{"tweet_id": "...", "score": 85, "reason": "..."}}]

Tweets to filter:
{tweets_json}"""


class ClaudeFilter:
    """Claude-based tweet filter."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514"):
        """Initialize Anthropic client.

        Args:
            api_key: Anthropic API key
            model: Claude model to use
        """
        self.client = Anthropic(api_key=api_key)
        self.model = model
        logger.info(f"Claude filter initialized with model: {model}")

    def filter_tweets(
        self,
        tweets: list[dict],
        threshold: int = 70,
    ) -> list[dict]:
        """Filter tweets using Claude.

        Args:
            tweets: List of tweet dictionaries
            threshold: Minimum score to pass filter (default 70)

        Returns:
            List of filtered tweets with scores and reasons
        """
        if not tweets:
            logger.warning("No tweets to filter")
            return []

        # Prepare tweets for Claude (minimal format)
        tweets_for_claude = []
        for tweet in tweets:
            tweets_for_claude.append({
                "tweet_id": tweet["tweet_id"],
                "author": tweet["author_username"],
                "text": tweet["text"],
                "likes": tweet.get("metrics", {}).get("likes", 0),
                "retweets": tweet.get("metrics", {}).get("retweets", 0),
            })

        tweets_json = json.dumps(tweets_for_claude, indent=2)
        prompt = PRODUCTION_PROMPT_V1.format(tweets_json=tweets_json)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                temperature=0.3,
                messages=[{"role": "user", "content": prompt}],
            )

            # Extract text response
            response_text = response.content[0].text
            scores = self._parse_response(response_text)

            # Map scores back to original tweets
            score_map = {s["tweet_id"]: s for s in scores}
            filtered_tweets = []

            for tweet in tweets:
                tweet_id = tweet["tweet_id"]
                if tweet_id in score_map:
                    score_data = score_map[tweet_id]
                    tweet["filter_score"] = score_data["score"]
                    tweet["filter_reason"] = score_data["reason"]
                    tweet["filtered"] = score_data["score"] >= threshold

                    if tweet["filtered"]:
                        filtered_tweets.append(tweet)
                else:
                    # Tweet not scored, default to skip
                    tweet["filter_score"] = 0
                    tweet["filter_reason"] = "Not scored by Claude"
                    tweet["filtered"] = False

            # Log all tweet scores for debugging
            logger.info("--- Tweet Scores ---")
            for tweet in tweets:
                status = "PASS" if tweet.get("filtered") else "SKIP"
                score = tweet.get("filter_score", 0)
                reason = tweet.get("filter_reason", "")
                author = tweet.get("author_username", "unknown")
                text_preview = tweet.get("text", "")[:60].replace("\n", " ")
                logger.info(
                    f"[{status}] Score {score:3d} | @{author}: {text_preview}..."
                )
                logger.info(f"         Reason: {reason}")
            logger.info("--- End Scores ---")

            logger.info(
                f"Filtered {len(filtered_tweets)}/{len(tweets)} tweets "
                f"(threshold: {threshold})"
            )
            return filtered_tweets

        except Exception as e:
            logger.error(f"Error filtering tweets with Claude: {e}")
            raise

    def _parse_response(self, response_text: str) -> list[dict]:
        """Parse Claude's JSON response.

        Args:
            response_text: Raw response from Claude

        Returns:
            List of score dictionaries
        """
        try:
            # Try to extract JSON from response
            # Claude might include markdown code blocks
            text = response_text.strip()

            # Remove markdown code blocks if present
            if text.startswith("```"):
                lines = text.split("\n")
                # Remove first line (```json or ```) and last line (```)
                text = "\n".join(lines[1:-1])

            scores = json.loads(text)

            # Validate structure
            validated = []
            for item in scores:
                if not isinstance(item, dict):
                    continue
                if "tweet_id" not in item or "score" not in item:
                    continue

                validated.append({
                    "tweet_id": str(item["tweet_id"]),
                    "score": int(item.get("score", 0)),
                    "reason": str(item.get("reason", "No reason provided")),
                })

            return validated

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse Claude response as JSON: {e}")
            logger.debug(f"Raw response: {response_text}")
            return self._fallback_parse(response_text)

    def _fallback_parse(self, response_text: str) -> list[dict]:
        """Fallback parsing for malformed responses.

        Args:
            response_text: Raw response from Claude

        Returns:
            List of score dictionaries (may be empty)
        """
        # Try to find any JSON-like structures
        import re

        pattern = r'\{"tweet_id":\s*"([^"]+)",\s*"score":\s*(\d+),\s*"reason":\s*"([^"]*)"\}'
        matches = re.findall(pattern, response_text)

        results = []
        for match in matches:
            results.append({
                "tweet_id": match[0],
                "score": int(match[1]),
                "reason": match[2],
            })

        if results:
            logger.warning(f"Fallback parsing recovered {len(results)} scores")
        else:
            logger.error("Fallback parsing found no valid scores")

        return results
