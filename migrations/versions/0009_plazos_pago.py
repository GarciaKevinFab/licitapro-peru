"""Conformidad, plazo legal de pago y expediente SIAF.

EL FALLO QUE ARREGLA

  `registrar_factura` calculaba la fecha de cobro como "factura + 30 dias
  corridos", un numero puesto a ojo. La Ley 32069, vigente desde el 22 de abril
  de 2025, dice otra cosa: 10 DIAS HABILES desde la CONFORMIDAD.

  Los dos elementos estaban mal, y en direcciones que se suman:

    - El ancla. El plazo corre desde la conformidad, no desde la factura. Sin
      guardar la conformidad era imposible calcularlo bien aunque el numero
      hubiera sido el correcto. Por eso hace falta la columna.

    - La cuenta. Habiles, no corridos. Ejemplo real: conformidad el viernes 24
      de julio de 2026 -> el limite legal cae el 12 de agosto, porque en medio
      hay dos Fiestas Patrias, la Batalla de Junin y tres fines de semana. Son
      19 dias corridos para 10 habiles. La formula vieja apuntaba al 23 de
      agosto y le habria dicho al proveedor que esperase mientras la entidad ya
      estaba en mora desde el 13: diez dias de reclamo perdidos.

POR QUE SE GUARDA LA FECHA LIMITE EN VEZ DE CALCULARLA AL VUELO

  Es la columna por la que se pregunta "a quien se le paso el plazo", y esa
  consulta tiene que poder filtrar en la base. Calcularla al leer obligaria a
  traer todos los pagos a memoria para descartar la mayoria.

  Se recalcula al guardar la conformidad, que es cuando puede cambiar.

POR QUE EL EXPEDIENTE SIAF SE GUARDA PERO NO SE CONSULTA SOLO

  La consulta oficial del MEF exige resolver un CAPTCHA. No se evade. Lo que si
  sirve es guardar el numero de expediente que el proveedor ya tiene en su orden
  de compra: asi la plataforma le da los tres datos que la consulta pide y que
  nadie recuerda de memoria, y el hace el ultimo paso.

Revision ID: 0009
Revises: 0008
"""
from alembic import op
import sqlalchemy as sa

revision = '0009'
down_revision = '0008'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # goods / services / works, tal como lo publica OCDS. Determina el plazo:
    # bienes y servicios tienen los 10 dias habiles; obras y consultoria de
    # obras se rigen por reglas propias y no se les inventa una fecha.
    op.add_column('licitaciones', sa.Column('categoria', sa.Text))

    # El momento desde el que corre el plazo. Sin esto no habia forma de
    # calcularlo bien, por muy correcto que fuera el numero de dias.
    op.add_column('pagos', sa.Column('fecha_conformidad', sa.Date))
    op.add_column('pagos', sa.Column('fecha_limite_pago', sa.Date))
    # El numero que figura en la orden de compra o de servicio. Se guarda para
    # podersele mostrar al usuario cuando vaya a consultar al MEF.
    op.add_column('pagos', sa.Column('expediente_siaf', sa.Text))

    # "Que pagos se pasaron de plazo" es la consulta que da valor a todo esto.
    op.create_index('idx_pagos_limite', 'pagos', ['fecha_limite_pago'])


def downgrade() -> None:
    op.drop_index('idx_pagos_limite', table_name='pagos')
    op.drop_column('pagos', 'expediente_siaf')
    op.drop_column('pagos', 'fecha_limite_pago')
    op.drop_column('pagos', 'fecha_conformidad')
    op.drop_column('licitaciones', 'categoria')
