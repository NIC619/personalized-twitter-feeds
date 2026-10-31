# Review brief: branch `preference-store`

Four commits on top of `main` (`87f0c2d`), one per phase. Review the branch
as a whole or commit by commit; each phase leaves the bot runnable.

```
git log --oneline main..preference-store
1c2fc5e Phase 4: RSS feeds, LLM newsletter extraction, bot module split
56254d1 Phase 3: general preference store, capture, imports, compiled profiles, MCP
7e5bab9 Phase 2: reactions as votes, model-proposed reasons, notes, click tracking
c0694b0 Phase 1: per-item RAG, offline eval, exploration, retire prompt A/B
```

Run tests with `source .venv/bin/activate && python -m pytest tests/ -q`
(331 pass). Nothing has been deployed; the Supabase migrations have not
been run against production.

## What the project is now

A personal preference store with a Twitter curator on top. Daily run:
fetch timeline + starred authors + subscribed RSS feeds → drop retweets and
blocked keywords → look up each item's 3 nearest voted neighbours in
pgvector → Claude scores 0-100 against a compiled preference profile and tags
a domain → per-author thresholds → 2 random below-threshold "exploration"
items added → send to Telegram. Every vote, reaction, confirmed reason,
note, link open and captured URL becomes a row in `signals`. A weekly job
compiles signals into per-domain Markdown profiles that feed back into the
scoring prompt. An MCP server exposes the store to other agents.

## Where to look, in priority order

1. **Data migration** `scripts/migrations/2026-10-31_phase3_items.sql`
   (and the two before it). Copies `tweets` → `items`, `feedback` →
   `signals`, `tweet_embeddings` → `item_embeddings`, adds an FK from
   signals to items, defines `match_voted_items` RPC and `item_votes` view.
   Must be idempotent and safe on ~36k items / 1.2k votes. Check the
   feedback→signals dedup window (120 s) against phase-2 double writes, the
   orphan-signal delete before the FK, and that `scripts/setup_database.py`
   (fresh install) matches.
2. **`src/database.py`** (rewritten). All reads now target the new tables.
   Check PostgREST embedding syntax (`items(*)` joins rely on the FK),
   pagination in `_vote_rows`, and that `get_labeled_items` picks the
   latest vote per item.
3. **Vote flow** `src/bot/feedback.py` + `src/feedback_flow.py`. State is
   in-memory dicts on the bot (`_pending_feedback`, `_item_prompts`,
   `_reason_prompts`, `_awaiting_reason`, `_message_items`). Look for
   races between the 10 s undo grace, reason confirmation arriving before
   the save (`_pending_reasons`), reaction add/change/remove sequences, and
   restarts losing state (DB fallback is `lookup_message_callback`).
4. **Webhook server** `src/webhook_server.py` + `TelegramCurator.run_webhook`.
   Replaces PTB's built-in server with tornado so `/r/<item_id>` can share
   the port. Check `Update.de_json` feeding `update_queue`, `set_webhook`
   with `allowed_updates=ALL_TYPES`, shutdown, and that the redirect never
   leaks errors or caches.
5. **Scorer** `src/claude_filter.py`. Single prompt, tool-use schema with
   `domain` enum, `max_tokens=8192`, batch 25, temperature 0. Also
   `propose_reason` and `extract_newsletter_entries`. Check `_extract_scores`
   validation (unknown ids dropped, clamping, duplicates).
6. **Scheduler** `src/scheduler.py`: feed step mutates `new_tweets` and
   `tweets_for_filtering`; exploration picks from `tweets_for_filtering`
   (which the scorer annotates in place); `load_scoring_profile` budget;
   weekly job scheduling with `schedule`.
7. **Capture** `src/capture.py` + `src/bot/capture.py`. Message
   classification rules; forwarded-message origin extraction; note vs
   command ambiguity.
8. **Profile compiler** `src/profile_compiler.py`: prompt content, evidence
   caps, `scoring_profile` budget trimming, versioning via `save_profile`.
9. **Agent API / MCP** `src/preference_api.py`, `src/mcp_server.py`
   (supports mcp SDK 1.x and 2.x imports). `record_signal` creates items;
   check validation and that nothing here can write garbage item ids.
10. **Feeds** `src/feeds.py`: stdlib RSS/Atom parser, discovery order
    (page `<link rel=alternate>` first, then common paths), dedup by item
    id against `get_scored_ids`, full-text enrichment for short summaries.
11. **Eval** `scripts/eval_prompt.py`: metrics (`auc`, `metrics_at`,
    `summarize`), stored-embedding reuse, parallel batches, the
    `stored@send` comparison column. Caveats are documented at the top.
12. **Imports** `scripts/import_items.py`: CSV column heuristics, like.js
    parsing, idempotency via existing item ids, bulk signal insert.

## Known gaps and deliberate choices

- Local `.env` keys were stale during development, so nothing was run
  against live Claude/OpenAI until after the branch was committed. The
  eval/model comparison is being run now; results are not in the branch.
- Legacy tables (`tweets`, `feedback`, `tweet_embeddings`,
  `ab_test_scores`, `newsletter_preferences`) are kept; nothing reads them.
- `is_blog_content`, `is_twitter_profile_url` imports in `src/bot/*` are
  partly unused (mechanical split); pyflakes reports only unused imports
  and placeholder-less f-strings inherited from the original code.
- Telegram reactions in a private chat: the bot receives
  `message_reaction` updates only because `allowed_updates` includes them;
  verify on the real bot after deploy.
- Exploration items set `filtered=True` in `items`; the `exploration`
  column disambiguates. `get_author_stats` does not weight 🔥 differently.
- No email ingestion for newsletters; all three of the user's newsletters
  turned out to have feeds.
- The internal item key was renamed `tweet_id` → `item_id` by sed across
  src/, scripts/, tests/ (not `scripts/setup_database.py` or migrations).
  Look for stragglers in strings/log messages.

## Questions a reviewer could answer

- Any PostgREST limits the new queries will hit beyond the 1000-row
  pagination already handled (`_vote_rows`, `get_labeled_items`)?
- Is the tornado webhook handler robust to Telegram retries and
  concurrent posts (it just enqueues)?
- `random.sample` for exploration is unseeded by design; is there a reason
  to prefer deterministic daily picks?
- `scoring_profile` budget is 7000 chars; is that the right trade-off
  against prompt cost for 25-item batches?
