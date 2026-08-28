"""Vencimientos que inhabilitan, y licitaciones que se siguen sin postular.

DE DONDE SALEN ESTAS DOS IDEAS

  Del prototipo ProveedorPE, que nunca tuvo scrapers ni datos reales pero si
  habia trabajado bien dos huecos de producto que aqui faltaban. El codigo no
  se pudo aprovechar -- MongoDB y React Native contra PostgreSQL y Jinja2 --
  pero las funciones si.

VENCIMIENTOS: POR QUE IMPORTA MAS DE LO QUE PARECE

  `empresas.rnp_vigencia` y `rnp_categoria` YA EXISTIAN en el esquema y no las
  leia nadie. Ni siquiera se podian escribir: el formulario de empresa tiene
  `rnp_numero` y no la fecha.

  Mientras tanto, el prompt del analisis de viabilidad dice literalmente que la
  inscripcion vigente en el RNP es condicion para contratar. O sea: el producto
  sabe que el RNP caduca, guarda la fecha, y nunca la mira.

  Lo que eso le hace a un cliente: prepara un expediente completo -- responde
  preguntas, carga experiencia, genera ocho documentos -- para un procedimiento
  al que no puede presentarse. El trabajo se descubre inutil en mesa de partes.

POR QUE EL RNP SIGUE EN `empresas` Y NO SE MUEVE AQUI

  Es un atributo de la empresa, no una anotacion suya: lo exige la ley, tiene
  categoria y capitulo, y el analisis lo consulta como dato de la empresa.
  Moverlo a una tabla de anotaciones libres lo degradaria a "una fecha mas que
  el usuario apunto".

  Esta tabla es para lo demas: polizas, cartas fianza, vigencias de poder,
  certificados. Cosas que cada proveedor sigue de forma distinta y que no tiene
  sentido modelar una por una. La vista las une.

SEGUIDAS: EL PASO QUE FALTABA

  Hoy la unica accion sobre una licitacion es postular, y postular abre un
  expediente. Es demasiado compromiso para algo que todavia estas evaluando:
  entre "me avisaron" y "me presento" hay dias de mirar bases y consultar.

  Sin ese paso intermedio, quien duda no tiene donde apuntarlo y acaba llevando
  la lista en otra parte -- que es justo donde empieza a no necesitar el
  producto.

POR QUE SEGUIR CUELGA DEL USUARIO Y NO DE LA EMPRESA

  Se sigue antes de decidir con que empresa presentarse, y a veces con ninguna.
  Colgarlo de la empresa obligaria a elegir una para poder marcar interes, que
  es exactamente la decision que este paso permite aplazar.

Revision ID: 0012
Revises: 0011
"""
from alembic import op
import sqlalchemy as sa

revision = '0012'
down_revision = '0011'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'vencimientos',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('empresa_id', sa.Integer,
                  sa.ForeignKey('empresas.id', ondelete='CASCADE'),
                  nullable=False),
        # Texto libre y no un enum: los documentos que un proveedor vigila
        # cambian con el rubro y con la entidad, y un enum obligaria a una
        # migracion cada vez que aparezca uno nuevo.
        sa.Column('tipo', sa.Text, nullable=False),
        sa.Column('descripcion', sa.Text),
        sa.Column('fecha_vencimiento', sa.Date, nullable=False),
        sa.Column('notas', sa.Text),
        sa.Column('creado_en', sa.TIMESTAMP, nullable=False,
                  server_default=sa.func.now()),
    )
    # La consulta caliente es "que vence pronto", ordenado por fecha.
    op.create_index('idx_vencimientos_fecha', 'vencimientos',
                    ['empresa_id', 'fecha_vencimiento'])

    op.create_table(
        'licitaciones_seguidas',
        sa.Column('usuario_id', sa.Integer,
                  sa.ForeignKey('usuarios.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('licitacion_id', sa.Text,
                  sa.ForeignKey('licitaciones.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('creado_en', sa.TIMESTAMP, nullable=False,
                  server_default=sa.func.now()),
    )
    # Clave primaria compuesta: seguir dos veces la misma licitacion no es un
    # estado distinto de seguirla una, asi que lo impide la base en vez de
    # dejar que cada ruta se acuerde de comprobarlo.
    op.create_primary_key('pk_licitaciones_seguidas', 'licitaciones_seguidas',
                          ['usuario_id', 'licitacion_id'])


def downgrade() -> None:
    op.drop_table('licitaciones_seguidas')
    op.drop_index('idx_vencimientos_fecha', table_name='vencimientos')
    op.drop_table('vencimientos')
