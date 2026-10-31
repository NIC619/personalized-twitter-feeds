-- Phase 4: RSS/Atom feed subscriptions polled during the daily run.
CREATE TABLE IF NOT EXISTS public.feeds (
    url TEXT PRIMARY KEY,                 -- the feed URL
    title TEXT,
    site_url TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_fetched_at TIMESTAMPTZ,
    last_error TEXT,
    error_count INTEGER NOT NULL DEFAULT 0
);
ALTER TABLE public.feeds ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_all" ON public.feeds;
CREATE POLICY "anon_all" ON public.feeds FOR ALL TO anon USING (true) WITH CHECK (true);

-- newsletter_preferences is no longer used (section parsing was replaced by
-- LLM extraction). Drop it when convenient:
--   DROP TABLE IF EXISTS public.newsletter_preferences;
