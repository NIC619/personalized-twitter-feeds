"""Claude AI filter for tweet curation."""

import json
import logging
import re
from typing import Optional

from anthropic import Anthropic
import httpx

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


PRODUCTION_PROMPT_V2 = """You are curating a Twitter feed for Nic Lin, a protocol researcher and Project Lead at Puffer Finance working on UniFi Based Rollup (TEE proofs, L1↔L2 synchronous composability) and Preconf AVS.

Background: Former Senior Protocol Engineer at imToken Labs (Account Abstraction, OFA, rollup security) and Blockchain Engineer at Ethereum Foundation (Eth 2.0, ERC-2938). Ethereum Support Program grantee with 30+ technical articles. Speaker at Devcon and ETHTaipei.

## User Feedback Context
Based on past feedback, here are similar tweets the user has voted on:

{rag_context}

Use this context to adjust your scores. If a new tweet is similar to liked tweets, boost its score. If similar to disliked tweets, lower it.

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


# V3: Interests-only — no bio/background, just a prioritized topic list
PROMPT_V3_INTERESTS_ONLY = """Score these tweets 0-100 for an Ethereum protocol researcher with these interests (highest to lowest priority):

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
- Drama, gossip

Return JSON array:
[{{"tweet_id": "...", "score": 85, "reason": "..."}}]

Tweets to filter:
{tweets_json}"""


# V4: Binary decision — forces a clear send/skip instead of ambiguous middle scores
PROMPT_V4_BINARY = """You are filtering tweets for an Ethereum protocol researcher focused on based rollups, preconfirmations, TEE proving, L1↔L2 composability (Puffer Finance/UniFi), MEV/PBS, Account Abstraction, ZK proofs, data availability, and L2 architecture.

For each tweet, decide: would this person want to read it? Be selective — only pass tweets with genuine technical substance or directly relevant news.

Score 70-100 for YES (worth reading), 0-49 for NO (skip).
Avoid scores in 50-69 — commit to a clear decision.

Scoring guide:
- 90-100: Directly about their active work or breakthrough research in their core areas
- 70-89: Solid technical content in adjacent areas they'd learn from
- 0-49: Everything else (surface-level news, marketing, price talk, drama, engagement farming)

Return JSON array:
[{{"tweet_id": "...", "score": 85, "reason": "..."}}]

Tweets to filter:
{tweets_json}"""


# V5: Negative-first — lead with rejection criteria to reduce false positives
PROMPT_V5_STRICT = """You are aggressively filtering tweets. Most tweets should be SKIPPED. Only pass tweets that an Ethereum protocol researcher would genuinely benefit from reading.

SKIP these (score 0-49):
- Price speculation, trading signals, market commentary
- NFT drops, meme coins, celebrity/influencer takes
- Engagement farming, giveaways, "gm" posts, motivational threads
- Product marketing without technical depth
- Drama, gossip, hot takes without substance
- Surface-level news that just restates announcements
- Opinions without technical analysis
- Anything you'd see on a generic crypto news feed

PASS these (score 70-100):
- Based rollups, preconfirmations, sequencer design, TEE proving
- L1↔L2 composability, Puffer Finance / UniFi updates
- MEV, OFA, PBS, block building research
- Account Abstraction (ERC-4337, ERC-7702) developments
- ZK proofs, Data Availability, blob market analysis
- L2 architecture deep-dives with original insight
- Ethereum protocol changes, EIPs with technical detail
- Smart contract security findings, exploit analysis
- Rollup economics and security model discussions

When in doubt, SKIP. A false negative (missing a good tweet) is better than a false positive (sending noise).

Return JSON array:
[{{"tweet_id": "...", "score": 85, "reason": "..."}}]

Tweets to filter:
{tweets_json}"""


# V3.5: Persona-driven with refined interest map and signal/noise criteria
PROMPT_V3_5_PERSONA = """You are a Protocol Architect and Smart Contract Security Researcher. You have deep expertise in the Ethereum roadmap, specifically the evolution of PBS, L2 scalability, and Account Abstraction. You are skeptical of hype, marketing, and price-talk. You prioritize technical correctness, game-theoretic robustness, and censorship-resistance. Your tone is professional, concise, and analytical. You speak the language of EIPs, slot auctions, and state transitions.

Analyze the provided tweets. Filter out all "Noise" and score the "Signal" according to these specific research interests.

[SIGNAL CRITERIA]
Keep (score 70-100): EIP/RIP discussions, ethresear.ch links, GitHub PRs, game theory analysis, MEV-aware design, and TEE/AI-agent infrastructure.
Discard as Noise (score 0-49): Price speculation, $ETH charts, engagement farming ("Thoughts?"), airdrops, and generic news without technical substance.

[INTEREST MAP & KEYWORDS]
1. Censorship Resistance: FOCIL, IL (Inclusion List), EIP-7547, MCP, BRAID.
2. Market Structures: ePBS, PTC, Execution Tickets/Auctions, MEV, Auction Theory, Mechanism Design.
3. L2 & Interop: Based Rollups, UniFi, Synchronous Composability, Shared Sequencers, Super Builder, OP Stack.
4. Account Abstraction: ERC-4337, EIP-7702, EIP-8141, Tempo, RIP-7702, Session Keys, Intents (OIF/ERC-7683).
5. TEE & AI: SGX, TDX, SEV-SNP, Agentic Sovereignty, Agent-to-Agent Economies, Verifiable Inference.

[SCORING]
- 90-100: Directly matches interest map keywords with deep technical substance
- 70-89: Solid technical content in adjacent areas, original insight
- 50-69: Tangentially relevant, surface-level
- 0-49: Noise — skip

Return JSON array:
[{{"tweet_id": "...", "score": 85, "reason": "..."}}]

Tweets to filter:
{tweets_json}"""


PROMPT_REGISTRY = {
    "V1": PRODUCTION_PROMPT_V1,
    "V2": PRODUCTION_PROMPT_V2,
    "V3": PROMPT_V3_INTERESTS_ONLY,
    "V3.5": PROMPT_V3_5_PERSONA,
    "V4": PROMPT_V4_BINARY,
    "V5": PROMPT_V5_STRICT,
}


class ClaudeFilter:
    """Claude-based tweet filter."""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6", batch_size: int = 50):
        """Initialize Anthropic client.

        Args:
            api_key: Anthropic API key
            model: Claude model to use
            batch_size: Max tweets per API call (default 45)
        """
        self.client = Anthropic(
            api_key=api_key,
            timeout=httpx.Timeout(600.0, connect=10.0),
        )
        self.model = model
        self.batch_size = batch_size
        logger.info(f"Claude filter initialized with model: {model}")

    def filter_tweets(
        self,
        tweets: list[dict],
        threshold: int = 70,
        rag_context: Optional[str] = None,
    ) -> list[dict]:
        """Filter tweets using Claude.

        Args:
            tweets: List of tweet dictionaries
            threshold: Minimum score to pass filter (default 70)
            rag_context: Optional RAG context string with similar voted tweets

        Returns:
            List of filtered tweets with scores and reasons
        """
        if not tweets:
            logger.warning("No items to filter")
            return []

        # Batch items if there are more than batch_size
        if len(tweets) > self.batch_size:
            logger.info(
                f"Splitting {len(tweets)} items into batches of {self.batch_size}"
            )
            all_scores = []
            for i in range(0, len(tweets), self.batch_size):
                batch = tweets[i : i + self.batch_size]
                batch_num = i // self.batch_size + 1
                total_batches = (len(tweets) + self.batch_size - 1) // self.batch_size
                logger.info(
                    f"Scoring batch {batch_num}/{total_batches} "
                    f"({len(batch)} items)..."
                )
                scores = self._score_batch(batch, rag_context)
                all_scores.extend(scores)
        else:
            all_scores = self._score_batch(tweets, rag_context)

        # Map scores back to original tweets
        score_map = {s["tweet_id"]: s for s in all_scores}
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

        # Log all item scores for debugging
        logger.info("--- Item Scores ---")
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
            f"Filtered {len(filtered_tweets)}/{len(tweets)} items "
            f"(threshold: {threshold})"
        )
        return filtered_tweets

    def _score_batch(
        self,
        tweets: list[dict],
        rag_context: Optional[str] = None,
    ) -> list[dict]:
        """Score a single batch of tweets with Claude.

        Returns:
            List of score dicts with tweet_id, score, reason
        """
        tweets_for_claude = []
        for tweet in tweets:
            entry = {
                "tweet_id": tweet["tweet_id"],
                "author": tweet["author_username"],
                "text": tweet["text"],
                "likes": tweet.get("metrics", {}).get("likes", 0),
                "retweets": tweet.get("metrics", {}).get("retweets", 0),
            }
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
            tweets_for_claude.append(entry)

        tweets_json = json.dumps(tweets_for_claude, indent=2)

        if rag_context:
            prompt = PRODUCTION_PROMPT_V2.format(
                tweets_json=tweets_json,
                rag_context=rag_context,
            )
            logger.info("Using RAG-enhanced prompt (V2)")
        else:
            prompt = PRODUCTION_PROMPT_V1.format(tweets_json=tweets_json)
            logger.info("Using standard prompt (V1, no RAG context)")

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                temperature=0.3,
                messages=[{"role": "user", "content": prompt}],
            )

            response_text = response.content[0].text
            return self._parse_response(response_text)

        except Exception as e:
            logger.error(f"Error filtering tweets with Claude: {e}")
            raise

    def score_tweets_with_prompt(
        self,
        tweets: list[dict],
        prompt_key: str,
        rag_context: Optional[str] = None,
    ) -> list[dict]:
        """Score tweets with a named prompt from the registry, returning raw scores.

        Unlike filter_tweets, this does not apply threshold filtering.

        Args:
            tweets: List of tweet dictionaries
            prompt_key: Key into PROMPT_REGISTRY (e.g. 'V1', 'V2')
            rag_context: Optional RAG context string

        Returns:
            List of dicts with tweet_id, score, reason
        """
        if not tweets:
            return []

        prompt_template = PROMPT_REGISTRY.get(prompt_key)
        if not prompt_template:
            logger.error(f"Unknown prompt key: {prompt_key}")
            return []

        # Batch items if there are more than batch_size
        if len(tweets) > self.batch_size:
            logger.info(
                f"Splitting {len(tweets)} items into batches of {self.batch_size} "
                f"for prompt '{prompt_key}'"
            )
            all_scores = []
            for i in range(0, len(tweets), self.batch_size):
                batch = tweets[i : i + self.batch_size]
                batch_num = i // self.batch_size + 1
                total_batches = (len(tweets) + self.batch_size - 1) // self.batch_size
                logger.info(
                    f"Scoring batch {batch_num}/{total_batches} "
                    f"({len(batch)} items) with prompt '{prompt_key}'..."
                )
                scores = self._score_batch_with_prompt(
                    batch, prompt_template, rag_context
                )
                all_scores.extend(scores)
            logger.info(
                f"Scored {len(all_scores)} items with prompt '{prompt_key}'"
            )
            return all_scores

        scores = self._score_batch_with_prompt(tweets, prompt_template, rag_context)
        logger.info(f"Scored {len(scores)} items with prompt '{prompt_key}'")
        return scores

    def _score_batch_with_prompt(
        self,
        tweets: list[dict],
        prompt_template: str,
        rag_context: Optional[str] = None,
    ) -> list[dict]:
        """Score a single batch of tweets with a given prompt template."""
        tweets_for_claude = []
        for tweet in tweets:
            entry = {
                "tweet_id": tweet["tweet_id"],
                "author": tweet["author_username"],
                "text": tweet["text"],
                "likes": tweet.get("metrics", {}).get("likes", 0),
                "retweets": tweet.get("metrics", {}).get("retweets", 0),
            }
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
            tweets_for_claude.append(entry)

        tweets_json = json.dumps(tweets_for_claude, indent=2)

        if rag_context and "{rag_context}" in prompt_template:
            prompt = prompt_template.format(
                tweets_json=tweets_json,
                rag_context=rag_context,
            )
        else:
            prompt = prompt_template.format(tweets_json=tweets_json)

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                temperature=0.3,
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = response.content[0].text
            return self._parse_response(response_text)
        except Exception as e:
            logger.error(f"Error scoring tweets with prompt: {e}")
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

            # Remove trailing commas before ] (common LLM output issue)
            text = re.sub(r',\s*]', ']', text)
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
        # Try to find any JSON-like structures (handles escaped quotes in reason)
        pattern = r'\{"tweet_id":\s*"([^"]+)",\s*"score":\s*(\d+),\s*"reason":\s*"((?:[^"\\]|\\.)*)"\}'
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
