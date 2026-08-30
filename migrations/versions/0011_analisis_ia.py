"""El analisis con IA, que se cobraba y no existia.

LO QUE HABIA

  `planes.analisis_ia` estaba en TRUE para Pro (S/99) y Empresa (S/199), y
  `shared.suscripciones.puede_usar_ia` comprobaba ese permiso correctamente.
  Pero `radar_bot/analyzer.py` no tenia un solo importador en todo el proyecto,
  y `puede_usar_ia` tampoco: la funcion, su portero y su prueba existian, y
  entre las tres no se ejecutaba ninguna. Se cobraba por una casilla.

POR QUE UNA TABLA NUEVA Y NO `licitaciones.bases_analisis`

  El analisis se hace CONTRA UNA EMPRESA: el prompt lleva su experiencia, su
  equipo tecnico y sus rubros, y la respuesta habla de si a ESA empresa le
  conviene presentarse. Guardarlo en `licitaciones` -- una fila que comparten
  todos los inquilinos -- significa que el analisis hecho con la experiencia de
  una constructora se le muestra a la siguiente empresa que abra esa ficha.

  Es el mismo fallo que ya tuvo `licitaciones.notificado`: un dato de un
  inquilino guardado en la fila comun. Alli se noto tarde porque con un solo
  destinatario no se veia. Aqui no hace falta esperar a que se note.

POR QUE UN TOPE MENSUAL Y NO BARRA LIBRE

  Cada analisis es una llamada de pago a la API de Anthropic y la paga el dueno
  de la plataforma, no el cliente. Sin tope, una sola cuenta que abra fichas en
  bucle -- por curiosidad o por un script -- gasta mas de lo que paga al mes, y
  el primer aviso seria la factura.

  El tope es por mes natural y por usuario. NULL significa sin limite, para
  poder abrir la mano a una cuenta concreta sin tocar codigo.

POR QUE SE GUARDA EL COSTE

  `tokens_entrada` y `tokens_salida` vienen de `usage` en cada respuesta. Sin
  eso, la unica forma de saber cuanto cuesta el producto es mirar la factura a
  fin de mes, cuando ya no se puede hacer nada. Con eso se responde "cuanto me
  cuesta sostener el plan Pro" con una consulta.

Revision ID: 0011
Revises: 0010
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'analisis_ia',
        sa.Column('id', sa.BigInteger, primary_key=True),
        # Quien lo pidio. Es la clave del aislamiento: nadie ve el analisis de
        # otro, porque toda lectura filtra por esta columna.
        sa.Column('usuario_id', sa.Integer,
                  sa.ForeignKey('usuarios.id', ondelete='CASCADE'),
                  nullable=False),
        # Contra que empresa se analizo. Un usuario del plan Empresa tiene
        # varias, y el analisis de una no vale para otra: distinta experiencia,
        # distinto equipo, distinta conclusion.
        sa.Column('empresa_id', sa.Integer,
                  sa.ForeignKey('empresas.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('licitacion_id', sa.Text,
                  sa.ForeignKey('licitaciones.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('score', sa.REAL),
        sa.Column('recomendacion', sa.Text),   # 'licitar' | 'evaluar' | 'pasar'
        sa.Column('resultado', postgresql.JSONB, nullable=False),
        # De donde salio: 'ia' o 'heuristico'. Sin esto es imposible distinguir
        # un analisis real de uno de respaldo cuando la API fallo, y el cliente
        # del plan Pro veria el heuristico creyendo que pago por el otro.
        sa.Column('origen', sa.Text, nullable=False, server_default='ia'),
        sa.Column('modelo', sa.Text),
        sa.Column('tokens_entrada', sa.Integer),
        sa.Column('tokens_salida', sa.Integer),
        sa.Column('creado_en', sa.TIMESTAMP, nullable=False,
                  server_default=sa.func.now()),
    )
    # Un analisis vigente por (usuario, empresa, licitacion): al repetir se
    # sobrescribe en vez de acumular filas, y la ficha sabe cual mostrar sin
    # ordenar por fecha.
    op.create_unique_constraint(
        'uq_analisis_ia_destino', 'analisis_ia',
        ['usuario_id', 'empresa_id', 'licitacion_id'])
    # La consulta del tope: "cuantos lleva este usuario este mes".
    op.create_index('idx_analisis_ia_cuota', 'analisis_ia',
                    ['usuario_id', 'creado_en'])

    # Tope mensual por plan. NULL = sin limite.
    op.add_column('planes', sa.Column('analisis_ia_mes', sa.Integer))
    # Los planes sin IA quedan en 0 y no en NULL: NULL significa "sin limite",
    # y dejarlo asi en el plan gratuito seria regalar justo lo que se cobra.
    op.execute("UPDATE planes SET analisis_ia_mes = 0 WHERE analisis_ia IS NOT TRUE")
    op.execute("UPDATE planes SET analisis_ia_mes = 60  WHERE codigo = 'pro'")
    op.execute("UPDATE planes SET analisis_ia_mes = 300 WHERE codigo = 'empresa'")


def downgrade() -> None:
    op.drop_column('planes', 'analisis_ia_mes')
    op.drop_index('idx_analisis_ia_cuota', table_name='analisis_ia')
    op.drop_constraint('uq_analisis_ia_destino', 'analisis_ia', type_='unique')
    op.drop_table('analisis_ia')
