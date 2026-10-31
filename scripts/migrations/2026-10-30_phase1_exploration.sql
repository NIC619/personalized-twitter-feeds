-- Phase 1: exploration flag on sent items.
-- Items sent although they scored below the threshold (random sample each
-- run, marked 🎲 in Telegram). Needed to measure recall below the threshold.
ALTER TABLE public.tweets ADD COLUMN IF NOT EXISTS exploration BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_tweets_exploration ON public.tweets(exploration) WHERE exploration;
