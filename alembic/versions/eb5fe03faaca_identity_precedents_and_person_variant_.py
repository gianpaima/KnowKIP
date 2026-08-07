"""identity precedents and person variant detection

Revision ID: eb5fe03faaca
Revises: 4ab8354e1ef2
Create Date: 2026-08-06 18:45:50.079287

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "eb5fe03faaca"
down_revision = "4ab8354e1ef2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "identity_precedent",
        sa.Column("subject_type", sa.String(length=30), nullable=False),
        sa.Column("name_normalized", sa.String(length=400), nullable=False),
        sa.Column("role_context", sa.String(length=400), nullable=False),
        sa.Column("person_id", sa.String(length=32), nullable=False),
        sa.Column("source_person_mention_id", sa.String(length=32), nullable=False),
        sa.Column("review_decision_id", sa.String(length=32), nullable=False),
        sa.Column("reviewer", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_reason", sa.Text(), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["person_id"], ["person.id"]),
        sa.ForeignKeyConstraint(["review_decision_id"], ["review_decision.id"]),
        sa.ForeignKeyConstraint(["source_person_mention_id"], ["person_mention.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("identity_precedent", schema=None) as batch_op:
        batch_op.create_index(
            "ix_identity_precedent_key", ["name_normalized", "role_context"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_identity_precedent_person_id"), ["person_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_identity_precedent_review_decision_id"),
            ["review_decision_id"],
            unique=False,
        )

    with op.batch_alter_table("person", schema=None) as batch_op:
        batch_op.add_column(sa.Column("merged_into_person_id", sa.String(length=32), nullable=True))
        batch_op.create_foreign_key(
            "fk_person_merged_into_person_id", "person", ["merged_into_person_id"], ["id"]
        )

    with op.batch_alter_table("person_mention", schema=None) as batch_op:
        batch_op.add_column(sa.Column("role_context_raw", sa.String(length=400), nullable=True))
        batch_op.add_column(
            sa.Column("role_context_normalized", sa.String(length=400), nullable=True)
        )
        batch_op.add_column(sa.Column("identity_precedent_id", sa.String(length=32), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_person_mention_identity_precedent_id"),
            ["identity_precedent_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_person_mention_role_context_normalized"),
            ["role_context_normalized"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("person_mention", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_person_mention_role_context_normalized"))
        batch_op.drop_index(batch_op.f("ix_person_mention_identity_precedent_id"))
        batch_op.drop_column("identity_precedent_id")
        batch_op.drop_column("role_context_normalized")
        batch_op.drop_column("role_context_raw")

    with op.batch_alter_table("person", schema=None) as batch_op:
        batch_op.drop_constraint("fk_person_merged_into_person_id", type_="foreignkey")
        batch_op.drop_column("merged_into_person_id")

    with op.batch_alter_table("identity_precedent", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_identity_precedent_review_decision_id"))
        batch_op.drop_index(batch_op.f("ix_identity_precedent_person_id"))
        batch_op.drop_index("ix_identity_precedent_key")

    op.drop_table("identity_precedent")
