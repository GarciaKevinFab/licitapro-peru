"""Los documentos de una cotizacion: la cotizacion por items y la declaracion jurada.

POR QUE EN PDF

  Los dos se firman. Con DNIe, ReFirma solo firma PDF; con firma escaneada,
  la entidad los imprime o los reenvia tal cual. Salen por
  `shared.pdf_firmable`, que ya resuelve cabecera, firma y sello.

LA DECLARACION JURADA DEPENDE DE LA ENTIDAD

  La generica ("Declaracion Jurada de Datos del Postor") vale para la mayoria.
  Pero hay entidades que exigen SU formato y devuelven cualquier otro: el
  Gobierno Regional de Madre de Dios adjunta a cada SC una "Declaracion Jurada
  Unica y Obligatoria" con diez declaraciones fijas y un campo para la cuenta
  interbancaria. Se reproduce aqui con los datos de la empresa ya puestos.
  Para sumar otra entidad basta con otra entrada en `formato_dj`.
"""
from __future__ import annotations

import re
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from shared.config import normalizar
from shared.cotizacion import cantidad_legible, monto, precio_legible, total
from shared.pdf_firmable import generar_pdf

_VACIO = "____________________"


def _e(valor) -> str:
    """Texto del usuario dentro del mini-HTML de reportlab.

    Sin escapar, "K & A SISTEMAS S.A.C." rompe el parrafo: reportlab lee el
    "&" como el inicio de una entidad.
    """
    return escape(str(valor)) if valor not in (None, "") else ""


def concepto_corto(objeto: str | None, limite: int = 220) -> str:
    """El concepto sin la etiqueta [BIENES] y cortado en una palabra entera.

    Los conceptos de GOREMAD llevan detras un parrafo de advertencias ("MUY
    IMPORTANTE: EL POSTOR DEBE REVISAR..."), y `objeto` viene ya cortado a 500
    caracteres: copiado entero, la declaracion terminaba en "...LAS CARACT".
    """
    texto = re.sub(r"^\[[^\]]+\]\s*", "", (objeto or "").strip())
    texto = re.split(r"\s+-\s+MUY IMPORTANTE", texto, maxsplit=1, flags=re.I)[0]
    if len(texto) <= limite:
        return texto
    return texto[:limite].rsplit(" ", 1)[0].rstrip(" -,;:") + "…"


def referencia(prop: dict) -> str:
    """'COT-5884-2026-GOREMAD', y no el id interno de la base."""
    return prop.get("nomenclatura") or concepto_corto(prop.get("objeto"), 80)


# ─── Declaracion jurada ─────────────────────────────────────

def formato_dj(prop: dict) -> str | None:
    """Que formato de declaracion exige la entidad. None = el generico."""
    nomen = (prop.get("nomenclatura") or "").upper()
    entidad = normalizar(prop.get("entidad") or "")
    if nomen.endswith("-GOREMAD") or "gobierno regional de madre de dios" in entidad:
        return "goremad"
    return None


def _generica(emp: dict, prop: dict) -> tuple[str, str, list]:
    parrafos = [
        (f"El que suscribe, {_e(emp.get('representante_legal')) or _VACIO}, "
         f"identificado con DNI N.º {_e(emp.get('dni_representante')) or _VACIO}, "
         f"en calidad de {_e(emp.get('cargo_representante')) or 'representante legal'} "
         f"de {_e(emp.get('razon_social'))}, con RUC N.º {_e(emp.get('ruc')) or _VACIO} "
         f"y domicilio en {_e(emp.get('direccion')) or _VACIO}, "
         f"DECLARO BAJO JURAMENTO lo siguiente:"),
        ("1. Que los datos consignados en el presente documento son veraces y "
         "corresponden a la situación actual de mi representada."),
        ("2. Que no me encuentro incurso en ninguno de los impedimentos para "
         "contratar con el Estado establecidos en la Ley General de "
         "Contrataciones Públicas."),
        ("3. Que conozco, acepto y me someto a las bases, condiciones y "
         "procedimientos del proceso de selección."),
        ("4. Que me comprometo a mantener vigente mi oferta durante el plazo "
         "señalado en las bases y a suscribir el contrato en caso de resultar "
         "adjudicado."),
        f"Proceso: {_e(referencia(prop))} — {_e(concepto_corto(prop.get('objeto')))}",
        f"Entidad convocante: {_e(prop.get('entidad'))}",
    ]
    return "DECLARACIÓN JURADA DE DATOS DEL POSTOR", _e(prop.get("entidad")), parrafos


# Texto del formato que el GOREMAD adjunta a sus SC ("DECLARACION JURADA 2026
# ACTUALIZADO LEY 32069"). Su original numera dos veces el 6; aqui va seguido.
_GOREMAD_DECLARO = [
    "No tener impedimentos para contratar con el Estado (Ley N° 32069).",
    "No encontrarme en el Registro de Sancionados e Inhabilitados para contratar con el Estado.",
    "No tener sentencia condenatoria firme en procesos penales.",
    ("No tener sanción administrativamente con inhabilitación temporal o permanente en el "
     "ejercicio de mis derechos para participar en procesos de selección (Ley N° 32069)."),
    ("No tener grado de parentesco hasta el segundo grado de consanguinidad, segundo de "
     "afinidad con los funcionarios de dirección y/o personal de confianza del Gobierno "
     "Regional de Madre de Dios o que tengan injerencia directa o indirecta en los "
     "procesos de selección."),
    "Contar con el Registro Nacional de Proveedores (RNP) vigente al momento de la suscripción de la presente.",
    "Contar con Cuenta Corriente Interbancaria.",
    "Contar con Registro Único de Contribuyentes en la condición de Activo y Habido.",
    "Que, cumplo con lo establecido en las Especificaciones Técnicas y/o Términos de Referencia.",
    ("Que, la información adjuntada en mi Curriculum Vitae se ajusta a la verdad y me someto "
     "a la fiscalización posterior. (El presente numeral es de aplicación solo a los "
     "servicios administrativos)."),
]
_GOREMAD_ADEMAS = [
    ("Me comprometo a mantener vigente esta oferta y a perfeccionar el contrato, en caso "
     "resultara favorecido con la Buena Pro, así como cumplir las Especificaciones Técnicas "
     "y/o Términos de Referencia."),
    "Me comprometo a cumplir con las fechas y los plazos establecidos.",
    "Me comprometo a entregar productos y/o brindar servicios de calidad y garantía.",
]


def _goremad(emp: dict, prop: dict, cci: str | None) -> tuple[str, str, list]:
    parrafos = [
        "SEÑORES:<br/><b>GOBIERNO REGIONAL DE MADRE DE DIOS.</b><br/>Presente:",
        "<u>DATOS DEL POSTOR</u>",
        (f"Yo, <b>{_e(emp.get('representante_legal')) or _VACIO}</b>, representante legal "
         f"de la Empresa <b>{_e(emp.get('razon_social'))}</b>, identificado con DNI N° "
         f"<b>{_e(emp.get('dni_representante')) or _VACIO}</b>, RUC N° "
         f"<b>{_e(emp.get('ruc')) or _VACIO}</b> con domicilio fiscal en "
         f"<b>{_e(emp.get('direccion')) or _VACIO}</b> y Cuenta Corriente Interbancaria "
         f"<b>{_e(cci) or _VACIO}</b>."),
        "<b>DECLARO BAJO JURAMENTO QUE:</b>",
    ]
    parrafos += [f"{i}. {t}" for i, t in enumerate(_GOREMAD_DECLARO, 1)]
    parrafos.append("<b>ADEMÁS:</b>")
    parrafos += [f"{i}. {t}" for i, t in enumerate(_GOREMAD_ADEMAS, 1)]
    parrafos.append("<b>NOTA:</b> Conozco de las sanciones penales y administrativas en caso "
                    "de incurrir en falsedad de las declaraciones juradas antes expuestas.")
    parrafos.append(f"Referencia: {_e(referencia(prop))}")
    return ("DECLARACIÓN JURADA ÚNICA Y OBLIGATORIA PARA SER POSTOR O PROVEEDOR "
            "DEL GOBIERNO REGIONAL DE MADRE DE DIOS",
            "Oficina de Abastecimiento y Servicios Auxiliares", parrafos)


async def generar_dj(prop: dict, emp: dict, cci: str | None, con_dnie: bool) -> str:
    if formato_dj(prop) == "goremad":
        titulo, sub, parrafos = _goremad(emp, prop, cci)
    else:
        titulo, sub, parrafos = _generica(emp, prop)
    return await generar_pdf(
        nombre_archivo=f"declaracion-jurada-{prop['id']}.pdf", titulo=titulo,
        subtitulo=sub, parrafos=parrafos, empresa=emp, con_dnie=con_dnie)


# ─── Cotizacion por items ───────────────────────────────────

def _estilo_celda(alineado: int = 0, negrita: bool = False) -> ParagraphStyle:
    base = getSampleStyleSheet()["Normal"]
    return ParagraphStyle(
        f"celda{alineado}{negrita}", parent=base, fontSize=8, leading=10,
        alignment=alineado, fontName="Helvetica-Bold" if negrita else "Helvetica")


def tabla_items(items: list[dict]) -> Table:
    """La misma tabla de la SC: cantidad, unidad, descripcion, marca, P.U., total.

    Un item sin precio sale con la celda vacia, no con 0.00: un cero firmado
    es una oferta de regalar el bien.
    """
    izq, der, cen = _estilo_celda(0), _estilo_celda(2), _estilo_celda(1)
    cab = _estilo_celda(1, negrita=True)
    filas = [[Paragraph(t, cab) for t in
              ("N°", "CANT.", "UNIDAD", "DESCRIPCIÓN", "MARCA", "P. UNIT. (S/)", "P. TOTAL (S/)")]]
    for i, it in enumerate(items, 1):
        pu = it.get("precio_unitario")
        st = it.get("subtotal")
        filas.append([
            Paragraph(str(i), cen),
            Paragraph(cantidad_legible(it["cantidad"]), der),
            Paragraph(_e(it.get("unidad")), cen),
            Paragraph(_e(it["descripcion"]), izq),
            Paragraph(_e(it.get("marca")), izq),
            Paragraph(precio_legible(pu) if pu is not None else "", der),
            Paragraph(monto(st) if st is not None else "", der),
        ])
    filas.append(["", "", "", "", "", Paragraph("TOTAL", _estilo_celda(2, True)),
                  Paragraph(monto(total(items)), _estilo_celda(2, True))])

    t = Table(filas, colWidths=[0.8 * cm, 1.3 * cm, 1.9 * cm, 5.2 * cm, 2.6 * cm,
                                2.0 * cm, 2.2 * cm], repeatRows=1)
    ultima = len(filas) - 1
    t.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, ultima - 1), 0.5, colors.HexColor("#8A979E")),
        ("BOX", (5, ultima), (6, ultima), 0.8, colors.HexColor("#12181C")),
        ("INNERGRID", (5, ultima), (6, ultima), 0.5, colors.HexColor("#8A979E")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#EEF2F4")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    return t


async def generar_cotizacion(prop: dict, emp: dict, items: list[dict], condiciones: dict,
                             cci: str | None, con_dnie: bool) -> str:
    """La cotizacion lista para enviar: tabla de items, condiciones y contacto."""
    contacto = " / ".join(x for x in (_e(emp.get("telefono")), _e(emp.get("email"))) if x)
    parrafos = [
        f"Señores: <b>{_e(prop.get('entidad'))}</b>",
        f"Referencia: <b>{_e(referencia(prop))}</b> — {_e(concepto_corto(prop.get('objeto')))}",
        ("De nuestra consideración: por medio de la presente hacemos llegar nuestra "
         "cotización para los bienes y/o servicios solicitados, de acuerdo con las "
         "especificaciones técnicas y/o términos de referencia de la entidad."),
        tabla_items(items),
        Spacer(1, 10),
        "<b>CONDICIONES</b>",
        "• Moneda: Soles (S/). Los precios incluyen I.G.V. y todo concepto que incida en el costo.",
    ]
    for clave, rotulo in (("plazo_entrega", "Plazo de entrega / ejecución"),
                          ("validez", "Validez de la cotización"),
                          ("garantia", "Garantía"),
                          ("forma_pago", "Forma de pago")):
        if condiciones.get(clave):
            parrafos.append(f"• {rotulo}: {_e(condiciones[clave])}")
    if cci:
        parrafos.append(f"• Cuenta corriente interbancaria (CCI): {_e(cci)}")
    parrafos.append(
        f"<b>DATOS DEL POSTOR</b><br/>Razón social: {_e(emp.get('razon_social'))}<br/>"
        f"RUC: {_e(emp.get('ruc'))}<br/>Domicilio fiscal: {_e(emp.get('direccion'))}"
        + (f"<br/>Contacto para coordinaciones de entrega y consultas: {contacto}"
           if contacto else ""))
    return await generar_pdf(
        nombre_archivo=f"cotizacion-{prop['id']}.pdf", titulo="COTIZACIÓN",
        subtitulo=_e(referencia(prop)), parrafos=parrafos, empresa=emp, con_dnie=con_dnie)
