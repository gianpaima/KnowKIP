"""el contexto de rol admite celdas largas con notas al pie

Una mención del cuadro de la RM 197-2026-PCM (2541397-1) traía el cargo con las
notas al pie de la tabla: más de 400 caracteres, y el INSERT abortaba la ingesta
del documento entero (StringDataRightTruncation). El texto es fiel a la fuente;
lo que sobraba era el límite: con 1000 la mención se registra y cualquier
contaminación queda a la vista de la revisión en lugar de perder el documento.

Revision ID: d7a3f81c52e4
Revises: a91f6d24c8b1
Create Date: 2026-08-17 00:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d7a3f81c52e4"
down_revision = "a91f6d24c8b1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("person_mention", schema=None) as batch_op:
        batch_op.alter_column(
            "role_context_raw", type_=sa.String(1000), existing_type=sa.String(400)
        )
        batch_op.alter_column(
            "role_context_normalized", type_=sa.String(1000), existing_type=sa.String(400)
        )


def downgrade() -> None:
    # Bajar recortaría contextos ya registrados; que falle es preferible a
    # truncar en silencio lo que la fuente escribió.
    with op.batch_alter_table("person_mention", schema=None) as batch_op:
        batch_op.alter_column(
            "role_context_normalized", type_=sa.String(400), existing_type=sa.String(1000)
        )
        batch_op.alter_column(
            "role_context_raw", type_=sa.String(400), existing_type=sa.String(1000)
        )
