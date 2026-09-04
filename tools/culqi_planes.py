"""Crea en Culqi un plan por cada plan de pago y periodo, y guarda su pln_.

    python tools/culqi_planes.py            # ensena lo que haria y no lo hace
    python tools/culqi_planes.py --aplicar  # lo hace

POR QUE HACE FALTA UN COMANDO Y NO SE CREA AL VUELO

  Un plan de Culqi es un objeto con precio y frecuencia que despues cobra solo.
  Crearlo dentro del checkout significaria que un visitante que abre
  /comprar/pro?periodo=anual puede crear objetos de cobro en la pasarela, y que
  un error de configuracion se descubre con un cliente delante. Aqui se crea
  una vez, a mano, mirando lo que va a pasar antes de que pase.

POR QUE ENSENA ANTES DE HACER

  Los planes tienen precio y frecuencia, y equivocarse en cualquiera de los dos
  no da error: da cobros mal hechos que nadie ve hasta el mes siguiente. Por
  eso el modo por defecto es la simulacion y hay que pedir --aplicar
  explicitamente.

ES IDEMPOTENTE

  Si la columna ya tiene un pln_, no se recrea: se comprueba contra Culqi que
  siga existiendo y se deja como esta. Volver a lanzarlo despues de anadir un
  plan a la tabla crea solo el que falta.

  Culqi NO deja borrar un plan con suscripciones vivas, asi que crear uno de
  mas no es gratis: queda en el panel para siempre. Esa es la otra razon de que
  esto no se ejecute solo.

CUIDADO CON CAMBIAR UN PRECIO

  El importe vive DENTRO del plan de Culqi. Cambiar precio_mensual en la tabla
  no cambia lo que se le cobra a quien ya esta suscrito a ese plan: para eso
  hay que crear un plan nuevo y migrar las suscripciones. Este comando avisa
  cuando detecta esa diferencia, pero no la arregla solo -- moverle el precio a
  un cliente sin decirselo no lo decide un script.
"""
import argparse
import asyncio
import logging
import os
import sys
import unicodedata

sys.path.insert(0, os.getcwd())

from shared import culqi
from shared.db import connection

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("culqi_planes")

PERIODOS = ("mensual", "anual")
COLUMNA = {"mensual": "culqi_plan_id_mensual", "anual": "culqi_plan_id_anual"}
PRECIO = {"mensual": "precio_mensual", "anual": "precio_anual"}


def short_name(codigo: str, periodo: str) -> str:
    """Identificador corto del plan en Culqi: 'plan-pro-mensual'.

    Sin tildes ni mayusculas y derivado del codigo de la tabla, no del nombre
    visible: el nombre se puede cambiar en cualquier momento ("Pro" -> "Pro
    2026") y el short_name quedaria describiendo otra cosa.
    """
    limpio = unicodedata.normalize("NFKD", codigo).encode("ascii", "ignore").decode()
    return f"plan-{limpio.lower().strip()}-{periodo}"


async def _planes_de_pago() -> list[dict]:
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT codigo, nombre, precio_mensual, precio_anual,
                      culqi_plan_id_mensual, culqi_plan_id_anual
                 FROM planes
                WHERE activo = TRUE
                ORDER BY orden""")
    return [dict(f) for f in filas]


async def _guardar(codigo: str, periodo: str, plan_id: str) -> None:
    # El nombre de columna sale de un diccionario cerrado, no del argumento:
    # asi no hay forma de interpolar nada que venga de fuera en el SQL.
    async with connection() as conn:
        await conn.execute(
            f"UPDATE planes SET {COLUMNA[periodo]} = $2 WHERE codigo = $1",
            codigo, plan_id)


def _describe(plan: dict, periodo: str, precio) -> str:
    return (f"{plan['codigo']}/{periodo}: S/ {precio} "
            f"({culqi.a_centimos(precio)} centimos) "
            f"como '{short_name(plan['codigo'], periodo)}'")


async def main(aplicar: bool) -> int:
    modo = culqi.modo()
    log.info("Culqi en modo %s", modo)
    if modo == "simulado":
        log.warning(
            "En modo SIMULADO no se crea nada en Culqi: los pln_ que veas "
            "llevan '_sim_' y no sirven para cobrar. Pon CULQI_MODO=prueba con "
            "las llaves pk_test_/sk_test_ para crearlos de verdad.")

    # Se comprueba ANTES de tocar nada: si falta el intervalo, que se sepa aqui
    # y no despues de haber creado la mitad de los planes.
    try:
        for periodo in PERIODOS:
            culqi.intervalo_de(periodo)
    except culqi.ConfiguracionCulqi as e:
        log.error("%s", e)
        return 2

    planes = await _planes_de_pago()
    if not planes:
        log.error("No hay planes activos en la tabla `planes`.")
        return 1

    por_crear: list[tuple[dict, str]] = []
    for plan in planes:
        for periodo in PERIODOS:
            precio = plan[PRECIO[periodo]]
            if not precio or float(precio) <= 0:
                # El plan gratuito y cualquier periodo sin precio no tienen
                # plan de Culqi: no hay nada que cobrar.
                continue
            if plan[COLUMNA[periodo]]:
                continue
            por_crear.append((plan, periodo))

    # ─── Lo que ya existe: se comprueba, no se toca ─────
    for plan in planes:
        for periodo in PERIODOS:
            existente = plan[COLUMNA[periodo]]
            if not existente:
                continue
            try:
                remoto = await culqi.leer_plan(existente)
            except culqi.ErrorCulqi as e:
                log.error("%s/%s apunta a %s y Culqi no lo reconoce: %s. "
                          "Vacia esa columna a mano si quieres recrearlo.",
                          plan["codigo"], periodo, existente, e.merchant_message)
                continue
            log.info("%s/%s ya existe: %s (no se toca)",
                     plan["codigo"], periodo, existente)
            esperado = culqi.a_centimos(plan[PRECIO[periodo]])
            actual = remoto.get("amount")
            if actual is not None and int(actual) != esperado and not remoto.get("simulado"):
                log.warning(
                    "  OJO: la tabla dice %s centimos y el plan de Culqi cobra "
                    "%s. El importe vive DENTRO del plan: cambiarlo en la tabla "
                    "no cambia lo que se le cobra a quien ya esta suscrito. "
                    "Hace falta un plan nuevo y migrar las suscripciones.",
                    esperado, actual)

    if not por_crear:
        log.info("Nada que crear: todos los planes de pago tienen su pln_.")
        return 0

    # ─── Lo que se va a hacer, antes de hacerlo ─────────
    print()
    print("Se crearan estos planes en Culqi "
          f"(modo {modo}, intervalos mensual={culqi.intervalo_de('mensual')} "
          f"anual={culqi.intervalo_de('anual')}):")
    for plan, periodo in por_crear:
        print("  - " + _describe(plan, periodo, plan[PRECIO[periodo]]))
    print()

    if not aplicar:
        print("Nada hecho. Vuelve a lanzarlo con --aplicar si es correcto.")
        print("Recuerda: Culqi no deja borrar un plan con suscripciones vivas.")
        return 0

    creados = fallidos = 0
    for plan, periodo in por_crear:
        precio = plan[PRECIO[periodo]]
        try:
            creado = await culqi.crear_plan(
                nombre=f"{plan['nombre']} {periodo}",
                short_name=short_name(plan["codigo"], periodo),
                descripcion=f"LicitaPro Peru - plan {plan['nombre']} "
                            f"con facturacion {periodo}",
                monto=precio,
                periodo=periodo,
                metadata={"plan_codigo": plan["codigo"], "periodo": periodo})
        except (culqi.ErrorCulqi, culqi.ConfiguracionCulqi) as e:
            fallidos += 1
            detalle = getattr(e, "merchant_message", str(e))
            log.error("No se pudo crear %s/%s: %s",
                      plan["codigo"], periodo, detalle)
            continue

        plan_id = creado.get("id") or ""
        if not plan_id.startswith("pln_"):
            fallidos += 1
            log.error("Culqi devolvio algo que no es un plan para %s/%s: %s",
                      plan["codigo"], periodo, creado)
            continue

        # Se guarda INMEDIATAMENTE, plan a plan, y no en un lote al final: si
        # el proceso muere a mitad, lo ya creado queda asociado. Guardarlo al
        # final dejaria planes vivos en Culqi que nuestra tabla no conoce, y
        # la siguiente ejecucion crearia duplicados.
        await _guardar(plan["codigo"], periodo, plan_id)
        creados += 1
        log.info("Creado %s/%s -> %s", plan["codigo"], periodo, plan_id)

    log.info("Terminado: %d creados, %d fallidos", creados, fallidos)
    return 1 if fallidos else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--aplicar", action="store_true",
                   help="crea de verdad los planes en Culqi")
    sys.exit(asyncio.run(main(p.parse_args().aplicar)))
