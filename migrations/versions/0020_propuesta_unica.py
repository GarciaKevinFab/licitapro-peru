"""Una sola propuesta por licitacion y empresa.

POR QUE

  `web/propuestas.py` mira si ya hay propuesta para (licitacion, empresa) y,
  si no la hay, la inserta. Son dos sentencias sueltas: un doble clic en
  "Postular" manda dos peticiones que pasan las dos por el SELECT antes de que
  ninguna llegue al INSERT, y la empresa queda con dos propuestas para la
  misma licitacion. Cada una con sus preguntas, sus items y su precio; el
  usuario edita una y descarga el ZIP de la otra.

  La unica forma de que no pase nunca es que la base lo impida. Con el indice,
  la segunda peticion choca con un UniqueViolation en vez de crear la copia.

POR QUE FALLA EN VEZ DE LIMPIAR SI YA HAY DUPLICADOS

  En produccion no los hay (comprobado el 2026-10-20 antes de escribir esto).
  Si alguna base los tuviera, decidir cual de las dos sobra es decidir que
  trabajo del cliente se tira: no lo hace una migracion a ciegas. Se aborta
  con la lista de pares repetidos para que alguien lo mire y lo resuelva.

  Las filas con licitacion_id o empresa_id NULL no cuentan: en un indice unico
  NULL no es igual a NULL, y asi debe ser (no son propuestas de nadie).

SE PUEDE APLICAR CON LA APLICACION CORRIENDO

  La tabla es pequena; el CREATE INDEX bloquea las escrituras en propuestas
  unos milisegundos. IF NOT EXISTS la hace idempotente frente a una base
  creada desde shared/schema.sql, que ya trae el indice.

Revision ID: 0020
Revises: 0019
"""
from alembic import op
from sqlalchemy import text

revision = '0020'
down_revision = '0019'
branch_labels = None
depends_on = None

INDICE = "uq_propuestas_licitacion_empresa"


def upgrade() -> None:
    repetidos = op.get_bind().execute(text(
        """SELECT licitacion_id, empresa_id, COUNT(*) AS n, array_agg(id ORDER BY id) AS ids
             FROM propuestas
            WHERE licitacion_id IS NOT NULL AND empresa_id IS NOT NULL
            GROUP BY licitacion_id, empresa_id
           HAVING COUNT(*) > 1
            ORDER BY licitacion_id, empresa_id
            LIMIT 20""")).fetchall()
    if repetidos:
        detalle = "; ".join(
            f"licitacion {r.licitacion_id} / empresa {r.empresa_id}: propuestas {list(r.ids)}"
            for r in repetidos)
        raise RuntimeError(
            "No se puede crear el indice unico propuestas(licitacion_id, empresa_id): "
            f"hay propuestas duplicadas ({detalle}). Decide a mano cual se queda y "
            "borra o reasigna las demas; esta migracion no borra datos.")
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {INDICE} "
        "ON propuestas (licitacion_id, empresa_id)")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDICE}")
