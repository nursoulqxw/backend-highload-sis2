-- EXPLAIN ANALYZE of every query T7 sends to Postgres, with realistic parameters.
-- Run: docker compose exec -T postgres psql -U postgres -d counters -f - < db/explain.sql
-- BUFFERS shows how many 8 KB pages were read from cache (hit) or disk (read).

\pset pager off
\echo '=== Q1 like: INSERT ... ON CONFLICT DO NOTHING (rolled back, so the data does not change)'
BEGIN;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
INSERT INTO likes (user_id, post_id) VALUES (987654321, 4242) ON CONFLICT DO NOTHING;
ROLLBACK;

\echo '=== Q2 unlike: DELETE by primary key (rolled back)'
BEGIN;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
DELETE FROM likes WHERE user_id = 12345 AND post_id = (SELECT post_id FROM likes WHERE user_id = 12345 LIMIT 1);
ROLLBACK;

\echo '=== Q3 cold read x1: one post'
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT post_id, likes, views FROM post_counters WHERE post_id = ANY(ARRAY[31415926]::bigint[]);

\echo '=== Q3 cold read x100: 100 random posts of 50M'
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT post_id, likes, views FROM post_counters
WHERE post_id = ANY(ARRAY(SELECT (g * 2654435761 % 50000000) + 1 FROM generate_series(1, 100) g)::bigint[]);

\echo '=== Q4 liked_by_me: one user, 20 posts'
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
SELECT post_id FROM likes
WHERE user_id = 12345 AND post_id = ANY(ARRAY(SELECT (g * 7919 % 100000) + 1 FROM generate_series(1, 20) g)::bigint[]);

\echo '=== Q5 sync worker: upsert 1000 counters with unnest (rolled back)'
BEGIN;
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF)
INSERT INTO post_counters (post_id, likes, views, updated_at)
SELECT p, 1, 30, now() FROM generate_series(1, 1000) p
ON CONFLICT (post_id) DO UPDATE SET likes = EXCLUDED.likes, views = EXCLUDED.views, updated_at = now();
ROLLBACK;
