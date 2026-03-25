"""Bot 1: LicitaRadar — Detecta, filtra y analiza licitaciones."""
import os
import logging
import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("radar_bot")

from shared.db import (
    get_pool, get_licitaciones_nuevas, marcar_notificada,
    get_config, update_config, get_empresas_activas,
)
from shared.config import (
    format_monto, format_fecha, dias_restantes, prioridad_emoji,
    DEPARTAMENTOS, TIPOS_PROCEDIMIENTO, ADMIN_ID,
)
from radar_bot.scrapers.seace import scrape_seace
from radar_bot.scrapers.orchestrator import run_all_scrapers, format_scraping_report


# ─── Formatters ──────────────────────────────────────────
def format_licitacion_alert(lic) -> tuple[str, InlineKeyboardMarkup]:
    dias = dias_restantes(lic["fecha_cierre"])
    score = lic.get("score_viabilidad") or 0
    emoji = prioridad_emoji(score, dias)
    
    dias_text = f"({dias} días)" if dias else ""
    score_text = f"\n🎯 Viabilidad: {score:.0f}%" if score else ""
    monto_text = format_monto(lic["monto_referencial"]) if lic["monto_referencial"] else "No especificado"
    tipo_text = TIPOS_PROCEDIMIENTO.get(lic["tipo"], lic["tipo"] or "—")
    
    text = (
        f"{emoji} **{tipo_text}**{score_text}\n\n"
        f"📋 {lic['nomenclatura'] or lic['id']}\n"
        f"🏛️ {lic['entidad']}\n"
        f"📦 {lic['objeto'][:200]}\n"
        f"💰 {monto_text}\n"
        f"📅 Cierre: {format_fecha(lic['fecha_cierre'])} {dias_text}\n"
        f"📍 {lic['departamento'] or '—'}"
    )
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🚀 Licitar", callback_data=f"licitar_{lic['id']}"),
            InlineKeyboardButton("📄 Bases", callback_data=f"bases_{lic['id']}"),
        ],
        [
            InlineKeyboardButton("💰 Precio", callback_data=f"precio_{lic['id']}"),
            InlineKeyboardButton("❌ Pasar", callback_data=f"pasar_{lic['id']}"),
        ],
    ])
    return text, keyboard


# ─── Handlers ────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update_config(user_id)
    await update.message.reply_text(
        "🔍 **LicitaRadar** — Bot de detección de licitaciones\n\n"
        "Escaneo 12 fuentes a nivel nacional cada hora.\n"
        "Te notifico solo las que te convienen.\n\n"
        "Comandos principales:\n"
        "/hoy — Licitaciones nuevas del día\n"
        "/buscar [keyword] — Buscar por palabra clave\n"
        "/config — Configurar regiones y filtros\n"
        "/licitar [id] — Decidir participar\n"
        "/stats — Estadísticas de detección\n\n"
        "🟢 Sistema activo. Escaneos cada 60 minutos.",
        parse_mode="Markdown",
    )


async def cmd_hoy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lics = await get_licitaciones_nuevas(limit=10)
    if not lics:
        await update.message.reply_text("📭 No hay licitaciones nuevas relevantes hoy. Sigo buscando...")
        return
    
    await update.message.reply_text(
        f"📊 **Resumen del día** — {datetime.now().strftime('%d/%m/%Y')}\n"
        f"Se encontraron {len(lics)} licitaciones relevantes:",
        parse_mode="Markdown",
    )
    
    for lic in lics[:10]:
        text, kb = format_licitacion_alert(lic)
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")
        await marcar_notificada(lic["id"])


async def cmd_buscar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Uso: /buscar [palabra clave]\nEjemplo: /buscar sistemas")
        return
    
    keyword = " ".join(ctx.args)
    from shared.db import connection
    async with connection() as conn:
        lics = await conn.fetch(
            """SELECT * FROM licitaciones 
            WHERE (objeto ILIKE $1 OR entidad ILIKE $1)
            AND descartado = FALSE
            ORDER BY fecha_cierre ASC NULLS LAST
            LIMIT 10""",
            f"%{keyword}%",
        )
    
    if not lics:
        await update.message.reply_text(f"No se encontraron licitaciones para '{keyword}'")
        return
    
    await update.message.reply_text(f"🔍 {len(lics)} resultados para '{keyword}':")
    for lic in lics[:5]:
        text, kb = format_licitacion_alert(lic)
        await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")


async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    config = await get_config(user_id)
    regiones = ", ".join(config["regiones"]) if config["regiones"] else "Todas (sin filtro)"
    keywords = ", ".join(config["keywords"][:10]) if config["keywords"] else "Ninguna"
    
    await update.message.reply_text(
        f"⚙️ **Configuración actual**\n\n"
        f"📍 Regiones: {regiones}\n"
        f"🔑 Keywords: {keywords}{'...' if len(config['keywords']) > 10 else ''}\n"
        f"💰 Monto: {format_monto(config['monto_min'])} — {format_monto(config['monto_max'])}\n\n"
        f"Para modificar:\n"
        f"`/region add Cusco`\n"
        f"`/region remove Puno`\n"
        f"`/keyword add 'sistema académico'`\n"
        f"`/monto min 5000 max 300000`",
        parse_mode="Markdown",
    )


async def cmd_region(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if len(ctx.args) < 2:
        await update.message.reply_text(
            "Uso:\n/region add Cusco\n/region remove Puno\n/region list\n\n"
            f"Departamentos válidos: {', '.join(DEPARTAMENTOS)}"
        )
        return
    
    action = ctx.args[0].lower()
    user_id = update.effective_user.id
    config = await get_config(user_id)
    regiones = list(config["regiones"]) if config["regiones"] else []
    
    if action == "add":
        region = " ".join(ctx.args[1:])
        if region not in DEPARTAMENTOS:
            await update.message.reply_text(f"❌ '{region}' no es un departamento válido")
            return
        if region not in regiones:
            regiones.append(region)
            await update_config(user_id, regiones=regiones)
        await update.message.reply_text(f"✅ Región '{region}' agregada. Monitoreando: {', '.join(regiones)}")
    elif action == "remove":
        region = " ".join(ctx.args[1:])
        regiones = [r for r in regiones if r != region]
        await update_config(user_id, regiones=regiones)
        await update.message.reply_text(f"✅ Región '{region}' removida.")
    elif action == "list":
        await update.message.reply_text(f"📍 Regiones activas: {', '.join(regiones) or 'Todas'}")


async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data.startswith("licitar_"):
        lid = data.replace("licitar_", "")
        # Crear propuesta en DB y notificar al PrepBot
        from shared.db import crear_propuesta, get_empresa
        empresa = await get_empresa(1)  # Default: K&A
        prop_id = await crear_propuesta(lid, 1)
        
        await query.edit_message_text(
            f"🚀 **¡ACTIVADO!** Propuesta #{prop_id}\n\n"
            f"📋 Licitación: {lid}\n"
            f"🏢 Empresa: {empresa['razon_social']}\n\n"
            f"👉 Revisa @LicitaPrepBot para el progreso\n"
            f"El bot de preparación está trabajando...",
            parse_mode="Markdown",
        )
    
    elif data.startswith("pasar_"):
        lid = data.replace("pasar_", "")
        from shared.db import connection
        async with connection() as conn:
            await conn.execute("UPDATE licitaciones SET descartado=TRUE WHERE id=$1", lid)
        await query.edit_message_text("❌ Licitación descartada.")
    
    elif data.startswith("bases_"):
        lid = data.replace("bases_", "")
        from shared.db import connection
        async with connection() as conn:
            lic = await conn.fetchrow("SELECT * FROM licitaciones WHERE id=$1", lid)
        if lic and lic["url"]:
            await query.message.reply_text(f"📎 Bases: {lic['url']}")
        else:
            await query.message.reply_text("⚠️ URL de bases no disponible")


# ─── Scheduled Tasks ─────────────────────────────────────
async def scheduled_scrape(app: Application):
    """Ejecuta TODOS los scrapers cada hora y envía alertas."""
    log.info("Iniciando scraping programado de 12 fuentes...")
    try:
        results = await run_all_scrapers()
        
        # Enviar reporte de scraping
        if ADMIN_ID and results["total_nuevas"] > 0:
            bot = app.bot
            report = format_scraping_report(results)
            await bot.send_message(ADMIN_ID, report, parse_mode="Markdown")
            
            # Enviar alertas de las nuevas
            lics = await get_licitaciones_nuevas(limit=5)
            for lic in lics:
                text, kb = format_licitacion_alert(lic)
                try:
                    await bot.send_message(ADMIN_ID, text, reply_markup=kb, parse_mode="Markdown")
                    await marcar_notificada(lic["id"])
                except Exception as e:
                    log.error(f"Error sending alert: {e}")
        
        log.info(f"Scraping completado: {results['total_nuevas']} nuevas de {len(results['por_fuente'])} fuentes")
    except Exception as e:
        log.error(f"Scheduled scrape failed: {e}")


# ─── Main ────────────────────────────────────────────────
async def post_init(application: Application):
    """Inicializa DB pool dentro del event loop correcto."""
    await get_pool()
    log.info("DB pool initialized in bot event loop")


def main():
    token = os.getenv("RADAR_BOT_TOKEN")
    if not token:
        log.error("RADAR_BOT_TOKEN not set!")
        return

    app = Application.builder().token(token).post_init(post_init).build()

    # Handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("hoy", cmd_hoy))
    app.add_handler(CommandHandler("buscar", cmd_buscar))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("region", cmd_region))
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Scheduler — scrape cada 60 minutos (no ejecutar al inicio)
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_scrape, "interval", minutes=60,
        args=[app], id="seace_scrape",
    )
    scheduler.start()

    log.info("🔍 LicitaRadar Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
