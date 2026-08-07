"""person identifiers

Revision ID: 75f16770c199
Revises: eb5fe03faaca
Create Date: 2026-08-06 20:37:47.432390

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "75f16770c199"
down_revision = "eb5fe03faaca"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "person_identifier",
        sa.Column("person_mention_id", sa.String(length=32), nullable=False),
        sa.Column(
            "scheme",
            sa.Enum(
                "DNI",
                "CARNE_EXTRANJERIA",
                "PASAPORTE",
                "RUC",
                name="identifier_scheme",
                native_enum=False,
                length=48,
            ),
            nullable=False,
        ),
        sa.Column("value_raw", sa.String(length=60), nullable=False),
        sa.Column("value_normalized", sa.String(length=60), nullable=False),
        sa.Column("evidence_span_id", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["evidence_span_id"], ["evidence_span.id"]),
        sa.ForeignKeyConstraint(["person_mention_id"], ["person_mention.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("person_mention_id", "scheme", "value_normalized"),
    )
    with op.batch_alter_table("person_identifier", schema=None) as batch_op:
        batch_op.create_index(
            "ix_person_identifier_lookup", ["scheme", "value_normalized"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_person_identifier_person_mention_id"),
            ["person_mention_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("person_identifier", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_person_identifier_person_mention_id"))
        batch_op.drop_index("ix_person_identifier_lookup")

    op.drop_table("person_identifier")
