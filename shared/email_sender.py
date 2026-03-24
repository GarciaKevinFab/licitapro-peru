"""Email sender — Envío de notificaciones por SMTP (Gmail / SendGrid)."""
import os
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import aiosmtplib

from shared.config import format_monto, format_fecha

log = logging.getLogger("licitapro.email")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASSWORD")
EMAIL_FROM = os.getenv("SMTP_USER", "noreply@licitapro.pe")
EMAIL_DEST = os.getenv("EMAIL_DESTINATARIO", "ventas@sisac.pe")


async def enviar_email(destinatario: str, asunto: str, html_body: str) -> bool:
    """Envía un email HTML vía SMTP."""
    if not SMTP_USER or not SMTP_PASS:
        log.warning("SMTP no configurado, saltando envío de email")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"] = EMAIL_FROM
    msg["To"] = destinatario
    msg.attach(MIMEText(html_body, "html"))

    try:
        await aiosmtplib.send(
            msg, hostname=SMTP_HOST, port=SMTP_PORT,
            username=SMTP_USER, password=SMTP_PASS,
            use_tls=False, start_tls=True,
        )
        log.info(f"Email enviado a {destinatario}: {asunto}")
        return True
    except Exception as e:
        log.error(f"Error enviando email: {e}")
        return False


async def notificar_buena_pro(contrato: dict, licitacion: dict) -> bool:
    """Email cuando se gana la buena pro."""
    monto = format_monto(contrato["monto_adjudicado"]) if contrato.get("monto_adjudicado") else "—"
    html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
    <div style="background: linear-gradient(135deg, #1D9E75, #0F6E56); padding: 24px; border-radius: 12px 12px 0 0; text-align: center;">
        <h1 style="color: white; margin: 0; font-size: 24px;">&#127942; Buena Pro Ganada</h1>
    </div>
    <div style="padding: 24px; border: 1px solid #e0e0e0; border-top: none; border-radius: 0 0 12px 12px;">
        <table style="width: 100%; border-collapse: collapse;">
            <tr><td style="padding: 10px; color: #888; width: 140px;">Licitacion</td>
                <td style="padding: 10px; font-weight: bold;">{licitacion.get('nomenclatura') or licitacion['id']}</td></tr>
            <tr style="background: #f9f9f9;"><td style="padding: 10px; color: #888;">Entidad</td>
                <td style="padding: 10px;">{licitacion['entidad']}</td></tr>
            <tr><td style="padding: 10px; color: #888;">Objeto</td>
                <td style="padding: 10px;">{licitacion['objeto'][:200]}</td></tr>
            <tr style="background: #f9f9f9;"><td style="padding: 10px; color: #888;">Monto Adjudicado</td>
                <td style="padding: 10px; font-weight: bold; color: #1D9E75; font-size: 18px;">{monto}</td></tr>
            <tr><td style="padding: 10px; color: #888;">Adjudicacion</td>
                <td style="padding: 10px;">{format_fecha(contrato.get('fecha_adjudicacion'))}</td></tr>
        </table>
        <hr style="margin: 20px 0; border: none; border-top: 1px solid #eee;">
        <h3 style="color: #D85A30; margin: 0 0 12px;">Proximos Pasos</h3>
        <ul style="color: #555; line-height: 1.8;">
            <li>Firma de contrato: dentro de 8 dias habiles</li>
            <li>Presentar carta fianza (10%): junto con firma</li>
            <li>Preparar documentos de ejecucion</li>
        </ul>
        <p style="color: #999; font-size: 11px; margin-top: 24px; text-align: center;">
            Enviado automaticamente por LicitaPro Peru
        </p>
    </div>
    </body></html>
    """
    return await enviar_email(EMAIL_DEST, f"BUENA PRO GANADA - {licitacion.get('nomenclatura') or licitacion['id']}", html)


async def notificar_plazo_proximo(plazo: dict, contrato: dict) -> bool:
    """Email de alerta de plazo próximo a vencer."""
    from datetime import date
    dias = (plazo["fecha_limite"] - date.today()).days
    urgencia_color = "#D85A30" if dias <= 1 else "#E6A817" if dias <= 3 else "#1D9E75"

    html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
    <div style="background: {urgencia_color}; padding: 20px; border-radius: 12px 12px 0 0; text-align: center;">
        <h2 style="color: white; margin: 0;">Plazo en {dias} dia(s)</h2>
    </div>
    <div style="padding: 20px; border: 1px solid #e0e0e0; border-top: none; border-radius: 0 0 12px 12px;">
        <p><strong>{plazo['descripcion']}</strong></p>
        <p>Fecha limite: {format_fecha(plazo['fecha_limite'])}</p>
        <p>Contrato: {contrato.get('numero_contrato', '—')}</p>
        <p style="color: #999; font-size: 11px; margin-top: 20px; text-align: center;">
            LicitaPro Peru — Alerta automatica
        </p>
    </div>
    </body></html>
    """
    return await enviar_email(EMAIL_DEST, f"PLAZO PROXIMO ({dias}d) - {plazo['descripcion']}", html)


async def notificar_pago_recibido(pago: dict, contrato: dict) -> bool:
    """Email cuando se registra un pago."""
    html = f"""
    <html><body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
    <div style="background: #1D9E75; padding: 20px; border-radius: 12px 12px 0 0; text-align: center;">
        <h2 style="color: white; margin: 0;">Pago Recibido</h2>
    </div>
    <div style="padding: 20px; border: 1px solid #e0e0e0; border-top: none; border-radius: 0 0 12px 12px;">
        <p><strong>Monto:</strong> {format_monto(pago['monto'])}</p>
        <p><strong>Concepto:</strong> {pago.get('concepto', '—')}</p>
        <p><strong>Contrato:</strong> {contrato.get('numero_contrato', '—')}</p>
        <p><strong>Factura:</strong> {pago.get('factura_numero', '—')}</p>
        <p style="color: #999; font-size: 11px; margin-top: 20px; text-align: center;">
            LicitaPro Peru
        </p>
    </div>
    </body></html>
    """
    return await enviar_email(EMAIL_DEST, f"PAGO RECIBIDO - {format_monto(pago['monto'])}", html)
