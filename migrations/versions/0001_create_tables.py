"""create likes and post_counters

Revision ID: 0001
Revises:
Create Date: 2026-10-29
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
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
    op.execute(
        """
        CREATE TABLE post_counters (
            post_id    BIGINT PRIMARY KEY,
            likes      BIGINT NOT NULL DEFAULT 0,
            views      BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE post_counters")
    op.execute("DROP TABLE likes")
