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
import os
import re
from datetime import date
from pathlib import Path

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
# Sin distinguir mayusculas y tolerando espacios de mas entre palabras. POR
# QUE: el OCR de Tesseract a veces lee "Proveedor de Bienes" o parte la linea
# con dos espacios, y entonces el capitulo no aparecia y la constancia se
# rechazaba como "sin ningun capitulo vigente".
_RE_CAPITULO = re.compile(
    "|".join(r"\s+".join(map(re.escape, c.split())) for c in CAPITULOS),
    re.IGNORECASE)
_RE_FECHA = r"(\d{2}/\d{2}/\d{4})"
# El RUC siempre con su etiqueta delante. POR QUE: la reserva anterior tomaba
# el primer numero de 11 digitos que empezara como un RUC, y en una constancia
# hay otros (el codigo de verificacion, el numero de tramite): la constancia
# podia "ser" de un RUC que no era el de la empresa y pasar el control.
_RE_RUC = re.compile(r"\bRUC\b[^\d\n]{0,15}((?:10|15|16|17|20)\d{9})\b", re.IGNORECASE)


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

    ruc = re.search(r"RUC\s*N\s*\S{0,2}\s*(\d{11})", plano) or _RE_RUC.search(plano)

    # Cada capitulo, con el tramo de texto hasta el siguiente.
    marcas = list(_RE_CAPITULO.finditer(plano))
    capitulos = []
    for i, m in enumerate(marcas):
        tramo = plano[m.end(): marcas[i + 1].start() if i + 1 < len(marcas) else len(plano)]
        desde = re.search(r"Desde\s*" + _RE_FECHA, tramo)
        hasta = re.search(r"Hasta\s*" + _RE_FECHA, tramo)
        capitulos.append({
            "capitulo": CAPITULOS[" ".join(m.group(0).upper().split())],
            "desde": _fecha(desde.group(1) if desde else None),
            "hasta": _fecha(hasta.group(1) if hasta else None),
        })
    if not capitulos:
        raise ConstanciaInvalida(
            "No se encontró ningún capítulo vigente en la constancia.")

    vencimientos = [c["hasta"] for c in capitulos if c["hasta"]]
    # "Indeterminada" se afirma solo si se leyo la vigencia de cada capitulo
    # y ninguna trae "Hasta". POR QUE: antes un OCR que no leia NINGUNA fecha
    # daba tambien "vigencia indeterminada", y una constancia vencida se
    # presentaba como "no vence". No haber leido la fecha no es lo mismo que
    # que la fecha no exista.
    leida = all(c["desde"] or c["hasta"] for c in capitulos)
    return {
        "ruc": ruc.group(1) if ruc else None,
        "capitulos": capitulos,
        # Para el formulario:
        "rnp_numero": ruc.group(1) if ruc else None,
        "rnp_categoria": ", ".join(c["capitulo"] for c in capitulos),
        # La que vence antes manda; None si todas son indeterminadas.
        "rnp_vigencia": min(vencimientos) if vencimientos else None,
        "indeterminada": leida and not vencimientos,
        # False cuando algun capitulo salio sin fecha: hay que revisarla a mano.
        "vigencia_leida": leida,
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


# ─── El PDF de la constancia, para adjuntarlo ───────────────
#
# Las cotizaciones del GOREMAD piden adjuntar la constancia del RNP. Cuando el
# usuario la sube para rellenar la empresa, se guarda tambien el archivo: asi
# el paquete de envio la incluye sin pedirsela otra vez.

# Anclado a la raiz del proyecto: relativo al cwd, un proceso lanzado desde
# otra carpeta guardaba la constancia donde el paquete de envio no la busca.
DIR_PDF = Path(os.getenv("RNP_DIR")
               or Path(__file__).resolve().parents[1] / "data" / "rnp")


def ruta_pdf(empresa_id: int) -> Path:
    return DIR_PDF / f"empresa_{int(empresa_id)}.pdf"


def guardar_pdf(empresa_id: int, contenido: bytes) -> None:
    """Guarda con reemplazo atomico: o la constancia vieja o la nueva entera."""
    import tempfile

    destino = ruta_pdf(empresa_id)
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, temporal = tempfile.mkstemp(dir=destino.parent, prefix=".tmp_", suffix=".pdf")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(contenido)
        os.replace(temporal, destino)
    except BaseException:
        Path(temporal).unlink(missing_ok=True)
        raise
