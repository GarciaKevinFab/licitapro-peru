"""Correo verificado: la cuenta demuestra que el correo es suyo.

POR QUE

  El alta solo comprobaba que el correo tuviera una "@". Cualquiera podia
  registrarse con un correo ajeno, y eso importa por dos motivos:

  1. El rol de administrador se decide por correo (LICITAPRO_ADMIN_EMAIL, ver
     web/admin.py). Si esa cuenta no existia todavia -- una base nueva, un
     correo de dueno cambiado en el .env --, el primero que la registrara era
     administrador: la lista de clientes, sus datos y "entrar como".
  2. Los avisos y los recibos salen hacia ese correo. Una cuenta con un correo
     que no es de quien la usa le manda nuestros correos a un tercero.

  Desde aqui una cuenta nace SIN verificar y se verifica abriendo el enlace que
  se le envia. Mientras tanto entra y usa el panel con normalidad -- no se le
  bloquea nada que ya tuviera --, pero no cuenta como administradora.

QUE CREA

  usuarios.email_verificado_en   hora de Lima en que se verifico, o NULL.
  tokens_verificacion            enlaces de un solo uso, solo su hash, igual
                                 que tokens_recuperacion (0004). Tabla aparte
                                 y no un "tipo" en la de recuperacion: los
                                 limites de envio, la vigencia y el consumo son
                                 otros, y mezclarlos haria que pedir un enlace
                                 de verificacion gastara cupo de recuperacion.
                                 Guarda el `email` al que se mando: si la cuenta
                                 cambia de correo, un enlace viejo no verifica
                                 el nuevo.

LAS CUENTAS QUE YA EXISTEN SE DAN POR VERIFICADAS

  Llevan meses recibiendo avisos en ese correo y, sobre todo, la del dueno es
  una de ellas: dejarla sin verificar le cerraria /admin en el despliegue. Se
  marcan con la hora de la migracion.

  OJO, IDEMPOTENTE DE VERDAD: el relleno solo se hace si la columna NO existia.
  Un "UPDATE ... WHERE email_verificado_en IS NULL" a secas, repetido despues
  de abrir el alta, verificaria por la puerta de atras a todas las cuentas
  nuevas que aun no lo hicieron.

LAS COLUMNAS SON TIMESTAMP SIN ZONA EN HORA DE LIMA

  Como `tokens_recuperacion.expira`: se comparan contra
  (NOW() AT TIME ZONE 'America/Lima'), nunca contra NOW() a secas (la sesion del
  VPS corre en UTC; ver shared/seguridad.py).

Revision ID: 0022
Revises: 0021
"""
from alembic import op
from sqlalchemy import text

revision = '0022'
down_revision = '0021'
branch_labels = None
depends_on = None

_AHORA_LIMA = "(NOW() AT TIME ZONE 'America/Lima')"


def _hay_columna(tabla: str, columna: str) -> bool:
    return bool(op.get_bind().execute(text(
        """SELECT 1 FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = :t AND column_name = :c"""),
        {"t": tabla, "c": columna}).scalar())


def upgrade() -> None:
    if not _hay_columna("usuarios", "email_verificado_en"):
        op.execute("ALTER TABLE usuarios ADD COLUMN email_verificado_en TIMESTAMP NULL")
        # Solo en la primera pasada: ver "IDEMPOTENTE DE VERDAD" arriba.
        op.execute(f"UPDATE usuarios SET email_verificado_en = {_AHORA_LIMA}")

    op.execute(
        f"""CREATE TABLE IF NOT EXISTS tokens_verificacion (
                id          SERIAL PRIMARY KEY,
                usuario_id  INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
                token_hash  TEXT NOT NULL UNIQUE,
                email       TEXT NOT NULL,
                expira      TIMESTAMP NOT NULL,
                usado_en    TIMESTAMP,
                created_at  TIMESTAMP NOT NULL DEFAULT {_AHORA_LIMA}
            )""")
    op.execute("""CREATE INDEX IF NOT EXISTS idx_tokens_verificacion_usuario
                      ON tokens_verificacion(usuario_id, created_at)""")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS tokens_verificacion")
    op.execute("ALTER TABLE usuarios DROP COLUMN IF EXISTS email_verificado_en")
