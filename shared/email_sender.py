"""Email sender — Envío de notificaciones por SMTP (Gmail / SendGrid)."""
import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

import aiosmtplib

from shared import fechas, plantillas_correo
from shared.config import format_fecha, format_monto

log = logging.getLogger("licitapro.email")

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASSWORD")
EMAIL_FROM = os.getenv("SMTP_USER", "noreply@licitapro.pe")
# Sin destinatario por defecto: cablear el correo de una empresa concreta
# haria que, con varios inquilinos, las notificaciones de cualquier cuenta
# acabaran en un buzon ajeno.
EMAIL_DEST = os.getenv("EMAIL_DESTINATARIO")


# EL MODO DE TLS DEPENDE DEL PUERTO, Y NO SE PUEDE FIJAR
#
#   465 habla TLS desde el primer byte (implicito). 587 empieza en claro y sube
#   con STARTTLS. Son incompatibles: pedir STARTTLS en el 465 deja la conexion
#   colgada o la corta el servidor, y al reves falla la negociacion.
#
#   Estaba fijo en STARTTLS. Funcionaba con el 587 de Gmail, que era el valor
#   por defecto, y se rompia en cuanto alguien ponia el 465 -- que es
#   justamente lo que recomienda cPanel para este dominio.
def _modo_tls(puerto: int) -> dict:
    return {"use_tls": True, "start_tls": False} if puerto == 465 else {"use_tls": False, "start_tls": True}


async def enviar_email(destinatario: str, asunto: str, html_body: str,
                       texto: str = "") -> bool:
    """Envía un email HTML vía SMTP."""
    if not SMTP_USER or not SMTP_PASS:
        log.warning("SMTP no configurado, saltando envío de email")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = asunto
    msg["From"] = EMAIL_FROM
    msg["To"] = destinatario
    # DATE Y MESSAGE-ID: SIN ELLAS EL CORREO ACABA EN SPAM
    #
    #   Son obligatorias segun el RFC 5322 y casi todos los filtros penalizan
    #   su ausencia -- SpamAssassin tiene reglas especificas para las dos. Un
    #   mensaje sin Message-ID ademas rompe el hilo en el cliente: cada
    #   respuesta abre una conversacion nueva.
    #
    #   El servidor las acepta igual y devuelve 250, asi que el sintoma no es
    #   un error: es que el correo "se envia" y no aparece. Que es peor,
    #   porque no hay nada que mirar.
    #
    #   El dominio del Message-ID sale del remitente: uno inventado que no
    #   coincida con el From tambien puntua mal.
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=EMAIL_FROM.split("@")[-1] if "@" in EMAIL_FROM else None)
    # EL TEXTO PLANO VA PRIMERO. En multipart/alternative el cliente
    # elige la ULTIMA parte que sabe pintar, asi que el HTML tiene que
    # ir al final o todo el mundo veria la version en texto.
    if texto:
        msg.attach(MIMEText(texto, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        await aiosmtplib.send(
            msg, hostname=SMTP_HOST, port=SMTP_PORT,
            username=SMTP_USER, password=SMTP_PASS,
            **_modo_tls(SMTP_PORT),
        )
        log.info(f"Email enviado a {destinatario}: {asunto}")
        return True
    except Exception as e:
        log.error(f"Error enviando email: {e}")
        return False


async def notificar_buena_pro(contrato: dict, licitacion: dict) -> bool:
    """Aviso de buena pro ganada."""
    monto = (format_monto(contrato["monto_adjudicado"])
             if contrato.get("monto_adjudicado") else "—")
    nomenclatura = licitacion.get("nomenclatura") or licitacion["id"]
    texto, html = plantillas_correo.componer(
        titulo="Ganaste la buena pro",
        preencabezado=f"{nomenclatura} · {monto}",
        intro=[("La entidad adjudicó a tu favor. Desde aquí empiezan a correr "
               "los plazos, y son cortos.")],
        filas=[
            ("Licitación", nomenclatura),
            ("Entidad", licitacion["entidad"]),
            ("Objeto", licitacion["objeto"][:200]),
            ("Monto adjudicado", monto, True),
            ("Fecha de adjudicación",
             format_fecha(contrato.get("fecha_adjudicacion"))),
        ],
        pasos=["Firma del contrato: dentro de 8 días hábiles",
               "Carta fianza del 10%: se presenta junto con la firma",
               "Preparar los documentos de ejecución"],
    )
    return await enviar_email(EMAIL_DEST,
                              f"Buena pro ganada — {nomenclatura}",
                              html, texto)


async def notificar_plazo_proximo(plazo: dict, contrato: dict) -> bool:
    """Alerta de plazo por vencer.

    El color acompana al numero, nunca lo sustituye: el titulo dice cuantos
    dias quedan. Quien no distingue esos tonos lee exactamente lo mismo.
    """
    dias = (plazo["fecha_limite"] - fechas.hoy()).days
    if dias <= 1:
        acento = plantillas_correo.ROJO
    elif dias <= 3:
        acento = plantillas_correo.AMBAR
    else:
        acento = plantillas_correo.MENTA

    if dias < 0:
        cuando = f"Plazo vencido hace {abs(dias)} día(s)"
    elif dias == 0:
        cuando = "El plazo vence hoy"
    elif dias == 1:
        cuando = "El plazo vence mañana"
    else:
        cuando = f"El plazo vence en {dias} días"

    texto, html = plantillas_correo.componer(
        titulo=cuando,
        acento=acento,
        preencabezado="{} · {}".format(plazo["descripcion"],
                                   format_fecha(plazo["fecha_limite"])),
        intro=[plazo["descripcion"]],
        filas=[
            ("Fecha límite", format_fecha(plazo["fecha_limite"]), True),
            ("Contrato", contrato.get("numero_contrato") or "—"),
        ],
    )
    return await enviar_email(EMAIL_DEST,
                              "{} — {}".format(cuando, plazo["descripcion"]),
                              html, texto)


async def notificar_pago_recibido(pago: dict, contrato: dict) -> bool:
    """Aviso de pago registrado."""
    monto = format_monto(pago["monto"])
    texto, html = plantillas_correo.componer(
        titulo="Pago recibido",
        preencabezado="{} · {}".format(monto,
                                   contrato.get("numero_contrato") or "—"),
        intro=["La entidad registró un pago a tu favor."],
        filas=[
            ("Monto", monto, True),
            ("Concepto", pago.get("concepto") or "—"),
            ("Contrato", contrato.get("numero_contrato") or "—"),
            ("Factura", pago.get("factura_numero") or "—"),
        ],
    )
    return await enviar_email(EMAIL_DEST, f"Pago recibido — {monto}",
                              html, texto)
