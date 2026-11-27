"""SIS #3 tuning: partition likes by user_id, fillfactor + autovacuum for post_counters

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-28

Why (numbers in benchmark/db_results.md):

likes -> 16 hash partitions by user_id.
  Every query on likes has `user_id = $1` (like, unlike, liked_by_me), so Postgres reads
  only 1 of 16 partitions (partition pruning). The table only grows (20M rows now, ~1K new
  rows/s at peak); 16 small tables + small indexes are vacuumed and indexed in parallel,
  and one partition's index fits in memory much better than one huge index.
  Not by post_id: liked_by_me asks for one user and many posts -> it would touch many partitions.

post_counters -> fillfactor 80 + more frequent autovacuum. NOT partitioned.
  The sync worker UPDATEs thousands of rows every 5 s. With 20% free space on each page,
  Postgres can write the new row version on the same page and skip the index update
  ("HOT update"): less WAL, no index bloat. `updated_at` is not indexed, so HOT works.
  Not partitioned: GetCounters reads 100 random post_ids at once -> with hash partitions it
  would touch all of them; the table has a fixed size (1 row per post) and no old data to drop.

This migration rewrites both tables (minutes on 50M rows). In production you would do it
online (new table + dual write + backfill, or pg_repack); here downtime is fine.
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

PARTITIONS = 16


def upgrade() -> None:
    # The old index keeps its name; Postgres picks a free name for the new primary key.
    op.execute("ALTER TABLE likes RENAME TO likes_old")
    op.execute(
        """
        CREATE TABLE likes (
            user_id    BIGINT      NOT NULL,
            post_id    BIGINT      NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (user_id, post_id)
        ) PARTITION BY HASH (user_id)
        """
    )
    for i in range(PARTITIONS):
        op.execute(
            f"CREATE TABLE likes_p{i:02d} PARTITION OF likes"
            f" FOR VALUES WITH (MODULUS {PARTITIONS}, REMAINDER {i})"
        )
    op.execute("INSERT INTO likes SELECT * FROM likes_old")
    op.execute("DROP TABLE likes_old")

    op.execute(
        """
        ALTER TABLE post_counters SET (
            fillfactor = 80,
            autovacuum_vacuum_scale_factor = 0.01,
            autovacuum_analyze_scale_factor = 0.02
        )
        """
    )
    # fillfactor only applies to pages written from now on -> rewrite the table once.
    # VACUUM cannot run inside a transaction.
    with op.get_context().autocommit_block():
        op.execute("VACUUM FULL post_counters")
        op.execute("VACUUM ANALYZE likes")
        op.execute("ANALYZE post_counters")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE post_counters"
        " RESET (fillfactor, autovacuum_vacuum_scale_factor, autovacuum_analyze_scale_factor)"
    )
    op.execute("ALTER TABLE likes RENAME TO likes_part")
    op.execute(
        """
        CREATE TABLE likes (
            user_id    BIGINT      NOT NULL,
            post_id    BIGINT      NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            PRIMARY KEY (user_id, post_id)
        )
        """
    )
    op.execute("INSERT INTO likes SELECT * FROM likes_part")
    op.execute("DROP TABLE likes_part")
