"""Bot 2: LicitaPrep — Prepara propuestas y pregunta lo que falta."""
import html
import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
log = logging.getLogger("prep_bot")

from shared.archivos import ArchivoInvalido
from shared.config import format_monto
from shared.db import (
    connection,
    empresas_de,
    get_empresa,
    get_pool,
    get_preguntas_pendientes,
    get_usuario_por_telegram,
    kb_get,
    kb_set,
    responder_pregunta,
)
from shared.firma_manager import guardar_firma, obtener_todas_firmas


# ─── Autorizacion ────────────────────────────────────────
# Antes ningun comando de este bot miraba QUIEN escribia: cualquiera que diera
# con el bot veia las propuestas y los datos de TODAS las empresas, respondia
# preguntas ajenas, cambiaba estados y subia una firma a la empresa que
# quisiera. Ahora cada comando resuelve primero la cuenta vinculada al chat (el
# mismo vinculo que usa LicitaRadar, creado desde la web con un token de un solo
# uso) y todas las consultas se limitan a las empresas de esa cuenta.
#
# Un id ajeno responde exactamente lo mismo que un id que no existe: "no
# encontrado". Si respondiera "no es tuyo", el bot serviria para averiguar que
# ids existen.

MENSAJE_SIN_VINCULO = (
    "🔒 Este chat todavía no está vinculado a ninguna cuenta.\n\n"
    "Entra a tu panel de LicitaPro, ve a <b>Configuración</b> y pulsa "
    "<b>Conectar Telegram</b>. Es un clic: no tienes que copiar ningún "
    "número de identificación."
)


def esc(v) -> str:
    """Escapa para parse_mode=HTML. Razon social y objeto los escribe gente."""
    return html.escape(str(v if v is not None else ""), quote=False)


async def cuenta_de(update: Update):
    """Cuenta vinculada a quien escribe, o None.

    Se usa effective_user y no un id que venga en el texto: ese id lo pone
    Telegram, no el usuario, y por eso no se puede suplantar.
    """
    usuario = update.effective_user
    if usuario is None:
        return None
    return await get_usuario_por_telegram(usuario.id)


async def exige_cuenta(update: Update):
    """Devuelve la cuenta; si no hay, explica como vincularse y devuelve None."""
    cuenta = await cuenta_de(update)
    if cuenta:
        return cuenta
    # effective_message y no message: un boton (callback) no trae `message`
    # propio en el update, y la respuesta reventaria en vez de llegar.
    if update.effective_message is not None:
        await update.effective_message.reply_text(MENSAJE_SIN_VINCULO, parse_mode="HTML")
    return None


async def empresa_del_usuario(empresa_id: int, usuario_id: int):
    """La empresa si es de este usuario; None si no existe O si es de otro."""
    async with connection() as conn:
        return await conn.fetchrow(
            "SELECT * FROM empresas WHERE id=$1 AND usuario_id=$2",
            empresa_id, usuario_id,
        )


async def propuesta_del_usuario(propuesta_id: int, usuario_id: int):
    """La propuesta (con su licitacion) si es de una empresa del usuario."""
    async with connection() as conn:
        return await conn.fetchrow(
            """SELECT p.*, l.objeto, l.entidad FROM propuestas p
               JOIN licitaciones l ON p.licitacion_id = l.id
               JOIN empresas e ON p.empresa_id = e.id
               WHERE p.id=$1 AND e.usuario_id=$2""",
            propuesta_id, usuario_id,
        )


def _entero(texto: str) -> int | None:
    try:
        return int(texto)
    except (TypeError, ValueError):
        return None


async def _chat_del_dueno(empresa_id: int) -> int | None:
    """Chat de Telegram del dueno de la empresa, si lo vinculo.

    Los avisos de una propuesta iban a ADMIN_ID, el dueno de la PLATAFORMA: las
    preguntas sobre el banco o el DNI del representante de un cliente acababan
    en el chat de otra persona, que ademas ya no podria responderlas con /r
    porque la pregunta no es suya. Ahora van a quien puede contestarlas.
    """
    async with connection() as conn:
        return await conn.fetchval(
            """SELECT u.telegram_chat_id FROM empresas e
               JOIN usuarios u ON u.id = e.usuario_id
               WHERE e.id=$1 AND u.activo=TRUE""",
            empresa_id,
        )


async def _avisar(app, chat_id: int | None, texto: str, **kwargs) -> None:
    """Envia sin tumbar el planificador si el dueno nunca abrio este bot.

    Telegram rechaza escribir a quien no hablo antes con el bot. Eso no puede
    cortar la pasada: las demas propuestas tambien tienen que avisarse.
    """
    if not chat_id:
        return
    try:
        await app.bot.send_message(chat_id, texto, **kwargs)
    except Exception:  # noqa: BLE001
        log.warning("No se pudo avisar al chat %s", chat_id, exc_info=True)


# ─── Check for new proposals to prepare ──────────────────
async def check_nuevas_propuestas(app):
    """Revisa si hay propuestas nuevas que preparar."""
    async with connection() as conn:
        props = await conn.fetch(
            "SELECT p.*, l.objeto, l.entidad, l.monto_referencial "
            "FROM propuestas p JOIN licitaciones l ON p.licitacion_id = l.id "
            "WHERE p.estado = 'iniciado'"
        )

    for prop in props:
        await _avisar(
            app, await _chat_del_dueno(prop["empresa_id"]),
            f"📝 <b>NUEVA PROPUESTA EN PREPARACIÓN</b>\n\n"
            f"📋 {esc(prop['licitacion_id'])}\n"
            f"🏛️ {esc(prop['entidad'])}\n"
            f"📦 {esc((prop['objeto'] or '')[:150])}\n"
            f"💰 {format_monto(prop['monto_referencial']) if prop['monto_referencial'] else '—'}\n\n"
            f"⏳ Analizando bases y llenando anexos...\n"
            f"Te avisaré si necesito información.",
            parse_mode="HTML",
        )
        # Start auto-fill process
        await iniciar_autofill(prop["id"], prop["empresa_id"], app)


async def iniciar_autofill(propuesta_id: int, empresa_id: int, app):
    """Auto-fill de anexos. Pregunta lo que falta."""
    empresa = await get_empresa(empresa_id)
    if not empresa:
        return

    # Campos que necesitamos y dónde buscarlos
    campos_requeridos = [
        ("legal", "representante_legal", "¿Cuál es el nombre del representante legal?"),
        ("legal", "dni_representante", "¿Cuál es el DNI del representante legal?"),
        ("legal", "cargo_representante", "¿Cuál es el cargo del representante legal?"),
        ("legal", "partida_registral", "¿Cuál es el número de partida registral en SUNARP?"),
        ("legal", "domicilio_legal", "¿Cuál es el domicilio legal completo de la empresa?"),
        ("legal", "telefono_empresa", "¿Teléfono de contacto de la empresa?"),
        ("legal", "vigencia_poder", "¿Fecha de la vigencia de poder del representante?"),
        ("financiero", "entidad_bancaria", "¿En qué banco tiene cuenta la empresa?"),
        ("financiero", "cuenta_corriente", "¿Número de cuenta corriente? (para pagos)"),
        ("financiero", "cci", "¿Código de cuenta interbancario (CCI)?"),
    ]

    preguntas_a_hacer = []
    campos_completos = 0

    async with connection() as conn:
        for cat, clave, pregunta_text in campos_requeridos:
            # 1. Buscar en knowledge_base
            valor = await kb_get(empresa_id, cat, clave)
            if valor:
                campos_completos += 1
                continue

            # 2. Buscar en tabla empresas
            if clave in dict(empresa) and empresa.get(clave):
                await kb_set(empresa_id, cat, clave, str(empresa[clave]), "tabla_empresas")
                campos_completos += 1
                continue

            # 3. No lo tenemos → crear pregunta
            await conn.execute(
                """INSERT INTO preguntas
                (propuesta_id, empresa_id, campo_requerido, pregunta,
                 kb_categoria, kb_clave)
                VALUES ($1, $2, $3, $4, $5, $6)""",
                propuesta_id, empresa_id, clave, pregunta_text, cat, clave,
            )
            preguntas_a_hacer.append(pregunta_text)

        total_anexos = len(campos_requeridos)
        await conn.execute(
            """UPDATE propuestas SET
            anexos_completados=$2, anexos_totales=$3,
            preguntas_pendientes=$4,
            estado = CASE WHEN estado IN ('enviado','ganada','adjudicado','cancelado') THEN estado ELSE $5 END
            WHERE id=$1""",
            propuesta_id, campos_completos, total_anexos,
            len(preguntas_a_hacer),
            "preguntas_pendientes" if preguntas_a_hacer else "listo",
        )

    # Las preguntas van al dueno de la empresa, no al de la plataforma
    chat = await _chat_del_dueno(empresa_id)
    if preguntas_a_hacer and chat:
        await _avisar(
            app, chat,
            f"✅ <b>Progreso: {campos_completos}/{total_anexos} campos completados</b>\n\n"
            f"❓ Necesito {len(preguntas_a_hacer)} respuesta(s) tuyas:",
            parse_mode="HTML",
        )

        # Enviar preguntas una por una
        pendientes = await get_preguntas_pendientes(propuesta_id)
        for i, preg in enumerate(pendientes[:5], 1):
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "📝 Responder",
                    callback_data=f"resp_{preg['id']}",
                )
            ]])
            await _avisar(
                app, chat,
                f"❓ <b>Pregunta {i} de {len(preguntas_a_hacer)}:</b>\n\n"
                f"{esc(preg['pregunta'])}\n\n"
                f"<i>Responde con /r {preg['id']} [tu respuesta]</i>",
                reply_markup=kb,
                parse_mode="HTML",
            )
    elif not preguntas_a_hacer and chat:
        await _avisar(
            app, chat,
            f"✅ <b>¡Todos los campos completados!</b> ({campos_completos}/{total_anexos})\n\n"
            f"🧠 Ya tengo toda la información necesaria.\n\n"
            f"Usa /aprobar {propuesta_id} para ver cómo descargar el expediente.",
            parse_mode="HTML",
        )


# ─── Handlers ────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Sin cuenta no se ensena el menu: no sirve de nada y anuncia comandos que
    # luego se negarian. Se explica como vincular, igual que en LicitaRadar.
    if not await exige_cuenta(update):
        return
    await update.message.reply_text(
        "📝 <b>LicitaPrep</b> — Bot de preparación de propuestas\n\n"
        "Cuando decides licitar en @LicitaRadarBot, yo preparo todo:\n"
        "• Auto-lleno todos los anexos con datos de tu empresa\n"
        "• Si falta algo, te pregunto y APRENDO para siempre\n"
        "• El expediente lo descargas desde tu panel web\n\n"
        "Comandos:\n"
        "/estado — Ver propuestas en preparación\n"
        "/r [id] [respuesta] — Responder una pregunta\n"
        "/datos — Ver datos de tus empresas\n"
        "/aprobar [id] — Cómo descargar el expediente\n"
        "/firma — Firmas, sellos y logos de tus empresas",
        parse_mode="HTML",
    )


async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    usuario = await exige_cuenta(update)
    if not usuario:
        return
    async with connection() as conn:
        props = await conn.fetch(
            """SELECT p.*, l.objeto, l.entidad, e.razon_social
            FROM propuestas p
            JOIN licitaciones l ON p.licitacion_id = l.id
            JOIN empresas e ON p.empresa_id = e.id
            WHERE e.usuario_id = $1
              AND p.estado NOT IN ('enviado','cancelado')
            ORDER BY p.created_at DESC LIMIT 10""",
            usuario["id"],
        )

    if not props:
        await update.message.reply_text("📭 No hay propuestas en preparación.")
        return

    for prop in props:
        estado_emoji = {
            "iniciado": "🔄", "preguntas_pendientes": "❓",
            "listo": "✅", "revisando": "👁️",
        }.get(prop["estado"], "📝")

        await update.message.reply_text(
            f"{estado_emoji} <b>Propuesta #{prop['id']}</b>\n"
            f"📋 {esc(prop['entidad'])}\n"
            f"📦 {esc((prop['objeto'] or '')[:100])}\n"
            f"🏢 {esc(prop['razon_social'])}\n"
            f"📊 Campos: {prop['anexos_completados']}/{prop['anexos_totales']}\n"
            f"❓ Preguntas pendientes: {prop['preguntas_pendientes']}\n"
            f"Estado: {esc(prop['estado'])}",
            parse_mode="HTML",
        )


async def cmd_responder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Responder una pregunta: /r [id] [respuesta]"""
    usuario = await exige_cuenta(update)
    if not usuario:
        return
    if len(ctx.args) < 2:
        await update.message.reply_text("Uso: /r [id_pregunta] [tu respuesta]")
        return

    preg_id = _entero(ctx.args[0])
    if preg_id is None:
        await update.message.reply_text("❌ ID de pregunta inválido")
        return

    # La propiedad se comprueba ANTES de escribir: responder_pregunta guarda la
    # respuesta en la base de conocimiento de la empresa, asi que una pregunta
    # ajena permitiria cambiarle a otro el CCI donde le pagan.
    async with connection() as conn:
        preg = await conn.fetchrow(
            """SELECT pr.propuesta_id FROM preguntas pr
               JOIN empresas e ON pr.empresa_id = e.id
               WHERE pr.id=$1 AND e.usuario_id=$2""",
            preg_id, usuario["id"],
        )
    if not preg:
        await update.message.reply_text("❌ Pregunta no encontrada")
        return

    respuesta = " ".join(ctx.args[1:])
    await responder_pregunta(preg_id, respuesta)

    # Verificar si quedan más preguntas de esa propuesta
    async with connection() as conn:
        pendientes = await conn.fetchval(
            "SELECT COUNT(*) FROM preguntas WHERE propuesta_id=$1 AND respondida=FALSE",
            preg["propuesta_id"],
        )
        await conn.execute(
            "UPDATE propuestas SET preguntas_pendientes=$2 WHERE id=$1",
            preg["propuesta_id"], pendientes,
        )

        if pendientes == 0:
            await conn.execute(
                "UPDATE propuestas SET estado='listo' WHERE id=$1 "
                "AND estado NOT IN ('enviado','ganada','adjudicado','cancelado')",
                preg["propuesta_id"],
            )
            await update.message.reply_text(
                f"✅ ¡Respuesta guardada! 🧠 Aprendido para siempre.\n\n"
                f"🎉 <b>¡Todas las preguntas respondidas!</b>\n"
                f"Usa /aprobar {preg['propuesta_id']} para ver cómo descargar el expediente.",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(
                f"✅ Respuesta guardada. 🧠 Aprendido.\n"
                f"Quedan {pendientes} pregunta(s) pendiente(s).",
            )


async def cmd_datos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    usuario = await exige_cuenta(update)
    if not usuario:
        return
    empresas = await empresas_de(usuario["id"])
    if not empresas:
        await update.message.reply_text("No hay empresas registradas.")
        return
    for emp in empresas:
        rubros_text = ', '.join(emp['rubros'][:3]) if emp['rubros'] else '—'
        try:
            await update.message.reply_text(
                f"🏢 {emp['razon_social']}\n"
                f"RUC: {emp['ruc'] or '—'}\n"
                f"Rep. Legal: {emp['representante_legal'] or '⚠️ Sin registrar'}\n"
                f"DNI: {emp.get('dni_representante') or '—'}\n"
                f"Dirección: {emp.get('direccion') or '—'}\n"
                f"Teléfono: {emp.get('telefono') or '—'}\n"
                f"Email: {emp['email'] or '—'}\n"
                f"Rubros: {rubros_text}\n"
                f"RNP: {emp['rnp_numero'] or '⚠️ Sin registrar'}\n\n"
                # Antes sugeria /editar_empresa, un comando que no existe.
                f"Para editar estos datos entra a tu panel web, en Empresas.",
            )
        except Exception as e:  # noqa: BLE001
            await update.message.reply_text(
                f"🏢 {emp['razon_social']} (RUC: {emp['ruc'] or '—'})\n"
                f"Error: {str(e)[:100]}"
            )


async def cmd_aprobar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Dice la verdad sobre el expediente: se arma y descarga desde el panel.

    Antes ponia la propuesta en 'listo' y prometia un ZIP y un /descargar que
    no existian: el cliente esperaba un archivo que nunca llegaba, y el estado
    cambiaba aunque no se hubiera generado nada. El panel web ya arma el
    expediente completo (anexos llenos, cotizacion, paquete PDF) y comprueba la
    propiedad en cada ruta, asi que aqui solo se le indica el camino, sin
    tocar el estado.
    """
    usuario = await exige_cuenta(update)
    if not usuario:
        return
    if not ctx.args:
        await update.message.reply_text("Uso: /aprobar [id_propuesta]")
        return

    prop_id = _entero(ctx.args[0])
    prop = await propuesta_del_usuario(prop_id, usuario["id"]) if prop_id is not None else None
    if not prop:
        await update.message.reply_text("❌ Propuesta no encontrada")
        return

    if prop["preguntas_pendientes"] > 0:
        await update.message.reply_text(
            f"⚠️ Aún hay {prop['preguntas_pendientes']} preguntas sin responder.\n"
            f"Usa /estado para ver cuáles faltan."
        )
        return

    await update.message.reply_text(
        f"📄 <b>Propuesta #{prop_id}</b>\n\n"
        f"📋 {esc(prop['entidad'])}\n"
        f"📦 {esc((prop['objeto'] or '')[:100])}\n\n"
        f"El expediente se arma y se descarga desde tu panel web:\n"
        f"entra a <b>Propuestas</b> → <b>#{prop_id}</b> y pulsa "
        f"<b>Descargar</b>.",
        parse_mode="HTML",
    )


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await exige_cuenta(update):
        return

    if query.data.startswith("resp_"):
        preg_id = query.data.replace("resp_", "")
        await query.message.reply_text(
            f"📝 Responde con:\n<code>/r {esc(preg_id)} [tu respuesta]</code>",
            parse_mode="HTML",
        )


# ─── Firma/Sello/Logo handlers ──────────────────────────
async def cmd_firma(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Gestionar firmas: /firma [empresa_id] o /firma para ver estado."""
    usuario = await exige_cuenta(update)
    if not usuario:
        return

    if not ctx.args:
        # Solo las empresas de quien pregunta: antes salian las de todos.
        empresas = await empresas_de(usuario["id"])
        texto = "📝 <b>Firmas, sellos y logos registrados:</b>\n\n"
        for emp in empresas:
            firmas = await obtener_todas_firmas(emp["id"])
            firma_ok = "✅" if firmas["firma"]["existe"] else "❌"
            sello_ok = "✅" if firmas["sello"]["existe"] else "❌"
            logo_ok = "✅" if firmas["logo"]["existe"] else "❌"
            texto += (
                f"🏢 <b>{esc(emp['razon_social'])}</b> (ID: {emp['id']})\n"
                f"  Firma: {firma_ok}  Sello: {sello_ok}  Logo: {logo_ok}\n\n"
            )
        texto += (
            "Para subir, envía una <b>imagen</b> con el caption:\n"
            "<code>firma [empresa_id]</code> — Firma del representante\n"
            "<code>sello [empresa_id]</code> — Sello de la empresa\n"
            "<code>logo [empresa_id]</code> — Logo de la empresa\n\n"
            "Ejemplo: envía foto con caption <code>firma 1</code>"
        )
        await update.message.reply_text(texto, parse_mode="HTML")
        return

    emp_id = _entero(ctx.args[0])
    if emp_id is None:
        await update.message.reply_text("Uso: /firma [empresa_id]")
        return
    emp = await empresa_del_usuario(emp_id, usuario["id"])
    if not emp:
        await update.message.reply_text("❌ Empresa no encontrada")
        return
    firmas = await obtener_todas_firmas(emp_id)
    texto = f"🏢 <b>{esc(emp['razon_social'])}</b>\n\n"
    for tipo, info in firmas.items():
        # Sin la ruta del servidor: no le sirve al cliente y describe el disco.
        estado = "✅ Registrado" if info["existe"] else "❌ No registrado"
        texto += f"  {tipo}: {estado}\n"
    await update.message.reply_text(texto, parse_mode="HTML")


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Maneja fotos enviadas — para subir firmas/sellos/logos."""
    caption = (update.message.caption or "").strip().lower()
    if not caption:
        return

    # Parsear caption: "firma 1", "sello 2", "logo 1"
    parts = caption.split()
    if len(parts) < 2 or parts[0] not in ("firma", "sello", "logo"):
        return

    usuario = await exige_cuenta(update)
    if not usuario:
        return

    tipo = parts[0]
    empresa_id = _entero(parts[1])
    if empresa_id is None:
        await update.message.reply_text("❌ Formato: <code>firma [empresa_id]</code>", parse_mode="HTML")
        return

    # La firma del representante legal es lo que da validez a una declaracion
    # jurada: poder ponerla en la empresa de otro es poder firmar por el.
    emp = await empresa_del_usuario(empresa_id, usuario["id"])
    if not emp:
        await update.message.reply_text("❌ Empresa no encontrada")
        return

    # Se baja a memoria y no a un temporal: guardar_firma valida y re-codifica
    # los bytes (shared.archivos), asi que no hace falta tocar el disco con lo
    # que mando el cliente antes de saber si es una imagen.
    photo = update.message.photo[-1]  # Mayor resolución
    file = await ctx.bot.get_file(photo.file_id)
    datos = bytes(await file.download_as_bytearray())

    try:
        await guardar_firma(empresa_id, tipo, datos)
    except ArchivoInvalido as e:
        # El mensaje de ArchivoInvalido esta escrito para el usuario.
        await update.message.reply_text(f"❌ {e}")
        return

    await update.message.reply_text(
        f"✅ <b>{tipo.capitalize()} guardado</b> para {esc(emp['razon_social'])}\n\n"
        f"Se usará automáticamente en todos los documentos generados.",
        parse_mode="HTML",
    )


# ─── Main ────────────────────────────────────────────────
async def post_init(application: Application):
    """Inicializa DB pool dentro del event loop correcto."""
    await get_pool()
    log.info("DB pool initialized in bot event loop")


def main():
    token = os.getenv("PREP_BOT_TOKEN")
    if not token:
        log.error("PREP_BOT_TOKEN not set!")
        return

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("estado", cmd_estado))
    app.add_handler(CommandHandler("r", cmd_responder))
    app.add_handler(CommandHandler("datos", cmd_datos))
    app.add_handler(CommandHandler("aprobar", cmd_aprobar))
    app.add_handler(CommandHandler("firma", cmd_firma))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Check for new proposals every 30 seconds
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        check_nuevas_propuestas, "interval", seconds=30,
        args=[app], id="check_propuestas",
    )
    scheduler.start()

    log.info("📝 LicitaPrep Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
