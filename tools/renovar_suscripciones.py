"""Renueva las suscripciones vencidas cobrando la tarjeta guardada.

Pensado para un cron diario:

    0 9 * * *  cd /ruta && python tools/renovar_suscripciones.py

Reintenta espaciado (una vez al dia, maximo 4 veces) antes de suspender.
Cortar al primer rechazo pierde clientes que si querian pagar.
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.getcwd())

from shared import izipay                                     # noqa: E402
from shared.seguridad import descifrar                        # noqa: E402
from shared.suscripciones import (                            # noqa: E402
    confirmar_pago, registrar_intento, registrar_intento_fallido,
    renovaciones_pendientes,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
log = logging.getLogger("renovar")


async def main() -> int:
    pendientes = await renovaciones_pendientes()
    log.info("Suscripciones por renovar: %d (pasarela en modo %s)",
             len(pendientes), izipay.modo())

    cobradas = fallidas = 0
    for s in pendientes:
        token = descifrar(s["token_tarjeta"])
        if not token:
            log.error("Token ilegible para %s: se salta", s["email"])
            continue

        orden = izipay.nuevo_numero_orden("REN")
        # El intento se registra ANTES de cobrar: si el proceso muere a mitad,
        # queda rastro y el webhook puede casarlo despues.
        await registrar_intento(s["usuario_id"], s["monto"], orden)

        r = await izipay.cobrar_con_token(token, orden, float(s["monto"]))
        if r["ok"]:
            await confirmar_pago(orden, r.get("transaction_id"), r.get("detalle"))
            cobradas += 1
            log.info("Renovada %s por S/%s", s["email"], s["monto"])
        else:
            await registrar_intento_fallido(s["usuario_id"])
            fallidas += 1
            log.warning("Cobro rechazado para %s (intento %d): %s",
                        s["email"], s["intentos_fallidos"] + 1, r.get("detalle"))

    log.info("Renovacion terminada: %d cobradas, %d fallidas", cobradas, fallidas)
    return 0


sys.exit(asyncio.run(main()))
