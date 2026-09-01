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
import html
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
        await conn.fetchval(
            """INSERT INTO notificaciones_enviadas (usuario_id, licitacion_id, canal)
               SELECT $1, l.id, $2 FROM licitaciones l
                -- Hora de Lima, no UTC, y la MISMA regla de vigencia que
                -- `licitaciones_para_usuario` (shared/db.py): si divergen,
                -- activar un canal manda como "nuevas" cosas que el panel ya
                -- ensenaba desde hace dias.
                WHERE (l.fecha_cierre > (NOW() AT TIME ZONE 'America/Lima')
                       OR (l.fecha_cierre IS NULL AND l.fecha_publicacion >
                           (NOW() AT TIME ZONE 'America/Lima') - INTERVAL '7 days'))
                  AND l.descartado = FALSE
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

def _esc(texto) -> str:
    """Escapa para HTML lo que escribe una fuente externa.

    POR QUE, SI HOY NINGUNA LICITACION TIENE UN "&"

      Comprobado contra produccion: 0 de 842. Pero el objeto y la entidad los
      escribe una entidad del Estado en su portal, no nosotros, y "SERVICIOS
      GENERALES A & B S.A.C." es un nombre de empresa normal en Peru.

      El dia que llegue uno, esto no se rompe a medias:

        - Telegram envia con `parse_mode="HTML"`. Un "&" suelto o un "<" hace
          que la API devuelva 400 y el mensaje NO SE MANDE. Se pierde el aviso
          ENTERO de ese usuario, no solo esa linea.
        - En el correo, el cliente ve el resumen cortado por la mitad.

      Es decir: la primera empresa peruana con un "&" en el nombre dejaria sin
      aviso a todos los usuarios a los que les encajara esa licitacion. Y el
      sintoma seria "no me llegan las alertas", que es el sintoma de otras
      cinco cosas.

    quote=False porque el texto va en el cuerpo, no dentro de un atributo:
    convertir las comillas ahi solo ensuciaria lo que lee el cliente.
    """
    return html.escape(str(texto or ""), quote=False)


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
            f"• {_esc(_titulo(l))}\n  {_esc(l.get('entidad'))} · {_monto(l)} · cierra {cuando}")
    if len(lics) > MAX_EN_RESUMEN:
        lineas.append(f"…y {len(lics) - MAX_EN_RESUMEN} más en tu panel.")
    return "\n".join(lineas)


def _resumen_html(lics: list[dict]) -> str:
    filas = "".join(
        f"<li><b>{_esc(_titulo(l))}</b><br>{_esc(l.get('entidad'))} — {_monto(l)}"
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


# ─── Avisos de contrato: plazos y cobros vencidos ────────
# Mismo fallo que tenian las alertas de licitaciones: win_bot los mandaba todos
# a ADMIN_ID. El dueno de un contrato no es el dueno de la plataforma, y lo que
# aqui se avisa -- un plazo que vence, una entidad que se paso del plazo de
# pago -- solo le sirve a quien tiene que actuar.

def _canales_de(usuario: dict, config: dict | None) -> list[str]:
    """Por donde se le puede escribir a este usuario ahora mismo."""
    canales = []
    if usuario.get("telegram_chat_id"):
        canales.append(CANAL_TELEGRAM)
    if usuario.get("whatsapp_estado") == "activo":
        canales.append(CANAL_WHATSAPP)
    if (config or {}).get("email_notificaciones"):
        canales.append(CANAL_EMAIL)
    return canales


async def _dueno_de_empresa(empresa_id: int) -> dict | None:
    async with connection() as conn:
        return await conn.fetchrow(
            """SELECT u.id, u.nombre, u.email, u.telegram_chat_id,
                      u.whatsapp_numero, u.whatsapp_estado
                 FROM empresas e JOIN usuarios u ON u.id = e.usuario_id
                WHERE e.id = $1 AND u.activo = TRUE""",
            empresa_id)


async def avisar_cobros_vencidos() -> dict:
    """Avisa a cada proveedor de los pagos a los que se les paso el plazo legal.

    Este es el aviso que convierte el calculo en producto: sin el, el proveedor
    solo se entera si entra a mirar, y entonces daba igual haber calculado la
    fecha. Lo que se le dice no es "te deben" sino "ya puedes reclamar", que es
    la informacion accionable.

    No se avisa durante la prorroga de 5 dias: la entidad todavia puede
    ampararse en ella, y empujar a reclamar ahi le quema credito al cliente
    para cuando de verdad tenga razon.
    """
    from win_bot.payment_tracker import pagos_vencidos

    parte = {"avisados": 0, "pagos": 0}
    por_usuario: dict[int, list] = {}

    for pago in await pagos_vencidos():
        if pago.get("en_prorroga"):
            continue
        dueno = await _dueno_de_empresa(pago["empresa_id"])
        if not dueno:
            continue
        por_usuario.setdefault(dueno["id"], []).append((dueno, pago))

    for uid, filas in por_usuario.items():
        dueno = filas[0][0]
        config = await get_config_usuario(uid)
        if not _en_horario(config):
            continue
        pagos = [p for _, p in filas]
        parte["pagos"] += len(pagos)

        lineas = []
        for p in pagos[:MAX_EN_RESUMEN]:
            lineas.append(
                f"• {p.get('concepto') or 'Cobro'} — S/ {float(p['monto'] or 0):,.2f}\n"
                f"  {p.get('entidad') or ''} · venció el "
                f"{p['fecha_limite_pago'].strftime('%d/%m/%Y')} "
                f"({p['dias_mora']} días hábiles)")
        texto = ("💰 <b>Tienes cobros con el plazo legal vencido</b>\n\n"
                 + "\n".join(lineas)
                 + "\n\nLa Ley 32069 da 10 días hábiles desde la conformidad. "
                   "Ya puedes reclamar formalmente a la entidad.")

        enviado = False
        for canal in _canales_de(dict(dueno), config):
            if canal == CANAL_TELEGRAM:
                enviado |= await _por_telegram_texto(dueno["telegram_chat_id"], texto)
            elif canal == CANAL_WHATSAPP:
                ok, _ = await whatsapp.enviar_plantilla(
                    dueno["whatsapp_numero"], PLANTILLA_AVISO,
                    [dueno.get("nombre") or "Hola", str(len(pagos)),
                     "cobros con el plazo vencido"])
                enviado |= ok
        parte["avisados"] += bool(enviado)

    log.info("Aviso de cobros vencidos: %s", parte)
    return parte


async def _por_telegram_texto(chat_id: int, texto: str) -> bool:
    """Envio suelto por Telegram. Comparte transporte con el resumen de avisos."""
    token = os.getenv("RADAR_BOT_TOKEN") or os.getenv("WIN_BOT_TOKEN")
    if not token or not chat_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": texto[:4000],
                      "parse_mode": "HTML", "disable_web_page_preview": True})
        return r.status_code == 200
    except Exception as e:
        log.error("Telegram: fallo avisando a %s: %s", chat_id, e)
        return False


# ─── Deteccion de adjudicaciones ─────────────────────────

CANAL_ADJUDICACION = "adjudicacion"

# Formas societarias peruanas. Se quitan antes de comparar porque el mismo
# proveedor aparece como "SOTOMAYOR FAM E.I.R.L." en un sitio y como
# "Sotomayor Fam EIRL" en otro, y sin esto no casaria casi nunca.
_FORMAS = (
    r"sociedad anonima cerrada",
    r"sociedad anonima abierta",
    r"sociedad anonima",
    r"empresa individual de responsabilidad limitada",
    r"sociedad comercial de responsabilidad limitada",
    r"s\s*a\s*c", r"s\s*a\s*a", r"s\s*r\s*l", r"e\s*i\s*r\s*l", r"s\s*a",
)


def _clave_empresa(nombre: str) -> str:
    """Normaliza una razon social para poder compararla.

    Sin tildes, sin puntuacion y sin forma societaria. Devuelve cadena vacia
    para lo que no tiene nombre: quien llama comprueba ese vacio, porque dos
    cadenas vacias serian "iguales" y ese es el peor falso positivo posible.
    """
    import re

    from shared.config import normalizar

    t = normalizar(nombre or "")
    t = re.sub(r"[^\w\s]", " ", t)
    for forma in _FORMAS:
        t = re.sub(r"\b" + forma + r"\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()


async def detectar_adjudicaciones() -> dict:
    """Avisa cuando el nombre del ganador coincide con el de una empresa propia.

    POR QUE ESTO EXISTE

      `check_adjudicaciones` corria cada 30 minutos, consultaba las propuestas
      enviadas, iteraba y hacia `pass`. Un TODO decia que hacia falta scrapear
      el SEACE, y el SEACE pide CAPTCHA. Pero la API OCDS publica el nombre del
      adjudicatario, asi que ya no hace falta scrapear nada: el dato llegaba y
      se estaba tirando.

    POR QUE AVISA Y NO CREA EL CONTRATO

      El cruce es por NOMBRE, no por RUC. Medido contra la API: de 2.702
      procesos resueltos, 2.664 traen el nombre del ganador y CERO traen su
      RUC -- la parte del proveedor llega con additionalIdentifiers en null.

      Un nombre normalizado es buena pista y mala prueba: dos empresas pueden
      llamarse casi igual. Crear el contrato solo seria meter datos falsos en
      la cuenta de alguien y hacerle perseguir un cobro que no existe. Decirle
      "parece que ganaste, confirmalo" cuesta un clic y no puede equivocarse
      en su contra.
    """
    parte = {"revisadas": 0, "coincidencias": 0, "avisados": 0}

    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT p.id AS propuesta_id, p.licitacion_id,
                      e.razon_social, e.usuario_id,
                      l.proveedor_ganador, l.nomenclatura, l.objeto, l.entidad,
                      l.monto_adjudicado
                 FROM propuestas p
                 JOIN empresas e ON e.id = p.empresa_id
                 JOIN licitaciones l ON l.id = p.licitacion_id
                WHERE p.estado = 'enviado'
                  AND l.proveedor_ganador IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM contratos c
                                   WHERE c.propuesta_id = p.id)""")

    por_usuario: dict[int, list] = {}
    for f in filas:
        parte["revisadas"] += 1
        mia = _clave_empresa(f["razon_social"])
        gano = _clave_empresa(f["proveedor_ganador"])
        # La comprobacion de vacio no sobra: sin ella, dos nombres que se
        # quedan en nada tras normalizar darian una coincidencia falsa y le
        # diriamos a alguien que gano un proceso que no gano.
        if not mia or mia != gano:
            continue
        parte["coincidencias"] += 1

        async with connection() as conn:
            ya = await conn.fetchval(
                """SELECT 1 FROM notificaciones_enviadas
                    WHERE usuario_id = $1 AND licitacion_id = $2 AND canal = $3""",
                f["usuario_id"], f["licitacion_id"], CANAL_ADJUDICACION)
        if ya:
            continue
        por_usuario.setdefault(f["usuario_id"], []).append(dict(f))

    for uid, ganadas in por_usuario.items():
        async with connection() as conn:
            usuario = await conn.fetchrow(
                """SELECT id, nombre, email, telegram_chat_id,
                          whatsapp_numero, whatsapp_estado
                     FROM usuarios WHERE id = $1 AND activo = TRUE""", uid)
        if not usuario:
            continue

        lineas = []
        for g in ganadas[:MAX_EN_RESUMEN]:
            monto = (f"S/ {float(g['monto_adjudicado']):,.2f}"
                     if g.get("monto_adjudicado") else "monto no publicado")
            lineas.append(f"• {_titulo(g)}")
            lineas.append(f"  {g.get('entidad') or ''} · {monto}")
        texto = (
            "🏆 <b>Parece que ganaste</b>\n\n"
            + "\n".join(lineas)
            + "\n\nEl nombre del adjudicatario coincide con el de tu empresa. "
              "Entra al panel y confírmalo para empezar a llevar el contrato."
        )

        enviado = False
        if usuario["telegram_chat_id"]:
            enviado |= await _por_telegram_texto(usuario["telegram_chat_id"], texto)
        if usuario["whatsapp_estado"] == "activo":
            ok, _ = await whatsapp.enviar_plantilla(
                usuario["whatsapp_numero"], PLANTILLA_AVISO,
                [usuario.get("nombre") or "Hola", str(len(ganadas)),
                 "procesos donde figuras como adjudicatario"])
            enviado |= ok

        if enviado:
            await anotar_envio(uid, [g["licitacion_id"] for g in ganadas],
                               CANAL_ADJUDICACION)
            parte["avisados"] += 1

    log.info("Deteccion de adjudicaciones: %s", parte)
    return parte
