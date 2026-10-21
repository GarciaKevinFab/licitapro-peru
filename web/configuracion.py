"""Configuracion de la cuenta: vinculo de Telegram, credenciales y filtros."""
import logging
import os
from decimal import Decimal
from urllib.parse import quote, quote_plus

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from shared.config import DEPARTAMENTOS, a_soles
from shared.db import (
    borrar_credencial,
    borrar_cuenta,
    estado_credenciales,
    get_config_usuario,
    guardar_credencial,
    quitar_whatsapp,
    set_token_telegram,
    set_whatsapp_pendiente,
    token_telegram_vigente,
    update_config,
)
from shared.seguridad import nuevo_token_telegram
from shared.suscripciones import regiones_permitidas
from shared.whatsapp import configurado as whatsapp_configurado
from shared.whatsapp import enviar_plantilla, normalizar_numero
from web.auth import usuario_actual, verificar_password_de

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
    bot = os.getenv("TELEGRAM_BOT_USERNAME", "LicitaRadar_SI_bot")
    token = await token_telegram_vigente(usuario["id"])
    # TRES CAMINOS AL BOT, Y NO UNA REDIRECCION A t.me
    #
    #   Antes el boton mandaba directo a t.me/<bot>?start=<token>. En el
    #   escritorio esa pagina solo ofrece "START BOT", que intenta abrir la app
    #   con tg://: sin Telegram Desktop instalado, o con Brave bloqueando el
    #   protocolo, el boton no hace nada y el usuario se queda ahi. Ahora el
    #   codigo se ensena aqui con la app, Telegram Web y el mensaje a mano.
    vinculo = None
    if token:
        tg = f"tg://resolve?domain={bot}&start={token}"
        vinculo = {
            "token": token,
            "comando": f"/start {token}",
            "app": tg,
            "movil": f"https://t.me/{bot}?start={token}",
            "web": "https://web.telegram.org/k/#?tgaddr=" + quote(tg, safe=""),
        }
    return _plantillas(request).TemplateResponse(request, "configuracion.html", {
        "vinculo": vinculo,
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
    """Genera el token de un solo uso y vuelve a Configuracion, que lo ensena.

    No se le pide su ID numerico a proposito: si escribiera uno ajeno, sus
    alertas irian al chat de un desconocido y el sistema no podria detectarlo.
    Con el enlace profundo el chat_id lo entrega Telegram.
    """
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)

    token, expira = nuevo_token_telegram()
    await set_token_telegram(usuario["id"], token, expira)
    return RedirectResponse("/configuracion#telegram", status_code=303)


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
async def guardar(request: Request, tipo: str = Form(""), valor: str = Form("")):
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
async def borrar(request: Request, tipo: str = Form("")):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    if tipo in TIPOS_CREDENCIAL:
        await borrar_credencial(usuario["id"], tipo)
    return RedirectResponse("/configuracion?aviso=Credencial+eliminada", status_code=303)


@router.post("/configuracion/filtros")
# `Form(...)` en el valor por defecto NO es el error que B008 persigue.
#
#   La regla avisa de llamadas evaluadas una sola vez al definir la funcion --
#   el clasico `def f(x=[])`. Aqui es al reves: asi es COMO SE DECLARA un campo
#   de formulario en FastAPI. Lo que devuelve `Form()` no es un valor por
#   defecto, es el descriptor que FastAPI lee al montar la ruta para saber que
#   ese parametro viene del cuerpo. Moverlo dentro de la funcion, que es lo que
#   sugiere la regla, deja el endpoint sin leer el formulario.
async def guardar_filtros(request: Request, regiones: list[str] = Form([]),  # noqa: B008
                          keywords: str = Form(""), keywords_excluir: str = Form(""),
                          monto_min: str = Form("0"), monto_max: str = Form("999999999")):
    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar", status_code=303)
    minimo = _monto_filtro(monto_min, Decimal(0))
    maximo = _monto_filtro(monto_max, Decimal(999999999))
    if minimo is None or maximo is None:
        return RedirectResponse(
            "/configuracion?error=" + quote_plus(
                "El rango de montos tiene que ser de números entre 0 y 999,999,999,999."),
            status_code=303)

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
    limpias, _aviso_tope = await regiones_permitidas(
        usuario["id"], [r for r in regiones if r in DEPARTAMENTOS])

    await update_config(
        _clave_config(config, usuario),
        regiones=limpias,
        keywords=lista(keywords),
        keywords_excluir=lista(keywords_excluir),
        monto_min=minimo,
        monto_max=maximo,
    )
    return RedirectResponse("/configuracion?aviso=Filtros+guardados", status_code=303)


# NUMERIC(14,2): lo que no llega a un billon cabe.
_TOPE_FILTRO = Decimal(10) ** 12


def _monto_filtro(texto: str, si_vacio: Decimal) -> Decimal | None:
    """Un extremo del rango de montos del radar, o None si no vale.

    Llega como texto y no como `float` de FastAPI: monto_min y monto_max son
    NUMERIC desde la migracion 0021, y un "inf" que el float aceptaba ya no
    cabe en la columna (un 500 al guardar); "nan" se guardaba y dejaba el
    filtro sin ninguna licitacion. Vacio es el valor por defecto.
    """
    if not (texto or "").strip():
        return si_vacio
    valor = a_soles(texto.replace(",", ""))
    if valor is None or valor < 0 or valor >= _TOPE_FILTRO:
        return None
    return valor


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


# ─── Cambiar la contrasena ───────────────────────────────

@router.post("/configuracion/password")
async def cambiar_mi_password(request: Request, actual: str = Form(""),
                              nueva: str = Form("")):
    """Contrasena nueva sabiendo la actual.

    Hasta ahora no habia otra forma que pedir un enlace de recuperacion, que es
    mandar al correo -- un canal que no controlamos -- algo que el titular puede
    hacer aqui en un paso.

    LAS MISMAS REGLAS QUE EL ALTA Y LA RECUPERACION

      `password_debil` (minimo 10, rechaza mas de 72 bytes, que bcrypt no
      admite) y su mensaje tal cual. Una regla mas floja aqui seria la puerta
      trasera para guardar contrasenas que el registro rechaza; una sin
      mensaje, un 500 con una frase larga.

    LA ACTUAL SE COMPRUEBA CON EL FRENO DEL LOGIN

      Sin el, una sesion olvidada abierta seria un banco de pruebas de
      contrasenas sin tope. Los fallos cuentan en la misma tabla que /entrar.

    EL RESTO DE SESIONES SE CIERRA, ESTA NO

      El hash nuevo cambia la huella (web/auth.py), asi que toda cookie
      anterior queda revocada -- es para lo que suele cambiarse una contrasena
      --. Esta se vuelve a abrir con `iniciar_sesion` para no echar a quien
      acaba de hacerlo bien. Los enlaces de recuperacion pendientes tambien
      caen (`cambiar_password`).
    """
    from shared.admin_cuentas import cambiar_password
    from shared.db import anotar_intento_fallido, intentos_recientes
    from shared.seguridad import hashear_password, password_debil
    from web.auth import (
        MAX_INTENTOS_ACCESO,
        MAX_INTENTOS_POR_IP,
        VENTANA_INTENTOS_MIN,
        iniciar_sesion,
    )
    from web.limites import ip_del_cliente

    def con_error(msg: str):
        return RedirectResponse("/configuracion?error=" + quote_plus(msg) + "#password",
                                status_code=303)

    usuario = await usuario_actual(request)
    if not usuario:
        return RedirectResponse("/entrar?siguiente=/configuracion", status_code=303)
    if request.session.get("admin_original"):
        # iniciar_sesion vaciaria la sesion y con ella la vuelta del dueno a la
        # suya. El dueno tiene su propio formulario en la ficha del cliente.
        return con_error("Estás dentro de la cuenta de un cliente: cambia su "
                         "contraseña desde el panel de administración.")

    ip = ip_del_cliente(request.scope)
    por_email, por_ip = await intentos_recientes(usuario["email"], ip,
                                                 VENTANA_INTENTOS_MIN)
    if por_email >= MAX_INTENTOS_ACCESO or por_ip >= MAX_INTENTOS_POR_IP:
        log.warning("Cambio de contrasena frenado para el usuario %s", usuario["id"])
        return con_error("Demasiados intentos seguidos. Espera unos minutos y "
                         "vuelve a intentarlo.")
    if not await verificar_password_de(usuario["id"], actual):
        await anotar_intento_fallido(usuario["email"], ip)
        return con_error("La contraseña actual no es correcta. No se cambió nada.")

    motivo = password_debil(nueva)
    if motivo:
        return con_error(motivo)

    nuevo_hash = hashear_password(nueva)
    if not await cambiar_password(usuario["id"], nuevo_hash):
        return RedirectResponse("/entrar", status_code=303)
    iniciar_sesion(request, {"id": usuario["id"], "password_hash": nuevo_hash})
    log.info("Contrasena cambiada por su titular, usuario %s", usuario["id"])
    return RedirectResponse(
        "/configuracion?aviso=" + quote_plus(
            "Contraseña cambiada. Cerramos las demás sesiones abiertas de tu cuenta."),
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

    # `usuario_actual` ya no devuelve el hash (no debe llegar a las plantillas):
    # se comprueba contra la base.
    if not await verificar_password_de(usuario["id"], password):
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
