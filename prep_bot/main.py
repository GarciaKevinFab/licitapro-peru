"""Bot 2: LicitaPrep — Prepara propuestas y pregunta lo que falta."""
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("prep_bot")

from shared.db import (
    get_pool, get_preguntas_pendientes, responder_pregunta,
    kb_get, kb_set, get_empresa, get_empresas_activas, connection,
)
from shared.config import format_monto, ADMIN_ID
from shared.firma_manager import guardar_firma, obtener_todas_firmas


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
        if ADMIN_ID:
            await app.bot.send_message(
                ADMIN_ID,
                f"📝 <b>NUEVA PROPUESTA EN PREPARACIÓN</b>\n\n"
                f"📋 {prop['licitacion_id']}\n"
                f"🏛️ {prop['entidad']}\n"
                f"📦 {prop['objeto'][:150]}\n"
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
            preguntas_pendientes=$4, estado=$5
            WHERE id=$1""",
            propuesta_id, campos_completos, total_anexos,
            len(preguntas_a_hacer),
            "preguntas_pendientes" if preguntas_a_hacer else "listo",
        )

    # Enviar preguntas al usuario
    if preguntas_a_hacer and ADMIN_ID:
        await app.bot.send_message(
            ADMIN_ID,
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
            await app.bot.send_message(
                ADMIN_ID,
                f"❓ <b>Pregunta {i} de {len(preguntas_a_hacer)}:</b>\n\n"
                f"{preg['pregunta']}\n\n"
                f"<i>Responde con /r {preg['id']} [tu respuesta]</i>",
                reply_markup=kb,
                parse_mode="HTML",
            )
    elif not preguntas_a_hacer and ADMIN_ID:
        await app.bot.send_message(
            ADMIN_ID,
            f"✅ <b>¡Todos los campos completados!</b> ({campos_completos}/{total_anexos})\n\n"
            f"🧠 Ya tengo toda la información necesaria.\n"
            f"📄 Generando expediente...\n\n"
            f"Usa /aprobar {propuesta_id} cuando quieras generar el ZIP final.",
            parse_mode="HTML",
        )


# ─── Handlers ────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 <b>LicitaPrep</b> — Bot de preparación de propuestas\n\n"
        "Cuando decides licitar en @LicitaRadarBot, yo preparo todo:\n"
        "• Auto-lleno todos los anexos con datos de tu empresa\n"
        "• Si falta algo, te pregunto y APRENDO para siempre\n"
        "• Genero propuesta técnica y económica\n"
        "• Armo expediente ZIP listo para SEACE\n\n"
        "Comandos:\n"
        "/estado — Ver propuestas en preparación\n"
        "/r [id] [respuesta] — Responder una pregunta\n"
        "/datos — Ver datos de tus empresas\n"
        "/aprobar [id] — Generar expediente final\n"
        "/equipo — Gestionar equipo técnico",
        parse_mode="HTML",
    )


async def cmd_estado(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    async with connection() as conn:
        props = await conn.fetch(
            """SELECT p.*, l.objeto, l.entidad, e.razon_social
            FROM propuestas p 
            JOIN licitaciones l ON p.licitacion_id = l.id
            JOIN empresas e ON p.empresa_id = e.id
            WHERE p.estado NOT IN ('enviado','cancelado')
            ORDER BY p.created_at DESC LIMIT 10"""
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
            f"📋 {prop['entidad']}\n"
            f"📦 {prop['objeto'][:100]}\n"
            f"🏢 {prop['razon_social']}\n"
            f"📊 Campos: {prop['anexos_completados']}/{prop['anexos_totales']}\n"
            f"❓ Preguntas pendientes: {prop['preguntas_pendientes']}\n"
            f"Estado: {prop['estado']}",
            parse_mode="HTML",
        )


async def cmd_responder(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Responder una pregunta: /r [id] [respuesta]"""
    if len(ctx.args) < 2:
        await update.message.reply_text("Uso: /r [id_pregunta] [tu respuesta]")
        return
    
    try:
        preg_id = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID de pregunta inválido")
        return
    
    respuesta = " ".join(ctx.args[1:])
    await responder_pregunta(preg_id, respuesta)
    
    # Verificar si quedan más preguntas de esa propuesta
    async with connection() as conn:
        preg = await conn.fetchrow("SELECT propuesta_id FROM preguntas WHERE id=$1", preg_id)
        if preg:
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
                    "UPDATE propuestas SET estado='listo' WHERE id=$1",
                    preg["propuesta_id"],
                )
                await update.message.reply_text(
                    f"✅ ¡Respuesta guardada! 🧠 Aprendido para siempre.\n\n"
                    f"🎉 <b>¡Todas las preguntas respondidas!</b>\n"
                    f"Usa /aprobar {preg['propuesta_id']} para generar el expediente.",
                    parse_mode="HTML",
                )
            else:
                await update.message.reply_text(
                    f"✅ Respuesta guardada. 🧠 Aprendido.\n"
                    f"Quedan {pendientes} pregunta(s) pendiente(s).",
                )


async def cmd_datos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    empresas = await get_empresas_activas()
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
                f"Editar: /editar_empresa {emp['id']} [campo] [valor]",
            )
        except Exception as e:
            await update.message.reply_text(
                f"🏢 {emp['razon_social']} (RUC: {emp['ruc'] or '—'})\n"
                f"Error: {str(e)[:100]}"
            )


async def cmd_aprobar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /aprobar [id_propuesta]")
        return
    
    prop_id = int(ctx.args[0])
    async with connection() as conn:
        prop = await conn.fetchrow(
            "SELECT p.*, l.objeto, l.entidad FROM propuestas p "
            "JOIN licitaciones l ON p.licitacion_id = l.id WHERE p.id=$1",
            prop_id,
        )
    
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
        f"📄 <b>Generando expediente para Propuesta #{prop_id}</b>\n\n"
        f"📋 {prop['entidad']}\n"
        f"📦 {prop['objeto'][:100]}\n\n"
        f"⏳ Generando documentos...\n"
        f"(En la versión completa, aquí se genera el ZIP con todos los anexos)\n\n"
        f"📎 Expediente estará disponible en /descargar {prop_id}",
        parse_mode="HTML",
    )
    
    async with connection() as conn:
        await conn.execute(
            "UPDATE propuestas SET estado='listo' WHERE id=$1", prop_id
        )


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data.startswith("resp_"):
        preg_id = query.data.replace("resp_", "")
        await query.message.reply_text(
            f"📝 Responde con:\n<code>/r {preg_id} [tu respuesta]</code>",
            parse_mode="HTML",
        )


# ─── Firma/Sello/Logo handlers ──────────────────────────
async def cmd_firma(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Gestionar firmas: /firma [empresa_id] o /firma para ver estado."""
    # Si no hay args, mostrar estado de firmas de todas las empresas
    empresas = await get_empresas_activas()

    if not ctx.args:
        texto = "📝 <b>Firmas, sellos y logos registrados:</b>\n\n"
        for emp in empresas:
            firmas = await obtener_todas_firmas(emp["id"])
            firma_ok = "✅" if firmas["firma"]["existe"] else "❌"
            sello_ok = "✅" if firmas["sello"]["existe"] else "❌"
            logo_ok = "✅" if firmas["logo"]["existe"] else "❌"
            texto += (
                f"🏢 <b>{emp['razon_social']}</b> (ID: {emp['id']})\n"
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

    # Si tiene args, mostrar firma específica
    try:
        emp_id = int(ctx.args[0])
        emp = await get_empresa(emp_id)
        if not emp:
            await update.message.reply_text("❌ Empresa no encontrada")
            return
        firmas = await obtener_todas_firmas(emp_id)
        texto = f"🏢 <b>{emp['razon_social']}</b>\n\n"
        for tipo, info in firmas.items():
            estado = f"✅ {info['path']}" if info["existe"] else "❌ No registrado"
            texto += f"  {tipo}: {estado}\n"
        await update.message.reply_text(texto, parse_mode="HTML")
    except ValueError:
        await update.message.reply_text("Uso: /firma [empresa_id]")


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Maneja fotos enviadas — para subir firmas/sellos/logos."""
    caption = (update.message.caption or "").strip().lower()
    if not caption:
        return

    # Parsear caption: "firma 1", "sello 2", "logo 1"
    parts = caption.split()
    if len(parts) < 2 or parts[0] not in ("firma", "sello", "logo"):
        return

    tipo = parts[0]
    try:
        empresa_id = int(parts[1])
    except ValueError:
        await update.message.reply_text("❌ Formato: <code>firma [empresa_id]</code>", parse_mode="HTML")
        return

    # Verificar empresa
    emp = await get_empresa(empresa_id)
    if not emp:
        await update.message.reply_text("❌ Empresa no encontrada")
        return

    # Descargar foto
    photo = update.message.photo[-1]  # Mayor resolución
    file = await ctx.bot.get_file(photo.file_id)

    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    tmp.close()
    await file.download_to_drive(tmp.name)

    # Guardar firma
    path = await guardar_firma(empresa_id, tipo, tmp.name)

    # Limpiar temporal
    os.unlink(tmp.name)

    await update.message.reply_text(
        f"✅ <b>{tipo.capitalize()} guardado</b> para {emp['razon_social']}\n\n"
        f"📎 {path}\n"
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
