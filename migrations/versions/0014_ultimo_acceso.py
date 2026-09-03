"""Cuando entro por ultima vez cada cuenta.

PARA QUE

  El panel del dueno lista las cuentas con su plan y sus recuentos, y la
  pregunta que mas se hace delante de esa lista es "¿esta persona sigue
  usando esto?". Sin una marca de acceso solo se puede adivinar por la fecha
  de alta o por si tiene propuestas, y las dos mienten: hay quien entra cada
  manana a mirar y nunca prepara una propuesta.

  Se escribe al iniciar sesion (web/auth.py) y no en cada peticion: una
  escritura por peticion en la tabla mas leida del sistema es pagar en cada
  clic por un dato que solo cambia una vez al dia.

ANTES Y DESPUES DE APLICARLA

  El codigo funciona en los dos estados. `anotar_acceso` traga el error de
  columna inexistente y lo deja en el log; la lista del panel pregunta a
  information_schema si la columna esta y omite la columna si no. Asi la
  migracion se aplica a mano cuando toque sin que el despliegue dependa de
  ella.

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa

revision = '0014'
down_revision = '0013'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('usuarios', sa.Column('ultimo_acceso', sa.TIMESTAMP))


def downgrade() -> None:
    op.drop_column('usuarios', 'ultimo_acceso')
