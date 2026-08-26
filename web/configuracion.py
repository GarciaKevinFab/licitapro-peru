"""Configuracion de la cuenta: vinculo de Telegram, credenciales y filtros."""
import logging
import os

from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared.config import DEPARTAMENTOS
from shared.db import (
    borrar_credencial, borrar_cuenta, estado_credenciales, get_config_usuario,
    guardar_credencial, quitar_whatsapp, set_token_telegram,
    set_whatsapp_pendiente, update_config,
)
from shared.seguridad import nuevo_token_telegram, verificar_password
from shared.whatsapp import configurado as whatsapp_configurado, enviar_plantilla, normalizar_numero
from shared.suscripciones import regiones_permitidas
from web.auth import usuario_actual

log = logging.getLogger("web.configuracion")
router = APIRouter()

# Credenciales que el dueno puede cargar desde la web. Lista blanca: el tipo
# llega del formulario y termina en la BD, asi que no puede ser libre.
TIPOS_CREDENCIAL = {
    "seace_ruc": "RUC de proveedor en SEACE",
    "seace_password": "Contraseña RNP / SEACE",
    "smtp_user": "Usuario SMTP para notificaciones",
    "smtp_password": "Contraseña SMTP",
    "sunat_token": "Token del OSE para facturación",
}


def _plantillas(request: Request):
    return request.app.state.templates


@router.get("/configuracion", response_class=HTMLResponse)
async def ver_configuracion(request: Request, aviso: str = "", error: str = ""):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar?siguiente=/configuracion", status_code=303)

    config = await get_config_usuario(usuario["id"])
    return _plantillas(request).TemplateResponse("configuracion.html", {
        "request": request,
        "usuario": usuario,
        "config": config,
        "departamentos": DEPARTAMENTOS,
        "tipos_credencial": TIPOS_CREDENCIAL,
        # Solo si esta configurada y cuando: el valor NUNCA sale del servidor.
        "credenciales": await estado_credenciales(usuario["id"]),
        "bot_usuario": os.getenv("TELEGRAM_BOT_USERNAME", "LicitaRadar_SI_bot"),
        # Sin credenciales de Meta el canal no existe todavia: mejor decirlo
        # que aceptar un numero al que nunca llegara nada.
        "whatsapp_disponible": whatsapp_configurado(),
        "aviso": aviso,
        "error": error,
    })


@router.post("/configuracion/telegram/vincular")
async def iniciar_vinculo(request: Request):
    """Genera el token de un solo uso y manda al usuario al bot.

    No se le pide su ID numerico a proposito: si escribiera uno ajeno, sus
    alertas irian al chat de un desconocido y el sistema no podria detectarlo.
    Con el enlace profundo el chat_id lo entrega Telegram.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    token, expira = nuevo_token_telegram()
    await set_token_telegram(usuario["id"], token, expira)
    bot = os.getenv("TELEGRAM_BOT_USERNAME", "LicitaRadar_SI_bot")
    return RedirectResponse(f"https://t.me/{bot}?start={token}", status_code=303)


@router.post("/configuracion/telegram/desvincular")
async def desvincular(request: Request):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    from shared.db import connection
    async with connection() as conn:
        await conn.execute(
            "UPDATE usuarios SET telegram_chat_id=NULL WHERE id=$1", usuario["id"])
    return RedirectResponse("/configuracion?aviso=Telegram+desvinculado", status_code=303)


@router.post("/configuracion/credencial")
async def guardar(request: Request, tipo: str = Form(...), valor: str = Form(...)):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if tipo not in TIPOS_CREDENCIAL:
        return RedirectResponse("/configuracion?error=Tipo+no+permitido", status_code=303)
    if not valor.strip():
        return RedirectResponse("/configuracion?error=El+valor+esta+vacio", status_code=303)

    await guardar_credencial(usuario["id"], tipo, valor.strip())
    log.info("Credencial %s actualizada para usuario %s", tipo, usuario["id"])
    return RedirectResponse("/configuracion?aviso=Credencial+guardada", status_code=303)


@router.post("/configuracion/credencial/borrar")
async def borrar(request: Request, tipo: str = Form(...)):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if tipo in TIPOS_CREDENCIAL:
        await borrar_credencial(usuario["id"], tipo)
    return RedirectResponse("/configuracion?aviso=Credencial+eliminada", status_code=303)


@router.post("/configuracion/filtros")
async def guardar_filtros(request: Request, regiones: list[str] = Form([]),
                          keywords: str = Form(""), keywords_excluir: str = Form(""),
                          monto_min: float = Form(0), monto_max: float = Form(999999999)):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    def lista(texto: str) -> list[str]:
        return [t.strip() for t in texto.replace("\n", ",").split(",") if t.strip()]

    config = await get_config_usuario(usuario["id"])
    if not config:
        from shared.db import connection
        async with connection() as conn:
            await conn.execute(
                "INSERT INTO user_config (user_id, usuario_id) VALUES ($1, $2)",
                -usuario["id"], usuario["id"])

    # El tope de regiones del plan se aplica al guardar, no al pintar la lista.
    limpias, aviso_tope = await regiones_permitidas(
        usuario["id"], [r for r in regiones if r in DEPARTAMENTOS])

    await update_config(
        _clave_config(config, usuario),
        regiones=limpias,
        keywords=lista(keywords),
        keywords_excluir=lista(keywords_excluir),
        monto_min=monto_min,
        monto_max=monto_max,
    )
    return RedirectResponse("/configuracion?aviso=Filtros+guardados", status_code=303)


def _clave_config(config, usuario) -> int:
    """update_config indexa por la columna heredada user_id."""
    return config["user_id"] if config else -usuario["id"]


# ─── WhatsApp ────────────────────────────────────────────

@router.post("/configuracion/whatsapp")
async def vincular_whatsapp(request: Request, numero: str = Form(""),
                            consiento: str = Form("")):
    """Guarda el numero y pide confirmacion POR WhatsApp.

    No se activa aqui. El alta la cierra el propio dueno del numero
    respondiendo desde su telefono, y esa respuesta cumple dos cosas a la vez:

      - Prueba que el numero es suyo. Si alguien se equivoca de digito, el que
        recibiria las alertas es un desconocido, y a nosotros nos penaliza Meta.
      - Es el consentimiento demostrable que exige la politica de WhatsApp y,
        de paso, la Ley 29733 para tratar un dato personal.

    La casilla de la web sola no basta para lo primero: cualquiera puede
    teclear un numero ajeno y marcarla.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    if not consiento:
        return RedirectResponse(
            "/configuracion?error=" + quote_plus(
                "Marca la casilla de consentimiento para recibir avisos por WhatsApp."),
            status_code=303)

    e164 = normalizar_numero(numero)
    if not e164:
        return RedirectResponse(
            "/configuracion?error=" + quote_plus(
                "Ese número no es un móvil válido. Escríbelo como 987654321 "
                "o +51987654321. WhatsApp no funciona con teléfonos fijos."),
            status_code=303)

    await set_whatsapp_pendiente(usuario["id"], e164)

    plantilla = os.getenv("WHATSAPP_PLANTILLA_ALTA", "confirmacion_licitapro")
    ok, detalle = await enviar_plantilla(
        e164, plantilla, [usuario.get("nombre") or "Hola"])
    if not ok:
        log.error("No se pudo enviar la confirmacion de WhatsApp a %s: %s",
                  e164, detalle)
        return RedirectResponse(
            "/configuracion?error=" + quote_plus(
                "Guardamos tu número pero no pudimos enviarte el mensaje de "
                "confirmación. Revísalo o inténtalo de nuevo en unos minutos."),
            status_code=303)

    return RedirectResponse(
        "/configuracion?aviso=" + quote_plus(
            f"Te escribimos al {e164}. Responde ALTA en WhatsApp para activar "
            f"los avisos."),
        status_code=303)


@router.post("/configuracion/whatsapp/quitar")
async def quitar_whatsapp_web(request: Request):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    await quitar_whatsapp(usuario["id"])
    return RedirectResponse(
        "/configuracion?aviso=" + quote_plus("Ya no recibirás avisos por WhatsApp."),
        status_code=303)


# ─── Borrar la cuenta (Ley 29733) ────────────────────────

@router.post("/configuracion/cuenta/borrar")
async def borrar_mi_cuenta(request: Request, password: str = Form(""),
                           confirmo: str = Form("")):
    """Ejercicio del derecho de supresion. Irreversible.

    Se pide la contrasena, no una casilla sola: es la unica forma de saber que
    quien borra es el titular y no alguien que se encontro una sesion abierta.
    Para una accion que no tiene vuelta atras, el coste de teclearla es menor
    que el de perderlo todo por un clic ajeno.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    if not confirmo:
        return RedirectResponse(
            "/configuracion?error=" + quote_plus(
                "Marca la casilla para confirmar que entiendes que el borrado "
                "no tiene vuelta atrás."), status_code=303)

    if not verificar_password(password, usuario["password_hash"]):
        log.warning("Intento de borrado de la cuenta %s con contrasena incorrecta",
                    usuario["id"])
        return RedirectResponse(
            "/configuracion?error=" + quote_plus(
                "La contraseña no es correcta. No se borró nada."),
            status_code=303)

    resumen = await borrar_cuenta(usuario["id"])
    if not resumen.get("borrada"):
        return RedirectResponse(
            "/configuracion?error=" + quote_plus(
                "No pudimos completar el borrado. Escríbenos y lo resolvemos."),
            status_code=303)

    # La sesion apunta a un usuario que ya no existe: hay que cerrarla aqui, no
    # esperar a que la siguiente peticion se encuentre el hueco.
    request.session.clear()
    return RedirectResponse(
        "/entrar?aviso=" + quote_plus(
            "Tu cuenta y todos tus datos se borraron. Gracias por haberlo usado."),
        status_code=303)
