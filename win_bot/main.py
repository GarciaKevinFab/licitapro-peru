"""Bot 3: LicitaWin — Detecta adjudicaciones, trackea plazos y pagos."""
import os
import logging
import asyncio
from datetime import date, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("win_bot")

from shared.notificaciones import avisar_cobros_vencidos, detectar_adjudicaciones
from shared.db import (
    get_pool, get_contratos_activos, get_plazos_proximos, connection,
)
from shared.config import format_monto, format_fecha, ADMIN_ID

# Estos cuatro se USABAN sin importarse. `/factura`, `/conformidad`, `/entrega`
# y `/pago` no fallaban al arrancar el bot -- Python resuelve los nombres al
# ejecutar, no al importar -- sino en el momento en que alguien escribia el
# comando, con un NameError que el manejador de errores de la libreria se
# tragaba: el cliente veia que su mensaje no obtenia respuesta, y ya.
from win_bot.conformity_gen import generar_acta_conformidad, generar_informe_entrega
from win_bot.invoice_gen import generar_factura
from win_bot.payment_tracker import registrar_pago


# ─── Email sender ────────────────────────────────────────
async def enviar_email_buena_pro(contrato, licitacion):
    """Envía email de notificación cuando se gana la buena pro."""
    import aiosmtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_pass = os.getenv("SMTP_PASSWORD")
    destinatario = os.getenv("EMAIL_DESTINATARIO")

    if not smtp_user or not smtp_pass:
        log.warning("SMTP not configured, skipping email")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏆 BUENA PRO GANADA — {licitacion['nomenclatura'] or licitacion['id']}"
    msg["From"] = smtp_user
    msg["To"] = destinatario

    monto = format_monto(contrato["monto_adjudicado"]) if contrato["monto_adjudicado"] else "—"
    
    html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
    <div style="background: linear-gradient(135deg, #1D9E75, #0F6E56); padding: 20px; border-radius: 12px 12px 0 0;">
        <h1 style="color: white; margin: 0;">🏆 ¡Buena Pro Ganada!</h1>
    </div>
    <div style="padding: 20px; border: 1px solid #ddd; border-top: none; border-radius: 0 0 12px 12px;">
        <table style="width: 100%; border-collapse: collapse;">
            <tr><td style="padding: 8px; color: #666;">Licitación</td>
                <td style="padding: 8px; font-weight: bold;">{licitacion['nomenclatura'] or licitacion['id']}</td></tr>
            <tr><td style="padding: 8px; color: #666;">Entidad</td>
                <td style="padding: 8px;">{licitacion['entidad']}</td></tr>
            <tr><td style="padding: 8px; color: #666;">Objeto</td>
                <td style="padding: 8px;">{licitacion['objeto'][:200]}</td></tr>
            <tr><td style="padding: 8px; color: #666;">Monto Adjudicado</td>
                <td style="padding: 8px; font-weight: bold; color: #1D9E75;">{monto}</td></tr>
            <tr><td style="padding: 8px; color: #666;">Fecha Adjudicación</td>
                <td style="padding: 8px;">{format_fecha(contrato['fecha_adjudicacion'])}</td></tr>
        </table>
        <hr style="margin: 16px 0; border: none; border-top: 1px solid #eee;">
        <h3 style="color: #D85A30;">⏰ Próximos Plazos</h3>
        <p>• Firma de contrato: dentro de 8 días hábiles</p>
        <p>• Presentar carta fianza: junto con firma</p>
        <p style="color: #666; font-size: 12px; margin-top: 20px;">
            Enviado automáticamente por LicitaPro Perú
        </p>
    </div>
    </body></html>
    """
    msg.attach(MIMEText(html, "html"))

    try:
        await aiosmtplib.send(
            msg, hostname=smtp_host, port=smtp_port,
            username=smtp_user, password=smtp_pass,
            **({'use_tls': True, 'start_tls': False} if smtp_port == 465 else {'use_tls': False, 'start_tls': True}),
        )
        log.info(f"Email enviado a {destinatario}")
        return True
    except Exception as e:
        log.error(f"Error enviando email: {e}")
        return False


# ─── Monitor for wins ────────────────────────────────────
async def check_adjudicaciones(app):
    """Avisa al proveedor cuando figura como adjudicatario de una propuesta suya.

    Esto no hacia nada. Consultaba las propuestas enviadas, iteraba y hacia
    `pass`, con un TODO que decia que hacia falta scrapear el SEACE. El SEACE
    pide CAPTCHA, asi que el TODO era en la practica "nunca". Pero la API OCDS
    publica el nombre del adjudicatario: el dato llegaba y se estaba tirando.

    El aviso no crea el contrato. Se cruza por NOMBRE porque la API no entrega
    el RUC del proveedor, y un nombre es buena pista y mala prueba: quien
    confirma es el proveedor, con un clic.
    """
    parte = await detectar_adjudicaciones()
    if parte["coincidencias"]:
        log.info("Adjudicaciones detectadas: %s", parte)


async def _renovar_suscripciones():
    """Cobra las renovaciones que tocan. Un fallo aqui no puede tumbar el bot.

    Se envuelve porque el script esta escrito para ejecutarse suelto y termina
    devolviendo un codigo de salida; dentro del planificador una excepcion sin
    capturar dejaria el trabajo desprogramado en silencio.
    """
    try:
        from tools.renovar_suscripciones import main as renovar
        await renovar()
    except Exception as e:
        log.error("Fallo el cobro de renovaciones: %s", e, exc_info=True)


async def check_plazos_proximos(app):
    """Alerta sobre plazos que vencen en los próximos 3 días."""
    plazos = await get_plazos_proximos(dias=3)
    
    for plazo in plazos:
        if plazo["alerta_enviada"]:
            continue
        
        dias_faltan = (plazo["fecha_limite"] - date.today()).days
        urgencia = "🔴" if dias_faltan <= 1 else "🟡" if dias_faltan <= 3 else "🟢"
        
        if ADMIN_ID:
            await app.bot.send_message(
                ADMIN_ID,
                f"{urgencia} <b>PLAZO PRÓXIMO — {dias_faltan} día(s)</b>\n\n"
                f"📋 {plazo['objeto'][:100]}\n"
                f"🏛️ {plazo['entidad']}\n"
                f"📎 {plazo['descripcion']}\n"
                f"📅 Fecha límite: {format_fecha(plazo['fecha_limite'])}\n"
                f"📄 Contrato: {plazo['numero_contrato'] or '—'}",
                parse_mode="HTML",
            )
        
        async with connection() as conn:
            await conn.execute(
                "UPDATE plazos SET alerta_enviada=TRUE WHERE id=$1", plazo["id"]
            )


# ─── Handlers ────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏆 <b>LicitaWin</b> — Seguimiento post-adjudicación\n\n"
        "Monitoreo de contratos ganados, plazos y pagos.\n\n"
        "Comandos:\n"
        "/contratos — Contratos activos\n"
        "/plazos — Próximos plazos y fechas límite\n"
        "/pagos — Estado de pagos\n"
        "/ganar [propuesta_id] [monto] — Registrar buena pro\n"
        "/entrega [contrato_id] — Registrar entrega\n"
        "/factura [contrato_id] [monto] — Registrar factura\n"
        "/pago [contrato_id] [monto] — Registrar pago recibido\n"
        "/resumen — Dashboard mensual",
        parse_mode="HTML",
    )


async def cmd_contratos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    contratos = await get_contratos_activos()
    if not contratos:
        await update.message.reply_text("📭 No hay contratos activos.")
        return
    
    for c in contratos:
        estado_emoji = {
            "adjudicado": "📋", "contrato_firmado": "✍️",
            "en_ejecucion": "🔄", "entregado": "📦",
            "conformidad": "✅",
        }.get(c["estado"], "📄")
        
        monto = format_monto(c["monto_adjudicado"]) if c["monto_adjudicado"] else "—"
        
        await update.message.reply_text(
            f"{estado_emoji} <b>Contrato #{c['id']}</b> — {c['estado']}\n\n"
            f"🏛️ {c['entidad']}\n"
            f"📦 {c['objeto'][:120]}\n"
            f"💰 {monto}\n"
            f"📅 Entrega final: {format_fecha(c['fecha_entrega_final'])}\n"
            f"📄 N° Contrato: {c['numero_contrato'] or 'Pendiente firma'}",
            parse_mode="HTML",
        )


async def cmd_plazos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    plazos = await get_plazos_proximos(dias=30)
    if not plazos:
        await update.message.reply_text("✅ No hay plazos próximos en los siguientes 30 días.")
        return
    
    texto = "📅 <b>Próximos plazos (30 días)</b>\n\n"
    for p in plazos:
        dias = (p["fecha_limite"] - date.today()).days
        emoji = "🔴" if dias <= 3 else "🟡" if dias <= 7 else "🟢"
        check = "✅" if p["completado"] else emoji
        texto += f"{check} {format_fecha(p['fecha_limite'])} ({dias}d) — {p['descripcion']}\n"
    
    await update.message.reply_text(texto, parse_mode="HTML")


async def cmd_pagos(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    async with connection() as conn:
        pagos = await conn.fetch(
            """SELECT pg.*, c.numero_contrato, l.entidad, l.objeto
            FROM pagos pg
            JOIN contratos c ON pg.contrato_id = c.id
            JOIN licitaciones l ON c.licitacion_id = l.id
            ORDER BY pg.estado, pg.fecha_pago_esperada"""
        )
    
    if not pagos:
        await update.message.reply_text("📭 No hay pagos registrados.")
        return
    
    pendientes = [p for p in pagos if p["estado"] == "pendiente"]
    cobrados = [p for p in pagos if p["estado"] == "pagado"]
    
    total_pendiente = sum(p["monto"] for p in pendientes)
    total_cobrado = sum(p["monto"] for p in cobrados)
    
    texto = (
        f"💰 <b>Estado de Pagos</b>\n\n"
        f"⏳ Pendientes: {len(pendientes)} — {format_monto(total_pendiente)}\n"
        f"✅ Cobrados: {len(cobrados)} — {format_monto(total_cobrado)}\n\n"
    )
    
    if pendientes:
        texto += "<b>Pendientes:</b>\n"
        for p in pendientes[:5]:
            texto += f"• {format_monto(p['monto'])} — {p['entidad'][:30]} — {p['concepto']}\n"
    
    await update.message.reply_text(texto, parse_mode="HTML")


async def cmd_ganar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Registrar manualmente una buena pro: /ganar [propuesta_id] [monto]"""
    if len(ctx.args) < 2:
        await update.message.reply_text("Uso: /ganar [propuesta_id] [monto_adjudicado]")
        return
    
    prop_id = int(ctx.args[0])
    monto = float(ctx.args[1])
    
    async with connection() as conn:
        prop = await conn.fetchrow(
            "SELECT p.*, l.objeto, l.entidad, l.nomenclatura "
            "FROM propuestas p JOIN licitaciones l ON p.licitacion_id = l.id "
            "WHERE p.id=$1", prop_id
        )
        if not prop:
            await update.message.reply_text("❌ Propuesta no encontrada")
            return
        
        hoy = date.today()
        # Crear contrato
        contrato_id = await conn.fetchval(
            """INSERT INTO contratos 
            (propuesta_id, licitacion_id, empresa_id, monto_adjudicado,
             fecha_adjudicacion, estado)
            VALUES ($1, $2, $3, $4, $5, 'adjudicado') RETURNING id""",
            prop_id, prop["licitacion_id"], prop["empresa_id"], monto, hoy,
        )
        
        # Crear plazos automáticos
        plazos_default = [
            ("firma", "Firma de contrato", hoy + timedelta(days=8)),
            ("fianza", "Presentar carta fianza", hoy + timedelta(days=8)),
            ("entregable", "Primer entregable", hoy + timedelta(days=38)),
            ("entregable", "Entrega final", hoy + timedelta(days=68)),
            ("conformidad", "Conformidad del servicio", hoy + timedelta(days=75)),
            ("pago", "Pago estimado", hoy + timedelta(days=90)),
        ]
        
        for tipo, desc, fecha in plazos_default:
            await conn.execute(
                "INSERT INTO plazos (contrato_id, tipo, descripcion, fecha_limite) "
                "VALUES ($1, $2, $3, $4)",
                contrato_id, tipo, desc, fecha,
            )
        
        # Actualizar propuesta
        await conn.execute(
            "UPDATE propuestas SET estado='adjudicado' WHERE id=$1", prop_id
        )
        await conn.execute(
            "UPDATE licitaciones SET estado='adjudicado' WHERE id=$1", prop["licitacion_id"]
        )
    
    await update.message.reply_text(
        f"🏆🏆🏆 <b>¡BUENA PRO REGISTRADA!</b> 🏆🏆🏆\n\n"
        f"📋 {prop['nomenclatura'] or prop['licitacion_id']}\n"
        f"🏛️ {prop['entidad']}\n"
        f"📦 {prop['objeto'][:120]}\n"
        f"💰 Monto: {format_monto(monto)}\n\n"
        f"📅 Plazos creados automáticamente.\n"
        f"Usa /plazos para ver el timeline.\n\n"
        f"📧 Enviando notificación por email...",
        parse_mode="HTML",
    )
    
    # Enviar email
    async with connection() as conn:
        contrato = await conn.fetchrow("SELECT * FROM contratos WHERE id=$1", contrato_id)
        lic = await conn.fetchrow("SELECT * FROM licitaciones WHERE id=$1", prop["licitacion_id"])
    
    email_ok = await enviar_email_buena_pro(contrato, lic)
    if email_ok:
        await update.message.reply_text("✅ Email enviado exitosamente.")
        async with connection() as conn:
            await conn.execute("UPDATE contratos SET email_notificado=TRUE WHERE id=$1", contrato_id)
    else:
        await update.message.reply_text("⚠️ No se pudo enviar el email. Verifica la configuración SMTP.")


async def cmd_resumen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    async with connection() as conn:
        total_contratos = await conn.fetchval("SELECT COUNT(*) FROM contratos")
        activos = await conn.fetchval("SELECT COUNT(*) FROM contratos WHERE estado NOT IN ('pagado','cancelado')")
        total_monto = await conn.fetchval("SELECT COALESCE(SUM(monto_adjudicado),0) FROM contratos") or 0
        cobrado = await conn.fetchval("SELECT COALESCE(SUM(monto),0) FROM pagos WHERE estado='pagado'") or 0
        pendiente = await conn.fetchval("SELECT COALESCE(SUM(monto),0) FROM pagos WHERE estado='pendiente'") or 0
        prox_plazos = await conn.fetchval(
            "SELECT COUNT(*) FROM plazos WHERE completado=FALSE AND fecha_limite <= CURRENT_DATE + 7"
        )
    
    await update.message.reply_text(
        f"📊 <b>RESUMEN GENERAL</b>\n\n"
        f"📋 Contratos totales: {total_contratos}\n"
        f"🔄 Contratos activos: {activos}\n"
        f"💰 Monto total adjudicado: {format_monto(total_monto)}\n"
        f"✅ Cobrado: {format_monto(cobrado)}\n"
        f"⏳ Pendiente de cobro: {format_monto(pendiente)}\n"
        f"📅 Plazos próximos (7 días): {prox_plazos}",
        parse_mode="HTML",
    )


async def cmd_entrega(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Registrar entrega: /entrega [contrato_id] [descripcion]"""
    if len(ctx.args) < 2:
        await update.message.reply_text("Uso: /entrega [contrato_id] [descripcion]")
        return
    contrato_id = int(ctx.args[0])
    descripcion = " ".join(ctx.args[1:])
    path = await generar_informe_entrega(contrato_id, descripcion)
    if path:
        await update.message.reply_text(f"📦 Entrega registrada.\n📄 Informe: {path}", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Contrato no encontrado")


async def cmd_factura(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Generar factura: /factura [contrato_id] [monto] [concepto]"""
    if len(ctx.args) < 2:
        await update.message.reply_text("Uso: /factura [contrato_id] [monto] [concepto]")
        return
    contrato_id = int(ctx.args[0])
    monto = float(ctx.args[1])
    concepto = " ".join(ctx.args[2:]) if len(ctx.args) > 2 else "Servicio contratado"
    path = await generar_factura(contrato_id, monto, concepto)
    if path:
        await update.message.reply_text(f"📄 Factura generada: {format_monto(monto)}\n📎 {path}", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Error generando factura")


async def cmd_conformidad(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Generar acta de conformidad: /conformidad [contrato_id]"""
    if not ctx.args:
        await update.message.reply_text("Uso: /conformidad [contrato_id]")
        return
    contrato_id = int(ctx.args[0])
    obs = " ".join(ctx.args[1:]) if len(ctx.args) > 1 else ""
    path = await generar_acta_conformidad(contrato_id, obs)
    if path:
        await update.message.reply_text(f"✅ Acta generada: {path}", parse_mode="HTML")
    else:
        await update.message.reply_text("❌ Contrato no encontrado")


async def cmd_pago(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Registrar pago: /pago [contrato_id] [monto]"""
    if len(ctx.args) < 2:
        await update.message.reply_text("Uso: /pago [contrato_id] [monto]")
        return
    contrato_id = int(ctx.args[0])
    monto = float(ctx.args[1])
    pago_id = await registrar_pago(contrato_id, monto)
    await update.message.reply_text(f"💰 Pago registrado (#{pago_id}): {format_monto(monto)}", parse_mode="HTML")


# ─── Main ────────────────────────────────────────────────
async def post_init(application: Application):
    """Inicializa DB pool dentro del event loop correcto."""
    await get_pool()
    log.info("DB pool initialized in bot event loop")


def main():
    token = os.getenv("WIN_BOT_TOKEN")
    if not token:
        log.error("WIN_BOT_TOKEN not set!")
        return

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("contratos", cmd_contratos))
    app.add_handler(CommandHandler("plazos", cmd_plazos))
    app.add_handler(CommandHandler("pagos", cmd_pagos))
    app.add_handler(CommandHandler("ganar", cmd_ganar))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("entrega", cmd_entrega))
    app.add_handler(CommandHandler("factura", cmd_factura))
    app.add_handler(CommandHandler("conformidad", cmd_conformidad))
    app.add_handler(CommandHandler("pago", cmd_pago))

    # Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_adjudicaciones, "interval", minutes=30, args=[app])
    scheduler.add_job(check_plazos_proximos, "interval", hours=6, args=[app])
    # Los cobros vencidos se miran una vez al dia: la mora se cuenta en dias
    # habiles, asi que no cambia mas de una vez por jornada y avisar cada seis
    # horas seria repetir lo mismo cuatro veces.
    scheduler.add_job(avisar_cobros_vencidos, "interval", hours=24)
    # Las renovaciones tambien: el script existia y no lo disparaba nadie, asi
    # que tal cual estaba habia que ejecutarlo a mano cada dia o ninguna
    # suscripcion se renovaba nunca. Va aqui, junto a lo demas que mueve dinero.
    # `renovaciones_pendientes` ya filtra por ultimo_intento, asi que correrlo
    # a diario no machaca la pasarela con reintentos.
    scheduler.add_job(_renovar_suscripciones, "interval", hours=24)
    scheduler.start()

    log.info("🏆 LicitaWin Bot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
