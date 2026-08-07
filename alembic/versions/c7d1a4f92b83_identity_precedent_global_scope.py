"""identity precedent global scope (alias sin restricción de cargo)

Un precedente con role_context NULL es un alias declarado por un revisor: la
grafía ES esa persona, aparezca con el cargo que aparezca. Convive con el
precedente por cargo, que sigue siendo el alcance por defecto.

Revision ID: c7d1a4f92b83
Revises: 75f16770c199
Create Date: 2026-08-07 06:10:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c7d1a4f92b83"
down_revision = "75f16770c199"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("identity_precedent", schema=None) as batch_op:
        batch_op.alter_column(
            "role_context",
            existing_type=sa.String(length=400),
            nullable=True,
        )


def downgrade() -> None:
    # Un alias global no tiene representación en el esquema anterior. Borrarlo
    # destruiría una decisión humana en silencio (regla 3), así que la bajada
    # falla y deja al operador revocarlo o reasignarle un cargo explícitamente.
    bind = op.get_bind()
    pending = bind.execute(
        sa.text("SELECT COUNT(*) FROM identity_precedent WHERE role_context IS NULL")
    ).scalar_one()
    if pending:
        raise RuntimeError(
            f"{pending} precedente(s) de alcance global no son representables en el "
            "esquema anterior. Revócalos o asígnales un cargo antes de bajar esta "
            "revisión; esta migración no borra decisiones humanas."
        )
    with op.batch_alter_table("identity_precedent", schema=None) as batch_op:
        batch_op.alter_column(
            "role_context",
            existing_type=sa.String(length=400),
            nullable=False,
        )
