"""Datos de adjudicacion y banderas de direccionamiento.

QUE SE GUARDA Y POR QUE NO SE GUARDABA ANTES

  La API OCDS publica, en los releases ya adjudicados, la lista completa de
  postores y el proveedor ganador. El scraper los estaba tirando porque solo
  buscaba convocatorias abiertas. Son justo los datos con los que se detecta un
  proceso dirigido, asi que ahora se conservan.

LAS TRES BANDERAS SON LAS QUE TIENEN DATOS DETRAS, NO LAS QUE SUENAN BIEN

  Se midieron 500 procesos reales de la API antes de elegirlas. Dos candidatas
  obvias se descartaron por medicion, no por opinion:

  - "Adjudicado al 100% del valor referencial": la mediana del ratio
    adjudicado/referencial es exactamente 1.000 y 34 de 60 procesos estan en el
    100%. Adjudicar al referencial es la NORMA en estos datos, no una anomalia.
    Usarlo como bandera marcaria a mas de la mitad de las entidades del pais sin
    fundamento alguno.

  - "Especificaciones con marca": solo 2 menciones de marca o modelo en 556
    items. Las descripciones que publica la API vienen del catalogo CUBSO, no
    de las especificaciones tecnicas; esas viven dentro del PDF de las bases.
    Detectarlo exige leer el PDF, no este feed.

  Quedan tres que si se sostienen:

  1. postor_unico -- un solo postor en un proceso ya adjudicado. Medido: 9 de
     60 adjudicados. Es la senal clasica y aqui hay datos para calcularla.
  2. pocos_postores -- dos o tres. Mas debil, informativa.
  3. plazo_consultas_corto -- el plazo para preguntar y observar, comparado con
     el de su PROPIO tipo de procedimiento. Esto importa: las "Abreviadas" van
     de 2 a 9 dias (mediana 4) y las "Licitacion Publica" nunca bajan de 8
     (mediana 10). Un plazo de 2 dias es normal en la primera y anomalo en la
     segunda. Una bandera absoluta marcaria el 34% de todo y no diria nada.

POR QUE 'nivel' Y NO 'score'

  Un numero invita a ordenar y a comparar procesos entre si, y estas senales no
  se suman: dos banderas debiles no equivalen a una fuerte. `nivel` es 0, 1, 2 o
  3 y solo sirve para pintar el aviso y para filtrar.

  Y por eso el campo se llama `banderas` y no `corrupcion`. Son indicios que
  conviene revisar antes de gastar dinero preparando una propuesta, no una
  acusacion: un proceso con un solo postor puede tener un solo postor porque
  nadie mas fabrica eso. Quien decide es el proveedor, con la informacion
  delante.

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0008'
down_revision = '0007'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─── Datos de la adjudicacion ────────────────────────
    op.add_column('licitaciones', sa.Column('numero_postores', sa.Integer))
    op.add_column('licitaciones', sa.Column('proveedor_ganador', sa.Text))
    op.add_column('licitaciones', sa.Column('proveedor_ruc', sa.Text))
    op.add_column('licitaciones', sa.Column('monto_adjudicado', sa.Numeric(14, 2)))
    # Dias que dio la entidad para consultas y observaciones. Se guarda tal cual
    # lo publica la API en vez de recalcularlo entre fechas: la propia fuente ya
    # lo cuenta segun su norma, y rehacer esa cuenta a mano introduciria un
    # error nuestro donde no hay ninguno.
    op.add_column('licitaciones', sa.Column('plazo_consultas_dias', sa.Integer))

    # ─── Banderas calculadas ─────────────────────────────
    # Se guardan calculadas y no se computan al leer: el panel las filtra y
    # ordena, y recalcularlas en cada consulta obligaria a traer los datos de
    # todos los procesos para decidir cuales mostrar.
    op.add_column('licitaciones', sa.Column(
        'banderas', postgresql.JSONB, server_default=sa.text("'[]'::jsonb")))
    op.add_column('licitaciones', sa.Column(
        'banderas_nivel', sa.Integer, nullable=False, server_default='0'))

    # Filtrar "muestrame solo lo que tiene indicios" es la consulta que hace
    # util la funcion; sin indice recorreria la tabla entera cada vez.
    op.create_index('idx_lic_banderas_nivel', 'licitaciones', ['banderas_nivel'])


def downgrade() -> None:
    op.drop_index('idx_lic_banderas_nivel', table_name='licitaciones')
    for c in ('banderas_nivel', 'banderas', 'plazo_consultas_dias',
              'monto_adjudicado', 'proveedor_ruc', 'proveedor_ganador',
              'numero_postores'):
        op.drop_column('licitaciones', c)
