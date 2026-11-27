-- Seed data for SIS #3: 50M posts (post_counters) and 20M likes.
-- Run: docker compose exec -T postgres psql -U postgres -d counters -f - < db/seed.sql
-- Takes a few minutes. Safe to run again (it empties both tables first).
--
-- Shape of the data (the same as the load test):
--   * 50,000,000 posts, ids 1..50M.
--   * 10,000,000 users, 2 likes each = 20M likes:
--     the 1st like goes to one of 100K hot posts, the 2nd to any of the 50M posts.
--     So hot posts have ~100 likes, the long tail has 0 or 1. Real feeds look like this.

\timing on
SET synchronous_commit = off;          -- seeding only: do not wait for the WAL flush on commit
SET maintenance_work_mem = '512MB';    -- faster index build

TRUNCATE likes, post_counters;

-- 20M likes. The (user, post) pairs are unique by construction; ON CONFLICT is a safety net.
INSERT INTO likes (user_id, post_id, created_at)
SELECT (n - 1) / 2 + 1,
       CASE WHEN n % 2 = 0 THEN (n * 2654435761 % 100000) + 1
                           ELSE (n * 2654435761 % 50000000) + 1 END,
       now() - (n % 2592000) * interval '1 second'           -- spread over the last 30 days
FROM generate_series(1::bigint, 20000000) AS n
ON CONFLICT DO NOTHING;

-- 50M counters. likes = the real count from the likes table; views = about 30 per like + noise.
INSERT INTO post_counters (post_id, likes, views, updated_at)
SELECT p, coalesce(l.cnt, 0), coalesce(l.cnt, 0) * 30 + (p * 7919 % 50), now()
FROM generate_series(1::bigint, 50000000) AS p   -- bigint: p * 7919 does not fit in 32-bit integer
LEFT JOIN (SELECT post_id, count(*) AS cnt FROM likes GROUP BY post_id) l ON l.post_id = p;

VACUUM (ANALYZE) likes;
VACUUM (ANALYZE) post_counters;

SELECT relname, n_live_tup, pg_size_pretty(pg_total_relation_size(relid)) AS total_size
FROM pg_stat_user_tables WHERE relname IN ('likes', 'post_counters');
