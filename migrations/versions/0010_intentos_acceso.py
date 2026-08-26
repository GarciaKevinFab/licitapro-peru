"""Freno a los intentos de contrasena.

EL PROBLEMA, MEDIDO

  Veinte intentos de contrasena incorrecta seguidos: veinte respuestas 401 y la
  cuenta siguiendo operativa. Lo unico que ralentizaba era el coste de bcrypt,
  unos 0,4 segundos por intento. Eso permite miles de pruebas por hora contra
  una cuenta concreta.

  Lo llamativo es que la recuperacion de contrasena SI tenia limite
  (`MAX_PETICIONES_POR_HORA`). Se protegio la puerta lateral y se dejo la
  principal abierta.

POR QUE EN LA BASE Y NO EN REDIS

  Redis figura en requirements pero ningun modulo Python lo usa: es una
  dependencia muerta. Levantar una pieza de infraestructura nueva para un
  contador que se consulta una vez por inicio de sesion es desproporcionado, y
  ademas anade un servicio que puede caerse y dejar la puerta sin vigilancia.

  Guardarlo en memoria del proceso tampoco vale: en produccion hay varios
  contenedores, asi que cada uno llevaria su propia cuenta y el limite real
  seria el numero de replicas multiplicado por el limite configurado.

POR QUE SE ANOTA EL CORREO Y TAMBIEN LA IP

  Son dos ataques distintos y hace falta frenar los dos:

    - Muchas contrasenas contra UN correo: la fuerza bruta clasica.
    - Una contrasena comun contra MUCHOS correos ("password spraying"), que
      esquiva por completo un limite por cuenta porque nunca insiste con la
      misma.

  Por eso hay una fila por intento con su identificador, y el limite se
  comprueba sobre ambos. Se guarda el correo en claro y no un hash porque hay
  que poder mirarlo al investigar un incidente, y porque ya esta en claro en la
  tabla de usuarios: cifrarlo aqui no anadiria proteccion.

LO QUE ESTA TABLA NO DEBE HACER

  Bloquear la cuenta de forma permanente. Si bastara con fallar N veces contra
  el correo de alguien para dejarlo fuera, cualquiera podria echar del sistema
  a un cliente que paga. El bloqueo es por ventana de tiempo y se levanta solo.

Revision ID: 0010
Revises: 0009
"""
from alembic import op
import sqlalchemy as sa

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'intentos_acceso',
        sa.Column('id', sa.BigInteger, primary_key=True),
        # El correo probado, o la IP de origen. No se usa clave foranea a
        # usuarios a proposito: la mayoria de los intentos de un ataque van
        # contra correos que no existen, y esos tambien hay que contarlos.
        sa.Column('identificador', sa.Text, nullable=False),
        sa.Column('tipo', sa.Text, nullable=False),   # 'email' | 'ip'
        sa.Column('ocurrido_en', sa.TIMESTAMP, nullable=False,
                  server_default=sa.func.now()),
    )
    # La consulta caliente es "cuantos fallos de este identificador en los
    # ultimos N minutos", y ocurre en cada inicio de sesion.
    op.create_index('idx_intentos_busqueda', 'intentos_acceso',
                    ['identificador', 'tipo', 'ocurrido_en'])
    # Para la limpieza de lo viejo, que si no crece sin fin.
    op.create_index('idx_intentos_fecha', 'intentos_acceso', ['ocurrido_en'])


def downgrade() -> None:
    op.drop_index('idx_intentos_fecha', table_name='intentos_acceso')
    op.drop_index('idx_intentos_busqueda', table_name='intentos_acceso')
    op.drop_table('intentos_acceso')
