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
- [ ] Track accuracy: % of sent tweets that got 👍

### A/B Testing Framework ✅
- [x] Shadow scoring: score every tweet with control + challenger prompt, send based on control only
- [x] Prompt registry (`PROMPT_REGISTRY` in `claude_filter.py`) for named prompt variants
- [x] `ab_test_scores` table with paired entries per experiment
- [x] `get_ab_test_analysis` SQL function joining scores with feedback
- [x] RAG toggle (`RAG_ENABLED`) for isolating prompt changes from RAG effects
- [x] Analysis report: score gap, precision/recall/F1, Wilcoxon signed-rank test
- [x] CLI: `--ab-report <experiment_id>`
- [x] 5 prompt variants: V1 (bio+rubric), V2 (V1+RAG), V3 (interests-only), V4 (binary), V5 (strict)
- [ ] Auto-promote winning prompt after N votes

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

### UX Improvements
- [x] /stats command in Telegram (paginated author performance table with scores, votes, filter averages)
- [x] /starred command (list favorite authors)
- [x] /star command (add/toggle favorite author — supports username, @mention, URL, multiple authors)
- [ ] /unfav command (remove favorite author) — covered by /star toggle
- [ ] Reply to tweet message to add notes

## Future Ideas

- [ ] Migrate from tweepy to official X SDK (xdk) - see `docs/xdk-migration-notes.md`
- [ ] Multi-account support (curate multiple Twitter accounts)
- [ ] Topic clustering (group similar tweets together)
- [ ] Export to Notion/Obsidian
- [ ] Web dashboard for analytics
- [x] A/B test different prompts
- [ ] Auto-adjust threshold based on feedback ratio
- [ ] Support for Twitter lists (not just home timeline)
- [ ] Keyword alerts (always show tweets mentioning X)
