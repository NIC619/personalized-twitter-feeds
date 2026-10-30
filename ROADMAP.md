# Twitter Curator Roadmap

## Phase 1: Core MVP ✅

- [x] Twitter API client (fetch home timeline)
- [x] Claude filtering with scoring (0-100)
- [x] Telegram bot with formatted messages
- [x] Feedback buttons (👍 👎) with category/reason selection
- [x] Favorite author button (⭐) with toggle
- [x] Mute author button (🔇) with toggle
- [x] Supabase storage (tweets, feedback, favorite_authors, muted_authors)
- [x] CLI options (--once, --schedule, --bot-only, --test, -n, --hours)
- [x] Debug logging for all scores
- [x] Comprehensive test suite (pytest)
- [x] Undo feedback functionality (10-second grace period)
- [x] Retweet detection and filtering
- [x] Conversation handling for /star command

## Phase 2: Personalization & RAG

### Favorite Authors Boost ✅
- [x] Fetch favorite authors list before filtering
- [x] Fetch starred authors' tweets separately (configurable per-author limit)
- [x] Per-author threshold tiers (favorite: lower threshold, muted: higher threshold)
- [x] Show ⭐ indicator in /stats for favorite authors, 🔇 for muted

### Feedback-based RAG ✅
- [x] Generate embeddings for tweets using OpenAI `text-embedding-3-small` (1536 dims)
- [x] Store embeddings in Supabase pgvector (`tweet_embeddings` table with cosine similarity)
- [x] Embed voted tweets on feedback (incremental RAG corpus building)
- [x] Before filtering: find similar past tweets user voted on via pgvector
- [x] Inject RAG context into Claude prompt (liked/disliked tweets with similarity scores)
- [x] Backfill script for existing feedback (`scripts/backfill_embeddings.py`)
- [x] Graceful fallback: works without OpenAI key (uses V1 prompt, no RAG)
- [x] Per-item nearest voted neighbours attached to each scored item (replaced 5 global examples)
- [x] Offline eval against all votes: `scripts/eval_prompt.py` (AUC, recall, bands)
- [x] Exploration: random below-threshold items sent each run, marked 🎲
- [ ] Track accuracy: % of sent tweets that got 👍

### A/B Testing Framework (retired 2026-10-30 — see AB_TESTING_PLAN.md)
- [x] Shadow scoring: score every tweet with control + challenger prompt, send based on control only
- [x] Prompt registry (`PROMPT_REGISTRY` in `claude_filter.py`) for named prompt variants
- [x] `ab_test_scores` table with paired entries per experiment
- [x] `get_ab_test_analysis` SQL function joining scores with feedback
- [x] RAG toggle (`RAG_ENABLED`) for isolating prompt changes from RAG effects
- [x] Analysis report: score gap, precision/recall/F1, Wilcoxon signed-rank test
- [x] CLI: `--ab-report <experiment_id>`
- [x] 5 prompt variants: V1 (bio+rubric), V2 (V1+RAG), V3 (interests-only), V4 (binary), V5 (strict)

### Keyword Blocklist ✅
- [x] `blocked_keywords` table + CRUD in DatabaseClient
- [x] Pre-LLM filter: whole-word, case-insensitive across tweet text, X Article title/body, and quoted-tweet text
- [x] Scheduler hook applied between retweet filter and Claude scoring
- [x] Starred authors exempt from blocklist (documented in README and `/help`)
- [x] Telegram `/blockword` (add via conversation; one-per-line or comma-separated) and `/blockwords` (list with tap-to-remove inline buttons)

### Improvements
- [ ] Batch tweets by topic/thread for better context
- [ ] Handle Twitter threads (fetch full thread if 1/N detected)
- [ ] Add "borderline" category (score 60-69) with different styling

## Phase 3: Automation & Polish

### Scheduling ✅
- [x] Reliable daily scheduler (schedule library with async support)
- [x] Multiple runs per day option
- [x] Timezone-aware scheduling (default: Asia/Taipei)

### Monitoring
- [ ] Daily stats summary in Telegram
- [ ] Weekly digest of top tweets
- [ ] Alert if no tweets fetched (API issue)
- [ ] Track API costs
- [x] Persistent error log (WARNING+ to `error_log` table via `DatabaseErrorHandler`)
- [x] Monthly error report CLI (`--error-report YYYY-MM` / `last`) with sparkline trend

### UX Improvements
- [x] /stats command in Telegram (paginated author performance table with scores, votes, filter averages)
- [x] /starred command (list favorite authors)
- [x] /star command (add/toggle favorite author — supports username, @mention, URL, multiple authors)
- [ ] /unfav command (remove favorite author) — covered by /star toggle
- [x] Reply to tweet message to add notes
- [x] Emoji reactions as votes (🔥 strong / 👍 / 👎), removal retracts
- [x] Model-proposed vote reason with ✅ / ✏️ confirm (replaced fixed reason buttons)
- [x] Click tracking via /r/<item_id> redirect → `open` signals

## Phase 4: General preference store ✅ (2026-10-31)

- [x] `items` / `signals` / `item_embeddings` / `preference_profiles` schema with migration from legacy tables
- [x] LLM domain tagging on every scored item
- [x] Capture by sending: URLs, forwarded posts, free text → liked items
- [x] Import scripts: Raindrop, Readwise, generic CSV, Twitter archive likes
- [x] Weekly compiled per-domain profiles injected into scoring; `--export-preferences PREFERENCES.md`
- [x] MCP server: get_profile, search_liked, score_items, record_signal

## Phase 5: Feeds and newsletters ✅ (2026-10-31)

- [x] `feeds` table, `/feed add|list|remove`, RSS/Atom discovery from a site URL
- [x] Daily run polls feeds; new entries fetched in full and scored like tweets
- [x] `/newsletter` = LLM extraction of linked items from page text (HTML section heuristics and `/newsletter_prefs` removed)

## Future Ideas

- [x] Migrate from tweepy to official X SDK (xdk) - see `docs/xdk-migration-notes.md`
- [ ] Multi-account support (curate multiple Twitter accounts)
- [ ] Topic clustering (group similar tweets together)
- [x] Export: `--export-preferences PREFERENCES.md` (Notion/Obsidian can ingest Markdown)
- [ ] Web dashboard for analytics
- [x] A/B test different prompts
- [ ] Auto-adjust threshold based on feedback ratio
- [ ] Support for Twitter lists (not just home timeline)
- [ ] Email ingestion for newsletters without feeds (IMAP or forward-to-address)
- [ ] Keyword alerts (always show tweets mentioning X)
