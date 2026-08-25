"""Linea base: el esquema ya existe, creado por shared/schema.sql.

No hace nada a proposito. Solo marca el punto de partida para que a partir de
aqui todo cambio de esquema viaje en una migracion y no obligue a borrar la BD.

Revision ID: 0001
Revises:
"""
revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
