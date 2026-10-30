-- Phase 3: general preference store.
--
-- New tables: items (any content from any source), item_embeddings,
-- preference_profiles. The signals table from phase 2 becomes the only
-- feedback store. Legacy tables (tweets, feedback, tweet_embeddings,
-- ab_test_scores, newsletter_preferences) are copied from and left in place;
-- drop them yourself once you are satisfied.
--
-- Idempotent: safe to run more than once.

CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------- items
CREATE TABLE IF NOT EXISTS public.items (
    item_id TEXT PRIMARY KEY,                     -- tweet id, blog_<hash>, note_<hash>, ...
    source TEXT NOT NULL DEFAULT 'twitter',       -- twitter | blog | newsletter | rss | telegram | manual | import:<name>
    content_type TEXT NOT NULL DEFAULT 'tweet',   -- tweet | blog_post | note | video | ...
    url TEXT,
    title TEXT,
    author_username TEXT,
    author_name TEXT,
    text TEXT NOT NULL,
    created_at TIMESTAMPTZ,                       -- when the content was published
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metrics JSONB,
    raw_data JSONB,
    is_retweet BOOLEAN NOT NULL DEFAULT FALSE,
    domain TEXT,                                  -- LLM-assigned topic domain
    filtered BOOLEAN NOT NULL DEFAULT FALSE,
    filter_score FLOAT,
    filter_reason TEXT,
    exploration BOOLEAN NOT NULL DEFAULT FALSE,
    sent_to_telegram TIMESTAMPTZ,
    telegram_message_id BIGINT
);
CREATE INDEX IF NOT EXISTS idx_items_created_at ON public.items(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_items_sent ON public.items(sent_to_telegram);
CREATE INDEX IF NOT EXISTS idx_items_message ON public.items(telegram_message_id);
CREATE INDEX IF NOT EXISTS idx_items_domain ON public.items(domain);
CREATE INDEX IF NOT EXISTS idx_items_source ON public.items(source);
CREATE INDEX IF NOT EXISTS idx_items_url ON public.items(url);
CREATE INDEX IF NOT EXISTS idx_items_scored ON public.items(filter_score) WHERE filter_score IS NOT NULL;
ALTER TABLE public.items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_all" ON public.items;
CREATE POLICY "anon_all" ON public.items FOR ALL TO anon USING (true) WITH CHECK (true);

-- Copy legacy tweets (and blog posts stored in the tweets table)
INSERT INTO public.items (
    item_id, source, content_type, url, author_username, author_name, text,
    created_at, fetched_at, metrics, raw_data, is_retweet, filtered,
    filter_score, filter_reason, exploration, sent_to_telegram, telegram_message_id
)
SELECT
    t.tweet_id,
    CASE WHEN t.content_type = 'blog_post'
         THEN COALESCE(t.raw_data->>'source', 'blog') ELSE 'twitter' END,
    COALESCE(t.content_type, 'tweet'),
    t.url, t.author_username, t.author_name, t.text,
    t.created_at, t.fetched_at, t.metrics, t.raw_data,
    COALESCE(t.is_retweet, false), COALESCE(t.filtered, false),
    t.filter_score, t.filter_reason,
    COALESCE(t.exploration, false), t.sent_to_telegram, t.telegram_message_id
FROM public.tweets t
ON CONFLICT (item_id) DO NOTHING;

-- ------------------------------------------------------------- signals
-- (table created in the phase 2 migration; add the FK now that items exist)
CREATE TABLE IF NOT EXISTS public.signals (
    id BIGSERIAL PRIMARY KEY,
    item_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT,
    strength SMALLINT NOT NULL DEFAULT 1,
    reason TEXT,
    source TEXT,
    telegram_message_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_signals_item ON public.signals(item_id);
CREATE INDEX IF NOT EXISTS idx_signals_kind_created ON public.signals(kind, created_at DESC);
ALTER TABLE public.signals ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_all" ON public.signals;
CREATE POLICY "anon_all" ON public.signals FOR ALL TO anon USING (true) WITH CHECK (true);

-- Copy legacy votes that have no matching signal yet (phase 2 double-wrote
-- votes, so skip feedback rows with a vote signal within 2 minutes).
INSERT INTO public.signals (item_id, kind, value, strength, reason, source, telegram_message_id, created_at)
SELECT f.tweet_id, 'vote', f.user_vote, 1, f.notes, 'legacy_feedback', f.telegram_message_id, f.voted_at
FROM public.feedback f
JOIN public.items i ON i.item_id = f.tweet_id
WHERE NOT EXISTS (
    SELECT 1 FROM public.signals s
    WHERE s.item_id = f.tweet_id AND s.kind = 'vote'
      AND abs(extract(epoch FROM (s.created_at - f.voted_at::timestamptz))) < 120
);

-- Drop orphan signals (items that never existed) before adding the FK
DELETE FROM public.signals s WHERE NOT EXISTS (SELECT 1 FROM public.items i WHERE i.item_id = s.item_id);
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'signals_item_fk') THEN
        ALTER TABLE public.signals
            ADD CONSTRAINT signals_item_fk FOREIGN KEY (item_id)
            REFERENCES public.items(item_id) ON DELETE CASCADE;
    END IF;
END $$;

-- ----------------------------------------------------- item_embeddings
CREATE TABLE IF NOT EXISTS public.item_embeddings (
    item_id TEXT PRIMARY KEY REFERENCES public.items(item_id) ON DELETE CASCADE,
    embedding vector(1536) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_item_embeddings ON public.item_embeddings
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
ALTER TABLE public.item_embeddings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_all" ON public.item_embeddings;
CREATE POLICY "anon_all" ON public.item_embeddings FOR ALL TO anon USING (true) WITH CHECK (true);

INSERT INTO public.item_embeddings (item_id, embedding, created_at)
SELECT te.tweet_id, te.embedding, te.created_at
FROM public.tweet_embeddings te
JOIN public.items i ON i.item_id = te.tweet_id
ON CONFLICT (item_id) DO NOTHING;

-- -------------------------------------------------- preference_profiles
CREATE TABLE IF NOT EXISTS public.preference_profiles (
    id BIGSERIAL PRIMARY KEY,
    domain TEXT NOT NULL,                 -- 'overall' or a topic domain
    version INTEGER NOT NULL,
    content TEXT NOT NULL,                -- markdown
    stats JSONB,                          -- counts used to build it
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (domain, version)
);
CREATE INDEX IF NOT EXISTS idx_profiles_domain_version ON public.preference_profiles(domain, version DESC);
ALTER TABLE public.preference_profiles ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_all" ON public.preference_profiles;
CREATE POLICY "anon_all" ON public.preference_profiles FOR ALL TO anon USING (true) WITH CHECK (true);

-- ------------------------------------------------------- similarity RPC
-- Nearest voted items: each item's latest vote (strength, reason) + similarity.
CREATE OR REPLACE FUNCTION match_voted_items(
    query_embedding vector(1536),
    match_count int DEFAULT 5
)
RETURNS TABLE (
    item_id text,
    text text,
    author_username text,
    domain text,
    vote text,
    strength smallint,
    reason text,
    similarity float
)
LANGUAGE sql STABLE
AS $$
    WITH latest_vote AS (
        SELECT DISTINCT ON (s.item_id) s.item_id, s.value, s.strength, s.reason
        FROM public.signals s
        WHERE s.kind = 'vote' AND s.value IN ('up', 'down')
        ORDER BY s.item_id, s.created_at DESC
    )
    SELECT
        i.item_id,
        i.text,
        i.author_username,
        i.domain,
        lv.value AS vote,
        lv.strength,
        lv.reason,
        1 - (e.embedding <=> query_embedding) AS similarity
    FROM public.item_embeddings e
    JOIN public.items i ON i.item_id = e.item_id
    JOIN latest_vote lv ON lv.item_id = e.item_id
    ORDER BY e.embedding <=> query_embedding
    LIMIT match_count;
$$;

-- Latest vote per item as a view (handy for SQL exploration and the eval)
CREATE OR REPLACE VIEW public.item_votes
WITH (security_invoker = true) AS
SELECT DISTINCT ON (s.item_id)
    s.item_id, s.value AS vote, s.strength, s.reason, s.source, s.created_at AS voted_at
FROM public.signals s
WHERE s.kind = 'vote' AND s.value IN ('up', 'down')
ORDER BY s.item_id, s.created_at DESC;
