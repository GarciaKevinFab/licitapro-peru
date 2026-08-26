"""Plan gratuito permanente y la alerta como capacidad de pago.

QUE PROBLEMA DE NEGOCIO RESUELVE

  Los dos competidores serios del mercado peruano (Vigilante SEACE y Soy
  Proveedor) dejan buscar gratis PARA SIEMPRE y cobran por las alertas. Aqui la
  prueba duraba 14 dias y despues cortaba el acceso entero.

  Eso pierde por los dos lados. El proveedor que aun no ha visto pasar una
  licitacion suya de verdad no tiene con que decidir, asi que no paga; y al
  bloquearlo del todo lo empujamos a la web del competidor, que si le deja
  mirar. Un usuario en plan gratuito sigue siendo un cliente potencial: uno
  bloqueado es uno perdido.

  Ademas nos costaba mas de lo que parecia: el pozo de licitaciones ya esta
  scrapeado y almacenado, asi que dejarle mirar no anade coste. Lo que si
  cuesta dinero es avisarle -- cada WhatsApp se paga -- y lo que da valor es
  que le avisen a tiempo. Por eso la linea de pago va exactamente ahi.

POR QUE 'alertas' COMO COLUMNA Y NO COMO PRECIO CERO

  Que un plan cueste 0 no dice que puede hacer. La capacidad tiene que ser
  explicita para poder comprobarla al enviar, igual que `analisis_ia`. Si la
  regla fuera "precio > 0 entonces avisa", cualquier promocion o plan de
  cortesia futuro romperia el envio sin que nadie lo relacionara.

EL ANALISIS CON IA BAJA A PRO

  Estaba solo en Empresa (S/199) mientras el competidor lo da en su plan de
  S/49. Manteniendolo ahi, nuestro escalon de entrada era peor producto por el
  mismo dinero. En Pro (S/99) sigue cubriendo su coste real en la API de
  Anthropic sin regalar el escalon mas caro.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa

revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Por defecto TRUE para que los planes de pago que ya existen no se queden
    # mudos al aplicar la migracion. Solo el gratuito se marca en FALSE.
    op.add_column('planes', sa.Column(
        'alertas', sa.Boolean, nullable=False, server_default=sa.true()))

    op.execute("""
        INSERT INTO planes (codigo, nombre, precio_mensual, precio_anual,
                            max_empresas, max_regiones, analisis_ia, alertas, activo)
        VALUES ('gratis', 'Gratis', 0, 0, 1, 1, FALSE, FALSE, TRUE)
        ON CONFLICT (codigo) DO NOTHING
    """)

    op.execute("UPDATE planes SET analisis_ia = TRUE WHERE codigo = 'pro'")


def downgrade() -> None:
    op.execute("UPDATE planes SET analisis_ia = FALSE WHERE codigo = 'pro'")
    # Solo se quita si nadie lo esta usando: borrar el plan de un suscriptor
    # dejaria su fila de suscripcion apuntando al vacio.
    op.execute("""
        DELETE FROM planes WHERE codigo = 'gratis'
          AND NOT EXISTS (SELECT 1 FROM suscripciones WHERE plan_codigo = 'gratis')
    """)
    op.drop_column('planes', 'alertas')
