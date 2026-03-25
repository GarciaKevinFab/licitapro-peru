"""Firma Manager — Gestión de firmas, sellos y logos de empresas.

Permite subir imágenes de firma del representante legal, sello de empresa
y logo. Se usan automáticamente al generar documentos DOCX.
"""
import os
import logging
import shutil
from shared.db import connection, get_empresa

log = logging.getLogger("licitapro.firma")

FIRMAS_DIR = os.getenv("FIRMAS_DIR", "data/firmas")


def _ensure_dirs():
    os.makedirs(FIRMAS_DIR, exist_ok=True)


async def guardar_firma(empresa_id: int, tipo: str, source_path: str) -> str:
    """Guarda una imagen de firma/sello/logo para una empresa.

    Args:
        empresa_id: ID de la empresa
        tipo: 'firma' | 'sello' | 'logo'
        source_path: Path al archivo de imagen descargado (de Telegram)

    Returns: path donde se guardó el archivo
    """
    _ensure_dirs()

    # Determinar extensión
    ext = os.path.splitext(source_path)[1].lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.webp', '.bmp'):
        ext = '.png'

    # Nombre del archivo
    filename = f"empresa_{empresa_id}_{tipo}{ext}"
    dest_path = os.path.join(FIRMAS_DIR, filename)

    # Copiar archivo
    shutil.copy2(source_path, dest_path)

    # Actualizar DB
    campo_db = {
        'firma': 'firma_representante_path',
        'sello': 'sello_empresa_path',
        'logo': 'logo_empresa_path',
    }.get(tipo)

    if campo_db:
        async with connection() as conn:
            await conn.execute(
                f"UPDATE empresas SET {campo_db}=$2 WHERE id=$1",
                empresa_id, dest_path,
            )

    log.info(f"Guardado {tipo} para empresa {empresa_id}: {dest_path}")
    return dest_path


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

    if path and os.path.exists(path):
        return path
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
    """Elimina una firma/sello/logo."""
    path = await obtener_firma(empresa_id, tipo)
    if path and os.path.exists(path):
        os.remove(path)

    campo_db = {
        'firma': 'firma_representante_path',
        'sello': 'sello_empresa_path',
        'logo': 'logo_empresa_path',
    }.get(tipo)

    if campo_db:
        async with connection() as conn:
            await conn.execute(
                f"UPDATE empresas SET {campo_db}=NULL WHERE id=$1", empresa_id
            )

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
        except Exception as e:
            log.warning(f"No se pudo insertar imagen {image_path}: {e}")
    return False
