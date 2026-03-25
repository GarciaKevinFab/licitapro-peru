"""Bot 2: LicitaPrep — Prepara propuestas y pregunta lo que falta."""
import os
import logging
import asyncio
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
from shared.config import format_monto, format_fecha, ADMIN_ID
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
                f"📝 **NUEVA PROPUESTA EN PREPARACIÓN**\n\n"
                f"📋 {prop['licitacion_id']}\n"
                f"🏛️ {prop['entidad']}\n"
                f"📦 {prop['objeto'][:150]}\n"
                f"💰 {format_monto(prop['monto_referencial']) if prop['monto_referencial'] else '—'}\n\n"
                f"⏳ Analizando bases y llenando anexos...\n"
                f"Te avisaré si necesito información.",
                parse_mode="Markdown",
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
            f"✅ **Progreso: {campos_completos}/{total_anexos} campos completados**\n\n"
            f"❓ Necesito {len(preguntas_a_hacer)} respuesta(s) tuyas:",
            parse_mode="Markdown",
        )

        # Enviar preguntas una por una
        pendientes = await get_preguntas_pendientes(propuesta_id)
        for i, preg in enumerate(pendientes[:5], 1):
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    f"📝 Responder",
                    callback_data=f"resp_{preg['id']}",
                )
            ]])
            await app.bot.send_message(
                ADMIN_ID,
                f"❓ **Pregunta {i} de {len(preguntas_a_hacer)}:**\n\n"
                f"{preg['pregunta']}\n\n"
                f"_Responde con /r {preg['id']} [tu respuesta]_",
                reply_markup=kb,
                parse_mode="Markdown",
            )
    elif not preguntas_a_hacer and ADMIN_ID:
        await app.bot.send_message(
            ADMIN_ID,
            f"✅ **¡Todos los campos completados!** ({campos_completos}/{total_anexos})\n\n"
            f"🧠 Ya tengo toda la información necesaria.\n"
            f"📄 Generando expediente...\n\n"
            f"Usa /aprobar {propuesta_id} cuando quieras generar el ZIP final.",
            parse_mode="Markdown",
        )


# ─── Handlers ────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 **LicitaPrep** — Bot de preparación de propuestas\n\n"
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
        parse_mode="Markdown",
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
            f"{estado_emoji} **Propuesta #{prop['id']}**\n"
            f"📋 {prop['entidad']}\n"
            f"📦 {prop['objeto'][:100]}\n"
            f"🏢 {prop['razon_social']}\n"
            f"📊 Campos: {prop['anexos_completados']}/{prop['anexos_totales']}\n"
            f"❓ Preguntas pendientes: {prop['preguntas_pendientes']}\n"
            f"Estado: {prop['estado']}",
            parse_mode="Markdown",
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
                    f"🎉 **¡Todas las preguntas respondidas!**\n"
                    f"Usa /aprobar {preg['propuesta_id']} para generar el expediente.",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    f"✅ Respuesta guardada. 🧠 Aprendido.\n"
                    f"Quedan {pendientes} pregunta(s) pendiente(s).",
                )


async def cmd_datos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    empresas = await get_empresas_activas()
    for emp in empresas:
        await update.message.reply_text(
            f"🏢 **{emp['razon_social']}**\n"
            f"RUC: {emp['ruc'] or '—'}\n"
            f"Representante: {emp['representante_legal'] or '⚠️ Sin registrar'}\n"
            f"Email: {emp['email'] or '—'}\n"
            f"Rubros: {', '.join(emp['rubros']) if emp['rubros'] else '—'}\n"
            f"RNP: {emp['rnp_numero'] or '⚠️ Sin registrar'}\n\n"
            f"_Editar: /editar\\_empresa {emp['id']} [campo] [valor]_",
            parse_mode="Markdown",
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
        f"📄 **Generando expediente para Propuesta #{prop_id}**\n\n"
        f"📋 {prop['entidad']}\n"
        f"📦 {prop['objeto'][:100]}\n\n"
        f"⏳ Generando documentos...\n"
        f"(En la versión completa, aquí se genera el ZIP con todos los anexos)\n\n"
        f"📎 Expediente estará disponible en /descargar {prop_id}",
        parse_mode="Markdown",
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
            f"📝 Responde con:\n`/r {preg_id} [tu respuesta]`",
            parse_mode="Markdown",
        )


# ─── Firma/Sello/Logo handlers ──────────────────────────
async def cmd_firma(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Gestionar firmas: /firma [empresa_id] o /firma para ver estado."""
    # Si no hay args, mostrar estado de firmas de todas las empresas
    empresas = await get_empresas_activas()

    if not ctx.args:
        texto = "📝 **Firmas, sellos y logos registrados:**\n\n"
        for emp in empresas:
            firmas = await obtener_todas_firmas(emp["id"])
            firma_ok = "✅" if firmas["firma"]["existe"] else "❌"
            sello_ok = "✅" if firmas["sello"]["existe"] else "❌"
            logo_ok = "✅" if firmas["logo"]["existe"] else "❌"
            texto += (
                f"🏢 **{emp['razon_social']}** (ID: {emp['id']})\n"
                f"  Firma: {firma_ok}  Sello: {sello_ok}  Logo: {logo_ok}\n\n"
            )
        texto += (
            "Para subir, envía una **imagen** con el caption:\n"
            "`firma [empresa_id]` — Firma del representante\n"
            "`sello [empresa_id]` — Sello de la empresa\n"
            "`logo [empresa_id]` — Logo de la empresa\n\n"
            "Ejemplo: envía foto con caption `firma 1`"
        )
        await update.message.reply_text(texto, parse_mode="Markdown")
        return

    # Si tiene args, mostrar firma específica
    try:
        emp_id = int(ctx.args[0])
        emp = await get_empresa(emp_id)
        if not emp:
            await update.message.reply_text("❌ Empresa no encontrada")
            return
        firmas = await obtener_todas_firmas(emp_id)
        texto = f"🏢 **{emp['razon_social']}**\n\n"
        for tipo, info in firmas.items():
            estado = f"✅ {info['path']}" if info["existe"] else "❌ No registrado"
            texto += f"  {tipo}: {estado}\n"
        await update.message.reply_text(texto, parse_mode="Markdown")
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
        await update.message.reply_text("❌ Formato: `firma [empresa_id]`", parse_mode="Markdown")
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
        f"✅ **{tipo.capitalize()} guardado** para {emp['razon_social']}\n\n"
        f"📎 {path}\n"
        f"Se usará automáticamente en todos los documentos generados.",
        parse_mode="Markdown",
    )


# ─── Main ────────────────────────────────────────────────
def main():
    token = os.getenv("PREP_BOT_TOKEN")
    if not token:
        log.error("PREP_BOT_TOKEN not set!")
        return

    loop = asyncio.new_event_loop()
    loop.run_until_complete(get_pool())

    app = Application.builder().token(token).build()

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
