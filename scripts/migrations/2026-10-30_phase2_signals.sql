-- Phase 2: unified signals table.
-- Every explicit or implicit preference event lands here: votes (with
-- strength), reasons, notes, link opens. Votes are ALSO still written to
-- the legacy `feedback` table until phase 3 migrates reads over.
CREATE TABLE IF NOT EXISTS public.signals (
    id BIGSERIAL PRIMARY KEY,
    item_id TEXT NOT NULL,                 -- tweets.tweet_id (items.id after phase 3)
    kind TEXT NOT NULL,                    -- 'vote' | 'note' | 'open'
    value TEXT,                            -- vote: 'up' | 'down'
    strength SMALLINT NOT NULL DEFAULT 1,  -- vote: 1 like, 2 strong (🔥)
    reason TEXT,                           -- confirmed/edited reason, or note body
    source TEXT,                           -- 'button' | 'reaction' | 'like_cmd' | 'reply' | 'redirect'
    telegram_message_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signals_item ON public.signals(item_id);
CREATE INDEX IF NOT EXISTS idx_signals_kind_created ON public.signals(kind, created_at DESC);
ALTER TABLE public.signals ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_all" ON public.signals;
CREATE POLICY "anon_all" ON public.signals FOR ALL TO anon USING (true) WITH CHECK (true);

-- Reactions arrive with only a Telegram message id; look items up by it.
CREATE INDEX IF NOT EXISTS idx_tweets_telegram_message_id ON public.tweets(telegram_message_id);
