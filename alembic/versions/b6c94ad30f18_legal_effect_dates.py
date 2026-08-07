"""fecha de inicio de efectos determinada por norma

Cuando el documento no expresa desde cuándo surte efectos lo que dispone, la
fecha puede estar fijada por una norma (Ley N.º 27594 art. 6 para designaciones
y nombramientos; Reglamento General de la Ley del Servicio Civil art. 233.3 para
el término). Esa fecha NO se escribe en `effective_from`, que sigue diciendo lo
que el documento dice —NOT_STATED—, sino en columnas propias que llevan además
el fundamento citado.

Todo es aditivo y anulable: una fila sin determinación queda como estaba.

Revision ID: b6c94ad30f18
Revises: d5e2b81a4c60
Create Date: 2026-08-07 16:20:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "b6c94ad30f18"
down_revision = "d5e2b81a4c60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("personnel_event", schema=None) as batch_op:
        batch_op.add_column(sa.Column("legal_effect_from", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("legal_effect_basis_json", sa.JSON(), nullable=True))

    with op.batch_alter_table("role_assignment", schema=None) as batch_op:
        batch_op.add_column(sa.Column("legal_effect_from", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("legal_effect_to", sa.Date(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("role_assignment", schema=None) as batch_op:
        batch_op.drop_column("legal_effect_to")
        batch_op.drop_column("legal_effect_from")

    with op.batch_alter_table("personnel_event", schema=None) as batch_op:
        batch_op.drop_column("legal_effect_basis_json")
        batch_op.drop_column("legal_effect_from")
