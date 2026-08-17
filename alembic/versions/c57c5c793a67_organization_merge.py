"""las organizaciones pueden fusionarse tras revisión humana

ORG_VARIANT_CHECK preguntaba "¿es la misma entidad?" sin ofrecer forma de
responder que sí: fusionar organizaciones no estaba modelado y la única salida
era descartar la tarea, dejando el duplicado vivo. Con el espejo de
`person.merged_into_person_id`, la decisión LINK_ENTITY sobre una organización
absorbe el duplicado en la superviviente sin borrar la fila (regla 3): el
identificador ya publicado sigue resolviendo, apuntando a quien sobrevivió.

Revision ID: c57c5c793a67
Revises: c1d84f2b6a30
Create Date: 2026-08-17 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c57c5c793a67"
down_revision = "c1d84f2b6a30"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("organization", schema=None) as batch_op:
        batch_op.add_column(sa.Column("merged_into_organization_id", sa.String(32), nullable=True))
        batch_op.create_foreign_key(
            "fk_organization_merged_into",
            "organization",
            ["merged_into_organization_id"],
            ["id"],
        )


def downgrade() -> None:
    # Bajar con fusiones registradas las perdería en silencio; que falle la FK
    # inversa es preferible a olvidar una decisión humana.
    with op.batch_alter_table("organization", schema=None) as batch_op:
        batch_op.drop_constraint("fk_organization_merged_into", type_="foreignkey")
        batch_op.drop_column("merged_into_organization_id")
