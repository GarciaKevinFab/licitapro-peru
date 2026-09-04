"""Webhook de WhatsApp: confirmaciones de alta y bajas.

QUE ENTRA POR AQUI

  - El GET con el que Meta da de alta el webhook la primera vez.
  - Los mensajes que nos escriben los usuarios. De todos ellos solo nos
    interesan dos: "ALTA" (confirma que el numero es suyo y consiente) y
    "BAJA" (pide dejar de recibir).
  - Avisos de estado de entrega (enviado, entregado, leido). No llevan
    'messages' y se ignoran sin ruido: no son un error.

POR QUE SE VERIFICA LA FIRMA ANTES DE MIRAR NADA

  Sin firma, cualquiera que descubra la URL puede mandarnos "este numero se
  dio de baja" y dejar sin avisos a un cliente que paga, o "este numero
  confirmo" y hacer que le escribamos a un tercero. La comprobacion va primero
  y falla cerrada: sin secreto configurado no se acepta ni un mensaje.

POR QUE SIEMPRE SE RESPONDE 200 UNA VEZ VERIFICADO

  Meta reintenta lo que no recibe 200, y escala hasta desactivar el webhook si
  insistimos en fallar. Un error nuestro procesando un mensaje no debe
  convertirse en la perdida del canal entero: se registra y se contesta 200.
  Lo unico que se rechaza con 403 es lo que no viene firmado por Meta.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from shared.db import activar_whatsapp, baja_whatsapp
from shared.notificaciones import CANAL_WHATSAPP, sembrar_historico
from shared.whatsapp import (
    enviar_texto,
    es_baja,
    mensajes_entrantes,
    verificar_firma,
    verificar_suscripcion,
)

log = logging.getLogger("web.webhooks_whatsapp")
router = APIRouter()

# Palabras con las que se confirma el alta. Se acepta mas de una porque la
# gente responde lo que le sale, no lo que le pedimos.
PALABRAS_ALTA = frozenset({"alta", "si", "sí", "confirmo", "acepto", "ok", "start"})


@router.get("/webhooks/whatsapp")
async def alta_webhook(request: Request):
    """Meta comprueba que la URL es nuestra antes de empezar a mandar."""
    p = request.query_params
    reto = verificar_suscripcion(
        p.get("hub.mode", ""), p.get("hub.verify_token", ""), p.get("hub.challenge", ""))
    if reto is None:
        return PlainTextResponse("No autorizado", status_code=403)
    return PlainTextResponse(reto)


@router.post("/webhooks/whatsapp")
async def recibir(request: Request):
    # El cuerpo CRUDO: la firma se calcula sobre los bytes exactos que llegaron.
    # Volver a serializar el JSON cambiaria espacios u orden y la firma no
    # cuadraria nunca.
    crudo = await request.body()
    if not verificar_firma(crudo, request.headers.get("X-Hub-Signature-256", "")):
        return JSONResponse({"error": "firma invalida"}, status_code=403)

    try:
        cuerpo = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": True, "nota": "cuerpo no es JSON"})

    for mensaje in mensajes_entrantes(cuerpo):
        numero, texto = mensaje["numero"], mensaje["texto"]
        try:
            await _procesar(numero, texto)
        except Exception:
            # Un fallo con un mensaje no puede tumbar el resto del lote ni
            # provocar que Meta reintente todo el paquete.
            log.exception("Fallo procesando mensaje de %s", numero)

    return JSONResponse({"ok": True})


def _normalizado(texto: str) -> str:
    return (texto or "").strip().lower()


async def _procesar(numero: str, texto: str) -> None:
    """Aplica la orden del usuario. La baja se mira ANTES que el alta.

    Si alguien escribe algo ambiguo, equivocarse hacia "deja de escribirle" es
    recuperable con un mensaje suyo; equivocarse hacia "sigue escribiendole"
    es exactamente lo que Meta penaliza.
    """
    if es_baja(texto):
        usuario_id = await baja_whatsapp(numero)
        if usuario_id:
            log.info("Baja de WhatsApp del usuario %s", usuario_id)
            # Se responde dentro de la ventana de 24 h que abrio su mensaje,
            # asi que aqui si vale texto libre y no hace falta plantilla.
            await enviar_texto(numero, (
                "Listo, no volveremos a escribirte por WhatsApp. "
                "Tus licitaciones te siguen esperando en el panel, y puedes "
                "reactivar los avisos cuando quieras desde Configuración."))
        return

    if _normalizado(texto) in PALABRAS_ALTA:
        usuario_id = await activar_whatsapp(numero)
        if not usuario_id:
            # Nadie tenia ese numero pendiente. No se da de alta nada: seria
            # empezar a escribirle a quien solo nos mando un mensaje suelto.
            log.info("Confirmacion desde %s sin alta pendiente: se ignora", numero)
            return
        # Lo ya publicado queda marcado como visto para que el primer aviso
        # sea de algo nuevo y no un volcado del historico.
        await sembrar_historico(usuario_id, CANAL_WHATSAPP)
        log.info("WhatsApp confirmado para el usuario %s", usuario_id)
        await enviar_texto(numero, (
            "Avisos activados. Te escribiremos cuando aparezcan licitaciones "
            "que encajen con tus filtros, agrupadas en un resumen. "
            "Responde BAJA en cualquier momento para dejar de recibirlos."))
