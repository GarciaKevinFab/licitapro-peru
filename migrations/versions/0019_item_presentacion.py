"""La presentacion ofertada de cada item, aparte de la marca.

POR QUE

  En la SC del GOREMAD firmada a mano (5928-2026) el postor escribe la marca
  en su columna ("Quaker") y la presentacion que de verdad entrega al lado de
  la descripcion ("x 330 gr", "a granel"). Hasta ahora las dos iban juntas en
  `marca`, y al llenar el formato de la entidad salian en la columna
  equivocada. Son dos datos: la marca es la marca; la presentacion es lo que
  puede no coincidir con lo pedido y la entidad tiene que verlo junto al item.

SE PUEDE APLICAR CON LA APLICACION CORRIENDO

  Columna nulable; el codigo anterior no la menciona.

Revision ID: 0019
Revises: 0018
"""
from alembic import op

revision = '0019'
down_revision = '0018'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE propuesta_items ADD COLUMN IF NOT EXISTS presentacion TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE propuesta_items DROP COLUMN IF EXISTS presentacion")
