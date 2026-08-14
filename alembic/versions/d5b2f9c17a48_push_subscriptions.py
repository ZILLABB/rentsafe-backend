"""push subscriptions

Area watches personalised the in-app feed but delivered nothing to a phone, so a
flood report for the street a tenant was about to sign on only existed if they
happened to open the app that week.

One row per browser rather than per user: people use a phone and a laptop, and
push subscriptions are revoked per device by the browser.

Revision ID: d5b2f9c17a48
Revises: c4a1e07f2b31
Create Date: 2026-08-14

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5b2f9c17a48"
down_revision: Union[str, Sequence[str], None] = "c4a1e07f2b31"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.String(length=200), nullable=False),
        sa.Column("auth", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        # Re-subscribing the same browser must update this row, not add another,
        # or the user gets one copy of every notification per stale row.
        sa.UniqueConstraint("endpoint", name="uq_push_endpoint"),
    )


def downgrade() -> None:
    op.drop_table("push_subscriptions")
