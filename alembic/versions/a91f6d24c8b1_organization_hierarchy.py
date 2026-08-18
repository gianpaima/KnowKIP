"""las organizaciones pueden depender de una entidad mayor

Un programa nacional adscrito ("Programa Nacional de Infraestructura Educativa
- PRONIED") es una entidad con puestos propios, pero pertenece a su ministerio:
sin modelar esa dependencia, el expediente y la revisión mostraban el programa
y el ministerio como fichas inconexas, y ordenar "qué cargos hay dentro del
sector Educación" era imposible. La adscripción solo la escribe el catálogo
curado (domain/state_entities.py) o una decisión humana; nunca se infiere.

Revision ID: a91f6d24c8b1
Revises: c57c5c793a67
Create Date: 2026-08-17 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a91f6d24c8b1"
down_revision = "c57c5c793a67"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("organization", schema=None) as batch_op:
        batch_op.add_column(sa.Column("parent_organization_id", sa.String(32), nullable=True))
        batch_op.create_foreign_key(
            "fk_organization_parent",
            "organization",
            ["parent_organization_id"],
            ["id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("organization", schema=None) as batch_op:
        batch_op.drop_constraint("fk_organization_parent", type_="foreignkey")
        batch_op.drop_column("parent_organization_id")
