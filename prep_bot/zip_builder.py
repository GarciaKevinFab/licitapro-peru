"""ZIP Builder — Genera expediente ZIP completo listo para SEACE."""
import logging
import os
import zipfile

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

log = logging.getLogger("prep.zip")

EXPEDIENTES_DIR = os.getenv("EXPEDIENTES_DIR", "data/expedientes")
TEMPLATES_DIR = os.getenv("TEMPLATES_DIR", "templates")


async def generar_expediente_zip(propuesta_id: int) -> str | None:
    """Genera el expediente ZIP completo con todos los documentos.

    Contenido del ZIP:
    - 01_Carta_Presentacion.docx
    - 02_Declaracion_Jurada.docx
    - 03_Experiencia_Postor.docx
    - 04_Propuesta_Tecnica.docx (si existe)
    - 05_Propuesta_Economica.docx
    - 06_Documentos_Habilitantes/
        - RNP.pdf (si existe)
        - Vigencia_Poder.pdf (si existe)
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
            log.error(f"Propuesta #{propuesta_id} no encontrada")
            return None

    empresa_id = prop["empresa_id"]
    empresa = await get_empresa(empresa_id)
    licitacion = dict(prop)
    # Una contratacion menor se arma distinto: la cotizacion por items en vez
    # de la propuesta economica de un solo monto, la declaracion jurada en el
    # formato de la entidad cuando lo exige, y sin los anexos de un concurso
    # (experiencia, personal clave, DJ de plazo) que nadie pidio.
    menor = cotizacion.es_cotizacion_menor(prop["tipo"])
    items = await cotizacion.items_de(propuesta_id)
    cci = await kb_get(empresa_id, "financiero", "cci")

    # Crear directorio de salida
    os.makedirs(EXPEDIENTES_DIR, exist_ok=True)
    os.makedirs(os.path.join(TEMPLATES_DIR, "output"), exist_ok=True)

    nombre_safe = prop["nomenclatura"] or prop["lic_id"]
    nombre_safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in nombre_safe)
    zip_name = f"Expediente_{nombre_safe}_{empresa['ruc']}.zip"
    zip_path = os.path.join(EXPEDIENTES_DIR, zip_name)

    documentos_generados = []

    try:
        # 1. Carta de presentación
        log.info("Generando carta de presentación...")
        path = await generar_carta_presentacion(propuesta_id, empresa_id, licitacion)
        documentos_generados.append(("01_Carta_Presentacion.docx", path))

        # 2. Declaración jurada
        log.info("Generando declaración jurada...")
        if formato_dj(licitacion):
            path = await generar_dj(licitacion, dict(empresa), cci, con_dnie=True)
            documentos_generados.append(("02_Declaracion_Jurada.pdf", path))
        else:
            path = await generar_declaracion_jurada(propuesta_id, empresa_id, licitacion)
            documentos_generados.append(("02_Declaracion_Jurada.docx", path))

        # 3. Experiencia del postor
        if not menor:
            log.info("Generando experiencia del postor...")
            path = await generar_experiencia_postor(propuesta_id, empresa_id, licitacion)
            documentos_generados.append(("03_Experiencia_Postor.docx", path))

        # 4. Propuesta técnica (si fue generada previamente)
        tecnica_path = os.path.join(TEMPLATES_DIR, "output", f"propuesta_tecnica_{propuesta_id}.docx")
        if os.path.exists(tecnica_path):
            documentos_generados.append(("04_Propuesta_Tecnica.docx", tecnica_path))

        # 5. Propuesta económica
        precio = prop.get("precio_ofertado") or prop.get("monto_referencial") or 0
        if items:
            log.info("Generando cotización por ítems...")
            path = await generar_cotizacion(
                licitacion, dict(empresa), items,
                await cotizacion.condiciones_de(propuesta_id), cci, con_dnie=True)
            documentos_generados.append(("05_Cotizacion.pdf", path))
        elif precio:
            log.info("Generando propuesta económica...")
            path = await generar_propuesta_economica(propuesta_id, empresa_id, licitacion, precio)
            documentos_generados.append(("05_Propuesta_Economica.docx", path))

        # 6-8. Los anexos que ningún otro paso cubre: declaración jurada de
        # plazo, compromiso del personal clave y pacto de integridad. Los tres
        # se exigen de forma habitual, y el módulo que los escribe llevaba
        # desde el principio sin que nadie lo llamara.
        try:
            from prep_bot.autofill.annexes import generar_anexos_complementarios
            from shared.knowledge_base import obtener_datos_empresa_completos
            datos = await obtener_datos_empresa_completos(empresa_id)
            anexos = await generar_anexos_complementarios(
                propuesta_id, empresa_id, licitacion, datos)
            if menor:
                # De los tres, la SC pide solo el pacto de integridad.
                anexos = [a for a in anexos if "Pacto_Integridad" in a[0]]
            documentos_generados.extend(anexos)
        except Exception:
            # No tumba el expediente: los cinco documentos principales ya están
            # y valen por sí solos. Se registra para poder arreglarlo.
            log.exception("Los anexos complementarios fallaron")

        # Crear ZIP
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for nombre_zip, filepath in documentos_generados:
                if os.path.exists(filepath):
                    zf.write(filepath, nombre_zip)
                    log.info(f"  + {nombre_zip}")

            # Agregar archivo de índice
            indice = _generar_indice(prop, empresa, documentos_generados, menor)
            zf.writestr("00_INDICE.txt", indice)

        # Actualizar propuesta en DB
        async with connection() as conn:
            await conn.execute(
                "UPDATE propuestas SET expediente_zip_path=$2, estado='listo' WHERE id=$1",
                propuesta_id, zip_path,
            )

        log.info(f"Expediente ZIP generado: {zip_path} ({len(documentos_generados)} documentos)")
        return zip_path

    except Exception as e:  # noqa: BLE001
        log.error(f"Error generando expediente ZIP: {e}")
        return None


def _generar_indice(prop, empresa, documentos: list[tuple], menor: bool = False) -> str:
    """Genera un archivo de índice del expediente."""
    lines = [
        "=" * 60,
        "EXPEDIENTE DE PROPUESTA",
        "=" * 60,
        f"Fecha de generación: {fechas.ahora().strftime('%d/%m/%Y %H:%M')}",
        "",
        f"Licitación: {prop.get('nomenclatura') or prop['licitacion_id']}",
        f"Entidad: {prop['entidad']}",
        f"Objeto: {prop['objeto'][:200]}",
        f"Monto referencial: {format_monto(prop['monto_referencial']) if prop.get('monto_referencial') else '—'}",
        "",
        f"Empresa: {empresa['razon_social']}",
        f"RUC: {empresa['ruc']}",
        "",
        "DOCUMENTOS INCLUIDOS:",
        "-" * 40,
    ]

    for nombre_zip, _ in documentos:
        lines.append(f"  [x] {nombre_zip}")

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
