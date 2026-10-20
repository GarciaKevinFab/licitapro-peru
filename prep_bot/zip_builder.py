"""ZIP Builder — Genera expediente ZIP completo listo para SEACE.

QUE NO HACE MAS

  - Omitir en silencio un documento que no se genero y listarlo igual con
    [x] en el indice. Ahora cada documento se lee a memoria en cuanto se
    genera; si uno obligatorio no sale, el expediente NO se arma y la web dice
    cual y por que (`ExpedienteError`, `DocumentoIncompleto`). Si el que falta
    es opcional, va en `advertencias`, en el indice y en 00_FALTANTES.txt.
  - Escribir el ZIP directamente sobre la ruta que apunta la base. Un fallo a
    mitad dejaba un ZIP truncado donde estaba el bueno. Ahora se escribe a un
    temporal en la misma carpeta y se cambia de nombre al final (`os.replace`
    es atomico dentro del mismo disco).
  - Llamarse `Expediente_<nomenclatura>_<ruc>.zip`. Dos usuarios sin RUC
    cargado chocaban en `Expediente_X_None.zip` y uno podia bajarse el del
    otro. Ahora el nombre en disco lleva el id de la propuesta, que es unico.
  - Devolver None sin decir por que. La web mostraba "No se pudo armar el
    expediente" y nadie sabia si era el DNI, la SC o el disco.
"""
import asyncio
import logging
import os
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from prep_bot.document_gen import (
    generar_carta_presentacion,
    generar_declaracion_jurada,
    generar_experiencia_postor,
    generar_propuesta_economica,
)
from shared import cotizacion, fechas
from shared.config import format_monto
from shared.db import connection, get_empresa, kb_get
from shared.documentos_menores import formato_dj, generar_cotizacion, generar_dj
from shared.incompleto import DocumentoIncompleto

log = logging.getLogger("prep.zip")

EXPEDIENTES_DIR = os.getenv("EXPEDIENTES_DIR", "data/expedientes")
TEMPLATES_DIR = os.getenv("TEMPLATES_DIR", "templates")


class ExpedienteError(Exception):
    """El expediente no se pudo armar. El mensaje es para el usuario."""


@dataclass
class Expediente:
    ruta: str
    documentos: list[str] = field(default_factory=list)
    advertencias: list[str] = field(default_factory=list)


def ruta_expediente(propuesta_id: int) -> str:
    """Donde vive el ZIP de una propuesta. Por id: unico aunque falte el RUC."""
    return os.path.join(EXPEDIENTES_DIR, f"expediente_propuesta_{int(propuesta_id)}.zip")


async def _leer(ruta: str, documento: str, borrar: bool = False) -> bytes:
    """El documento recien generado, a memoria. Si no esta en disco es un
    fallo del generador y se dice cual."""
    try:
        contenido = await asyncio.to_thread(Path(ruta).read_bytes)
    except OSError as e:
        raise ExpedienteError(
            f"No se pudo generar {documento}: el archivo no quedó en el servidor. "
            "Inténtalo de nuevo.") from e
    if borrar:
        # Los PDF de documentos_menores se escriben con un nombre fijo por
        # propuesta y los reescribe tambien la descarga suelta: se leen y se
        # quitan para no dejar copias sueltas en el disco.
        await asyncio.to_thread(Path(ruta).unlink, True)
    return contenido


async def generar_expediente_zip(propuesta_id: int) -> Expediente:
    """Genera el expediente ZIP completo con todos los documentos.

    Contenido del ZIP:
    - 01_Carta_Presentacion.docx
    - 02_Declaracion_Jurada.docx
    - 03_Experiencia_Postor.docx
    - 04_Propuesta_Tecnica.docx
    - 05_Propuesta_Economica.docx (o 05_Cotizacion.pdf en una cotizacion)
    - 06-08 anexos complementarios
    - 00_INDICE.txt, y 00_FALTANTES.txt si hay advertencias

    Levanta ExpedienteError o DocumentoIncompleto con el motivo; devuelve el
    Expediente (ruta, documentos incluidos y advertencias) si se armo.
    """
    async with connection() as conn:
        prop = await conn.fetchrow(
            """SELECT p.*, l.objeto, l.entidad, l.nomenclatura, l.monto_referencial,
                      l.id as lic_id, l.tipo
            FROM propuestas p JOIN licitaciones l ON p.licitacion_id = l.id
            WHERE p.id=$1""",
            propuesta_id,
        )
    if not prop:
        raise ExpedienteError("La propuesta no existe o no tiene licitación asociada.")

    empresa_id = prop["empresa_id"]
    empresa = await get_empresa(empresa_id)
    if not empresa:
        raise ExpedienteError("La empresa de esta propuesta ya no existe.")
    licitacion = dict(prop)
    # Una contratacion menor se arma distinto: la cotizacion por items en vez
    # de la propuesta economica de un solo monto, la declaracion jurada en el
    # formato de la entidad cuando lo exige, y sin los anexos de un concurso
    # (experiencia, personal clave, DJ de plazo) que nadie pidio.
    menor = cotizacion.es_cotizacion_menor(prop["tipo"])
    items = await cotizacion.items_de(propuesta_id)
    cci = await kb_get(empresa_id, "financiero", "cci")

    documentos: list[tuple[str, bytes]] = []
    advertencias: list[str] = []
    temporales: list[str] = []

    async def _docx(nombre_zip: str, documento: str, generar) -> None:
        """Genera un .docx propio, lo lee y lo anota para borrarlo al final."""
        try:
            ruta = await generar()
        except (DocumentoIncompleto, ExpedienteError):
            raise
        except Exception as e:
            log.exception("Expediente %s: %s fallo", propuesta_id, documento)
            raise ExpedienteError(
                f"No se pudo generar {documento}. Revisa los datos de la empresa e "
                "inténtalo de nuevo.") from e
        documentos.append((nombre_zip, await _leer(ruta, documento)))
        temporales.append(ruta)

    # 1. Carta de presentación
    await _docx("01_Carta_Presentacion.docx", "la carta de presentación",
                lambda: generar_carta_presentacion(propuesta_id, empresa_id, licitacion))

    # 2. Declaración jurada
    dj_llena = await _formato_lleno(propuesta_id, prop["lic_id"], "dj", advertencias,
                                    empresa=dict(empresa), cci=cci)
    if dj_llena:
        documentos.append(("02_Declaracion_Jurada.pdf", dj_llena))
    elif formato_dj(licitacion):
        ruta = await generar_dj(licitacion, dict(empresa), cci, con_dnie=True)
        documentos.append(("02_Declaracion_Jurada.pdf",
                           await _leer(ruta, "la declaración jurada", borrar=True)))
    else:
        await _docx("02_Declaracion_Jurada.docx", "la declaración jurada",
                    lambda: generar_declaracion_jurada(propuesta_id, empresa_id, licitacion))

    # 3. Experiencia del postor
    if not menor:
        await _docx("03_Experiencia_Postor.docx", "el anexo de experiencia del postor",
                    lambda: generar_experiencia_postor(propuesta_id, empresa_id, licitacion))

    # 4. Propuesta técnica (si fue generada previamente con "Generar documentos")
    tecnica_path = os.path.join(TEMPLATES_DIR, "output", f"propuesta_tecnica_{propuesta_id}.docx")
    if os.path.exists(tecnica_path):
        documentos.append(("04_Propuesta_Tecnica.docx",
                           await _leer(tecnica_path, "la propuesta técnica")))
    elif not menor:
        # En un concurso es el documento que puntua: no se calla.
        advertencias.append("No va la propuesta técnica: pulsa «Generar documentos» y vuelve "
                            "a armar el expediente.")

    # 5. Propuesta económica
    precio = prop.get("precio_ofertado") or prop.get("monto_referencial") or 0
    if items:
        if any(it["precio_unitario"] is None for it in items):
            raise ExpedienteError("Pon precio a todos los ítems antes de armar el expediente.")
        condiciones = await cotizacion.condiciones_de(propuesta_id)
        sc = await _formato_lleno(propuesta_id, prop["lic_id"], "sc", advertencias,
                                  empresa=dict(empresa), items=items, condiciones=condiciones)
        if not sc:
            ruta = await generar_cotizacion(
                licitacion, dict(empresa), items, condiciones, cci, con_dnie=True)
            sc = await _leer(ruta, "la cotización", borrar=True)
        documentos.append(("05_Cotizacion.pdf", sc))
    elif precio:
        await _docx("05_Propuesta_Economica.docx", "la propuesta económica",
                    lambda: generar_propuesta_economica(propuesta_id, empresa_id,
                                                        licitacion, precio))
    else:
        raise ExpedienteError("Fija el precio ofertado antes de armar el expediente: sin él "
                              "no hay propuesta económica.")

    # 6-8. Los anexos que ningún otro paso cubre: declaración jurada de
    # plazo, compromiso del personal clave y pacto de integridad. Cada uno que
    # no sale queda en `advertencias`: ya no se pierde en el log.
    from prep_bot.autofill.annexes import generar_anexos_complementarios
    from shared.knowledge_base import obtener_datos_empresa_completos
    try:
        datos = await obtener_datos_empresa_completos(empresa_id)
        anexos = await generar_anexos_complementarios(
            propuesta_id, empresa_id, licitacion, datos, menor=menor,
            advertencias=advertencias)
    except DocumentoIncompleto:
        raise
    except Exception:
        log.exception("Expediente %s: los anexos complementarios fallaron", propuesta_id)
        anexos = []
        advertencias.append("No se pudieron generar los anexos complementarios (DJ de plazo, "
                            "pacto de integridad): agrégalos a mano.")
    for nombre_zip, ruta in anexos:
        documentos.append((nombre_zip, await _leer(ruta, nombre_zip)))
        temporales.append(ruta)

    if not menor and not await _tiene_experiencia(empresa_id):
        advertencias.append("El anexo de experiencia va vacío: no hay experiencia registrada.")

    indice = _generar_indice(prop, empresa, documentos, menor, advertencias)
    zip_path = ruta_expediente(propuesta_id)
    await asyncio.to_thread(_escribir_zip, zip_path, documentos, indice, advertencias)
    for ruta in temporales:
        await asyncio.to_thread(Path(ruta).unlink, True)

    # Actualizar propuesta en DB
    async with connection() as conn:
        await conn.execute(
            "UPDATE propuestas SET expediente_zip_path=$2, estado = CASE WHEN estado IN "
            "('enviado','ganada','adjudicado','cancelado') THEN estado ELSE 'listo' END WHERE id=$1",
            propuesta_id, zip_path,
        )

    log.info("Expediente ZIP generado: %s (%s documentos, %s advertencias)",
             zip_path, len(documentos), len(advertencias))
    return Expediente(ruta=zip_path, documentos=[n for n, _ in documentos],
                      advertencias=advertencias)


async def _tiene_experiencia(empresa_id: int) -> bool:
    async with connection() as conn:
        return bool(await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM experiencia WHERE empresa_id=$1)", empresa_id))


def texto_faltantes(advertencias: list[str]) -> str:
    """El 00_FALTANTES.txt: lo que el usuario tiene que revisar antes de enviar."""
    lineas = ["LO QUE FALTA O HAY QUE REVISAR ANTES DE ENVIAR", "=" * 60, ""]
    lineas += [f"  [ ] {a}" for a in advertencias]
    lineas += ["", "Este ZIP se descargó con estos avisos. Corrígelos antes de presentar."]
    return "\n".join(lineas)


def _escribir_zip(destino: str, documentos: list[tuple[str, bytes]], indice: str,
                  advertencias: list[str]) -> None:
    """Escribe el ZIP a un temporal y lo pone en su sitio al final.

    POR QUE NO DIRECTO: `ZipFile(destino, "w")` trunca el ZIP anterior en el
    acto. Si algo falla a mitad, queda un ZIP corrupto en la ruta a la que
    apunta la base, y el usuario se baja eso. Con el temporal, o queda el
    nuevo entero o sigue el anterior."""
    carpeta = os.path.dirname(destino) or "."
    os.makedirs(carpeta, exist_ok=True)
    fd, temporal = tempfile.mkstemp(prefix=".expediente_", suffix=".tmp", dir=carpeta)
    try:
        with os.fdopen(fd, "wb") as f, zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED) as zf:
            for nombre_zip, contenido in documentos:
                zf.writestr(nombre_zip, contenido)
            zf.writestr("00_INDICE.txt", indice)
            if advertencias:
                zf.writestr("00_FALTANTES.txt", texto_faltantes(advertencias))
        os.replace(temporal, destino)
    except BaseException:
        Path(temporal).unlink(missing_ok=True)
        raise


def _generar_indice(prop, empresa, documentos: list[tuple], menor: bool = False,
                    advertencias: list[str] | None = None) -> str:
    """Genera un archivo de índice del expediente.

    Solo lista con [x] lo que de verdad va dentro: `documentos` son los que ya
    se leyeron a memoria, no los que se intento generar."""
    lines = [
        "=" * 60,
        "EXPEDIENTE DE PROPUESTA",
        "=" * 60,
        f"Fecha de generación: {fechas.ahora().strftime('%d/%m/%Y %H:%M')}",
        "",
        f"Licitación: {prop.get('nomenclatura') or prop['licitacion_id']}",
        f"Entidad: {prop['entidad']}",
        f"Objeto: {(prop['objeto'] or '')[:200]}",
        f"Monto referencial: {format_monto(prop['monto_referencial']) if prop.get('monto_referencial') else '—'}",
        "",
        f"Empresa: {empresa['razon_social']}",
        f"RUC: {empresa['ruc'] or '—'}",
        "",
        "DOCUMENTOS INCLUIDOS:",
        "-" * 40,
    ]

    for nombre_zip, _ in documentos:
        lines.append(f"  [x] {nombre_zip}")

    if advertencias:
        lines.extend(["", "ADVERTENCIAS (ver 00_FALTANTES.txt):", "-" * 40])
        lines.extend(f"  [ ] {a}" for a in advertencias)

    if menor:
        # Una cotizacion no se sube a SEACE: se envia por donde diga la SC
        # (el portal de cotizaciones de la entidad, un correo o mesa de partes).
        lines.extend([
            "",
            "ANTES DE ENVIAR:",
            "-" * 40,
            "  [ ] Firmar la cotizacion y la declaracion jurada (DNIe o firma y sello)",
            "  [ ] Adjuntar la ficha tecnica o catalogo del fabricante de cada item",
            "  [ ] Revisar que marca y presentacion cumplan las especificaciones",
            "",
            "COMO SE PRESENTA:",
            "-" * 40,
            "Por el canal que indique la solicitud de cotizacion: el portal de",
            "cotizaciones de la entidad, el correo de logistica o mesa de partes,",
            "dentro del plazo de la SC.",
            "",
            "Generado automáticamente por LicitaPro Perú",
        ])
        return "\n".join(lines)

    lines.extend([
        "",
        "DOCUMENTOS QUE DEBE AGREGAR MANUALMENTE:",
        "-" * 40,
        "  [ ] Vigencia de poder actualizada",
        "  [ ] Constancia RNP vigente",
        "  [ ] Constancia de no estar inhabilitado",
        "  [ ] Documentos de equipo técnico (CVs, títulos, colegiatura)",
        "  [ ] Carta fianza (si aplica)",
        "",
        "INSTRUCCIONES PARA SUBIR A SEACE:",
        "-" * 40,
        "1. Ingresa a https://www2.seace.gob.pe/ con tu RNP",
        "2. Busca el procedimiento por nomenclatura",
        "3. Selecciona 'Registrar Propuesta'",
        "4. Sube cada documento en la sección correspondiente",
        "5. Firma digitalmente con tu certificado",
        "6. Confirma el envío",
        "",
        "Generado automáticamente por LicitaPro Perú",
    ])

    return "\n".join(lines)


async def listar_expedientes() -> list[dict]:
    """Lista todos los expedientes generados."""
    if not os.path.exists(EXPEDIENTES_DIR):
        return []

    expedientes = []
    for f in os.listdir(EXPEDIENTES_DIR):
        if f.endswith(".zip"):
            path = os.path.join(EXPEDIENTES_DIR, f)
            size_mb = os.path.getsize(path) / (1024 * 1024)
            expedientes.append({
                "nombre": f,
                "path": path,
                "size_mb": round(size_mb, 2),
                "fecha": fechas.desde_marca(os.path.getmtime(path)),
            })

    return sorted(expedientes, key=lambda x: x["fecha"], reverse=True)


async def _formato_lleno(propuesta_id: int, licitacion_id: str, tipo: str,
                         advertencias: list[str], empresa: dict,
                         cci: str | None = None, items: list | None = None,
                         condiciones: dict | None = None) -> bytes | None:
    """El formato de la entidad (subido o bajado del portal) ya lleno; None si no
    hay o no se pudo llenar. En ese caso va el nuestro, y si fue porque NO SE
    PUDO llenar, queda en `advertencias`: la entidad puede exigir el suyo.

    `DocumentoIncompleto` no se traga: le falta un dato a la empresa, y con el
    nuestro le faltaria igual.

    Sin firma ni sello: el expediente sale preparado para firmar con DNIe."""
    from shared import llenar_formatos
    try:
        if tipo == "sc":
            return await llenar_formatos.sc_llena(propuesta_id, licitacion_id, empresa, items,
                                                  condiciones)
        return await llenar_formatos.dj_llena(propuesta_id, licitacion_id, empresa, cci)
    except llenar_formatos.FormatoNoReconocido as e:
        log.warning("Formato %s de la propuesta %s no se pudo llenar: %s", tipo, propuesta_id, e)
        nombre = "La solicitud de cotización" if tipo == "sc" else "La declaración jurada"
        advertencias.append(f"{nombre} de la entidad no se pudo llenar ({e}); va en nuestro "
                            "formato. Revisa si la entidad exige el suyo.")
        return None
