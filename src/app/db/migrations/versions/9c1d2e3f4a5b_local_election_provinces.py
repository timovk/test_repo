"""local elections per date: election.provinces

A local election now holds every province voting on its date (one combined election night;
docs/LOCAL_ELECTIONS.md).  ``election.provinces`` lists them ("OV,ZE,NB").

Revision ID: 9c1d2e3f4a5b
Revises: 572107020ab6
Create Date: 2026-10-10 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "9c1d2e3f4a5b"
down_revision: str | None = "572107020ab6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("election", schema=None) as batch_op:
        batch_op.add_column(sa.Column("provinces", sa.String(length=80), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("election", schema=None) as batch_op:
        batch_op.drop_column("provinces")
