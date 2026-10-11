"""Los documentos que publica la entidad con cada solicitud: SC, DJ, EETT.

POR QUE

  Para llenar el formato de la entidad hace falta el formato. El portal de
  cotizaciones del GOREMAD publica con cada solicitud su PDF ("SC N° 5884-
  2026-GOREMAD.pdf"), la declaracion jurada que exige y las especificaciones
  tecnicas. El VPS no alcanza ese portal (solo responde desde Peru), asi que
  los baja la PC puente al cosechar y los deja aqui, y la web los toma sin
  que el usuario suba nada.

  `contenido` es NULL cuando solo se guarda el enlace: las EETT suelen ser
  escaneos de varios MB y el usuario las abre en el portal. La SC y la DJ, que
  son las que se llenan, si se guardan enteras.

CUANTO OCUPA

  Una SC pesa ~75 KB y la DJ ~400 KB. El puente vacia `contenido` de las
  solicitudes cerradas hace mas de 30 dias que nadie postulo: lo que vive en
  la tabla son las abiertas.

SE PUEDE APLICAR CON LA APLICACION CORRIENDO

  Tabla nueva; el codigo anterior no la menciona.

Revision ID: 0017
Revises: 0016
"""
from alembic import op

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS licitacion_documentos (
            id SERIAL PRIMARY KEY,
            licitacion_id TEXT NOT NULL REFERENCES licitaciones(id) ON DELETE CASCADE,
            tipo TEXT NOT NULL,          -- sc | dj | eett | otro
            categoria TEXT,              -- como lo rotula la entidad
            nombre TEXT,
            url TEXT NOT NULL,
            tamano INT,
            sha256 TEXT,
            contenido BYTEA,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (licitacion_id, url)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_licdoc_lic ON licitacion_documentos (licitacion_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS licitacion_documentos")
