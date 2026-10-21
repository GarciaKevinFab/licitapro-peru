"""Document Generator — Genera documentos DOCX/PDF para propuestas.

NINGUN DOCUMENTO LEGAL SALE CON UN HUECO

  Antes, si faltaba el representante, su DNI o el domicilio, la carta salia
  con "[REPRESENTANTE]" o "[DNI]" escrito tal cual, y la declaracion jurada
  con la firma de nadie. El usuario no lo veia hasta abrir el .docx -- si lo
  abria -- y la entidad lo descalificaba en mesa de partes. Ahora se levanta
  `DocumentoIncompleto` con la lista de lo que falta y la web se lo dice antes
  de armar nada.

DE DONDE SALEN LOS DATOS DEL REPRESENTANTE

  De la tabla `empresas` primero, que es lo que el usuario edita en la ficha
  de la empresa y lo que ve. La knowledge_base es una copia que se aprende de
  las respuestas y puede quedarse vieja: si el representante cambio y el
  usuario lo corrigio en la ficha, la carta tiene que decir el nuevo.
"""
import asyncio
import logging
import os
from datetime import date

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from shared import fechas
from shared.config import desglose_igv, format_fecha, format_monto
from shared.db import connection, get_empresa
from shared.firma_manager import insertar_imagen_en_docx, obtener_firma
from shared.incompleto import DocumentoIncompleto
from shared.membrete import documento_word
from shared.rutas import directorio

log = logging.getLogger("prep.docgen")

TEMPLATES_DIR = str(directorio("TEMPLATES_DIR", "templates"))

# POR QUE UNA TABLA Y NO strftime('%B'): '%B' depende del locale del proceso.
# En el contenedor no hay locale es_PE y la carta salia fechada "20 de October
# de 2026". Es la misma tabla que usa shared/pdf_firmable.py.
_MES = {1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
        7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre",
        12: "diciembre"}


def fecha_larga(d: date | None = None) -> str:
    """'20 de octubre de 2026', sin depender del locale."""
    d = d or fechas.hoy()
    return f"{d.day:02d} de {_MES[d.month]} de {d.year}"


def referencia(licitacion: dict) -> str:
    """La nomenclatura del proceso o, si no tiene, el id de la LICITACION.

    `licitacion.get('nomenclatura', licitacion['id'])` tenia dos fallos: la
    clave existe aunque valga None (y la carta decia "Ref: None"), y en el
    expediente este dict es la fila de la PROPUESTA, cuyo `id` es el de la
    propuesta y no el de la licitacion.
    """
    return str(licitacion.get("nomenclatura") or licitacion.get("lic_id")
               or licitacion.get("licitacion_id") or licitacion.get("id") or "")


def _primero(*valores) -> str:
    for v in valores:
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def datos_legales(empresa, datos: dict) -> dict:
    """Representante, DNI, cargo y domicilio: la tabla `empresas` manda y la
    knowledge_base cubre lo que la tabla no tiene."""
    emp = dict(empresa) if empresa else {}
    return {
        "representante": _primero(emp.get("representante_legal"),
                                  datos.get("legal.representante_legal")),
        "dni": _primero(emp.get("dni_representante"), datos.get("legal.dni_representante")),
        "cargo": _primero(emp.get("cargo_representante"),
                          datos.get("legal.cargo_representante")) or "Gerente General",
        "domicilio": _primero(emp.get("direccion"), datos.get("legal.domicilio_legal"),
                              datos.get("legal.direccion_legal")),
    }


_ROTULOS = {"representante": "el representante legal",
            "dni": "el DNI del representante",
            "domicilio": "el domicilio legal"}


def exigir(documento: str, legales: dict, *campos: str) -> None:
    """Levanta DocumentoIncompleto si falta alguno de `campos`."""
    faltan = [_ROTULOS[c] for c in campos if not legales.get(c)]
    if faltan:
        raise DocumentoIncompleto(documento, faltan)


async def _guardar(doc, nombre: str) -> str:
    path = os.path.join(TEMPLATES_DIR, "output", nombre)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # python-docx escribe a disco de forma sincrona: fuera del bucle de eventos,
    # o la web entera se congela mientras se guarda un documento.
    await asyncio.to_thread(doc.save, path)
    return path


async def _documento(empresa_id: int):
    """El Word nuevo (sobre la hoja membretada si la hay), en un hilo.

    POR QUE: abrir el .docx membretado con python-docx y, si falta el .json de
    medidas, medir la hoja con pdfium es trabajo sincrono de disco y CPU; en
    el bucle de eventos congelaba la web entera mientras se generaba.
    """
    return await asyncio.to_thread(documento_word, empresa_id)


def _set_cell_text(cell, text, bold=False, size=10):
    cell.text = ""
    run = cell.paragraphs[0].add_run(str(text))
    run.bold = bold
    run.font.size = Pt(size)
    run.font.name = "Arial"


async def generar_carta_presentacion(propuesta_id: int, empresa_id: int, licitacion: dict) -> str:
    """Genera Anexo 1: Carta de Presentación del postor."""
    empresa = await get_empresa(empresa_id)
    datos = await _recopilar_datos(empresa_id)
    legales = datos_legales(empresa, datos)
    exigir("la carta de presentación", legales, "representante", "dni", "domicilio")

    doc = await _documento(empresa_id)
    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(11)

    # Encabezado
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    p.add_run(f"{legales['domicilio']}\n")
    p.add_run(fecha_larga())

    doc.add_paragraph()  # Espacio

    # Destinatario
    doc.add_paragraph("Señores")
    doc.add_paragraph(f"{licitacion['entidad']}").bold = True
    doc.add_paragraph("Presente.-")
    doc.add_paragraph()

    # Referencia
    ref = doc.add_paragraph()
    ref.add_run("Ref: ").bold = True
    ref.add_run(referencia(licitacion))
    doc.add_paragraph()

    # Cuerpo
    representante = legales["representante"]
    cargo = legales["cargo"]
    razon = empresa["razon_social"]
    ruc = empresa["ruc"]

    body = (
        f"Estimados señores:\n\n"
        f"El que suscribe, {representante}, identificado con DNI N° "
        f"{legales['dni']}, "
        f"en calidad de {cargo} y representante legal de {razon}, "
        f"identificada con RUC N° {ruc}, domiciliada en "
        f"{legales['domicilio']}, "
        f"presenta a ustedes la propuesta técnica y económica para la contratación de:\n\n"
        f"\"{licitacion['objeto']}\"\n\n"
        f"Para lo cual declaramos que cumplimos con los requisitos de calificación y "
        f"demás condiciones establecidas en las Bases del presente procedimiento de selección."
    )
    doc.add_paragraph(body)

    doc.add_paragraph()
    doc.add_paragraph("Atentamente,")
    doc.add_paragraph()

    # Insertar firma del representante (imagen)
    firma_path = await obtener_firma(empresa_id, 'firma')
    if firma_path:
        insertar_imagen_en_docx(doc, firma_path, width_cm=4.0)
    else:
        doc.add_paragraph()
        p_line = doc.add_paragraph()
        p_line.add_run("_" * 40)

    doc.add_paragraph(f"{representante}")
    doc.add_paragraph(f"{cargo}")
    doc.add_paragraph(f"{razon}")
    doc.add_paragraph(f"RUC: {ruc}")

    # Insertar sello si existe
    sello_path = await obtener_firma(empresa_id, 'sello')
    if sello_path:
        insertar_imagen_en_docx(doc, sello_path, width_cm=3.0)

    path = await _guardar(doc, f"carta_presentacion_{propuesta_id}.docx")
    log.info(f"Carta de presentación generada: {path}")
    return path


async def generar_declaracion_jurada(propuesta_id: int, empresa_id: int, licitacion: dict) -> str:
    """Genera Anexo 2: Declaración Jurada del postor."""
    empresa = await get_empresa(empresa_id)
    datos = await _recopilar_datos(empresa_id)
    legales = datos_legales(empresa, datos)
    exigir("la declaración jurada", legales, "representante", "domicilio")

    doc = await _documento(empresa_id)
    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(11)

    doc.add_heading("DECLARACION JURADA", level=1).alignment = WD_ALIGN_PARAGRAPH.CENTER

    ref = doc.add_paragraph()
    ref.alignment = WD_ALIGN_PARAGRAPH.CENTER
    ref.add_run(f"({referencia(licitacion)})")
    doc.add_paragraph()

    representante = legales["representante"]
    razon = empresa["razon_social"]
    ruc = empresa["ruc"]

    declaraciones = [
        f"Que {razon}, con RUC N° {ruc}, no se encuentra incursa en ninguno de los impedimentos para contratar con el Estado previstos en el artículo 11 de la Ley de Contrataciones del Estado.",
        "Que conocemos, aceptamos y nos sometemos a las condiciones y procedimientos del presente proceso de selección.",
        "Que somos responsables de la veracidad de los documentos e información que presentamos.",
        "Que nos comprometemos a mantener nuestra oferta durante el proceso de selección y a suscribir el contrato en caso de resultar favorecidos con la Buena Pro.",
        "Que no tenemos impedimento para participar en el proceso de selección ni para contratar con el Estado.",
        "Que conocemos las sanciones contenidas en la Ley de Contrataciones del Estado y su Reglamento.",
    ]

    for i, decl in enumerate(declaraciones, 1):
        p = doc.add_paragraph()
        p.add_run(f"{i}. ").bold = True
        p.add_run(decl)
        p.paragraph_format.space_after = Pt(6)

    doc.add_paragraph()
    doc.add_paragraph(f"{legales['domicilio']}, {fecha_larga()}")
    doc.add_paragraph()

    # Firma del representante
    firma_path = await obtener_firma(empresa_id, 'firma')
    if firma_path:
        insertar_imagen_en_docx(doc, firma_path, width_cm=4.0)
    else:
        doc.add_paragraph()
        doc.add_paragraph("_" * 40)

    doc.add_paragraph(f"{representante}")
    doc.add_paragraph("Representante Legal")
    doc.add_paragraph(f"{razon}")

    sello_path = await obtener_firma(empresa_id, 'sello')
    if sello_path:
        insertar_imagen_en_docx(doc, sello_path, width_cm=3.0)

    return await _guardar(doc, f"declaracion_jurada_{propuesta_id}.docx")


async def generar_experiencia_postor(propuesta_id: int, empresa_id: int, licitacion: dict) -> str:
    """Genera Anexo 4: Experiencia del Postor."""
    empresa = await get_empresa(empresa_id)

    async with connection() as conn:
        experiencias = await conn.fetch(
            "SELECT * FROM experiencia WHERE empresa_id=$1 ORDER BY monto DESC LIMIT 10",
            empresa_id,
        )

    doc = await _documento(empresa_id)
    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(10)

    doc.add_heading("EXPERIENCIA DEL POSTOR", level=1).alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph(f"Empresa: {empresa['razon_social']} | RUC: {empresa['ruc']}")
    doc.add_paragraph()

    table = doc.add_table(rows=1, cols=6)
    table.style = "Table Grid"
    headers = ["N°", "Entidad Contratante", "Objeto del Contrato", "Monto (S/)", "Fecha Inicio", "Fecha Fin"]
    for i, h in enumerate(headers):
        _set_cell_text(table.rows[0].cells[i], h, bold=True, size=9)

    for idx, exp in enumerate(experiencias, 1):
        row = table.add_row()
        _set_cell_text(row.cells[0], str(idx), size=9)
        _set_cell_text(row.cells[1], exp["entidad_contratante"] or "", size=9)
        _set_cell_text(row.cells[2], exp["objeto_contrato"] or "", size=9)
        _set_cell_text(row.cells[3], format_monto(exp["monto"]) if exp["monto"] else "—", size=9)
        _set_cell_text(row.cells[4], format_fecha(exp["fecha_inicio"]), size=9)
        _set_cell_text(row.cells[5], format_fecha(exp["fecha_fin"]), size=9)

    if experiencias:
        total = sum(e["monto"] for e in experiencias if e["monto"])
        doc.add_paragraph(f"\nMonto acumulado: {format_monto(total)}")

    return await _guardar(doc, f"experiencia_postor_{propuesta_id}.docx")


async def generar_propuesta_economica(propuesta_id: int, empresa_id: int,
                                       licitacion: dict, precio) -> str:
    """Genera propuesta económica.

    `precio` llega como Decimal (precio_ofertado es NUMERIC): el desglose se
    hace en Decimal y suma el total al centimo. `precio / 1.18` era un
    TypeError con Decimal, y con float podia descuadrar un centimo.
    """
    empresa = await get_empresa(empresa_id)
    datos = await _recopilar_datos(empresa_id)
    legales = datos_legales(empresa, datos)
    exigir("la propuesta económica", legales, "representante")

    doc = await _documento(empresa_id)
    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(11)

    doc.add_heading("PROPUESTA ECONOMICA", level=1).alignment = WD_ALIGN_PARAGRAPH.CENTER
    doc.add_paragraph(f"Ref: {referencia(licitacion)}")
    doc.add_paragraph()

    doc.add_paragraph(f"Señores: {licitacion['entidad']}")
    doc.add_paragraph()

    representante = legales["representante"]
    body = (
        f"Es grato dirigirnos a ustedes para presentar nuestra propuesta económica "
        f"por el monto total de {format_monto(precio)} (incluido IGV) para la contratación de:\n\n"
        f"\"{licitacion['objeto']}\"\n\n"
        f"Esta oferta incluye todos los tributos, seguros, transportes, inspecciones, "
        f"pruebas, y de ser el caso, los costos laborales conforme a la legislación vigente, "
        f"así como cualquier otro concepto que pueda tener incidencia sobre el costo."
    )
    doc.add_paragraph(body)

    table = doc.add_table(rows=4, cols=2)
    table.style = "Table Grid"
    _set_cell_text(table.rows[0].cells[0], "Concepto", bold=True)
    _set_cell_text(table.rows[0].cells[1], "Monto", bold=True)
    _set_cell_text(table.rows[1].cells[0], "Subtotal sin IGV")
    subtotal, igv = desglose_igv(precio)
    _set_cell_text(table.rows[1].cells[1], format_monto(subtotal))
    _set_cell_text(table.rows[2].cells[0], "IGV (18%)")
    _set_cell_text(table.rows[2].cells[1], format_monto(igv))
    _set_cell_text(table.rows[3].cells[0], "TOTAL", bold=True)
    _set_cell_text(table.rows[3].cells[1], format_monto(precio), bold=True)

    doc.add_paragraph()
    doc.add_paragraph(fecha_larga())
    doc.add_paragraph()

    firma_path = await obtener_firma(empresa_id, 'firma')
    if firma_path:
        insertar_imagen_en_docx(doc, firma_path, width_cm=4.0)
    else:
        firma = doc.add_paragraph()
        firma.add_run("_" * 40)

    doc.add_paragraph(f"{representante}")
    doc.add_paragraph(f"{empresa['razon_social']} | RUC: {empresa['ruc']}")

    sello_path = await obtener_firma(empresa_id, 'sello')
    if sello_path:
        insertar_imagen_en_docx(doc, sello_path, width_cm=3.0)

    return await _guardar(doc, f"propuesta_economica_{propuesta_id}.docx")


async def _recopilar_datos(empresa_id: int) -> dict:
    """Recopila todos los datos de KB para una empresa."""
    async with connection() as conn:
        rows = await conn.fetch(
            "SELECT categoria, clave, valor FROM knowledge_base WHERE empresa_id=$1",
            empresa_id,
        )
    return {f"{r['categoria']}.{r['clave']}": r["valor"] for r in rows}
