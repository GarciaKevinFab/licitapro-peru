"""Reparto de avisos: a cada usuario lo suyo, por el canal que eligio.

EL FALLO QUE ARREGLA ESTE MODULO

  El aviso de licitaciones vivia dentro de radar_bot y mandaba TODO a ADMIN_ID.
  Es decir: el producto multi-inquilino avisaba al dueno de la plataforma y a
  nadie mas. Los clientes pagaban por unas alertas que nunca salian de la
  cuenta del administrador.

  Ademas `licitaciones.notificado` era un booleano global: en cuanto una
  licitacion se marcaba como avisada, dejaba de estar disponible para todos los
  demas inquilinos para siempre. Los dos fallos se tapaban entre si, porque con
  un solo destinatario ninguno se notaba.

POR QUE UN RESUMEN Y NO UN MENSAJE POR LICITACION

  Producto: nadie quiere veinte notificaciones al dia; a la tercera se silencia
  el canal y el servicio deja de existir aunque siga funcionando.

  Dinero: WhatsApp se cobra por mensaje entregado. Veinte coincidencias diarias
  son 600 mensajes al mes por cliente sin agrupar, frente a 30 agrupando. Esa
  diferencia se come el margen de un plan de S/49.

CUANDO SE ANOTA EL ENVIO

  Despues de que el canal confirme, nunca antes. Anotarlo antes convierte
  cualquier fallo de red en un aviso que el cliente no recibe jamas y que el
  sistema cree entregado. El riesgo contrario -- caerse justo entre el envio y
  la anotacion, y repetir el aviso -- existe, pero un aviso repetido es una
  molestia y un aviso perdido es la razon por la que se paga el producto.

QUIEN NO RECIBE NADA

  - Quien tiene la suscripcion suspendida. Seguir avisando a quien dejo de
    pagar es regalar justo aquello por lo que se cobra.
  - Quien esta fuera de su horario. Un WhatsApp a las 3 de la manana no es un
    servicio, es un motivo de baja.
  - Quien no dio consentimiento explicito para WhatsApp.
"""
import logging
import os
from datetime import datetime

import httpx

from shared.db import connection, licitaciones_para_usuario, get_config_usuario
from shared.suscripciones import estado_suscripcion
from shared import whatsapp

log = logging.getLogger("shared.notificaciones")

CANAL_TELEGRAM = "telegram"
CANAL_WHATSAPP = "whatsapp"
CANAL_EMAIL = "email"

# Cuantas licitaciones se nombran dentro del resumen. Mas que esto no cabe en
# un mensaje legible, y el detalle esta en el panel de todos modos.
MAX_EN_RESUMEN = 5

# Nombre de la plantilla aprobada en Meta. Configurable porque el nombre lo
# fija la aprobacion, no nosotros, y no queremos tocar codigo por eso.
PLANTILLA_AVISO = os.getenv("WHATSAPP_PLANTILLA_AVISO", "aviso_licitaciones")

URL_PANEL = os.getenv("LICITAPRO_URL_PUBLICA", "https://licitapro.pe")


# ─── A quien ─────────────────────────────────────────────

async def destinatarios() -> list[dict]:
    """Usuarios que deben recibir avisos ahora mismo.

    El filtro de suscripcion se resuelve por usuario y no en el SQL porque el
    vencimiento se evalua al leer (una fila puede decir 'activa' y estar
    vencida). Duplicar esa logica en una consulta seria tener dos versiones de
    la misma regla, y acabarian divergiendo.
    """
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT u.id, u.nombre, u.email,
                      u.telegram_chat_id, u.whatsapp_numero, u.whatsapp_estado
                 FROM usuarios u
                WHERE u.activo = TRUE""")

    salida = []
    for f in filas:
        susc = await estado_suscripcion(f["id"])
        # Dos condiciones distintas: tener acceso al producto y tener DERECHO a
        # que le avisen. El plan gratuito cumple la primera y no la segunda, que
        # es justo donde esta la linea de pago.
        if not susc.get("acceso") or not susc.get("alertas"):
            continue
        salida.append(dict(f))
    return salida


def _en_horario(config: dict | None) -> bool:
    """True si ahora cae dentro de la franja que eligio el usuario.

    Sin configuracion se avisa: un usuario recien registrado que no ha tocado
    nada debe recibir sus alertas, no quedarse sin ellas por un campo vacio.
    """
    if not config:
        return True
    inicio = (config.get("horario_inicio") or "").strip()
    fin = (config.get("horario_fin") or "").strip()
    if not inicio or not fin:
        return True
    ahora = datetime.now().strftime("%H:%M")
    if inicio <= fin:
        return inicio <= ahora <= fin
    # Franja que cruza la medianoche (22:00 a 07:00).
    return ahora >= inicio or ahora <= fin


# ─── Que ─────────────────────────────────────────────────

async def pendientes_para(usuario_id: int, canal: str,
                          limite: int = 40) -> list[dict]:
    """Licitaciones que le encajan y que aun no se le han mandado por ese canal.

    El cruce se hace contra los candidatos y no contra toda la tabla de envios:
    esa tabla crece sin parar, y preguntar "que le he mandado ya" entera seria
    cada dia mas caro para el mismo resultado.
    """
    candidatas = await licitaciones_para_usuario(usuario_id, limite=limite * 3)
    if not candidatas:
        return []
    ids = [c["id"] for c in candidatas]

    async with connection() as conn:
        ya = await conn.fetch(
            """SELECT licitacion_id FROM notificaciones_enviadas
                WHERE usuario_id = $1 AND canal = $2
                  AND licitacion_id = ANY($3::text[])""",
            usuario_id, canal, ids)
    enviadas = {f["licitacion_id"] for f in ya}
    return [c for c in candidatas if c["id"] not in enviadas][:limite]


async def anotar_envio(usuario_id: int, licitacion_ids: list[str],
                       canal: str) -> None:
    """Deja constancia de lo entregado. ON CONFLICT hace la operacion repetible."""
    if not licitacion_ids:
        return
    async with connection() as conn:
        await conn.executemany(
            """INSERT INTO notificaciones_enviadas (usuario_id, licitacion_id, canal)
               VALUES ($1, $2, $3)
               ON CONFLICT (usuario_id, licitacion_id, canal) DO NOTHING""",
            [(usuario_id, lid, canal) for lid in licitacion_ids])


async def sembrar_historico(usuario_id: int, canal: str) -> int:
    """Marca como ya avisado todo lo que hoy le encaja, SIN enviar nada.

    Se llama al activar un canal. Sin esto, quien conecta WhatsApp un martes
    recibe el pozo entero -- cientos de licitaciones que llevaban semanas
    publicadas -- goteando en resumenes durante dias. El cliente no lo lee como
    "me pusieron al dia": lo lee como spam, y silencia el canal.

    La semantica que espera cualquiera al activar un aviso es "avisame de lo
    que pase A PARTIR DE AHORA". Esto la implementa: lo viejo queda anotado
    como visto y solo lo que aparezca despues genera mensaje. Lo antiguo sigue
    estando en el panel, que es donde se busca hacia atras.

    Se siembra TODO lo que sigue abierto, sin pasar por los filtros del
    usuario. Dos motivos: `licitaciones_para_usuario` corta en 500 filas, asi
    que filtrando quedaria un resto goteando; y marcar como vista una que no le
    encaja no le quita nada, porque por definicion nunca se la habriamos
    mandado. Sembrar de menos si tiene consecuencia visible; sembrar de mas no.

    Devuelve cuantas se sembraron, para poder decirselo al usuario.
    """
    async with connection() as conn:
        n = await conn.fetchval(
            """INSERT INTO notificaciones_enviadas (usuario_id, licitacion_id, canal)
               SELECT $1, l.id, $2 FROM licitaciones l
                WHERE l.fecha_cierre > NOW() AND l.descartado = FALSE
               ON CONFLICT (usuario_id, licitacion_id, canal) DO NOTHING
               RETURNING 1""",
            usuario_id, canal)
        # fetchval devuelve solo la primera fila; el recuento real se cuenta aparte.
        total = await conn.fetchval(
            """SELECT COUNT(*) FROM notificaciones_enviadas
                WHERE usuario_id = $1 AND canal = $2""",
            usuario_id, canal)
    log.info("Sembradas licitaciones como vistas para el usuario %s en %s (total %s)",
             usuario_id, canal, total)
    return total or 0


# ─── Como se redacta ─────────────────────────────────────

def _titulo(lic: dict) -> str:
    return (lic.get("nomenclatura") or lic.get("objeto") or "Sin descripción")[:90]


def _monto(lic: dict) -> str:
    m = lic.get("monto_referencial")
    # Buena parte de los procesos publican el monto en 0 o vacio: eso es "no
    # publicado", no "gratis". Decir "S/ 0" seria inventar un dato.
    return f"S/ {m:,.0f}" if m else "monto no publicado"


def _resumen_texto(lics: list[dict]) -> str:
    lineas = []
    for l in lics[:MAX_EN_RESUMEN]:
        cierre = l.get("fecha_cierre")
        cuando = cierre.strftime("%d/%m") if cierre else "sin fecha"
        lineas.append(
            f"• {_titulo(l)}\n  {l.get('entidad') or ''} · {_monto(l)} · cierra {cuando}")
    if len(lics) > MAX_EN_RESUMEN:
        lineas.append(f"…y {len(lics) - MAX_EN_RESUMEN} más en tu panel.")
    return "\n".join(lineas)


def _resumen_html(lics: list[dict]) -> str:
    filas = "".join(
        f"<li><b>{_titulo(l)}</b><br>{l.get('entidad') or ''} — {_monto(l)}"
        f" — cierra {l['fecha_cierre'].strftime('%d/%m/%Y') if l.get('fecha_cierre') else 'sin fecha'}</li>"
        for l in lics[:MAX_EN_RESUMEN])
    extra = (f"<p>Y {len(lics) - MAX_EN_RESUMEN} más en tu panel.</p>"
             if len(lics) > MAX_EN_RESUMEN else "")
    return f"<ul>{filas}</ul>{extra}<p><a href='{URL_PANEL}'>Ver todas</a></p>"


# ─── Canales ─────────────────────────────────────────────

async def _por_telegram(chat_id: int, lics: list[dict]) -> bool:
    """Manda el resumen por Telegram con la API HTTP directa.

    Se usa httpx y no la libreria del bot a proposito: asi este modulo no
    depende de tener una instancia de Application viva, y el reparto puede
    dispararse igual desde la web o desde una tarea programada.
    """
    token = os.getenv("RADAR_BOT_TOKEN")
    if not token:
        log.warning("Sin RADAR_BOT_TOKEN: no se puede avisar por Telegram.")
        return False
    texto = (f"🔔 <b>{len(lics)} licitación(es) nueva(s) para ti</b>\n\n"
             f"{_resumen_texto(lics)}")
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": texto[:4000],
                      "parse_mode": "HTML", "disable_web_page_preview": True})
        if r.status_code != 200:
            log.error("Telegram rechazo el aviso a %s: %s", chat_id, r.text[:200])
        return r.status_code == 200
    except Exception as e:
        log.error("Telegram: fallo de red avisando a %s: %s", chat_id, e)
        return False


async def _por_whatsapp(usuario: dict, lics: list[dict]) -> bool:
    """Manda el resumen por WhatsApp usando la plantilla aprobada.

    Solo a quien esta en estado 'activo': tener el numero no autoriza a
    escribir, hace falta el consentimiento, y Meta bloquea el numero de la
    empresa si se salta esa regla.
    """
    if usuario.get("whatsapp_estado") != "activo" or not usuario.get("whatsapp_numero"):
        return False
    ok, detalle = await whatsapp.enviar_plantilla(
        usuario["whatsapp_numero"], PLANTILLA_AVISO,
        [
            usuario.get("nombre") or "Hola",
            str(len(lics)),
            _titulo(lics[0]),
        ])
    if not ok:
        log.error("WhatsApp no salio para el usuario %s: %s", usuario["id"], detalle)
    return ok


async def _por_email(destino: str, lics: list[dict]) -> bool:
    from shared.email_sender import enviar_email
    return await enviar_email(
        destino,
        f"{len(lics)} licitación(es) nueva(s) para ti",
        f"<h2>{len(lics)} licitación(es) coinciden con tus filtros</h2>"
        f"{_resumen_html(lics)}")


# ─── Reparto ─────────────────────────────────────────────

async def repartir() -> dict:
    """Envia a cada usuario sus coincidencias por sus canales. Devuelve el parte.

    Un fallo en un canal no interrumpe al resto: se anota solo lo que salio, de
    modo que el proximo pase reintenta lo que fallo sin repetir lo entregado.
    """
    parte = {"usuarios": 0, "telegram": 0, "whatsapp": 0, "email": 0, "fallos": 0}

    for usuario in await destinatarios():
        config = await get_config_usuario(usuario["id"])
        if config and config.get("activo") is False:
            continue
        if not _en_horario(config):
            continue

        hubo_algo = False

        if usuario.get("telegram_chat_id"):
            lics = await pendientes_para(usuario["id"], CANAL_TELEGRAM)
            if lics:
                if await _por_telegram(usuario["telegram_chat_id"], lics):
                    await anotar_envio(usuario["id"], [l["id"] for l in lics],
                                       CANAL_TELEGRAM)
                    parte["telegram"] += 1
                    hubo_algo = True
                else:
                    parte["fallos"] += 1

        if usuario.get("whatsapp_estado") == "activo":
            lics = await pendientes_para(usuario["id"], CANAL_WHATSAPP)
            if lics:
                if await _por_whatsapp(usuario, lics):
                    await anotar_envio(usuario["id"], [l["id"] for l in lics],
                                       CANAL_WHATSAPP)
                    parte["whatsapp"] += 1
                    hubo_algo = True
                else:
                    parte["fallos"] += 1

        if (config or {}).get("email_notificaciones"):
            lics = await pendientes_para(usuario["id"], CANAL_EMAIL)
            if lics:
                if await _por_email(config["email_notificaciones"], lics):
                    await anotar_envio(usuario["id"], [l["id"] for l in lics],
                                       CANAL_EMAIL)
                    parte["email"] += 1
                    hubo_algo = True
                else:
                    parte["fallos"] += 1

        parte["usuarios"] += bool(hubo_algo)

    log.info("Reparto de avisos: %s", parte)
    return parte
