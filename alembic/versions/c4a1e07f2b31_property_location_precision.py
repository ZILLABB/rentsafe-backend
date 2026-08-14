"""property location precision

Records whether a property's coordinate is the building or only the
neighbourhood centroid. Lagos street-level coverage in OpenStreetMap is thin,
so registrations frequently resolve no closer than the area — and a map that
draws those as rooftop pins is claiming precision the data does not have.

Existing rows default to "exact": every property registered before this came
either from a pin drop or from a geocoder hit that did resolve the street.

Revision ID: c4a1e07f2b31
Revises: b9d74768b13a
Create Date: 2026-08-14

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c4a1e07f2b31"
down_revision: Union[str, Sequence[str], None] = "b9d74768b13a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "properties",
        sa.Column(
            "location_precision",
            sa.String(length=10),
            nullable=False,
            server_default="exact",
        ),
    )


def downgrade() -> None:
    op.drop_column("properties", "location_precision")
