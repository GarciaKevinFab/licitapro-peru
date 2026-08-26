"""Permitir borrar una cuenta de verdad (Ley 29733).

EL PROBLEMA

  La Ley 29733 de proteccion de datos personales da al titular el derecho de
  supresion: puede exigir que borremos sus datos, y hay que poder hacerlo. Pero
  `DELETE FROM usuarios` fallaba en cuanto el cliente tuviera una sola
  propuesta, porque propuestas, contratos, preguntas y user_config apuntaban a
  empresas con NO ACTION.

  Es decir: una obligacion legal bloqueada por una clave foranea. Y el fallo no
  aparecia hasta que alguien lo pedia, que es justo cuando hay un plazo que
  cumplir.

POR QUE CASCADE Y NO "DESACTIVAR"

  Desactivar no es borrar. Si el titular ejerce el derecho de supresion, dejar
  la fila con una marca de inactiva es seguir tratando sus datos. Para las
  bajas normales ya existe `empresas.activa = FALSE`, que conserva el historial
  y es lo que se usa a diario; esto es otra cosa y tiene que borrar.

  Las propuestas y los contratos son datos DEL cliente, no nuestros: nosotros
  los procesamos por encargo suyo. Cuando se va, se van con el. Las
  obligaciones de conservacion contable son suyas y las cumple con sus propias
  copias, no reteniendolas nosotros.

POR QUE user_config VA A SET NULL Y NO A CASCADE

  `empresa_default_id` es una preferencia ("con cual trabajo por defecto"), no
  una pertenencia. Borrar la configuracion entera del usuario porque desaparece
  una de sus empresas seria perderle las regiones, las palabras clave y el
  horario. La fila de user_config ya desaparece por su propia cascada desde
  usuarios cuando se borra la cuenta.

LO QUE ESTA MIGRACION NO PUEDE HACER

  Los logos, firmas y sellos viven en disco, en data/firmas/. Ninguna cascada
  los alcanza. Borrar la cuenta dejando ahi la firma escaneada del
  representante legal incumple exactamente lo que se pretende cumplir. De eso
  se encarga `borrar_cuenta` en shared/db.py, que limpia los archivos antes de
  tocar la fila.

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

# (tabla, restriccion, columna, regla)
CASCADAS = [
    ('propuestas', 'propuestas_empresa_id_fkey', 'empresa_id', 'CASCADE'),
    ('preguntas', 'preguntas_empresa_id_fkey', 'empresa_id', 'CASCADE'),
    ('contratos', 'contratos_empresa_id_fkey', 'empresa_id', 'CASCADE'),
    ('user_config', 'user_config_empresa_default_id_fkey',
     'empresa_default_id', 'SET NULL'),
]


def upgrade() -> None:
    for tabla, restriccion, columna, regla in CASCADAS:
        op.drop_constraint(restriccion, tabla, type_='foreignkey')
        op.create_foreign_key(restriccion, tabla, 'empresas',
                              [columna], ['id'], ondelete=regla)


def downgrade() -> None:
    for tabla, restriccion, columna, _ in CASCADAS:
        op.drop_constraint(restriccion, tabla, type_='foreignkey')
        op.create_foreign_key(restriccion, tabla, 'empresas', [columna], ['id'])
