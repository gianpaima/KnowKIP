"""bitácora del descubrimiento diario (crawl_item)

El descubrimiento por fecha convierte la ingesta en un proceso desatendido, y un
proceso desatendido necesita dejar por escrito qué vio y qué hizo con cada cosa.
`crawl_item` guarda, por corrida y dispositivo: lo que el índice declaraba, el
veredicto de relevancia con su regla, el estado final y el error literal si lo
hubo, más el artefacto del listado cuyos bytes lo declararon.

Sin esta tabla no hay forma de distinguir "ese día se publicaron 19 normas" de
"de las 32 publicadas, el filtro dejó fuera 13": lo descartado no aparece en
ninguna otra parte del modelo.

Aditiva: no toca ninguna tabla existente.

Revision ID: f2c9a41d7e05
Revises: b6c94ad30f18
Create Date: 2026-08-07 18:05:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "f2c9a41d7e05"
down_revision = "b6c94ad30f18"
branch_labels = None
depends_on = None

_RELEVANCE = ("RELEVANT", "NOT_RELEVANT", "UNDECIDED")
_STATUS = (
    "DISCOVERED",
    "SKIPPED_NOT_RELEVANT",
    "ALREADY_PRESENT",
    "INGESTED",
    "INGESTED_PDF_PENDING",
    "RETRY_PENDING",
    "FAILED",
)


def upgrade() -> None:
    op.create_table(
        "crawl_item",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("crawl_run_id", sa.String(length=32), nullable=False),
        sa.Column("source_series", sa.String(length=20), nullable=False),
        sa.Column("publication_code", sa.String(length=50), nullable=False),
        sa.Column("canonical_url", sa.String(length=500), nullable=True),
        sa.Column("listing_artifact_version_id", sa.String(length=32), nullable=True),
        sa.Column("issuer_raw", sa.String(length=500), nullable=True),
        sa.Column("document_type_raw", sa.String(length=200), nullable=True),
        sa.Column("number_raw", sa.String(length=200), nullable=True),
        sa.Column("summary_raw", sa.Text(), nullable=True),
        sa.Column("listed_date_raw", sa.String(length=100), nullable=True),
        sa.Column(
            "relevance",
            sa.Enum(*_RELEVANCE, name="relevance", native_enum=False, length=48),
            nullable=False,
        ),
        sa.Column("relevance_rule", sa.String(length=100), nullable=True),
        sa.Column("relevance_rationale", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(*_STATUS, name="crawl_item_status", native_enum=False, length=48),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("publication_item_id", sa.String(length=32), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["crawl_run_id"], ["crawl_run.id"]),
        sa.ForeignKeyConstraint(["listing_artifact_version_id"], ["artifact_version.id"]),
        sa.ForeignKeyConstraint(["publication_item_id"], ["publication_item.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("crawl_run_id", "source_series", "publication_code"),
    )
    op.create_index("ix_crawl_item_crawl_run_id", "crawl_item", ["crawl_run_id"])
    op.create_index("ix_crawl_item_publication_code", "crawl_item", ["publication_code"])
    op.create_index("ix_crawl_item_status", "crawl_item", ["status"])


def downgrade() -> None:
    op.drop_index("ix_crawl_item_status", table_name="crawl_item")
    op.drop_index("ix_crawl_item_publication_code", table_name="crawl_item")
    op.drop_index("ix_crawl_item_crawl_run_id", table_name="crawl_item")
    op.drop_table("crawl_item")
