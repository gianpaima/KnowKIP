"""resultado del procesamiento en la bitácora del recolector

Dos columnas que la primera corrida real hizo evidentes:

- `outcome_detail`: qué salió de procesar el dispositivo. Antes solo se
  imprimía por pantalla y se perdía.
- `events_extracted`: cuántos eventos de personal se extrajeron. Un cero en un
  dispositivo que el filtro llamó relevante es un hueco del extractor —el texto
  está capturado, pero nada se afirmó de él— y sin contarlo no se ve desde
  ninguna parte. En la edición del 2026-08-07 pasó en 6 de 19.

Aditiva y anulable: las filas anteriores quedan con NULL, que significa "no se
registró", no "cero eventos".

Revision ID: a91f6d3c40b7
Revises: f2c9a41d7e05
Create Date: 2026-08-07 19:40:00.000000

"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "a91f6d3c40b7"
down_revision = "f2c9a41d7e05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("crawl_item", schema=None) as batch_op:
        batch_op.add_column(sa.Column("outcome_detail", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("events_extracted", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("crawl_item", schema=None) as batch_op:
        batch_op.drop_column("events_extracted")
        batch_op.drop_column("outcome_detail")
