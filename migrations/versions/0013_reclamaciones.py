"""Libro de Reclamaciones (Ley 29571 y D.S. 101-2022-PCM).

POR QUE HACE FALTA, Y POR QUE AHORA

  Es obligatorio para cualquiera que venda a un consumidor en Peru, y las
  pasarelas lo comprueban al validar el comercio: es de las primeras cosas que
  se miran, junto al carrito y a los datos del proveedor.

  Hasta ahora LicitaPro no tenia. CargoXprez si -- misma empresa, misma
  obligacion --, asi que esta tabla es deliberadamente la misma que la suya:
  dos productos del mismo titular respondiendo reclamos con formatos distintos
  es como se pierde una hoja el dia que INDECOPI pide el libro.

LAS CUATRO COSAS QUE PIDE LA LEY, Y DONDE ESTAN

  1. Hoja con NUMERO CORRELATIVO      -> `numero`, identidad generada siempre
  2. COPIA al consumidor              -> `email`, y el envio lo hace la ruta
  3. RESPUESTA dentro de plazo        -> `limite_respuesta`, ya calculado
  4. CONSERVACION de lo reclamado     -> la fila entera, que no se borra

POR QUE `limite_respuesta` SE GUARDA Y NO SE CALCULA AL MIRAR

  El plazo corre desde la presentacion. Calcularlo al vuelo a partir de "hoy"
  daria una fecha distinta cada vez que alguien abriera la pantalla, y la
  primera consecuencia seria creerse dentro de plazo cuando ya se paso.

POR QUE NO HAY `usuario_id` OBLIGATORIO

  La ley da derecho a reclamar a cualquiera, sea cliente o no y haya iniciado
  sesion o no. Exigir cuenta para reclamar seria justo lo contrario de lo que
  la norma protege. Se guarda si hay sesion, y nada mas.
"""
from alembic import op

revision = '0013'
down_revision = '0012'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS reclamaciones (
            id               SERIAL PRIMARY KEY,
            -- El correlativo que exige la ley y que la persona necesita para
            -- acudir a INDECOPI. Se muestra como LR-000001.
            numero           BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE,

            -- reclamo = disconformidad con el servicio.
            -- queja    = malestar con la atencion.
            -- La ley las distingue, asi que hay que preguntarlo y no deducirlo.
            tipo             TEXT NOT NULL CHECK (tipo IN ('reclamo', 'queja')),

            nombre           TEXT NOT NULL,
            documento_tipo   TEXT NOT NULL,
            documento_numero TEXT NOT NULL,
            email            TEXT NOT NULL,
            telefono         TEXT,
            direccion        TEXT,
            es_menor_edad    BOOLEAN NOT NULL DEFAULT FALSE,
            apoderado        TEXT,

            bien_contratado  TEXT NOT NULL DEFAULT 'servicio',
            descripcion_bien TEXT,
            monto_reclamado  NUMERIC(10,2),

            detalle          TEXT NOT NULL,
            pedido           TEXT NOT NULL,

            estado           TEXT NOT NULL DEFAULT 'pendiente',
            respuesta        TEXT,
            respondido_en    TIMESTAMPTZ,
            limite_respuesta TIMESTAMPTZ NOT NULL,

            -- Si quien reclama tenia sesion abierta. Opcional a proposito.
            usuario_id       INTEGER REFERENCES usuarios(id) ON DELETE SET NULL,
            ip_solicitud     TEXT,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS rec_estado_idx ON reclamaciones (estado)")
    op.execute("CREATE INDEX IF NOT EXISTS rec_email_idx ON reclamaciones (email)")
    # Para el aviso de "se te pasa el plazo": lo que se consulta es lo
    # pendiente ordenado por vencimiento, no la tabla entera.
    op.execute("CREATE INDEX IF NOT EXISTS rec_limite_idx "
               "ON reclamaciones (limite_respuesta) WHERE estado = 'pendiente'")


def downgrade() -> None:
    # Se tira la tabla solo si esta VACIA. Una hoja del Libro de Reclamaciones
    # hay que conservarla: borrarla al revertir una migracion seria destruir la
    # prueba de un reclamo por un descuido de despliegue.
    op.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM reclamaciones) THEN
                DROP TABLE reclamaciones;
            ELSE
                RAISE NOTICE 'reclamaciones tiene filas: no se borra';
            END IF;
        END $$
    """)
