# Twitter Curator Roadmap

## Phase 1: Core MVP ✅

- [x] Twitter API client (fetch home timeline)
- [x] Claude filtering with scoring (0-100)
- [x] Telegram bot with formatted messages
- [x] Feedback buttons (👍 👎)
- [x] Favorite author button (⭐)
- [x] Supabase storage (tweets, feedback, favorite_authors)
- [x] CLI options (--once, --schedule, -n, --hours)
- [x] Debug logging for all scores
- [ ] End-to-end testing with real data

## Phase 2: Personalization & RAG

### Favorite Authors Boost
- [ ] Fetch favorite authors list before filtering
- [ ] Add to Claude prompt: "Boost +10 for tweets from: @author1, @author2..."
- [ ] Show ⭐ indicator in Telegram for favorite authors

### Feedback-based RAG
- [ ] Generate embeddings for tweets (Voyage AI or sentence-transformers)
- [ ] Store embeddings in Supabase pgvector
- [ ] Before filtering: find similar past tweets user voted on
- [ ] Add to prompt: "User liked these similar tweets: ... User disliked: ..."
- [ ] Track accuracy: % of sent tweets that got 👍

### Improvements
- [ ] Batch tweets by topic/thread for better context
- [ ] Handle Twitter threads (fetch full thread if 1/N detected)
- [ ] Add "borderline" category (score 60-69) with different styling

## Phase 3: Automation & Polish

### Scheduling
- [ ] Reliable daily scheduler (cron or cloud-based)
- [ ] Multiple runs per day option
- [ ] Timezone-aware scheduling

### Monitoring
- [ ] Daily stats summary in Telegram
- [ ] Weekly digest of top tweets
- [ ] Alert if no tweets fetched (API issue)
- [ ] Track API costs

### UX Improvements
- [ ] /stats command in Telegram (show feedback ratio)
- [ ] /favorites command (list favorite authors)
- [ ] /unfav command (remove favorite author)
- [ ] Reply to tweet message to add notes

## Future Ideas

- [ ] Multi-account support (curate multiple Twitter accounts)
- [ ] Topic clustering (group similar tweets together)
- [ ] Export to Notion/Obsidian
- [ ] Web dashboard for analytics
- [ ] A/B test different prompts
- [ ] Auto-adjust threshold based on feedback ratio
- [ ] Support for Twitter lists (not just home timeline)
- [ ] Keyword alerts (always show tweets mentioning X)
