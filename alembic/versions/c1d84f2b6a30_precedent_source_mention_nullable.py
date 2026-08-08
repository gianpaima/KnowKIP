"""el precedente puede sobrevivir al reproceso de la mención que lo motivó

`identity_precedent.source_person_mention_id` era NOT NULL, así que re-extraer
un documento cuyas menciones sostienen una decisión humana era imposible: el
DELETE de `person_mention` violaba la FK y `kipu reprocess` fallaba entero.

Anular la columna no afloja la trazabilidad. Lo que el precedente afirma
—"la grafía N, con el cargo C, es la persona P"— vive en sus propias columnas
(`name_normalized`, `role_context`, `person_id`) y en la `ReviewDecision` que
lo originó, que no se toca. El puntero a la mención dice cuál la motivó, y el
reproceso lo vuelve a apuntar a la mención equivalente del documento nuevo.
Solo queda NULL si la nueva extracción ya no produce esa mención, y entonces se
abre una tarea de revisión: que un precedente pierda su origen es algo que
tiene que decidir un humano, no algo que se arregle solo.

Revision ID: c1d84f2b6a30
Revises: a91f6d3c40b7
Create Date: 2026-08-08 02:10:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "c1d84f2b6a30"
down_revision = "a91f6d3c40b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("identity_precedent", schema=None) as batch_op:
        batch_op.alter_column(
            "source_person_mention_id", existing_type=sa.String(32), nullable=True
        )


def downgrade() -> None:
    # Volver a NOT NULL exige que ninguna fila haya quedado huérfana; si alguna
    # lo está, la migración debe fallar en vez de inventar un origen.
    with op.batch_alter_table("identity_precedent", schema=None) as batch_op:
        batch_op.alter_column(
            "source_person_mention_id", existing_type=sa.String(32), nullable=False
        )
