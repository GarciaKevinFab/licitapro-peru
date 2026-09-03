"""Identificadores de Culqi en planes, suscripciones y pagos.

PARA QUE

  Culqi cobra sola cada periodo, pero para eso hay que saber, de cada fila
  nuestra, a que objeto suyo corresponde:

    planes         -> el plan de Culqi (uno por periodo: mensual y anual son
                      DOS planes distintos alli, porque el importe y la
                      frecuencia son distintos)
    suscripciones  -> el cliente, la tarjeta y la suscripcion recurrente
    pagos          -> el cargo y el evento que lo anuncio

POR QUE NO SE BORRA NADA DE IZIPAY

  La afiliacion de Izipay sigue viva y en `pagos_suscripcion` hay cobros
  registrados con su numero de orden, ademas de los pagos manuales (efectivo,
  Yape, transferencia) que el panel del dueno registra con metodo 'manual'.
  Tirar esas columnas seria destruir el historial de lo cobrado para ahorrar
  dos campos nulos.

EL INDICE UNICO PARCIAL ES LA IDEMPOTENCIA DEL WEBHOOK

  `culqi_charge_id` es unico SOLO cuando no es nulo. Las dos mitades importan:

    - Unico: un aviso repetido -- y Culqi reintenta lo que no recibe 200 --
      no puede insertar dos veces el mismo cargo ni extender el periodo dos
      veces. La base lo impide, no una comprobacion previa que dos peticiones
      simultaneas pueden pasar a la vez.
    - Parcial (WHERE ... IS NOT NULL): los pagos de Izipay y los manuales no
      tienen cargo de Culqi. Con un UNIQUE normal, en Postgres varios NULL
      conviven; se escribe parcial de todas formas para que el indice sea
      pequeno y para que la intencion quede escrita.

SE PUEDE APLICAR CON LA APLICACION CORRIENDO

  Son columnas nulables y un indice sobre una tabla que hoy tiene decenas de
  filas: ni reescribe tablas ni bloquea nada apreciable. El codigo anterior a
  esta migracion no menciona ninguna de estas columnas, asi que el orden entre
  desplegar y migrar no importa.

Revision ID: 0015
Revises: 0014
"""
from alembic import op
import sqlalchemy as sa

revision = '0015'
down_revision = '0014'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Un plan de Culqi por periodo: alli el importe y la frecuencia van dentro
    # del plan, asi que "Pro mensual" y "Pro anual" son dos objetos distintos.
    op.add_column('planes', sa.Column('culqi_plan_id_mensual', sa.Text))
    op.add_column('planes', sa.Column('culqi_plan_id_anual', sa.Text))

    # El cliente y la tarjeta se guardan aparte de la suscripcion a proposito:
    # cambiar de plan cancela la suscripcion y crea otra, y sin el crd_ habria
    # que volver a pedirle la tarjeta al cliente para algo que el vive como un
    # simple cambio de plan.
    op.add_column('suscripciones', sa.Column('culqi_customer_id', sa.Text))
    op.add_column('suscripciones', sa.Column('culqi_card_id', sa.Text))
    op.add_column('suscripciones', sa.Column('culqi_subscription_id', sa.Text))

    op.add_column('pagos_suscripcion', sa.Column('culqi_charge_id', sa.Text))
    op.add_column('pagos_suscripcion', sa.Column('culqi_event_id', sa.Text))

    op.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS pagosusc_culqi_charge_uq
            ON pagos_suscripcion (culqi_charge_id)
         WHERE culqi_charge_id IS NOT NULL
    """)
    # Para resolver "de quien es esta suscripcion" desde el webhook, que es la
    # consulta que corre en cada aviso de Culqi.
    op.execute("""
        CREATE INDEX IF NOT EXISTS susc_culqi_sxn_idx
            ON suscripciones (culqi_subscription_id)
         WHERE culqi_subscription_id IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS susc_culqi_cus_idx
            ON suscripciones (culqi_customer_id)
         WHERE culqi_customer_id IS NOT NULL
    """)


def downgrade() -> None:
    # Revertir borra los identificadores de Culqi, y eso NO cancela nada alli:
    # las suscripciones siguen cobrando cada periodo y nos quedariamos sin
    # saber a quien pertenecen. Se avisa en voz alta antes de dejar caer las
    # columnas, que es lo unico que puede hacer una migracion.
    op.execute("""
        DO $$
        DECLARE vivas INT;
        BEGIN
            SELECT COUNT(*) INTO vivas FROM suscripciones
             WHERE culqi_subscription_id IS NOT NULL;
            IF vivas > 0 THEN
                RAISE WARNING 'Se van a perder % suscripciones de Culqi que '
                              'SIGUEN COBRANDO: cancelalas en el panel de '
                              'Culqi o no habra forma de asociarlas.', vivas;
            END IF;
        END $$
    """)
    op.execute("DROP INDEX IF EXISTS susc_culqi_cus_idx")
    op.execute("DROP INDEX IF EXISTS susc_culqi_sxn_idx")
    op.execute("DROP INDEX IF EXISTS pagosusc_culqi_charge_uq")
    op.drop_column('pagos_suscripcion', 'culqi_event_id')
    op.drop_column('pagos_suscripcion', 'culqi_charge_id')
    op.drop_column('suscripciones', 'culqi_subscription_id')
    op.drop_column('suscripciones', 'culqi_card_id')
    op.drop_column('suscripciones', 'culqi_customer_id')
    op.drop_column('planes', 'culqi_plan_id_anual')
    op.drop_column('planes', 'culqi_plan_id_mensual')
