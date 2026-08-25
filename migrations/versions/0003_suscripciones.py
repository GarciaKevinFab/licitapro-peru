"""Planes, suscripciones y pagos de suscripcion.

Notas de diseno:

  - Una suscripcion por usuario (UNIQUE en usuario_id). Cambiar de plan es un
    UPDATE, no una fila nueva; el historial de cobros vive en pagos_suscripcion.

  - El estado es una maquina explicita y no un booleano "activo": una cuenta en
    prueba, una vencida que aun esta en gracia y una suspendida se tratan
    distinto, y con un booleano no se distinguen.

  - `token_tarjeta` es BYTEA porque va cifrado con la misma clave maestra que
    las credenciales. Aqui NUNCA se guarda un numero de tarjeta: solo el token
    que devuelve Izipay, que sin su cuenta de comercio no sirve para nada.

  - `izipay_order_number` es UNIQUE: es la llave de idempotencia. El webhook de
    Izipay puede llegar dos veces, y sin esto se contaria el cobro doble.

  - Los montos son NUMERIC(12,2), no REAL. El resto del proyecto usa REAL para
    montos referenciales de licitaciones, donde el redondeo no importa; aqui es
    dinero que se cobra de verdad y el binario flotante no sirve.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'planes',
        sa.Column('codigo', sa.Text, primary_key=True),
        sa.Column('nombre', sa.Text, nullable=False),
        sa.Column('precio_mensual', sa.Numeric(12, 2), nullable=False),
        sa.Column('precio_anual', sa.Numeric(12, 2)),
        sa.Column('max_empresas', sa.Integer, nullable=False, server_default='1'),
        sa.Column('max_regiones', sa.Integer),          # NULL = sin limite
        sa.Column('analisis_ia', sa.Boolean, server_default=sa.false()),
        sa.Column('orden', sa.Integer, server_default='0'),
        sa.Column('activo', sa.Boolean, server_default=sa.true()),
    )

    op.create_table(
        'suscripciones',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('usuario_id', sa.Integer,
                  sa.ForeignKey('usuarios.id', ondelete='CASCADE'),
                  nullable=False, unique=True),
        sa.Column('plan_codigo', sa.Text, sa.ForeignKey('planes.codigo'), nullable=False),
        sa.Column('estado', sa.Text, nullable=False, server_default='prueba'),
        sa.Column('periodo', sa.Text, nullable=False, server_default='mensual'),
        sa.Column('inicia', sa.TIMESTAMP, server_default=sa.func.now()),
        sa.Column('vence', sa.TIMESTAMP),
        sa.Column('cancelada_en', sa.TIMESTAMP),
        # Token de tarjeta devuelto por Izipay, cifrado. Nunca el numero.
        sa.Column('token_tarjeta', sa.LargeBinary),
        sa.Column('tarjeta_marca', sa.Text),
        sa.Column('tarjeta_ultimos', sa.Text),
        sa.Column('intentos_fallidos', sa.Integer, server_default='0'),
        sa.Column('ultimo_intento', sa.TIMESTAMP),
        sa.Column('created_at', sa.TIMESTAMP, server_default=sa.func.now()),
    )
    op.create_index('idx_susc_estado', 'suscripciones', ['estado', 'vence'])

    op.create_table(
        'pagos_suscripcion',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('suscripcion_id', sa.Integer,
                  sa.ForeignKey('suscripciones.id', ondelete='CASCADE'), nullable=False),
        sa.Column('monto', sa.Numeric(12, 2), nullable=False),
        sa.Column('moneda', sa.Text, server_default='PEN'),
        sa.Column('estado', sa.Text, nullable=False, server_default='pendiente'),
        sa.Column('metodo', sa.Text, server_default='izipay'),
        # Llave de idempotencia: el webhook puede llegar repetido.
        sa.Column('izipay_order_number', sa.Text, unique=True),
        sa.Column('izipay_transaction_id', sa.Text),
        sa.Column('respuesta', postgresql.JSONB),
        sa.Column('created_at', sa.TIMESTAMP, server_default=sa.func.now()),
        sa.Column('confirmado_en', sa.TIMESTAMP),
    )
    op.create_index('idx_pagosusc_susc', 'pagos_suscripcion', ['suscripcion_id'])

    # Planes iniciales. Los precios son los del informe; el margen ya considera
    # que Izipay se lleva 4.06% + S/0.81 por cobro.
    op.execute("""
        INSERT INTO planes (codigo, nombre, precio_mensual, precio_anual,
                            max_empresas, max_regiones, analisis_ia, orden)
        VALUES
          ('basico',  'Básico',  49.00,   490.00, 1,  3,    FALSE, 1),
          ('pro',     'Pro',     99.00,   990.00, 3,  NULL, FALSE, 2),
          ('empresa', 'Empresa', 199.00, 1990.00, 99, NULL, TRUE,  3)
        ON CONFLICT (codigo) DO NOTHING
    """)

    # Toda cuenta existente arranca con 14 dias de prueba del plan Pro, para que
    # nadie se quede sin servicio al desplegar esto.
    op.execute("""
        INSERT INTO suscripciones (usuario_id, plan_codigo, estado, periodo, vence)
        SELECT id, 'pro', 'prueba', 'mensual', NOW() + INTERVAL '14 days'
          FROM usuarios
         WHERE NOT EXISTS (SELECT 1 FROM suscripciones s WHERE s.usuario_id = usuarios.id)
    """)


def downgrade() -> None:
    op.drop_index('idx_pagosusc_susc', table_name='pagos_suscripcion')
    op.drop_table('pagos_suscripcion')
    op.drop_index('idx_susc_estado', table_name='suscripciones')
    op.drop_table('suscripciones')
    op.drop_table('planes')
