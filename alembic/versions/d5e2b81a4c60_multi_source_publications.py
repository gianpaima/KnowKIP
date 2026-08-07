"""multi-fuente: autoridad del sistema fuente y documento con varias publicaciones

El mismo acto se publica en el diario oficial y en el portal de la entidad que
lo emitió, con identificadores distintos. Hasta ahora `publication_item` era
única por (serie, código) —el código de El Peruano— y `legal_document` colgaba
de exactamente una publicación, así que una segunda fuente solo cabía creando un
documento duplicado.

Cambios, todos aditivos salvo el UNIQUE que se amplía:
- `source_system.authority`: el peso jurídico del publicador.
- `publication_item.source_system_id` y UNIQUE ampliado a (sistema, serie, código).
- `document_source`: las demás publicaciones del mismo acto, con su papel.

Revision ID: d5e2b81a4c60
Revises: a3f1c07b52d9
Create Date: 2026-08-07 13:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d5e2b81a4c60"
down_revision = "a3f1c07b52d9"
branch_labels = None
depends_on = None

_AUTHORITY = sa.Enum(
    "OFFICIAL_GAZETTE",
    "ISSUING_ENTITY",
    "MIRROR",
    name="source_authority",
    native_enum=False,
    length=48,
)
_ROLE = sa.Enum(
    "AUTHORITATIVE",
    "CORROBORATING",
    name="document_source_role",
    native_enum=False,
    length=48,
)


# El UNIQUE original se creó sin nombre, así que cada motor le puso el suyo:
# PostgreSQL lo autonombra y SQLite lo deja anónimo. Para poder soltarlo hay que
# nombrarlo en cada caso; de ahí la bifurcación por dialecto en vez de un
# `batch_alter_table` uniforme que solo funcionaría en uno de los dos.
_PG_OLD_UNIQUE = "publication_item_source_series_publication_code_key"
_UNIQUE_NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}
_SQLITE_OLD_UNIQUE = "uq_publication_item_source_series"
_NEW_UNIQUE = "uq_publication_item_source"
_NEW_UNIQUE_COLUMNS = ["source_system_id", "source_series", "publication_code"]


def upgrade() -> None:
    with op.batch_alter_table("source_system", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("authority", _AUTHORITY, nullable=False, server_default="MIRROR")
        )

    with op.batch_alter_table("publication_item", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_system_id", sa.String(length=32), nullable=True))
        batch_op.create_index(
            "ix_publication_item_source_system_id", ["source_system_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_publication_item_source_system", "source_system", ["source_system_id"], ["id"]
        )

    # El UNIQUE viejo impide que dos publicadores compartan código; el nuevo lo
    # permite sin dejar de impedir el duplicado dentro de un mismo publicador.
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(_PG_OLD_UNIQUE, "publication_item", type_="unique")
        op.create_unique_constraint(_NEW_UNIQUE, "publication_item", _NEW_UNIQUE_COLUMNS)
    else:
        with op.batch_alter_table(
            "publication_item", schema=None, naming_convention=_UNIQUE_NAMING
        ) as batch_op:
            batch_op.drop_constraint(_SQLITE_OLD_UNIQUE, type_="unique")
            batch_op.create_unique_constraint(_NEW_UNIQUE, _NEW_UNIQUE_COLUMNS)

    # El Peruano ya estaba registrado como sistema fuente antes de que existiera
    # la columna, y el default (MIRROR) le quitaría precisamente la condición que
    # lo define. Corregirlo aquí y no en un backfill aparte: la fila existe
    # porque la creó la ingesta, y su autoridad no es opinable.
    op.execute(
        sa.text(
            "UPDATE source_system SET authority = 'OFFICIAL_GAZETTE' "
            "WHERE source_family = 'EL_PERUANO_NL'"
        )
    )

    op.create_table(
        "document_source",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("legal_document_id", sa.String(length=32), nullable=False),
        sa.Column("publication_item_id", sa.String(length=32), nullable=False),
        sa.Column("role", _ROLE, nullable=False),
        sa.Column("matched_by", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["legal_document_id"], ["legal_document.id"]),
        sa.ForeignKeyConstraint(["publication_item_id"], ["publication_item.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "legal_document_id", "publication_item_id", name="uq_document_source_pair"
        ),
    )
    op.create_index("ix_document_source_legal_document_id", "document_source", ["legal_document_id"])
    op.create_index(
        "ix_document_source_publication_item_id", "document_source", ["publication_item_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_document_source_publication_item_id", table_name="document_source")
    op.drop_index("ix_document_source_legal_document_id", table_name="document_source")
    op.drop_table("document_source")

    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(_NEW_UNIQUE, "publication_item", type_="unique")
        op.create_unique_constraint(
            _PG_OLD_UNIQUE, "publication_item", ["source_series", "publication_code"]
        )
        with op.batch_alter_table("publication_item", schema=None) as batch_op:
            batch_op.drop_constraint("fk_publication_item_source_system", type_="foreignkey")
            batch_op.drop_index("ix_publication_item_source_system_id")
            batch_op.drop_column("source_system_id")
    else:
        with op.batch_alter_table(
            "publication_item", schema=None, naming_convention=_UNIQUE_NAMING
        ) as batch_op:
            batch_op.drop_constraint(_NEW_UNIQUE, type_="unique")
            batch_op.create_unique_constraint(
                _SQLITE_OLD_UNIQUE, ["source_series", "publication_code"]
            )
            batch_op.drop_index("ix_publication_item_source_system_id")
            batch_op.drop_column("source_system_id")

    with op.batch_alter_table("source_system", schema=None) as batch_op:
        batch_op.drop_column("authority")
