"""publication_item.pdf_url (PDF declarado por la captura)

El visor de El Peruano publica en su payload la URL del PDF de cada dispositivo.
Es un puntero a la fuente, no evidencia: la evidencia sigue siendo el artefacto
capturado en el CAS. Se guarda para que la UI de revisión pueda ofrecer el
documento original sin re-parsear el HTML en cada render.

Nullable a propósito: una captura puede no declararlo, y en ese caso no se
inventa nada (regla: no inferir lo que la fuente no expresa).

Revision ID: a3f1c07b52d9
Revises: c7d1a4f92b83
Create Date: 2026-08-07 12:00:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a3f1c07b52d9"
down_revision = "c7d1a4f92b83"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("publication_item", schema=None) as batch_op:
        batch_op.add_column(sa.Column("pdf_url", sa.String(length=500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("publication_item", schema=None) as batch_op:
        batch_op.drop_column("pdf_url")
