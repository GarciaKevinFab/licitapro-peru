"""El dinero pasa de REAL a NUMERIC(14,2).

POR QUE

  REAL es un float de 32 bits: 24 bits de mantisa, unas siete cifras
  significativas. Por encima de S/ 262 144 ya no distingue un centimo de otro
  (S/ 300 000.10 se guardaba como 300 000.09375) y por encima de 16 777 216 ni
  siquiera los enteros: S/ 25 000 000.55 volvia como 25 000 000. Son los montos
  de una licitacion regional cualquiera, y de ellos salen la oferta, el filtro
  del radar, el estimador de precios y los cobros de cada contrato.

  NUMERIC(14,2) guarda exacto hasta S/ 999 999 999 999.99, casi un billon de
  soles: mas que cualquier contrato publico del pais. Es lo mismo que ya usa
  licitaciones.monto_adjudicado (0001), asi que el referencial y el adjudicado
  de una convocatoria quedan del mismo tipo.

QUE NO SE TOCA

  analisis_ia.score, licitaciones.score_viabilidad e historico_precios.
  ratio_adj_ref no son dinero: un puntaje de 0 a 100 y un cociente. Un float
  les sobra, y pasarlos a NUMERIC obligaria a convertir cada calculo
  estadistico que los usa sin ganar nada.

POR QUE ::double precision ANTES DE ::numeric

  En Postgres 17 `real::numeric` redondea a SEIS cifras significativas:
  `300000.1::real::numeric` da 300000 y 1234567::real da 1234570. Pasar por
  double conserva el valor binario entero (cabe de sobra en 53 bits), y
  round(..., 2) lo deja en centimos. El centimo que REAL ya perdio no se
  recupera, pero tampoco se pierde uno mas.

NaN E INFINITO PASAN A NULL; LO QUE NO CABE ABORTA

  REAL admite NaN e Infinity y NUMERIC(14,2) no los acepta. No son un monto:
  no hay informacion que perder, y NULL es lo que el codigo ya entiende como
  "no publicado". Un valor finito de un billon o mas es otra cosa: puede ser
  un error de lectura del scraper o un dato raro de verdad, y decidirlo es
  cosa de una persona. Como en 0020, se aborta con la lista.

BLOQUEO Y DURACION (para el despliegue)

  ALTER COLUMN ... TYPE reescribe la tabla entera y sus indices con un ACCESS
  EXCLUSIVE: mientras dura, nadie lee ni escribe esa tabla. Cada tabla se
  altera en UNA sentencia con todas sus columnas, para reescribirla una sola
  vez. Alembic corre la migracion en una transaccion, asi que los bloqueos se
  sueltan juntos al final.

  Medido en local (Postgres 17, SSD) con 50 000 licitaciones sinteticas
  (60 MB con indices): el `alembic upgrade` entero tarda menos de 3 s, el
  arranque de alembic incluido; el downgrade, lo mismo. Las demas tablas son
  pequenas y no cuentan. En el VPS cuente con el doble o el triple: segundos
  en los que la web y los bots esperan en vez de fallar. Aun asi conviene
  correrla con el radar parado, fuera de una cosecha.

  lock_timeout: si una consulta larga tiene la tabla tomada, el ALTER se
  quedaria en cola y detras de el TODAS las lecturas nuevas: la web entera
  colgada esperando. Con el tope, la migracion falla limpia a los 15 s y se
  reintenta; no deja la web parada.

Revision ID: 0021
Revises: 0020
"""
from alembic import op
from sqlalchemy import text

revision = '0021'
down_revision = '0020'
branch_labels = None
depends_on = None

TIPO = "NUMERIC(14,2)"
# Lo que NUMERIC(14,2) puede guardar despues de redondear a centimos.
TOPE = 10 ** 12

COLUMNAS = {
    "contratos": ("carta_fianza_monto", "monto_adjudicado"),
    "experiencia": ("monto",),
    "historico_precios": ("monto_adjudicado", "monto_referencial"),
    "licitaciones": ("monto_referencial",),
    "pagos": ("monto",),
    "propuestas": ("precio_ofertado", "precio_sugerido_max", "precio_sugerido_min"),
    "user_config": ("monto_max", "monto_min"),
}


def _fuera_de_rango(bind) -> list[str]:
    """Valores finitos que no caben en NUMERIC(14,2). Hasta 5 por columna."""
    hallados = []
    for tabla, columnas in COLUMNAS.items():
        for col in columnas:
            filas = bind.execute(text(
                f"""SELECT {col}::double precision AS v FROM {tabla}
                     WHERE {col} <> 'NaN'::real
                       AND {col} NOT IN ('Infinity'::real, '-Infinity'::real)
                       AND abs(round({col}::double precision::numeric, 2)) >= :tope
                     LIMIT 5"""), {"tope": TOPE}).fetchall()
            hallados += [f"{tabla}.{col} = {f.v!r}" for f in filas]
    return hallados


def upgrade() -> None:
    bind = op.get_bind()
    op.execute("SET LOCAL lock_timeout = '15s'")
    malos = _fuera_de_rango(bind)
    if malos:
        raise RuntimeError(
            f"Hay montos que no caben en {TIPO} (un billon de soles o mas): "
            + "; ".join(malos)
            + ". Revisa si son errores de lectura y corrigelos o ponlos a NULL a mano; "
              "esta migracion no decide que dato es bueno.")

    for tabla, columnas in COLUMNAS.items():
        # NaN e infinito no son un monto: NULL, que es "no publicado".
        for col in columnas:
            op.execute(
                f"""UPDATE {tabla} SET {col} = NULL
                     WHERE {col} = 'NaN'::real
                        OR {col} IN ('Infinity'::real, '-Infinity'::real)""")
        cambios = ", ".join(
            f"ALTER COLUMN {col} TYPE {TIPO} "
            f"USING round({col}::double precision::numeric, 2)"
            for col in columnas)
        op.execute(f"ALTER TABLE {tabla} {cambios}")

    # Los DEFAULT de user_config eran literales REAL; se reescriben como numeric
    # para que el esquema no arrastre un 999999999::real escondido.
    op.execute("ALTER TABLE user_config ALTER COLUMN monto_min SET DEFAULT 0, "
               "ALTER COLUMN monto_max SET DEFAULT 999999999")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '15s'")
    for tabla, columnas in COLUMNAS.items():
        cambios = ", ".join(
            f"ALTER COLUMN {col} TYPE REAL USING {col}::real" for col in columnas)
        op.execute(f"ALTER TABLE {tabla} {cambios}")
    op.execute("ALTER TABLE user_config ALTER COLUMN monto_min SET DEFAULT 0, "
               "ALTER COLUMN monto_max SET DEFAULT 999999999")
