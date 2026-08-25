"""Multi-inquilino: tabla usuarios, propiedad de empresas y config, credenciales.

Hasta ahora el sistema era de un solo usuario: las empresas estaban sembradas a
mano en schema.sql sin dueno, y el destinatario de las alertas era una variable
de entorno (TELEGRAM_ADMIN_ID). Esto introduce la identidad que faltaba.

Notas de diseno:

  - `licitaciones` NO se scopea: son datos publicos del Estado y el pozo es
    compartido entre todos los inquilinos. Lo privado es a quien le interesa
    cada una, que sale de user_config.
  - `propuestas` y `contratos` tampoco llevan usuario_id: cuelgan de empresa_id,
    y empresas ya tiene dueno. Anadir la columna seria denormalizar y abrir la
    puerta a que ambas discrepen.
  - `telegram_chat_id` lo entrega Telegram al vincular por enlace profundo, no
    lo escribe el usuario: asi no puede redirigir sus alertas al chat de otro.
  - Las credenciales van cifradas en BYTEA con la clave fuera de la BD.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'usuarios',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('email', sa.Text, nullable=False, unique=True),
        sa.Column('password_hash', sa.Text),
        sa.Column('nombre', sa.Text),
        sa.Column('telegram_chat_id', sa.BigInteger, unique=True),
        sa.Column('telegram_token', sa.Text),
        sa.Column('telegram_token_expira', sa.TIMESTAMP),
        sa.Column('plan', sa.Text, server_default='trial'),
        sa.Column('activo', sa.Boolean, server_default=sa.true()),
        sa.Column('created_at', sa.TIMESTAMP, server_default=sa.func.now()),
    )
    op.create_index('idx_usuarios_tg', 'usuarios', ['telegram_chat_id'])

    op.create_table(
        'credenciales',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('usuario_id', sa.Integer,
                  sa.ForeignKey('usuarios.id', ondelete='CASCADE'), nullable=False),
        sa.Column('tipo', sa.Text, nullable=False),
        sa.Column('valor_cifrado', sa.LargeBinary, nullable=False),
        sa.Column('actualizado_en', sa.TIMESTAMP, server_default=sa.func.now()),
        sa.UniqueConstraint('usuario_id', 'tipo', name='uq_credencial_por_tipo'),
    )

    op.add_column('empresas', sa.Column('usuario_id', sa.Integer))
    op.create_foreign_key('fk_empresas_usuario', 'empresas', 'usuarios',
                          ['usuario_id'], ['id'], ondelete='CASCADE')
    op.create_index('idx_empresas_usuario', 'empresas', ['usuario_id'])

    op.add_column('user_config', sa.Column('usuario_id', sa.Integer))
    op.create_foreign_key('fk_user_config_usuario', 'user_config', 'usuarios',
                          ['usuario_id'], ['id'], ondelete='CASCADE')
    op.create_unique_constraint('uq_user_config_usuario', 'user_config', ['usuario_id'])

    # Backfill. En el diseno viejo la configuracion real vivia en la fila
    # user_id=0 (la semilla) y la fila del Telegram real quedaba vacia, porque
    # get_config caia de vuelta a la 0. Al crear la identidad hay que juntar las
    # dos: la config de la fila 0 y el chat_id de la otra.
    op.execute("""
        INSERT INTO usuarios (email, nombre, plan, telegram_chat_id)
        SELECT COALESCE(
                   (SELECT NULLIF(email_notificaciones, '') FROM user_config WHERE user_id = 0),
                   'admin@licitapro.local'),
               'Cuenta inicial', 'owner',
               (SELECT user_id FROM user_config
                 WHERE user_id <> 0 ORDER BY created_at LIMIT 1)
        WHERE NOT EXISTS (SELECT 1 FROM usuarios)
    """)
    op.execute("UPDATE empresas SET usuario_id = (SELECT MIN(id) FROM usuarios) WHERE usuario_id IS NULL")

    # La fila que lleva la configuracion real pasa a ser la del usuario.
    op.execute("""
        UPDATE user_config SET usuario_id = (SELECT MIN(id) FROM usuarios)
        WHERE user_id = 0
    """)
    # Las filas de Telegram sin configuracion propia son redundantes: su unico
    # dato, el chat_id, ya vive en usuarios. Solo se borran si estan vacias de
    # verdad; una con configuracion sobrevive y queda visible con usuario_id NULL.
    op.execute("""
        DELETE FROM user_config
        WHERE usuario_id IS NULL
          AND COALESCE(array_length(regiones, 1), 0) = 0
          AND COALESCE(array_length(keywords, 1), 0) = 0
          AND COALESCE(array_length(keywords_excluir, 1), 0) = 0
    """)


def downgrade() -> None:
    op.drop_constraint('uq_user_config_usuario', 'user_config', type_='unique')
    op.drop_constraint('fk_user_config_usuario', 'user_config', type_='foreignkey')
    op.drop_column('user_config', 'usuario_id')
    op.drop_index('idx_empresas_usuario', table_name='empresas')
    op.drop_constraint('fk_empresas_usuario', 'empresas', type_='foreignkey')
    op.drop_column('empresas', 'usuario_id')
    op.drop_table('credenciales')
    op.drop_index('idx_usuarios_tg', table_name='usuarios')
    op.drop_table('usuarios')
