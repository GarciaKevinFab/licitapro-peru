"""Adaptador de Izipay. Todo lo especifico del proveedor vive aqui.

QUE ESTA VERIFICADO CONTRA LA API REAL (sondeado sin credenciales):

  - Hosts vivos:
        produccion  https://api-pw.izipay.pe
        sandbox     https://sandbox-api-pw.izipay.pe
  - Endpoint de token de sesion: POST /security/v1/Token/Generate
    (responde 405 a GET, o sea que existe y exige POST)
  - Envoltorio de respuesta: {"code": "...", "message": "...", "response": ...}
    con code "400" y "Estructura del request invalida" ante un cuerpo que no
    le gusta.

QUE **NO** ESTA VERIFICADO Y HAY QUE CONFIRMAR CON TU CUENTA DE COMERCIO:

  - Los nombres exactos de los campos del cuerpo de Token/Generate. La API
    devuelve el mismo error generico ante cualquier cuerpo, asi que su esquema
    no se puede deducir sondeando. Los campos de abajo salen de la
    documentacion publica de web-core, no de una respuesta exitosa.
  - El endpoint y el cuerpo del cobro recurrente con tarjeta tokenizada.
  - El algoritmo exacto de firma del webhook (IPN).

  Todo eso esta marcado con POR_CONFIRMAR. Mientras no se confirme, modo()
  devuelve "simulado" y el flujo completo de suscripcion se puede probar de
  punta a punta sin tocar la pasarela.

Por que un adaptador y no llamadas sueltas: cuando confirmes el contrato real,
o si cambias de pasarela, se toca este archivo y nada mas. Ademas IFS anuncio
en 2025 la absorcion de Izipay dentro de Interbank, asi que la posibilidad de
que esto cambie no es teorica.
"""
import hashlib
import hmac
import logging
import os
import secrets
from datetime import datetime

import httpx

log = logging.getLogger("shared.izipay")

HOST_PRODUCCION = "https://api-pw.izipay.pe"
HOST_SANDBOX = "https://sandbox-api-pw.izipay.pe"

RUTA_TOKEN = "/security/v1/Token/Generate"

# POR_CONFIRMAR: ruta del cobro con tarjeta tokenizada.
RUTA_COBRO_TOKEN = "/payment/v1/Charge/Token"

# Comision efectiva de Izipay con IGV ya incorporado:
# 3.44% x 1.18 = 4.0592%  y  S/0.69 x 1.18 = S/0.8142 por cobro.
# Sirve para mostrar el neto en el panel, no para cobrar.
COMISION_PORCENTAJE = 0.040592
COMISION_FIJA = 0.8142


def modo() -> str:
    """'produccion', 'sandbox' o 'simulado'.

    Simulado es el valor por defecto a proposito: sin credenciales hay que
    fallar hacia "no cobra nada", nunca hacia "intenta cobrar de verdad".
    """
    m = (os.getenv("IZIPAY_MODO") or "").strip().lower()
    if m in ("produccion", "sandbox"):
        if not (os.getenv("IZIPAY_MERCHANT_CODE") and os.getenv("IZIPAY_API_KEY")):
            log.warning(
                "IZIPAY_MODO=%s pero faltan IZIPAY_MERCHANT_CODE o IZIPAY_API_KEY: "
                "se sigue en modo simulado.", m)
            return "simulado"
        return m
    return "simulado"


def _host() -> str:
    return HOST_PRODUCCION if modo() == "produccion" else HOST_SANDBOX


def nuevo_numero_orden(prefijo: str = "LP") -> str:
    """Numero de orden unico. Es la llave de idempotencia del cobro."""
    return f"{prefijo}{datetime.now():%y%m%d%H%M%S}{secrets.token_hex(3).upper()}"


def _transaction_id() -> str:
    """Izipay pide un identificador numerico de transaccion por peticion."""
    return f"{datetime.now():%H%M%S}{secrets.randbelow(10000):04d}"


async def generar_token_sesion(numero_orden: str, monto: float,
                               email_cliente: str, moneda: str = "PEN") -> dict:
    """Token de sesion para abrir el formulario de pago en el navegador.

    Devuelve {"ok", "token", "modo", "detalle"}. Nunca lanza: un fallo de la
    pasarela no debe tumbar la peticion web.
    """
    if modo() == "simulado":
        # Token falso reconocible: si llegara a produccion por error, el
        # formulario de Izipay lo rechazaria en vez de cobrar algo raro.
        return {"ok": True, "modo": "simulado",
                "token": f"SIMULADO-{numero_orden}", "detalle": None}

    # POR_CONFIRMAR: nombres de campo tomados de la documentacion de web-core.
    cuerpo = {
        "requestSource": "ECOMMERCE",
        "merchantCode": os.getenv("IZIPAY_MERCHANT_CODE"),
        "orderNumber": numero_orden,
        "publicKey": os.getenv("IZIPAY_PUBLIC_KEY", ""),
        "amount": f"{monto:.2f}",
        "currency": moneda,
        "email": email_cliente,
    }
    cabeceras = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "transactionId": _transaction_id(),
        # POR_CONFIRMAR: la clave aparece documentada como "Authorization" en
        # unos sitios y como cabecera propia en otros. Se manda de ambas formas.
        "Authorization": os.getenv("IZIPAY_API_KEY", ""),
        "apikey": os.getenv("IZIPAY_API_KEY", ""),
    }

    try:
        async with httpx.AsyncClient(timeout=25) as cliente:
            r = await cliente.post(_host() + RUTA_TOKEN, json=cuerpo, headers=cabeceras)
        datos = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        log.error("Izipay Token/Generate fallo: %s", e, exc_info=True)
        return {"ok": False, "modo": modo(), "token": None, "detalle": str(e)[:200]}

    # El envoltorio {code, message, response} si esta verificado.
    respuesta = datos.get("response") or {}
    token = respuesta.get("token") if isinstance(respuesta, dict) else None
    if token:
        return {"ok": True, "modo": modo(), "token": token, "detalle": datos}

    log.warning("Izipay rechazo la peticion de token: %s", datos)
    return {"ok": False, "modo": modo(), "token": None, "detalle": datos}


async def cobrar_con_token(token_tarjeta: str, numero_orden: str, monto: float,
                           moneda: str = "PEN") -> dict:
    """Cobro recurrente contra una tarjeta ya tokenizada.

    Es lo que permite renovar la suscripcion sin volver a pedir la tarjeta.
    """
    if modo() == "simulado":
        return {"ok": True, "estado": "pagado", "modo": "simulado",
                "transaction_id": f"SIM-{numero_orden}", "detalle": None}

    # POR_CONFIRMAR: endpoint y cuerpo del cobro con token.
    cuerpo = {
        "requestSource": "ECOMMERCE",
        "merchantCode": os.getenv("IZIPAY_MERCHANT_CODE"),
        "orderNumber": numero_orden,
        "amount": f"{monto:.2f}",
        "currency": moneda,
        "cardToken": token_tarjeta,
    }
    cabeceras = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "transactionId": _transaction_id(),
        "Authorization": os.getenv("IZIPAY_API_KEY", ""),
    }
    try:
        async with httpx.AsyncClient(timeout=30) as cliente:
            r = await cliente.post(_host() + RUTA_COBRO_TOKEN, json=cuerpo,
                                   headers=cabeceras)
        datos = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except Exception as e:
        log.error("Izipay cobro con token fallo: %s", e, exc_info=True)
        return {"ok": False, "estado": "error", "transaction_id": None,
                "detalle": str(e)[:200]}

    aprobado = str(datos.get("code")) == "00"
    respuesta = datos.get("response") or {}
    return {
        "ok": aprobado,
        "estado": "pagado" if aprobado else "rechazado",
        "transaction_id": (respuesta.get("transactionId")
                           if isinstance(respuesta, dict) else None),
        "detalle": datos,
    }


def verificar_firma(cuerpo_crudo: bytes, firma_recibida: str) -> bool:
    """Valida la firma del webhook de Izipay.

    POR_CONFIRMAR: el algoritmo exacto. Se implementa HMAC-SHA256 sobre el
    cuerpo crudo con IZIPAY_HMAC_KEY, que es lo habitual en pasarelas.

    Sin clave configurada devuelve False, NO True: un webhook sin verificar es
    una orden de cobro que puede mandar cualquiera. Aqui hay que fallar cerrado.
    """
    clave = os.getenv("IZIPAY_HMAC_KEY")
    if not clave or not firma_recibida:
        log.warning("Webhook de Izipay sin firma verificable: se rechaza.")
        return False
    esperada = hmac.new(clave.encode(), cuerpo_crudo, hashlib.sha256).hexdigest()
    # compare_digest evita filtrar por tiempo cuanto coincide la firma.
    return hmac.compare_digest(esperada, firma_recibida.strip().lower())


def comision_estimada(monto: float) -> float:
    """Lo que se lleva Izipay de un cobro, con IGV incluido."""
    return round(monto * COMISION_PORCENTAJE + COMISION_FIJA, 2)
