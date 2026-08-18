"""la sucesión de una entidad es un vínculo persistente, no solo datos del catálogo

"¿Cuántos ministros tuvo Agricultura?" exige recorrer MIDAGRI → MINAGRI →
Ministerio de Agricultura como una sola historia. El catálogo ya declara la
cadena de nombres con sus normas; esta columna la vuelve consultable entre
fichas: cada organización puede apuntar a la ficha de su nombre anterior. Solo
la escribe la sincronización del catálogo o una decisión humana; nunca se
infiere del texto, y es distinta de la fusión (`merged_into_organization_id`:
misma entidad, grafía duplicada) y de la adscripción (`parent_organization_id`).

Revision ID: b6e2a7d94f13
Revises: d7a3f81c52e4
Create Date: 2026-08-18 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b6e2a7d94f13"
down_revision = "d7a3f81c52e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("organization", schema=None) as batch_op:
        batch_op.add_column(sa.Column("predecessor_organization_id", sa.String(32), nullable=True))
        batch_op.create_foreign_key(
            "fk_organization_predecessor",
            "organization",
            ["predecessor_organization_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("organization", schema=None) as batch_op:
        batch_op.drop_constraint("fk_organization_predecessor", type_="foreignkey")
        batch_op.drop_column("predecessor_organization_id")
