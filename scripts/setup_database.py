#!/usr/bin/env python3
"""Database setup script for the preference curator.

Prints the SQL for a FRESH install (items, signals, item_embeddings,
preference_profiles, authors, keywords, error log). Paste it into the
Supabase SQL Editor.

Existing deployments must NOT use this: run the files in
scripts/migrations/ in date order instead; they copy the legacy
tweets / feedback / tweet_embeddings tables into the new ones.
"""

SCHEMA_SQL = """-- Preference store schema (fresh install). Existing deployments: run the
-- files in scripts/migrations/ in date order instead.

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

-- ------------------------------------------------------------- signals
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

-- ------------------------------------------------------------ authors
CREATE TABLE IF NOT EXISTS public.favorite_authors (
    id BIGSERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,   -- lowercase, without @
    added_at TIMESTAMPTZ DEFAULT NOW(),
    notes TEXT
);
CREATE TABLE IF NOT EXISTS public.muted_authors (
    id BIGSERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    added_at TIMESTAMPTZ DEFAULT NOW(),
    notes TEXT
);
ALTER TABLE public.favorite_authors ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.muted_authors ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_all" ON public.favorite_authors;
DROP POLICY IF EXISTS "anon_all" ON public.muted_authors;
CREATE POLICY "anon_all" ON public.favorite_authors FOR ALL TO anon USING (true) WITH CHECK (true);
CREATE POLICY "anon_all" ON public.muted_authors FOR ALL TO anon USING (true) WITH CHECK (true);

-- ------------------------------------------------------------ keywords
CREATE TABLE IF NOT EXISTS public.blocked_keywords (
    keyword TEXT PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE public.blocked_keywords ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_all" ON public.blocked_keywords;
CREATE POLICY "anon_all" ON public.blocked_keywords FOR ALL TO anon USING (true) WITH CHECK (true);

-- ----------------------------------------------------------- error log
CREATE TABLE IF NOT EXISTS public.error_log (
    id BIGSERIAL PRIMARY KEY,
    logged_at TIMESTAMPTZ DEFAULT NOW(),
    source TEXT NOT NULL,
    level TEXT NOT NULL,
    error_type TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_error_log_logged_at ON public.error_log(logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_error_log_level ON public.error_log(level);
ALTER TABLE public.error_log ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_all" ON public.error_log;
CREATE POLICY "anon_all" ON public.error_log FOR ALL TO anon USING (true) WITH CHECK (true);

-- --------------------------------------------- newsletter preferences
-- (legacy; removed in phase 4)
CREATE TABLE IF NOT EXISTS public.newsletter_preferences (
    domain TEXT PRIMARY KEY,
    ignored_sections JSONB NOT NULL DEFAULT '[]'::jsonb,
    all_sections JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
ALTER TABLE public.newsletter_preferences ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_all" ON public.newsletter_preferences;
CREATE POLICY "anon_all" ON public.newsletter_preferences FOR ALL TO anon USING (true) WITH CHECK (true);
"""


def print_schema():
    """Print the schema SQL."""
    print("=" * 60)
    print("Preference Curator Database Schema (fresh install)")
    print("=" * 60)
    print()
    print("Copy and paste the following SQL into Supabase SQL Editor:")
    print()
    print("-" * 60)
    print(SCHEMA_SQL)
    print("-" * 60)
    print()
    print("After running the SQL:")
    print("1. Go to Supabase Dashboard > Table Editor")
    print("2. Verify tables exist: items, signals, item_embeddings, preference_profiles")
    print("3. Check that pgvector extension is enabled")
    print()
    print("Upgrading an existing deployment? Run scripts/migrations/*.sql in date order instead.")


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Print the fresh-install database schema")
    parser.add_argument("--sql-only", action="store_true", help="Print SQL only, no additional output")
    args = parser.parse_args()

    if args.sql_only:
        print(SCHEMA_SQL)
    else:
        print_schema()


if __name__ == "__main__":
    main()
