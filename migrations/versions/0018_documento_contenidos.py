"""Cada PDF de la entidad se guarda una sola vez, por su huella.

POR QUE

  La primera carga de licitacion_documentos (2026-10-12) dejo 54 declaraciones
  juradas que pesaban 31 MB y eran solo 4 archivos distintos: el GOREMAD
  adjunta la MISMA DJ a cada solicitud. Guardarla entera en cada fila llenaba
  la base a razon de ~0.6 MB por solicitud publicada.

  Ahora el contenido vive en documento_contenidos, indexado por su sha256, y
  licitacion_documentos solo guarda la huella. La columna `contenido` de esa
  tabla queda sin uso (se vacia aqui).

SE PUEDE APLICAR CON LA APLICACION CORRIENDO

  Tabla nueva y un traspaso de filas; el codigo anterior que lea `contenido`
  vera NULL y caera al formato propio, que es lo que ya hacia sin formato.

Revision ID: 0018
Revises: 0017
"""
from alembic import op

revision = '0018'
down_revision = '0017'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS documento_contenidos (
            sha256 TEXT PRIMARY KEY,
            contenido BYTEA NOT NULL,
            tamano INT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)
    op.execute("""
        INSERT INTO documento_contenidos (sha256, contenido, tamano)
        SELECT DISTINCT ON (sha256) sha256, contenido, octet_length(contenido)
          FROM licitacion_documentos
         WHERE contenido IS NOT NULL AND sha256 IS NOT NULL
        ON CONFLICT (sha256) DO NOTHING
    """)
    op.execute("UPDATE licitacion_documentos SET contenido = NULL WHERE contenido IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS ix_licdoc_sha ON licitacion_documentos (sha256)")


def downgrade() -> None:
    op.execute("""
        UPDATE licitacion_documentos d SET contenido = c.contenido
          FROM documento_contenidos c WHERE c.sha256 = d.sha256
    """)
    op.execute("DROP TABLE IF EXISTS documento_contenidos")
