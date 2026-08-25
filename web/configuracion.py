"""Configuracion de la cuenta: vinculo de Telegram, credenciales y filtros."""
import logging
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared.config import DEPARTAMENTOS
from shared.db import (
    borrar_credencial, estado_credenciales, get_config_usuario,
    guardar_credencial, set_token_telegram, update_config,
)
from shared.seguridad import nuevo_token_telegram
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

    await update_config(
        _clave_config(config, usuario),
        regiones=[r for r in regiones if r in DEPARTAMENTOS],
        keywords=lista(keywords),
        keywords_excluir=lista(keywords_excluir),
        monto_min=monto_min,
        monto_max=monto_max,
    )
    return RedirectResponse("/configuracion?aviso=Filtros+guardados", status_code=303)


def _clave_config(config, usuario) -> int:
    """update_config indexa por la columna heredada user_id."""
    return config["user_id"] if config else -usuario["id"]
