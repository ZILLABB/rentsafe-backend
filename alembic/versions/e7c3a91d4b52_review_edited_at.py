"""review edited_at

The Profile page promised a 48-hour edit/delete window that no endpoint
implemented. Adding the window means recording when an amendment happened: a
reader weighing an account of a named landlord is entitled to know the text was
rewritten after publication.

Revision ID: e7c3a91d4b52
Revises: d5b2f9c17a48
Create Date: 2026-08-14

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7c3a91d4b52"
down_revision: Union[str, Sequence[str], None] = "d5b2f9c17a48"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "reviews", sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("reviews", "edited_at")
