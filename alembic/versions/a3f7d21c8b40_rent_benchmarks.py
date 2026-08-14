"""rent benchmarks

Every rent figure in the app comes from tenants, which is the point — but it
also means a tenant told "your rent rose 40%" has nothing to judge that against.

The NBS publishes a dedicated HOUSING (RENT) INDEX inside the CPI (weight 4.23),
which is a real rent series rather than the Housing/Water/Electricity/Gas
division that bundles utilities in with rent and is what usually gets quoted by
mistake. National only — NBS does not break the rent index down by state.

Revision ID: a3f7d21c8b40
Revises: f2d8b0c53e19
Create Date: 2026-08-14

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3f7d21c8b40"
down_revision: Union[str, Sequence[str], None] = "f2d8b0c53e19"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "rent_benchmarks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "scope", sa.String(length=20), nullable=False, server_default="national",
            index=True,
        ),
        sa.Column("period_year", sa.Integer(), nullable=False),
        sa.Column("period_month", sa.SmallInteger(), nullable=False),
        sa.Column("index_value", sa.Numeric(10, 4), nullable=False),
        sa.Column("yoy_pct", sa.Numeric(6, 2), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "scope", "period_year", "period_month", name="uq_benchmark_period"
        ),
    )


def downgrade() -> None:
    op.drop_table("rent_benchmarks")
