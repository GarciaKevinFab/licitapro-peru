"""Los anexos 05 a 09 de la Municipalidad Provincial de Tambopata, llenos.

QUE SON

  Para sus contrataciones menores a 8 UIT la MPT publica un ZIP con seis
  formatos en Word (portal.munitambopata.gob.pe/servicios/contratacion-bienes-
  servicios). Se presentan llenos, firmados y sellados, junto con la SC:

    05  Oferta economica             conceptos, precios, plazo y datos del postor
    06  DJ de notificacion           datos del postor y del representante
    07  Carta de autorizacion de CCI CCI, banco, titular y RUC
    08  DJ de contratos menores      nombre y DNI del representante
    09  DJ de prohibiciones          razon social y DNI del representante
    10  Requisitos minimos           experiencia y formacion: depende de cada
                                     TDR y no se llena aqui (ver ANEXOS_A_MANO)

COMO SE LLENAN

  Sobre el .docx de la entidad, con python-docx, y despues a PDF con
  LibreOffice (shared/pdf_firma.docx_a_pdf). No se redibuja nada: el formato
  es el de la municipalidad, como pidio el usuario para el GOREMAD.

  Los marcadores ("[CONSIGNAR ...]", "……") vienen partidos en varios
  fragmentos de texto con formato propio. `_sustituir` reemplaza a traves de
  ellos sin perder negritas ni tipografia.

  Lo que no se sabe (sede registral, asiento) se deja en puntos para llenarlo
  a mano: un dato inventado en una declaracion jurada es peor que un hueco.

DE DONDE SALEN LAS PLANTILLAS

  Del ZIP del portal, bajado como mucho una vez al dia (data/plantillas_mpt).
  Si el portal no responde se usan las copias del repositorio
  (shared/plantillas/mpt_tambopata), bajadas el 2026-10-15.
"""
from __future__ import annotations

import copy
import io
import logging
import os
import re
import tempfile
import time
import zipfile
from datetime import date
from pathlib import Path

log = logging.getLogger("shared.anexos_mpt")

URL_FORMATOS = ("https://portal.munitambopata.gob.pe/storage/2025/abastecimiento/"
                "formatos-zip.zip")
REPO = Path(__file__).parent / "plantillas" / "mpt_tambopata"
CACHE = Path(os.getenv("PLANTILLAS_MPT_DIR", "data/plantillas_mpt"))
UN_DIA = 24 * 3600

ANEXOS = {
    "05": "Anexo_05_Oferta_Economica",
    "06": "Anexo_06_DJ_Notificacion",
    "07": "Anexo_07_Carta_Autorizacion_CCI",
    "08": "Anexo_08_DJ_Contratos_Menores",
    "09": "Anexo_09_DJ_Prohibiciones",
}
ANEXOS_A_MANO = {"10": "Anexo 10 - Cumplimiento de los requisitos mínimos (según el TDR)"}
PUNTOS = "……………………"


# ─── Plantillas ─────────────────────────────────────────────

def _refrescar_cache() -> None:
    """Baja el ZIP del portal si la copia local tiene mas de un dia."""
    marca = CACHE / ".bajado"
    if marca.exists() and time.time() - marca.stat().st_mtime < UN_DIA:
        return
    try:
        import httpx
        r = httpx.get(URL_FORMATOS, timeout=20, follow_redirects=True)
        r.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(r.content))
        CACHE.mkdir(parents=True, exist_ok=True)
        for info in z.infolist():
            m = re.match(r"Anexo (\d\d)", Path(info.filename).name)
            if m and info.filename.lower().endswith(".docx"):
                (CACHE / f"anexo_{m.group(1)}.docx").write_bytes(z.read(info))
        marca.touch()
    except Exception as e:  # noqa: BLE001
        log.warning("No se pudo bajar el ZIP de formatos de la MPT (%s): se usa la copia "
                    "del repositorio", e)


def plantilla(numero: str) -> Path:
    _refrescar_cache()
    propia = CACHE / f"anexo_{numero}.docx"
    return propia if propia.exists() else REPO / f"anexo_{numero}.docx"


# ─── Utilidades de python-docx ──────────────────────────────

def _sustituir(parrafo, patron: str, nuevo: str) -> bool:
    """Reemplaza la primera coincidencia de `patron` en el parrafo aunque este
    partida entre varios fragmentos (runs). El texto nuevo hereda el formato
    del primer fragmento tocado; los demas solo pierden lo reemplazado."""
    runs = parrafo.runs
    originales = [r.text for r in runs]
    m = re.search(patron, "".join(originales))
    if not m:
        return False
    a, b = m.span()
    pos, primero = 0, True
    for r, t in zip(runs, originales, strict=True):
        s0, s1 = pos, pos + len(t)
        pos = s1
        if s1 <= a or s0 >= b:
            continue
        ini, fin = max(a, s0) - s0, min(b, s1) - s0
        r.text = t[:ini] + (nuevo if primero else "") + t[fin:]
        if primero:
            _en_negro(r)
        primero = False
    return True


def _en_negro(run) -> None:
    """Los marcadores de la entidad van en rojo y cursiva para que se vean;
    lo llenado va como texto normal."""
    from docx.shared import RGBColor
    run.font.color.rgb = RGBColor(0, 0, 0)
    run.italic = False


def _poner(celda, texto: str, negrita: bool | None = None, tamano: float | None = None) -> None:
    """El texto de una celda, conservando el formato de su primer fragmento."""
    p = celda.paragraphs[0]
    if tamano:
        from docx.shared import Pt
        p.paragraph_format.space_before = p.paragraph_format.space_after = Pt(0)
    if p.runs:
        p.runs[0].text = texto
        for r in p.runs[1:]:
            r.text = ""
        if negrita is not None:
            p.runs[0].bold = negrita
    else:
        r = p.add_run(texto)
        if negrita is not None:
            r.bold = negrita
    if tamano:
        p.runs[0].font.size = Pt(tamano)
    for extra in celda.paragraphs[1:]:
        for r in extra.runs:
            r.text = ""


def _agregar(celda, texto: str) -> None:
    """Texto a continuacion de la etiqueta que ya tiene la celda ("RUC: ...")."""
    r = celda.paragraphs[0].add_run(" " + texto)
    r.bold = False


def _fecha(d: date) -> str:
    """dd/mm/aaaa: la celda del lugar y fecha es angosta y la fecha larga se
    partia en tres renglones, empujando la firma a una hoja nueva."""
    return d.strftime("%d/%m/%Y")


def _imagen_compuesta(imagenes: dict) -> str | None:
    """Una sola imagen con el sello y la firma encima, como se firma a mano.

    En Word las imagenes en linea no se pueden encimar: si la empresa tiene
    firma y sello por separado se componen aqui en un PNG temporal."""
    firma, sello = imagenes.get("firma"), imagenes.get("sello")
    if not (firma or sello):
        return None
    if not (firma and sello):
        return firma or sello
    from PIL import Image
    s = Image.open(sello).convert("RGBA")
    f = Image.open(firma).convert("RGBA")
    escala = min(s.width * 0.6 / f.width, s.height * 0.75 / f.height)
    f = f.resize((max(1, int(f.width * escala)), max(1, int(f.height * escala))))
    lienzo = s.copy()
    lienzo.alpha_composite(f, ((s.width - f.width) // 2, max(0, (s.height - f.height) // 2 - 8)))
    destino = Path(tempfile.mkdtemp(prefix="firma_")) / "firma_sello.png"
    lienzo.save(destino)
    return str(destino)


def _flotante(run, sube) -> None:
    """Convierte la imagen del `run` en flotante, sin ajuste de texto y
    centrada en la columna: se estampa encima del espacio de firma, como un
    sello en papel, sin agrandar la fila. En linea empujaba el rotulo de firma
    a una hoja nueva. `sube`: cuanto se eleva sobre el parrafo (EMU, negativo
    hacia arriba)."""
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls, qn
    inline = run._r.find(".//" + qn("wp:inline"))
    if inline is None:
        return
    extent = inline.find(qn("wp:extent"))
    doc_pr = inline.find(qn("wp:docPr"))
    grafico = inline.find(qn("a:graphic"))
    anchor = parse_xml(
        f'<wp:anchor {nsdecls("wp", "a")} distT="0" distB="0" distL="0" distR="0" '
        f'simplePos="0" relativeHeight="251659264" behindDoc="0" locked="0" '
        f'layoutInCell="1" allowOverlap="1">'
        f'<wp:simplePos x="0" y="0"/>'
        f'<wp:positionH relativeFrom="column"><wp:align>center</wp:align></wp:positionH>'
        f'<wp:positionV relativeFrom="paragraph"><wp:posOffset>{int(sube)}</wp:posOffset>'
        f'</wp:positionV>'
        f'<wp:extent cx="{extent.get("cx")}" cy="{extent.get("cy")}"/>'
        f'<wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/></wp:anchor>')
    anchor.append(doc_pr)
    anchor.append(parse_xml(f'<wp:cNvGraphicFramePr {nsdecls("wp")}/>'))
    anchor.append(grafico)
    inline.getparent().replace(inline, anchor)


def _firmar(tabla, empresa: dict, hoy: date, imagen: str | None) -> None:
    """La tabla del pie (3x3): lugar y fecha, firma y sello, nombre."""
    from docx.shared import Cm, Pt
    filas = tabla.rows
    _agregar(filas[0].cells[0], _fecha(hoy))
    # Lugar, fecha y firma en la misma hoja: si la oferta crece y no cabe, el
    # bloque entero pasa a la siguiente en vez de quedar la fecha sola arriba.
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    for fila in filas:
        tr_pr = fila._tr.get_or_add_trPr()
        if tr_pr.find(qn("w:cantSplit")) is None:
            tr_pr.append(OxmlElement("w:cantSplit"))
        for celda in fila.cells:
            for par in celda.paragraphs:
                par.paragraph_format.keep_with_next = True
    if imagen and len(filas) > 1 and len(filas[1].cells) > 1:
        p = filas[1].cells[1].paragraphs[0]
        run = p.add_run()
        run.add_picture(imagen, width=Cm(4.6))
        _flotante(run, Cm(-1.1))
    # El sello ya dice nombre, cargo y RUC: los datos escritos solo van cuando
    # no hay sello (se firma a mano o con DNIe).
    if not imagen and len(filas) > 2 and len(filas[2].cells) > 1:
        cel = filas[2].cells[1]
        for texto, neg in ((empresa.get("representante_legal") or "", True),
                           (f"DNI N° {empresa.get('dni_representante') or ''}", False),
                           (empresa.get("razon_social") or "", True)):
            if texto.strip():
                p = cel.add_paragraph()
                p.alignment = 1
                r = p.add_run(texto)
                r.bold, r.font.size = neg, Pt(8)


# ─── Cada anexo ─────────────────────────────────────────────

def _anexo_05(doc, d: dict) -> None:
    from shared.cotizacion import cantidad_legible, monto, precio_legible
    for p in doc.paragraphs:
        _sustituir(p, r"\[INDICAR EL N[ÚU]MERO DE ESTUDIO DE MERCADO\]\s*\(OBLIGATORIO\)",
                   d["referencia"])
    conceptos, datos, pie = doc.tables[0], doc.tables[1], doc.tables[2]
    modelo = copy.deepcopy(conceptos.rows[1]._tr)
    for k, it in enumerate(d["items"]):
        if k > 0:
            conceptos._tbl.append(copy.deepcopy(modelo))
        fila = conceptos.rows[-1]
        marca = " - ".join(t for t in ((f"Marca: {it['marca']}" if it.get("marca") else ""),
                                       (it.get("presentacion") or "")) if t)
        concepto = it.get("descripcion") or ""
        if marca:
            concepto += f" - {marca}"
        cantidad = cantidad_legible(it.get("cantidad")) if it.get("cantidad") is not None else ""
        if cantidad and it.get("unidad"):
            cantidad += f" {it['unidad']}"
        # Letra 8 en los renglones de items: con la de la plantilla, dos items y
        # el total ya mandaban la firma a una segunda hoja.
        _poner(fila.cells[0], concepto, negrita=False, tamano=8)
        _poner(fila.cells[1], cantidad, negrita=False, tamano=8)
        _poner(fila.cells[2], precio_legible(it["precio_unitario"])
               if it.get("precio_unitario") is not None else "", negrita=False, tamano=8)
        _poner(fila.cells[3], monto(it["subtotal"]) if it.get("subtotal") is not None else "",
               negrita=False, tamano=8)
    conceptos._tbl.append(copy.deepcopy(modelo))
    fila = conceptos.rows[-1]
    _poner(fila.cells[0], "", negrita=False, tamano=8)
    _poner(fila.cells[1], "", negrita=False, tamano=8)
    _poner(fila.cells[2], "TOTAL S/", negrita=True, tamano=8)
    _poner(fila.cells[3], monto(d["total"]), negrita=True, tamano=8)

    e, c = d["empresa"], d["condiciones"]
    valores = [c.get("plazo_entrega") or PUNTOS, c.get("garantia") or PUNTOS,
               e.get("razon_social") or "", e.get("ruc") or "", e.get("direccion") or "",
               e.get("email") or "", e.get("telefono") or ""]
    for fila, valor in zip(datos.rows, valores, strict=False):
        _poner(fila.cells[-1], valor, negrita=False)
    _firmar(pie, e, d["hoy"], d["imagen"])


def _anexo_06(doc, d: dict) -> None:
    e = d["empresa"]
    juridica = (e.get("ruc") or "").startswith("20")

    def registral(campo, valor):
        return f"{campo} {valor or PUNTOS}" if juridica else f"{campo} —"

    for p in doc.paragraphs:
        _sustituir(p, r"\[[…\.]+\]", e.get("representante_legal") or PUNTOS)
        _sustituir(p, r"\[CONSIGNAR SEG[ÚU]N CORRESPONDA\]", e.get("razon_social") or PUNTOS)
        _sustituir(p, r"\[CONSIGNAR TIPO DE DOCUMENTO DE IDENTIDAD\]", "DNI")
        _sustituir(p, r"\[CONSIGNAR N[ÚU]MERO DE DOCUMENTO DE IDENTIDAD\]",
                   e.get("dni_representante") or PUNTOS)
        _sustituir(p, r"Sede Registral de \[CONSIGNAR EN CASO DE SER PERSONA JUR[ÍI]DICA\]",
                   registral("Sede Registral de", d.get("sede_registral")))
        _sustituir(p, r"Partida Registral N[ºo°] \[CONSIGNAR EN CASO DE SER PERSONA JUR[ÍI]DICA\]",
                   registral("Partida Registral Nº", d.get("partida")))
        _sustituir(p, r"Asiento N[ºo°] \[CONSIGNAR EN CASO DE SER PERSONA JUR[ÍI]DICA\]",
                   registral("Asiento Nº", d.get("asiento")))
    t = doc.tables[0]
    _poner(t.rows[0].cells[1], e.get("razon_social") or "", negrita=False)
    _poner(t.rows[1].cells[1], e.get("direccion") or "", negrita=False)
    _agregar(t.rows[2].cells[0], e.get("ruc") or "")
    _poner(t.rows[2].cells[3], e.get("telefono") or "", negrita=False)
    _agregar(t.rows[3].cells[0], e.get("email") or "")
    _firmar(doc.tables[1], e, d["hoy"], d["imagen"])


def _anexo_07(doc, d: dict) -> None:
    e = d["empresa"]
    valores = (d.get("cci") or PUNTOS, d.get("banco") or PUNTOS,
               e.get("razon_social") or "", e.get("ruc") or "")
    for fila, valor in zip(doc.tables[0].rows, valores, strict=False):
        _poner(fila.cells[-1], valor, negrita=False)
    _firmar(doc.tables[1], e, d["hoy"], d["imagen"])


def _anexo_08(doc, d: dict) -> None:
    e = d["empresa"]
    for p in doc.paragraphs:
        _sustituir(p, r"declarante\s*[…\.]+\s*con DNI\s*[…\.]+",
                   f"declarante {e.get('representante_legal') or PUNTOS}, con DNI "
                   f"{e.get('dni_representante') or PUNTOS},")
    _firmar(doc.tables[0], e, d["hoy"], d["imagen"])


def _anexo_09(doc, d: dict) -> None:
    e = d["empresa"]
    for p in doc.paragraphs:
        _sustituir(p, r"Representante Legal de\s*[…\s]+,\s*identificado/a con DNI N°\s*[…\s]+,",
                   f"Representante Legal de {e.get('razon_social') or PUNTOS}, identificado/a "
                   f"con DNI N° {e.get('dni_representante') or PUNTOS},")
    _firmar(doc.tables[0], e, d["hoy"], d["imagen"])


_LLENADORES = {"05": _anexo_05, "06": _anexo_06, "07": _anexo_07, "08": _anexo_08,
               "09": _anexo_09}


def llenar_docx(numero: str, datos: dict) -> bytes:
    """El .docx del anexo lleno. `datos`: empresa, items, condiciones, total,
    referencia, cci, banco, partida, hoy e imagenes."""
    import docx
    doc = docx.Document(str(plantilla(numero)))
    d = {**datos, "imagen": _imagen_compuesta(datos.get("imagenes") or {})}
    _LLENADORES[numero](doc, d)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def anexos_pdf(datos: dict) -> tuple[dict[str, bytes], str | None]:
    """{nombre de archivo: contenido} de los anexos 05 a 09, en PDF.

    Si LibreOffice no esta, van en .docx y el segundo valor lo explica: el
    postor puede exportarlos desde Word, pero no se le esconde que faltan."""
    from shared.pdf_firma import docx_a_pdf, hay_conversor
    salida: dict[str, bytes] = {}
    aviso = None
    with tempfile.TemporaryDirectory(prefix="anexos_mpt_") as carpeta:
        for numero, nombre in ANEXOS.items():
            contenido = llenar_docx(numero, datos)
            if not hay_conversor():
                salida[f"{nombre}.docx"] = contenido
                aviso = ("Los anexos van en Word: este servidor no tiene LibreOffice para "
                         "pasarlos a PDF.")
                continue
            ruta = Path(carpeta) / f"{nombre}.docx"
            ruta.write_bytes(contenido)
            pdf, mensaje = await docx_a_pdf(str(ruta), carpeta)
            if pdf:
                salida[f"{nombre}.pdf"] = Path(pdf).read_bytes()
            else:
                salida[f"{nombre}.docx"] = contenido
                aviso = f"Los anexos van en Word: {mensaje}"
    return salida, aviso
