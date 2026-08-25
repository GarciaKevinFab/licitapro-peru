"""Tokens de recuperacion de contrasena.

Por que una tabla y no una columna en usuarios:

  - Un usuario puede pedir el enlace varias veces (no le llego, se equivoco de
    bandeja). Con una columna, cada peticion pisa la anterior y el enlace que
    ya recibio deja de funcionar sin motivo aparente.
  - Queda rastro: cuantas veces se pidio, desde que IP y si se uso. Sirve para
    detectar a alguien probando correos ajenos.

Por que se guarda el HASH y no el token:

  El token es tan poderoso como la contrasena misma: quien lo tenga puede
  entrar. Si se guardara en claro, un volcado de la base entregaria todos los
  enlaces de recuperacion vigentes. Guardando el SHA-256, el volcado no sirve
  de nada. Es el mismo razonamiento que con las contrasenas.

  No hace falta bcrypt aqui: el token son 32 bytes aleatorios, no algo que se
  pueda adivinar por fuerza bruta como una contrasena humana.

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'tokens_recuperacion',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('usuario_id', sa.Integer,
                  sa.ForeignKey('usuarios.id', ondelete='CASCADE'), nullable=False),
        # SHA-256 del token en hexadecimal. Nunca el token en claro.
        sa.Column('token_hash', sa.Text, nullable=False, unique=True),
        sa.Column('expira', sa.TIMESTAMP, nullable=False),
        sa.Column('usado_en', sa.TIMESTAMP),
        sa.Column('ip', sa.Text),
        sa.Column('created_at', sa.TIMESTAMP, server_default=sa.func.now()),
    )
    # Buscar por hash es la operacion caliente: pasa en cada apertura del enlace.
    op.create_index('idx_tokrec_hash', 'tokens_recuperacion', ['token_hash'])
    # Para el limite de peticiones por usuario y para la limpieza de vencidos.
    op.create_index('idx_tokrec_usuario', 'tokens_recuperacion',
                    ['usuario_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('idx_tokrec_usuario', table_name='tokens_recuperacion')
    op.drop_index('idx_tokrec_hash', table_name='tokens_recuperacion')
    op.drop_table('tokens_recuperacion')
