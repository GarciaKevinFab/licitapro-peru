"""Firma Manager — Gestión de firmas, sellos y logos de empresas.

Permite subir imágenes de firma del representante legal, sello de empresa
y logo. Se usan automáticamente al generar documentos DOCX.
"""
import asyncio
import logging
import os

from shared.archivos import MAX_BYTES, borrar_imagen, guardar_imagen
from shared.db import connection
from shared.rutas import en_raiz

log = logging.getLogger("licitapro.firma")


async def guardar_firma(empresa_id: int, tipo: str, origen: bytes | str) -> str:
    """Guarda una imagen de firma/sello/logo para una empresa.

    Delega en shared.archivos.guardar_imagen, que es quien valida. Antes esta
    funcion copiaba el archivo TAL CUAL (shutil.copy2), sin comprobar que fuera
    una imagen, con la extension que traia y con un nombre predecible
    (empresa_<id>_<tipo>.png): cualquier cosa subida por el bot acababa en el
    disco y luego dentro de los documentos, y la ruta de la firma de otra
    empresa se adivinaba sabiendo su id. Dos caminos de subida con reglas
    distintas garantizan que uno de los dos sea el agujero; ahora hay uno solo.

    NO comprueba la propiedad: eso se hace en el borde (handler del bot, ruta
    web), que es donde se sabe quien pide.

    Args:
        empresa_id: ID de la empresa, ya validado como del usuario
        tipo: 'firma' | 'sello' | 'logo'
        origen: los bytes de la imagen, o la ruta de un archivo con ellos

    Returns: path donde se guardó el archivo
    Raises: ArchivoInvalido si no es una imagen aceptable (mensaje para el usuario)
    """
    if isinstance(origen, (bytes, bytearray)):
        datos = bytes(origen)
    else:
        # Se lee uno de mas que el limite: basta para que guardar_imagen diga
        # "demasiado grande" sin cargar en memoria un archivo de gigas.
        datos = await asyncio.to_thread(_leer_con_tope, origen)
    return await guardar_imagen(empresa_id, tipo, datos)


async def obtener_firma(empresa_id: int, tipo: str) -> str | None:
    """Obtiene el path de la firma/sello/logo de una empresa."""
    campo_db = {
        'firma': 'firma_representante_path',
        'sello': 'sello_empresa_path',
        'logo': 'logo_empresa_path',
    }.get(tipo)

    if not campo_db:
        return None

    async with connection() as conn:
        path = await conn.fetchval(
            f"SELECT {campo_db} FROM empresas WHERE id=$1", empresa_id
        )

    # La base guarda "data/firmas/..." relativo en las filas viejas: se
    # resuelve contra la raiz del proyecto, no contra el cwd del proceso, o un
    # bot arrancado desde otra carpeta generaba los documentos sin firma.
    if path and en_raiz(path).is_file():
        return str(en_raiz(path))
    return None


async def obtener_todas_firmas(empresa_id: int) -> dict:
    """Obtiene todos los archivos de firma/sello/logo de una empresa."""
    resultado = {}
    for tipo in ('firma', 'sello', 'logo'):
        path = await obtener_firma(empresa_id, tipo)
        resultado[tipo] = {
            'path': path,
            'existe': path is not None,
        }
    return resultado


async def eliminar_firma(empresa_id: int, tipo: str) -> bool:
    """Elimina una firma/sello/logo.

    Delega en shared.archivos.borrar_imagen: resuelve la ruta contra la raiz
    y solo borra dentro del directorio de firmas. Antes hacia os.remove de lo
    que dijera la base, relativo al cwd.
    """
    await borrar_imagen(empresa_id, tipo)
    return True


def insertar_imagen_en_docx(doc, image_path: str, width_cm: float = 4.0):
    """Inserta una imagen en un documento DOCX.

    Args:
        doc: Document de python-docx
        image_path: Path a la imagen
        width_cm: Ancho en centímetros
    """
    from docx.shared import Cm
    if image_path and os.path.exists(image_path):
        try:
            doc.add_picture(image_path, width=Cm(width_cm))
            return True
        except Exception as e:  # noqa: BLE001
            log.warning(f"No se pudo insertar imagen {image_path}: {e}")
    return False


def _leer_con_tope(origen) -> bytes:
    """Uno de mas que el limite: basta para que guardar_imagen diga
    "demasiado grande" sin cargar en memoria un archivo de gigas."""
    with open(origen, "rb") as f:
        return f.read(MAX_BYTES + 1)
