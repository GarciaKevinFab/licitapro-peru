"""Subida de imagenes de la empresa: logo, firma escaneada y sello.

Una subida de archivos es de los sitios donde mas facil se compromete un
producto, asi que aqui las reglas son estrictas y explicitas:

  1. NO se confia en la extension ni en el Content-Type. Los manda el cliente y
     mienten. Lo que decide es si Pillow consigue decodificar la imagen.

  2. La imagen se RE-CODIFICA, no se guarda tal cual. Eso destruye cualquier
     carga util incrustada: un archivo puede ser PNG valido y a la vez contener
     otra cosa (los llamados poliglotas). Al decodificar y volver a escribir,
     solo sobreviven los pixeles.

  3. El nombre del archivo lo genera el servidor. Aceptar el del cliente abre
     travesia de rutas ("../../etc/passwd") y colisiones entre inquilinos.

  4. Limite de tamano ANTES de decodificar, y de dimensiones despues. Un PNG de
     10 KB puede descomprimirse en gigabytes: es la bomba de descompresion, y
     tumba el servidor sin necesidad de ningun exploit.

  5. Los archivos NO se sirven como estaticos. Salen por una ruta autenticada
     que comprueba la propiedad, porque la firma de un representante legal no
     puede quedar accesible con solo adivinar una URL.
"""
import io
import logging
import os
import secrets
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from shared.db import connection

log = logging.getLogger("shared.archivos")

DIRECTORIO = Path(os.getenv("FIRMAS_DIR", "data/firmas"))

# 5 MB. Un logo o una firma escaneada no pesan ni de lejos eso; el margen es
# para quien fotografia su firma con el movil.
MAX_BYTES = 5 * 1024 * 1024

# Techo de dimensiones tras decodificar. Tambien es la defensa contra la bomba
# de descompresion: Pillow avisa por encima de ~89 Mpx, y esto queda muy debajo.
MAX_LADO = 4000

TIPOS = {
    "logo":  ("logo_empresa_path",        "Logo de la empresa"),
    "firma": ("firma_representante_path", "Firma del representante legal"),
    "sello": ("sello_empresa_path",       "Sello de la empresa"),
}


class ArchivoInvalido(Exception):
    """Lo subido no sirve. El mensaje esta escrito para mostrarselo al usuario."""


def _validar_y_normalizar(datos: bytes, tipo: str) -> bytes:
    """Devuelve un PNG limpio, o lanza ArchivoInvalido con el motivo."""
    if not datos:
        raise ArchivoInvalido("El archivo llegó vacío.")
    if len(datos) > MAX_BYTES:
        raise ArchivoInvalido(
            f"La imagen pesa {len(datos) // 1024} KB y el máximo son "
            f"{MAX_BYTES // 1024} KB. Redúcela y vuelve a intentarlo.")

    try:
        imagen = Image.open(io.BytesIO(datos))
        # verify() detecta corrupcion, pero deja el objeto inutilizable:
        # hay que reabrirlo para poder trabajar con el.
        imagen.verify()
        imagen = Image.open(io.BytesIO(datos))
    except (UnidentifiedImageError, OSError, ValueError) as e:
        log.info("Subida rechazada, no es una imagen: %s", e)
        raise ArchivoInvalido(
            "Ese archivo no es una imagen válida. Usa PNG o JPG.") from e

    if max(imagen.size) > MAX_LADO:
        raise ArchivoInvalido(
            f"La imagen mide {imagen.size[0]}x{imagen.size[1]} px y el máximo "
            f"es {MAX_LADO} px por lado.")

    # Firma y sello conservan transparencia, para poder superponerlos sobre el
    # documento sin un recuadro blanco alrededor.
    imagen = imagen.convert("RGBA" if tipo in ("firma", "sello") else "RGB")

    salida = io.BytesIO()
    # Re-codificar es lo que limpia el archivo: se escriben pixeles, no el
    # contenido original. Sin metadatos, sin EXIF, sin nada mas.
    imagen.save(salida, format="PNG", optimize=True)
    return salida.getvalue()


async def guardar_imagen(empresa_id: int, tipo: str, datos: bytes) -> str:
    """Valida, limpia y guarda. Devuelve la ruta. Lanza ArchivoInvalido."""
    if tipo not in TIPOS:
        raise ArchivoInvalido("Tipo de imagen no permitido.")

    limpia = _validar_y_normalizar(datos, tipo)

    DIRECTORIO.mkdir(parents=True, exist_ok=True)
    # Nombre generado por el servidor. El sufijo aleatorio evita que alguien
    # adivine la ruta de la firma de otra empresa a partir de su id.
    nombre = f"empresa_{empresa_id}_{tipo}_{secrets.token_hex(8)}.png"
    destino = DIRECTORIO / nombre
    destino.write_bytes(limpia)

    columna = TIPOS[tipo][0]
    async with connection() as conn:
        anterior = await conn.fetchval(
            f"SELECT {columna} FROM empresas WHERE id=$1", empresa_id)
        await conn.execute(
            f"UPDATE empresas SET {columna}=$2 WHERE id=$1",
            empresa_id, str(destino))

    # La anterior se borra DESPUES de guardar la nueva: si el borrado fuera
    # antes y la escritura fallara, la empresa se quedaria sin imagen.
    if anterior and anterior != str(destino):
        _borrar_del_disco(anterior)

    log.info("Imagen %s guardada para la empresa %s (%d KB)",
             tipo, empresa_id, len(limpia) // 1024)
    return str(destino)


def _borrar_del_disco(ruta: str) -> None:
    """Borra solo si esta dentro del directorio de firmas.

    No es paranoia: la ruta viene de la base de datos, y si alguna vez entrara
    ahi un valor manipulado, esta comprobacion evita que el borrado alcance
    cualquier archivo del servidor.
    """
    try:
        p = Path(ruta).resolve()
        if p.is_file() and DIRECTORIO.resolve() in p.parents:
            p.unlink()
    except OSError as e:
        log.warning("No se pudo borrar %s: %s", ruta, e)


async def borrar_imagen(empresa_id: int, tipo: str) -> None:
    if tipo not in TIPOS:
        return
    columna = TIPOS[tipo][0]
    async with connection() as conn:
        ruta = await conn.fetchval(
            f"SELECT {columna} FROM empresas WHERE id=$1", empresa_id)
        await conn.execute(
            f"UPDATE empresas SET {columna}=NULL WHERE id=$1", empresa_id)
    if ruta:
        _borrar_del_disco(ruta)


async def rutas_de(empresa_id: int) -> dict:
    """{tipo: ruta} de las imagenes que existen de verdad en disco.

    Se comprueba el disco y no solo la base: un volumen mal montado dejaria
    rutas apuntando a archivos que ya no estan, y la generacion de documentos
    reventaria en el peor momento.
    """
    async with connection() as conn:
        fila = await conn.fetchrow(
            """SELECT logo_empresa_path, firma_representante_path,
                      sello_empresa_path FROM empresas WHERE id=$1""",
            empresa_id)
    if not fila:
        return {}
    salida = {}
    for tipo, (columna, _) in TIPOS.items():
        ruta = fila[columna]
        if ruta and Path(ruta).is_file():
            salida[tipo] = ruta
    return salida
