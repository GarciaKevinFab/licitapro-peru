"""Conversion a PDF para poder firmar con el DNI electronico.

POR QUE HACE FALTA PDF

  Un .docx no admite firma digital: el formato no tiene donde alojarla. La
  firma digital peruana (Ley 27269) se incrusta en el PDF como PAdES. Para que
  el cliente pueda firmar con su DNIe, el documento tiene que ser PDF.

QUE **NO** PUEDE HACER ESTA PLATAFORMA, Y POR QUE

  Firmar con el DNIe del cliente desde el servidor. No es cuestion de esfuerzo:
  la clave privada vive dentro del chip de la tarjeta y no sale de ahi nunca.
  Firmar exige la tarjeta fisica, el lector y el PIN del titular.

  Cualquier servicio que afirme firmar con el DNIe del cliente "en la nube" o
  guarda el PIN -- lo cual seria ilegal ademas de inseguro -- o no esta usando
  el DNIe de verdad.

  Lo que si se hace: dejar el PDF listo. El cliente lo firma en su maquina con
  Firma Peru (RENIEC) o con Adobe Reader y el controlador del lector, y sube el
  PDF ya firmado.

POR QUE LIBREOFFICE Y NO REPORTLAB

  Reutiliza la maquetacion de los DOCX que ya genera prep_bot. Rehacer esos
  documentos en reportlab seria duplicar logica que funciona y condenarla a
  divergir. Si LibreOffice no esta instalado NO se falla en silencio: se
  devuelve el motivo para poder explicarselo al usuario.
"""
import asyncio
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("shared.pdf_firma")

# En la imagen Docker se instala libreoffice-writer. En Windows el binario
# suele llamarse soffice.exe y no estar en el PATH.
BINARIOS = ("soffice", "libreoffice", "soffice.exe")

SEGUNDOS_TIMEOUT = 90


def hay_conversor() -> str | None:
    """Ruta del binario de LibreOffice, o None si no esta disponible."""
    for nombre in BINARIOS:
        ruta = shutil.which(nombre)
        if ruta:
            return ruta
    return None


async def docx_a_pdf(ruta_docx: str, carpeta_destino: str | None = None) -> tuple[str | None, str]:
    """Convierte un .docx a .pdf. Devuelve (ruta_pdf, mensaje).

    ruta_pdf es None si no se pudo convertir; el mensaje explica por que, para
    poder mostrarselo al usuario en vez de dejarlo adivinando.
    """
    if not os.path.isfile(ruta_docx):
        return None, "El documento de origen no existe."

    binario = hay_conversor()
    if not binario:
        return None, ("La conversión a PDF necesita LibreOffice, que no está "
                      "instalado en este servidor. Puedes descargar el DOCX y "
                      "exportarlo a PDF desde tu equipo.")

    destino = carpeta_destino or os.path.dirname(ruta_docx)
    os.makedirs(destino, exist_ok=True)

    try:
        proceso = await asyncio.create_subprocess_exec(
            binario, "--headless", "--norestore",
            "--convert-to", "pdf", "--outdir", destino, ruta_docx,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proceso.communicate(),
                                            timeout=SEGUNDOS_TIMEOUT)
        except asyncio.TimeoutError:
            proceso.kill()
            return None, "La conversión a PDF tardó demasiado y se canceló."
    except Exception as e:
        log.error("Fallo al invocar LibreOffice: %s", e, exc_info=True)
        return None, "No se pudo ejecutar el conversor de PDF."

    esperado = Path(destino) / (Path(ruta_docx).stem + ".pdf")
    if not esperado.is_file():
        log.error("LibreOffice no produjo el PDF: %s", (err or b"")[:300])
        return None, "El conversor no generó el PDF."

    log.info("PDF generado: %s", esperado.name)
    return str(esperado), "PDF generado."


def es_pdf(datos: bytes) -> bool:
    """Comprueba la cabecera real, no la extension del nombre."""
    return datos[:5] == b"%PDF-"


def tiene_firma_digital(datos: bytes) -> bool | None:
    """True si el PDF parece llevar una firma incrustada. None si no se puede saber.

    Es una comprobacion de forma, NO una validacion criptografica: dice que hay
    un objeto de firma, no que sea valida ni de quien. Validar de verdad exige
    verificar la cadena de certificados contra la CA de RENIEC, y eso no se
    improvisa. Sirve para avisar al usuario si sube el PDF sin firmar.
    """
    try:
        from pypdf import PdfReader
        from io import BytesIO
        lector = PdfReader(BytesIO(datos))
        campos = lector.get_fields() or {}
        return any(c.get("/FT") == "/Sig" for c in campos.values())
    except Exception as e:
        log.info("No se pudo inspeccionar el PDF: %s", e)
        return None


def instrucciones_dnie() -> dict:
    """Texto para la interfaz. Vive aqui para que haya una sola version."""
    return {
        "pasos": [
            "Descarga el PDF a tu computadora.",
            "Conecta el lector con tu DNI electrónico insertado.",
            "Ábrelo con Firma Perú (RENIEC) o con Adobe Reader configurado "
            "con el controlador del lector.",
            "Firma con el certificado de tu DNIe e ingresa tu PIN.",
            "Vuelve aquí y sube el PDF firmado.",
        ],
        "nota": ("Tu PIN y tu certificado nunca salen de tu tarjeta ni pasan por "
                 "LicitaPro. La firma ocurre en tu equipo; nosotros solo "
                 "preparamos el documento y guardamos el resultado."),
    }
