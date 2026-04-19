# Railway Deployment

This project deploys as a single always-on **Background Worker** on Railway. One process runs both the daily curation schedule and the Telegram bot polling loop (via `asyncio.gather` in `main.py:run_scheduled`).

## One-time setup

1. Push this repo to GitHub.
2. On [railway.app](https://railway.app), **New Project → Deploy from GitHub repo**. Select this repo.
3. In the service **Settings**:
   - **Start Command:** `python main.py --schedule`
   - **Service Type:** Background Worker (no public port needed).
   - Railway auto-detects Python via `requirements.txt` (nixpacks). If you want to pin Python, add a `.python-version` file (e.g. `3.11`).
4. Add environment variables (see below).
5. First deploy runs automatically. Subsequent pushes to the tracked branch auto-redeploy.

## Environment variables

### Secrets (required)
| Var | Source |
|---|---|
| `TWITTER_API_KEY` | X developer portal |
| `TWITTER_API_SECRET` | X developer portal |
| `TWITTER_ACCESS_TOKEN` | X developer portal |
| `TWITTER_ACCESS_SECRET` | X developer portal |
| `TWITTER_BEARER_TOKEN` | X developer portal |
| `ANTHROPIC_API_KEY` | console.anthropic.com |
| `TELEGRAM_BOT_TOKEN` | @BotFather |
| `TELEGRAM_CHAT_ID` | your Telegram chat id |
| `SUPABASE_URL` | Supabase project settings |
| `SUPABASE_KEY` | Supabase project settings (anon key) |
| `OPENAI_API_KEY` | *Optional.* Required only if `RAG_ENABLED=true`. |

### Tuning (all optional — defaults in `config/settings.py`)
- `SCHEDULE_HOUR` (default `9`)
- `SCHEDULE_TIMEZONE` (default `Asia/Taipei`) — IANA tz name; passed to the `schedule` library via `pytz`.
- `FETCH_HOURS`, `MAX_TWEETS`, `FILTER_THRESHOLD`
- `FAVORITE_THRESHOLD_OFFSET`, `MUTED_THRESHOLD_OFFSET`, `STARRED_AUTHOR_MAX_TWEETS`
- `RAG_ENABLED`, `EMBEDDING_MODEL`, `RAG_SIMILARITY_LIMIT`
- `AB_TEST_ENABLED`, `AB_TEST_EXPERIMENT_ID`, `AB_TEST_CHALLENGER_PROMPT`

### Timezone: `SCHEDULE_TIMEZONE` vs Railway's `TZ`

There are two separate timezone concerns — keep them in mind:

1. **Curation schedule time** — controlled by `SCHEDULE_TIMEZONE` (default `Asia/Taipei`). The code now passes this explicitly to the `schedule` library, so daily curation fires at `SCHEDULE_HOUR` in that IANA zone regardless of the container's clock. This is the one you almost certainly care about.
2. **Container local time** — controlled by Railway's `TZ` env var. Affects log timestamps and any `datetime.now()` calls without a tz. Railway containers default to **UTC**. If you want log timestamps to match your local clock, set `TZ=Asia/Taipei` (or whatever you prefer) as a Railway env var. This is cosmetic — it does **not** affect the curation schedule.

Recommended: set **both** to the same value to avoid confusion.

## Operational notes

### Logs
- Railway captures stdout automatically — view in the service's **Deployments → Logs** tab.
- WARNING+ logs are persisted to Supabase table `error_log` via `src/error_logger.py`. Generate a monthly report locally with `python main.py --error-report 2026-04` or `--error-report last`.
- **`curator.log` is wiped on every redeploy.** The file is written to the container's ephemeral filesystem (`main.py:42`). Don't rely on it for history on Railway — use stdout logs + the Supabase `error_log` table instead. Restarts (OOM, crash, Railway maintenance) also reset the file. If you need durable file logs, attach a Railway volume or switch to a log drain.

### Restart / crash behavior
- Dedup is DB-backed (`scheduler.py:_filter_new_tweets`), so restarts **won't** cause re-sends of already-scored tweets.
- An initial curation runs once on process start (`main.py:run_scheduled`). If it fails, the exception is logged and the bot still starts — the scheduled 9am run is unaffected.
- The scheduled daily job runs via a concurrent scheduler loop (`DailyCurator.run_scheduled`) alongside `telegram.run_polling()`. Exceptions inside the scheduled job are caught and logged; they don't kill the bot.
- **In-memory Telegram conversation state** (feedback notes, newsletter section picker, block-keyword list selections) is lost on restart. If a restart happens mid-flow, the user just re-issues the command.

### Updating
Push to the tracked branch → Railway auto-redeploys. No manual steps. During the ~30–60s rollover, the bot is briefly offline; Telegram holds updates for 24h so nothing is lost.

### Costs
Expect ~$5/mo on Railway's usage-based pricing for a small always-on Python worker. API costs (Anthropic, OpenAI, Twitter, Supabase) are separate.

## Local dev vs Railway
- Local: `.env` file + `python main.py --schedule`.
- Railway: env vars in the dashboard; same start command.
- Both read config via `pydantic-settings` in `config/settings.py`, so there's no deployment-specific branching in code.
