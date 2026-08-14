"""agent claims

`agents.profile_claimed` existed from the start and nothing could ever set it,
so the right-of-reply feature — which authorises on a matching phone_hash — was
unreachable by the agents it exists for.

Claims are admin-approved rather than automatic: granting someone the power to
reply as a named agent cannot rest on them asserting it, or a rival (or a
landlord answering criticism of themselves) could claim the profile.

Revision ID: f2d8b0c53e19
Revises: e7c3a91d4b52
Create Date: 2026-08-14

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2d8b0c53e19"
down_revision: Union[str, Sequence[str], None] = "e7c3a91d4b52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_claims",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "agent_id", sa.Integer(), sa.ForeignKey("agents.id"), nullable=False,
            index=True,
        ),
        sa.Column(
            "user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False,
            index=True,
        ),
        sa.Column("lasrera_number", sa.String(length=30), nullable=True),
        sa.Column("evidence_note", sa.Text(), nullable=True),
        sa.Column("contact_email", sa.String(length=120), nullable=True),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="pending",
            index=True,
        ),
        sa.Column("decided_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("agent_id", "user_id", name="uq_claim_agent_user"),
    )


def downgrade() -> None:
    op.drop_table("agent_claims")
