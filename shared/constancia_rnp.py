"""Lee la "Constancia de inscripcion" del RNP (PDF) para rellenar el RNP de la empresa.

POR QUE DEL PDF Y NO DE LA WEB DEL RNP

  La constancia sale de rnp.gob.pe/constancia/.../default_Todos.asp?RUC=, y esa
  URL responde sin captcha. Pero la pagina de busqueda que lleva a ella SI
  tiene captcha (ValidaCertificadoTodos.asp, campo captchacode). Pedir la
  constancia directo desde el servidor seria saltarse ese control, y no se
  evaden controles anti-bot de sistemas del Estado (mismo criterio que
  shared/sunat.py). El usuario descarga su constancia y la sube.

POR QUE HACE FALTA OCR

  La constancia es la pagina web impresa a PDF desde el navegador, y la fuente
  queda incrustada SIN tabla de caracteres: pypdf devuelve "/0/1/2/i255/..." en
  vez de texto. Se intenta primero el texto (hay PDFs que si lo traen) y, si
  no aparece "REGISTRO NACIONAL", se renderiza la pagina y se lee con
  Tesseract.

QUE SE SACA

  RUC, razon social y cada capitulo con su "Desde" y, si lo hubiera, su
  "Hasta". Desde 2017 (bienes y servicios) y con la Ley 32069 (obras) la
  inscripcion es de vigencia indeterminada: la constancia solo dice "Desde".
  Entonces NO hay fecha de vencimiento y no se inventa una.
"""
from __future__ import annotations

import io
import logging
import re
from datetime import date

log = logging.getLogger("shared.constancia_rnp")

MAX_BYTES = 5 * 1024 * 1024
MAX_PAGINAS = 2
DPI_OCR = 200


class ConstanciaInvalida(ValueError):
    """El archivo no es una constancia legible. El mensaje es para el usuario."""


# Nombre en la constancia -> como se escribe en el campo "Capitulo del RNP".
CAPITULOS = {
    "PROVEEDOR DE BIENES": "Bienes",
    "PROVEEDOR DE SERVICIOS": "Servicios",
    "EJECUTOR DE OBRAS": "Ejecutor de Obras",
    "CONSULTOR DE OBRAS": "Consultor de Obras",
}
_RE_CAPITULO = re.compile("|".join(CAPITULOS))
_RE_FECHA = r"(\d{2}/\d{2}/\d{4})"


def _fecha(texto: str | None) -> date | None:
    if not texto:
        return None
    # date() y no datetime.strptime: es un dia del calendario, no un instante.
    try:
        dia, mes, anio = (int(x) for x in texto.split("/"))
        return date(anio, mes, dia)
    except ValueError:
        return None


def parsear_texto(texto: str) -> dict:
    """Datos de la constancia a partir de su texto (extraido u OCR)."""
    plano = re.sub(r"[ \t]+", " ", texto or "")
    if "REGISTRO NACIONAL" not in plano.upper():
        raise ConstanciaInvalida("Ese PDF no parece una constancia del RNP.")

    ruc = re.search(r"RUC\s*N\s*\S{0,2}\s*(\d{11})", plano)
    if not ruc:
        ruc = re.search(r"\b((?:10|15|16|17|20)\d{9})\b", plano)

    # Cada capitulo, con el tramo de texto hasta el siguiente.
    marcas = list(_RE_CAPITULO.finditer(plano))
    capitulos = []
    for i, m in enumerate(marcas):
        tramo = plano[m.end(): marcas[i + 1].start() if i + 1 < len(marcas) else len(plano)]
        desde = re.search(r"Desde\s*" + _RE_FECHA, tramo)
        hasta = re.search(r"Hasta\s*" + _RE_FECHA, tramo)
        capitulos.append({
            "capitulo": CAPITULOS[m.group(0)],
            "desde": _fecha(desde.group(1) if desde else None),
            "hasta": _fecha(hasta.group(1) if hasta else None),
        })
    if not capitulos:
        raise ConstanciaInvalida(
            "No se encontró ningún capítulo vigente en la constancia.")

    vencimientos = [c["hasta"] for c in capitulos if c["hasta"]]
    return {
        "ruc": ruc.group(1) if ruc else None,
        "capitulos": capitulos,
        # Para el formulario:
        "rnp_numero": ruc.group(1) if ruc else None,
        "rnp_categoria": ", ".join(c["capitulo"] for c in capitulos),
        # La que vence antes manda; None si todas son indeterminadas.
        "rnp_vigencia": min(vencimientos) if vencimientos else None,
        "indeterminada": not vencimientos,
    }


def _texto_pdf(contenido: bytes) -> str:
    from pypdf import PdfReader
    lector = PdfReader(io.BytesIO(contenido))
    return "\n".join((p.extract_text() or "") for p in lector.pages[:MAX_PAGINAS])


def _texto_ocr(contenido: bytes) -> str:
    import pypdfium2 as pdfium
    import pytesseract

    documento = pdfium.PdfDocument(contenido)
    try:
        partes = []
        for i in range(min(len(documento), MAX_PAGINAS)):
            imagen = documento[i].render(scale=DPI_OCR / 72).to_pil()
            partes.append(pytesseract.image_to_string(imagen, lang="spa"))
        return "\n".join(partes)
    finally:
        documento.close()


def leer_constancia_rnp(contenido: bytes) -> dict:
    """Valida el archivo y devuelve los datos. ConstanciaInvalida si no sirve.

    Es sincrona y el OCR tarda un par de segundos: llamarla con
    asyncio.to_thread desde una ruta."""
    if not contenido or not contenido.startswith(b"%PDF"):
        raise ConstanciaInvalida("El archivo tiene que ser el PDF de la constancia del RNP.")
    if len(contenido) > MAX_BYTES:
        raise ConstanciaInvalida("El PDF pesa demasiado (máximo 5 MB).")
    try:
        texto = _texto_pdf(contenido)
    except Exception as e:
        raise ConstanciaInvalida("No se pudo leer el PDF. ¿Está dañado o protegido?") from e
    if "REGISTRO NACIONAL" in texto.upper():
        return parsear_texto(texto)
    try:
        texto = _texto_ocr(contenido)
    except Exception as e:
        log.warning("OCR de constancia RNP fallido: %s", e)
        raise ConstanciaInvalida(
            "No se pudo leer el texto de la constancia. Escribe los datos a mano.") from e
    return parsear_texto(texto)
