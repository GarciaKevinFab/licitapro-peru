"""Avisos por WhatsApp y registro de envios por usuario.

DOS PROBLEMAS DISTINTOS, UNA SOLA MIGRACION PORQUE NO SE SOSTIENEN POR SEPARADO

  1. No habia forma de guardar el numero de WhatsApp de un cliente.
  2. `licitaciones.notificado` es un booleano GLOBAL. En un producto de un solo
     usuario funcionaba. En multi-inquilino significa que el primer cliente al
     que se avisa quema esa licitacion para todos los demas: nadie mas la
     recibe nunca. Anadir un canal sobre esa base solo multiplica el fallo.

POR QUE UNA TABLA DE ENVIOS Y NO UNA COLUMNA

  Lo que hay que recordar no es "esta licitacion ya se aviso", sino "a ESTE
  usuario ya se le aviso de ESTA licitacion por ESTE canal". Son tres datos, no
  uno. La restriccion UNIQUE es la idempotencia: si el proceso se repite porque
  el scraper corrio dos veces o el envio fallo a medias, el segundo INSERT
  choca y no se manda nada duplicado. No hace falta logica extra.

  Se guarda el canal ademas del usuario porque un mismo aviso puede ir por
  Telegram y por WhatsApp, y el fallo de uno no debe silenciar al otro.

POR QUE SE GUARDA LA FECHA DE CONSENTIMIENTO

  Meta exige consentimiento previo y verificable para escribir a alguien por
  WhatsApp; sin el, el numero de la empresa acaba bloqueado. La Ley 29733
  peruana pide lo mismo para tratar un dato personal. `whatsapp_opt_in_en` es
  la prueba de ambas cosas, y `whatsapp_opt_out_en` deja constancia de que la
  baja se respeto, que es lo que se revisa cuando alguien reclama.

  El numero se guarda en claro, igual que el correo: hay que poder enviarle. No
  se cifra porque cifrarlo obligaria a descifrarlo en cada envio sin ganar nada
  frente a un volcado de la base, donde el correo ya identifica a la persona.

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─── WhatsApp en la cuenta ───────────────────────────
    # E.164 con prefijo pais: +51987654321. Se normaliza al guardar, no aqui,
    # porque un CHECK en la base rechazaria el formulario con un 500 en vez de
    # explicarle al usuario que le falta el +51.
    op.add_column('usuarios', sa.Column('whatsapp_numero', sa.Text))

    # Ciclo de vida del consentimiento, no del numero:
    #   sin_configurar -> pendiente -> activo -> baja
    # 'pendiente' existe porque tener el numero no autoriza a escribir: hace
    # falta que la persona confirme. Sin ese estado intermedio, guardar el
    # numero y enviar serian la misma accion.
    op.add_column('usuarios', sa.Column(
        'whatsapp_estado', sa.Text, nullable=False, server_default='sin_configurar'))
    op.add_column('usuarios', sa.Column('whatsapp_opt_in_en', sa.TIMESTAMP))
    op.add_column('usuarios', sa.Column('whatsapp_opt_out_en', sa.TIMESTAMP))

    # ─── Registro de envios ──────────────────────────────
    op.create_table(
        'notificaciones_enviadas',
        sa.Column('id', sa.BigInteger, primary_key=True),
        sa.Column('usuario_id', sa.Integer,
                  sa.ForeignKey('usuarios.id', ondelete='CASCADE'), nullable=False),
        sa.Column('licitacion_id', sa.Text,
                  sa.ForeignKey('licitaciones.id', ondelete='CASCADE'), nullable=False),
        sa.Column('canal', sa.Text, nullable=False),
        sa.Column('enviado_en', sa.TIMESTAMP, server_default=sa.func.now()),
    )
    # Esta restriccion ES la idempotencia. Sin ella, un reintento manda el
    # mismo aviso otra vez, y en WhatsApp cada envio se cobra.
    op.create_unique_constraint(
        'uq_notif_usuario_licitacion_canal',
        'notificaciones_enviadas', ['usuario_id', 'licitacion_id', 'canal'])
    # Consulta caliente: "de estas licitaciones, cuales no le he mandado aun".
    op.create_index('idx_notif_usuario', 'notificaciones_enviadas',
                    ['usuario_id', 'enviado_en'])


def downgrade() -> None:
    op.drop_index('idx_notif_usuario', table_name='notificaciones_enviadas')
    op.drop_constraint('uq_notif_usuario_licitacion_canal',
                       'notificaciones_enviadas', type_='unique')
    op.drop_table('notificaciones_enviadas')
    op.drop_column('usuarios', 'whatsapp_opt_out_en')
    op.drop_column('usuarios', 'whatsapp_opt_in_en')
    op.drop_column('usuarios', 'whatsapp_estado')
    op.drop_column('usuarios', 'whatsapp_numero')
