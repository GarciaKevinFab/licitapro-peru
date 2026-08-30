"""Suscripcion: elegir plan, pagar con Izipay y recibir la confirmacion."""
import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from shared import izipay
from shared.db import connection
from shared.suscripciones import (
    cambiar_plan, cancelar, confirmar_pago, estado_suscripcion, registrar_intento,
)
from web.auth import usuario_actual

log = logging.getLogger("web.suscripcion")
router = APIRouter()


def _plantillas(request: Request):
    return request.app.state.templates


async def _planes():
    async with connection() as conn:
        return await conn.fetch(
            "SELECT * FROM planes WHERE activo=TRUE ORDER BY orden")


@router.get("/suscripcion", response_class=HTMLResponse)
async def ver(request: Request, aviso: str = "", error: str = ""):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar?siguiente=/suscripcion", status_code=303)

    susc = await estado_suscripcion(usuario["id"])
    async with connection() as conn:
        historial = await conn.fetch(
            """SELECT ps.* FROM pagos_suscripcion ps
                 JOIN suscripciones s ON s.id = ps.suscripcion_id
                WHERE s.usuario_id = $1
                ORDER BY ps.created_at DESC LIMIT 12""",
            usuario["id"])

    return _plantillas(request).TemplateResponse("suscripcion.html", {
        "request": request, "usuario": usuario, "s": susc,
        "planes": await _planes(), "historial": historial,
        "modo_pasarela": izipay.modo(), "comision": izipay.comision_estimada,
        "aviso": aviso, "error": error,
    })


@router.post("/suscripcion/elegir")
async def elegir(request: Request, plan: str = Form(...), periodo: str = Form("mensual")):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if not await cambiar_plan(usuario["id"], plan, periodo):
        return RedirectResponse("/suscripcion?error=Plan+o+periodo+no+valido",
                                status_code=303)
    return RedirectResponse("/suscripcion?aviso=Plan+actualizado.+Ya+puedes+pagar",
                            status_code=303)


@router.post("/suscripcion/pagar")
async def pagar(request: Request):
    """Abre el cobro: crea la orden y pide el token de sesion a Izipay.

    El numero de orden se genera y se guarda ANTES de llamar a la pasarela. Si
    se guardara despues, un pago confirmado sin fila local quedaria huerfano: el
    cliente habria pagado sin recibir el servicio.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    susc = await estado_suscripcion(usuario["id"])
    if not susc.get("existe"):
        return RedirectResponse("/suscripcion?error=No+tienes+plan+seleccionado",
                                status_code=303)

    monto = (susc["precio_anual"] if susc["periodo"] == "anual"
             else susc["precio_mensual"])
    if not monto:
        return RedirectResponse("/suscripcion?error=Ese+plan+no+tiene+precio+configurado",
                                status_code=303)

    numero_orden = izipay.nuevo_numero_orden()
    if not await registrar_intento(usuario["id"], monto, numero_orden):
        return RedirectResponse("/suscripcion?error=No+se+pudo+iniciar+el+cobro",
                                status_code=303)

    resultado = await izipay.generar_token_sesion(
        numero_orden, float(monto), usuario["email"])

    if not resultado["ok"]:
        log.error("Izipay no devolvio token para %s: %s", numero_orden,
                  resultado.get("detalle"))
        return RedirectResponse(
            "/suscripcion?error=La+pasarela+no+respondio.+Intenta+de+nuevo",
            status_code=303)

    return _plantillas(request).TemplateResponse("pagar.html", {
        "request": request, "usuario": usuario,
        "token": resultado["token"], "modo": resultado["modo"],
        "numero_orden": numero_orden, "monto": monto,
        "plan": susc.get("plan_nombre"), "periodo": susc["periodo"],
    })


@router.post("/suscripcion/retorno")
async def retorno(request: Request, numero_orden: str = Form(...),
                  simulado: str = Form("")):
    """Vuelta del navegador tras el formulario de pago.

    El retorno del navegador NO es prueba de pago: lo manda el cliente. La
    confirmacion de verdad llega por webhook, firmada. En modo simulado si se
    acepta, porque existe justamente para probar el flujo sin pasarela.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    if simulado and izipay.modo() == "simulado":
        await confirmar_pago(numero_orden, f"SIM-{numero_orden}", {"origen": "simulado"})
        return RedirectResponse("/suscripcion?aviso=Pago+simulado+confirmado",
                                status_code=303)

    return RedirectResponse(
        "/suscripcion?aviso=Pago+enviado.+Se+confirmara+cuando+la+pasarela+avise",
        status_code=303)


@router.get("/webhooks/izipay")
async def comprobacion_webhook():
    """Responde al validador de Izipay, que comprueba la URL antes de aceptarla.

    POR QUE HACE FALTA UNA RUTA GET AQUI

      La regla de notificacion se llama literalmente [CHECKURL]: antes de
      guardarla, Izipay pide la direccion y exige que conteste. Como el webhook
      solo admitia POST -- que es lo correcto para el aviso real --, el
      validador recibia 405 y rechazaba la URL con "dominio desconocido o
      inaccesible", que apunta a un problema de DNS y no lo era.

    ESTO NO ABRE NADA

      Devuelve una constante. No lee la peticion, no toca la base y no cambia
      ningun estado. El cobro sigue entrando SOLO por POST y con firma HMAC
      verificada; un aviso sin firmar se rechaza con 401 pase lo que pase.
      Aceptar GET aqui equivaldria a dejar que cualquiera confirmase pagos.
    """
    return JSONResponse({"ok": True, "servicio": "webhook izipay"})


@router.post("/webhooks/izipay")
async def webhook(request: Request):
    """Confirmacion servidor-a-servidor de Izipay.

    Sin sesion: la autenticidad la da la firma, no la cookie. Se rechaza todo lo
    que no venga firmado; un webhook sin verificar es una orden de cobro que
    puede mandar cualquiera.
    """
    crudo = await request.body()
    firma = (request.headers.get("x-izipay-signature")
             or request.headers.get("signature") or "")

    if not izipay.verificar_firma(crudo, firma):
        log.warning("Webhook de Izipay con firma invalida desde %s",
                    request.client.host if request.client else "?")
        return JSONResponse({"ok": False, "motivo": "firma invalida"}, status_code=401)

    try:
        datos = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "motivo": "cuerpo no es json"}, status_code=400)

    numero_orden = (datos.get("orderNumber") or datos.get("order_number")
                    or (datos.get("response") or {}).get("orderNumber"))
    if not numero_orden:
        return JSONResponse({"ok": False, "motivo": "sin numero de orden"},
                            status_code=400)

    aprobado = (str(datos.get("code")) == "00"
                or datos.get("status") in ("PAID", "AUTHORIZED"))
    if not aprobado:
        log.info("Webhook de %s indica pago no aprobado: %s", numero_orden,
                 datos.get("message"))
        return JSONResponse({"ok": True, "aplicado": False})

    transaction_id = (datos.get("transactionId")
                      or (datos.get("response") or {}).get("transactionId"))
    aplicado = await confirmar_pago(numero_orden, transaction_id, datos)
    # 200 tambien cuando ya estaba aplicado: devolver error haria que Izipay
    # reintentara eternamente un webhook que ya procesamos bien.
    return JSONResponse({"ok": True, "aplicado": aplicado})


@router.post("/suscripcion/cancelar")
async def cancelar_suscripcion(request: Request):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    await cancelar(usuario["id"])
    return RedirectResponse(
        "/suscripcion?aviso=Suscripcion+cancelada.+Conservas+el+acceso+hasta+el+fin+del+periodo",
        status_code=303)
