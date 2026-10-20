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
import signal
import tempfile
import time
import uuid
from pathlib import Path

log = logging.getLogger("shared.pdf_firma")

# En la imagen Docker se instala libreoffice-writer. En Windows el binario
# suele llamarse soffice.exe y no estar en el PATH.
BINARIOS = ("soffice", "libreoffice", "soffice.exe")

SEGUNDOS_TIMEOUT = 90

# Conversiones a la vez. POR QUE: cada LibreOffice arranca un proceso de cientos
# de MB; en un VPS chico tres a la vez lo dejan sin memoria. Y con el perfil
# compartido (antes de usar uno por llamada) la segunda simultanea fallaba sin
# mas explicacion que "no genero el PDF".
SIMULTANEAS = max(1, int(os.getenv("LIBREOFFICE_SIMULTANEAS", "2")))
_semaforo: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _semaforo_del_bucle() -> asyncio.Semaphore:
    """Un semaforo por bucle de eventos: queda atado al bucle donde espera."""
    global _semaforo
    bucle = asyncio.get_running_loop()
    if _semaforo is None or _semaforo[0] is not bucle:
        _semaforo = (bucle, asyncio.Semaphore(SIMULTANEAS))
    return _semaforo[1]


def _comando(binario: str, perfil: Path, destino: str, ruta_docx: str) -> list[str]:
    """La linea de LibreOffice. Aparte para que las pruebas la sustituyan.

    `-env:UserInstallation` le da a cada llamada su propio perfil. POR QUE:
    LibreOffice bloquea el perfil del usuario mientras corre; una segunda
    conversion con el mismo perfil se engancha a la primera instancia o sale
    sin hacer nada, y lo unico que se veia era "el conversor no genero el PDF".
    """
    return [binario, f"-env:UserInstallation={perfil.as_uri()}",
            "--headless", "--norestore",
            "--convert-to", "pdf", "--outdir", destino, ruta_docx]


async def _matar(proceso) -> None:
    """Mata LibreOffice y todo lo que haya lanzado, y espera a que muera.

    `soffice` arranca a su vez `soffice.bin`: matar solo al padre dejaba al
    hijo vivo, con el perfil bloqueado y la memoria ocupada. En POSIX el
    proceso nace en su propia sesion (`start_new_session`) y se mata el grupo
    entero. Sin el `wait` posterior el proceso queda zombi.
    """
    try:
        if os.name == "posix":
            os.killpg(proceso.pid, signal.SIGKILL)
        else:
            proceso.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        await asyncio.wait_for(proceso.wait(), timeout=10)
    except TimeoutError:
        log.error("LibreOffice (pid %s) no termino tras matarlo", proceso.pid)


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
    esperado = Path(destino) / (Path(ruta_docx).stem + ".pdf")
    perfil = Path(tempfile.gettempdir()) / f"lo_{uuid.uuid4().hex}"

    async with _semaforo_del_bucle():
        # El PDF de una conversion anterior con el mismo nombre se borra ANTES.
        # POR QUE: si esta conversion fallaba, el `is_file()` de abajo
        # encontraba el viejo y se entregaba como nuevo -- el documento de ayer
        # con los datos de ayer, sin ningun aviso.
        try:
            esperado.unlink(missing_ok=True)
        except OSError:
            log.exception("No se pudo quitar el PDF anterior %s", esperado)
            return None, "No se pudo preparar la conversión a PDF."

        inicio = time.time()
        extra = {"start_new_session": True} if os.name == "posix" else {}
        try:
            proceso = await asyncio.create_subprocess_exec(
                *_comando(binario, perfil, destino, ruta_docx),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                **extra,
            )
            try:
                _, err = await asyncio.wait_for(proceso.communicate(),
                                                timeout=SEGUNDOS_TIMEOUT)
            except TimeoutError:
                await _matar(proceso)
                esperado.unlink(missing_ok=True)
                return None, "La conversión a PDF tardó demasiado y se canceló."
            except asyncio.CancelledError:
                # El cliente cerro la pestana: la conversion ya no la espera
                # nadie, pero LibreOffice seguiria vivo ocupando el semaforo.
                await _matar(proceso)
                raise
        except Exception:
            log.exception("Fallo al invocar LibreOffice")
            return None, "No se pudo ejecutar el conversor de PDF."
        finally:
            shutil.rmtree(perfil, ignore_errors=True)

    if proceso.returncode != 0:
        # Un codigo distinto de 0 es un fallo aunque haya dejado un archivo:
        # puede ser un PDF a medias.
        log.error("LibreOffice salio con codigo %s: %s",
                  proceso.returncode, (err or b"")[:300])
        esperado.unlink(missing_ok=True)
        return None, "El conversor de PDF falló."
    if (not esperado.is_file() or esperado.stat().st_mtime < inicio - 1
            or not _empieza_como_pdf(esperado)):
        log.error("LibreOffice no produjo el PDF: %s", (err or b"")[:300])
        return None, "El conversor no generó el PDF."

    log.info("PDF generado: %s", esperado.name)
    return str(esperado), "PDF generado."


def _empieza_como_pdf(ruta: Path) -> bool:
    with open(ruta, "rb") as f:
        return es_pdf(f.read(5))


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
        from io import BytesIO

        from pypdf import PdfReader
        lector = PdfReader(BytesIO(datos))
        campos = lector.get_fields() or {}
        return any(c.get("/FT") == "/Sig" for c in campos.values())
    except Exception as e:  # noqa: BLE001
        log.info("No se pudo inspeccionar el PDF: %s", e)
        return None


def instrucciones_dnie() -> dict:
    """Texto para la interfaz. Vive aqui para que haya una sola version."""
    return {
        "pasos": [
            "Descarga el PDF a tu computadora.",
            "Conecta el lector con tu DNI electrónico insertado.",
            ("Ábrelo con Firma Perú (RENIEC) o con Adobe Reader configurado "
            "con el controlador del lector."),
            "Firma con el certificado de tu DNIe e ingresa tu PIN.",
            "Vuelve aquí y sube el PDF firmado.",
        ],
        "nota": ("Tu PIN y tu certificado nunca salen de tu tarjeta ni pasan por "
                 "LicitaPro. La firma ocurre en tu equipo; nosotros solo "
                 "preparamos el documento y guardamos el resultado."),
    }
