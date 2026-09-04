"""Genera documentos PDF listos para firmar.

Dos caminos de firma, y conviene tener clara la diferencia porque no son
equivalentes ante la ley ni ante una entidad:

  FIRMA ESCANEADA (imagen)
    Se incrusta la imagen que subio el usuario. Es una representacion visual y
    nada mas: no prueba quien firmo ni que el documento no se haya alterado
    despues. Vale para tramites que solo piden "el papel con la firma".

  FIRMA DIGITAL CON DNIe
    La hace RENIEC. El certificado vive en el chip del DNI electronico y la
    clave privada NUNCA sale de la tarjeta: por eso hacen falta lector fisico,
    el software ReFirma y el PIN del titular. Tiene la misma validez legal que
    una firma manuscrita.

    Eso implica que la plataforma NO PUEDE firmar por el usuario, y no es una
    limitacion que convenga sortear: hacerlo exigiria pedirle su PIN, que es
    justo lo que jamas hay que pedir. Lo que si podemos hacer es entregar el
    documento en PDF bien formado y con su espacio de firma preparado, para que
    lo firme en su equipo.

Por eso estos documentos salen en PDF y no en DOCX: ReFirma firma PDF.
"""
import logging
import os
from datetime import date
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    Image as ImagenPDF,
)
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from shared.archivos import rutas_de
from shared import fechas

log = logging.getLogger("shared.pdf_firmable")

SALIDA = Path(os.getenv("EXPEDIENTES_DIR", "data/expedientes"))

# Alto reservado al bloque de firma. ReFirma coloca ahi su representacion
# visual; si no queda sitio, la encaja donde puede y el documento queda feo.
ALTO_FIRMA = 3.2 * cm

_MES = {1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo",
        6: "junio", 7: "julio", 8: "agosto", 9: "septiembre",
        10: "octubre", 11: "noviembre", 12: "diciembre"}


def _estilos():
    hojas = getSampleStyleSheet()
    return {
        "titulo": ParagraphStyle(
            "titulo", parent=hojas["Heading1"], fontSize=13, leading=16,
            alignment=1, spaceAfter=14, textColor=colors.HexColor("#12181C")),
        "sub": ParagraphStyle(
            "sub", parent=hojas["Normal"], fontSize=8.5, leading=11,
            alignment=1, textColor=colors.HexColor("#6B7880"), spaceAfter=18),
        "cuerpo": ParagraphStyle(
            "cuerpo", parent=hojas["Normal"], fontSize=10, leading=15,
            spaceAfter=9, alignment=4),   # 4 = justificado
        "pie": ParagraphStyle(
            "pie", parent=hojas["Normal"], fontSize=7.5, leading=10,
            textColor=colors.HexColor("#7A8A93")),
        "etiqueta": ParagraphStyle(
            "etiqueta", parent=hojas["Normal"], fontSize=8, leading=11,
            alignment=1, textColor=colors.HexColor("#46555E")),
    }


def _cabecera(empresa: dict, imagenes: dict, est: dict) -> list:
    """Logo a la izquierda, datos de la empresa a la derecha."""
    datos = [f"<b>{empresa.get('razon_social') or ''}</b>"]
    if empresa.get("ruc"):
        datos.append(f"RUC {empresa['ruc']}")
    if empresa.get("direccion"):
        datos.append(empresa["direccion"])
    contacto = " · ".join(x for x in (empresa.get("telefono"),
                                      empresa.get("email")) if x)
    if contacto:
        datos.append(contacto)
    bloque = Paragraph("<br/>".join(datos), est["pie"])

    tabla = None
    if imagenes.get("logo"):
        try:
            logo = ImagenPDF(imagenes["logo"], width=3.2 * cm, height=2 * cm,
                             kind="proportional")
            tabla = Table([[logo, bloque]], colWidths=[3.6 * cm, 12.4 * cm])
        except Exception as e:
            # Un logo ilegible no puede impedir que se genere el documento.
            log.warning("No se pudo incrustar el logo: %s", e)
    if tabla is None:
        tabla = Table([[bloque]], colWidths=[16 * cm])

    tabla.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("LINEBELOW", (0, 0), (-1, -1), 0.6, colors.HexColor("#DCE3E7")),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    return [tabla, Spacer(1, 16)]


def _bloque_firma(empresa: dict, imagenes: dict, est: dict, con_dnie: bool) -> list:
    """Zona de firma: imagen escaneada, o hueco preparado para el DNIe."""
    nombre = empresa.get("representante_legal") or "Representante legal"
    cargo = empresa.get("cargo_representante") or "Representante Legal"
    dni = empresa.get("dni_representante")

    elementos = [Spacer(1, 26)]

    if con_dnie:
        # Recuadro vacio y rotulado. ReFirma pondra aqui su sello visual.
        marco = Table([[Paragraph(
            "Espacio reservado para la firma digital<br/>"
            "<font size=7>Firmar con DNI electrónico usando ReFirma (RENIEC)</font>",
            est["etiqueta"])]], colWidths=[8.4 * cm], rowHeights=[ALTO_FIRMA])
        marco.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#B8C4CA")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FAFBFC")),
        ]))
        elementos.append(marco)
    elif imagenes.get("firma"):
        try:
            elementos.append(ImagenPDF(imagenes["firma"], width=5.5 * cm,
                                       height=2.2 * cm, kind="proportional"))
        except Exception as e:
            log.warning("No se pudo incrustar la firma: %s", e)
            elementos.append(Spacer(1, ALTO_FIRMA))
    else:
        # Sin imagen y sin DNIe: se deja el hueco para firmar a mano.
        elementos.append(Spacer(1, ALTO_FIRMA))

    pie = [f"<b>{nombre}</b>", cargo]
    if dni:
        pie.append(f"DNI {dni}")
    elementos += [
        Table([[""]], colWidths=[7 * cm], style=TableStyle(
            [("LINEABOVE", (0, 0), (-1, -1), 0.8, colors.HexColor("#12181C"))])),
        Paragraph("<br/>".join(pie), est["pie"]),
    ]

    if imagenes.get("sello") and not con_dnie:
        try:
            elementos += [Spacer(1, 10),
                          ImagenPDF(imagenes["sello"], width=3 * cm,
                                    height=3 * cm, kind="proportional")]
        except Exception as e:
            log.warning("No se pudo incrustar el sello: %s", e)

    if con_dnie:
        elementos += [Spacer(1, 14), Paragraph(
            "Este documento está preparado para firma digital. Ábrelo con "
            "ReFirma PDF (descargable en pki.reniec.gob.pe), conecta tu lector "
            "e inserta tu DNI electrónico. La firma digital tiene la misma "
            "validez legal que la manuscrita.", est["pie"])]

    return elementos


async def generar_pdf(nombre_archivo: str, titulo: str, subtitulo: str,
                      parrafos: list, empresa, con_dnie: bool = True) -> str:
    """Genera el PDF y devuelve su ruta.

    con_dnie=True deja el recuadro preparado para la firma digital.
    con_dnie=False incrusta la firma escaneada y el sello, si los hay.
    """
    empresa = dict(empresa)
    imagenes = await rutas_de(empresa["id"])
    est = _estilos()

    SALIDA.mkdir(parents=True, exist_ok=True)
    ruta = SALIDA / nombre_archivo

    doc = SimpleDocTemplate(
        str(ruta), pagesize=A4,
        leftMargin=2.5 * cm, rightMargin=2.5 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
        title=titulo, author=empresa.get("razon_social") or "")

    hoy = fechas.hoy()
    partes = _cabecera(empresa, imagenes, est)
    partes.append(Paragraph(titulo, est["titulo"]))
    if subtitulo:
        partes.append(Paragraph(subtitulo, est["sub"]))
    for p in parrafos:
        partes.append(Paragraph(p, est["cuerpo"]))

    partes += [Spacer(1, 12), Paragraph(
        f"{empresa.get('departamento') or 'Lima'}, "
        f"{hoy.day} de {_MES[hoy.month]} de {hoy.year}", est["cuerpo"])]
    partes += _bloque_firma(empresa, imagenes, est, con_dnie)

    doc.build(partes)
    log.info("PDF generado: %s (dnie=%s)", ruta, con_dnie)
    return str(ruta)
