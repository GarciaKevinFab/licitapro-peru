"""Cotizacion por items: marca y precio unitario de cada bien pedido.

POR QUE

  Una solicitud de cotizacion no se responde con un monto: se responde con la
  misma tabla que manda la entidad -- cantidad, unidad, descripcion, MARCA,
  PRECIO UNITARIO y PRECIO TOTAL por cada item. Hasta ahora la propuesta solo
  guardaba `precio_ofertado`, un numero suelto, y no habia donde poner que el
  arroz es Cholo a S/ 4.00 el kilo. La prueba con la SC 5884-2026-GOREMAD
  (2026-10-10) lo dejo a la vista: nueve productos con su precio y ningun
  sitio para ellos.

  `precio_ofertado` se queda y pasa a ser la SUMA de los items cuando los
  hay. Asi todo lo que ya lo lee (la ficha, el ZIP, el validador) sigue
  viendo el total sin enterarse de que ahora se calcula.

  `cotizacion_condiciones` guarda lo que la SC deja en blanco al pie: plazo
  de entrega, validez, garantia, forma de pago. Es un JSONB porque cada
  entidad pide un juego distinto y ninguno se consulta por separado.

SE PUEDE APLICAR CON LA APLICACION CORRIENDO

  Una tabla nueva y una columna nulable: el codigo anterior no menciona
  ninguna de las dos.

Revision ID: 0016
Revises: 0015
"""
from alembic import op

revision = '0016'
down_revision = '0015'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ON DELETE CASCADE: borrar la cuenta borra empresas -> propuestas (0006),
    # y los items tienen que caer con ellas o el borrado se detiene aqui.
    op.execute("""
        CREATE TABLE IF NOT EXISTS propuesta_items (
            id SERIAL PRIMARY KEY,
            propuesta_id INT NOT NULL REFERENCES propuestas(id) ON DELETE CASCADE,
            orden INT NOT NULL,
            descripcion TEXT NOT NULL,
            cantidad NUMERIC(14,3) NOT NULL,
            unidad TEXT,
            marca TEXT,
            precio_unitario NUMERIC(14,4),
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW(),
            UNIQUE (propuesta_id, orden)
        )
    """)
    op.execute("ALTER TABLE propuestas ADD COLUMN IF NOT EXISTS cotizacion_condiciones JSONB")


def downgrade() -> None:
    op.execute("ALTER TABLE propuestas DROP COLUMN IF EXISTS cotizacion_condiciones")
    op.execute("DROP TABLE IF EXISTS propuesta_items")
