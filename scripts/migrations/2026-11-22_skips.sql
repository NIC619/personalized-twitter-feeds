-- Skips: an explicit "seen, not for me" vote, weaker than down.
--
-- Adds value 'skip' to vote signals (the 😐 button or reaction on an item;
-- source 'meh' = the reader tapped "rest are meh" under a run, which skips
-- every unvoted, unopened item of that run). The neighbour RPC and the
-- latest-vote view now include skips so the scorer and the eval see them.
-- A later real vote on the same item still wins (latest per item).

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
        WHERE s.kind = 'vote' AND s.value IN ('up', 'down', 'skip')
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

CREATE OR REPLACE VIEW public.item_votes
WITH (security_invoker = true) AS
SELECT DISTINCT ON (s.item_id)
    s.item_id, s.value AS vote, s.strength, s.reason, s.source, s.created_at AS voted_at
FROM public.signals s
WHERE s.kind = 'vote' AND s.value IN ('up', 'down', 'skip')
ORDER BY s.item_id, s.created_at DESC;

-- Lookups by delivery time ("rest are meh" reads the run's sent items)
CREATE INDEX IF NOT EXISTS idx_items_sent_to_telegram
    ON public.items(sent_to_telegram) WHERE sent_to_telegram IS NOT NULL;
