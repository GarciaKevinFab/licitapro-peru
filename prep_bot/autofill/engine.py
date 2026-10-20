"""Motor de auto-fill — Llena anexos usando knowledge_base + datos empresa.

Aqui vivian tambien una carta de presentacion y una declaracion jurada "de
respaldo" que nadie llamaba: firmaban con el representante en blanco, ponian
"Lima" como lugar aunque la empresa fuera de Puerto Maldonado y fechaban con
strftime('%B') ("October"). Las que van al expediente son las de
prep_bot/document_gen.py; las de aqui se quitaron para que nadie las use por
error.
"""
import logging
import os

from shared.db import connection, get_empresa, kb_get, kb_set

log = logging.getLogger("prep.autofill")

TEMPLATES_DIR = os.getenv("TEMPLATES_DIR", "/app/templates")


# Campos requeridos por tipo de anexo
ANEXO_FIELDS = {
    "carta_presentacion": [
        ("legal", "representante_legal", "Nombre del representante legal"),
        ("legal", "dni_representante", "DNI del representante"),
        ("legal", "cargo_representante", "Cargo del representante"),
        ("legal", "razon_social", None),  # None = no preguntar, viene de empresa
        ("legal", "ruc", None),
        ("legal", "direccion_legal", "Dirección legal completa"),
        ("legal", "telefono", None),
        ("legal", "email", None),
    ],
    "declaracion_jurada": [
        ("legal", "representante_legal", None),
        ("legal", "dni_representante", None),
        ("legal", "razon_social", None),
        ("legal", "ruc", None),
        ("legal", "partida_registral", "Número de partida registral (SUNARP)"),
    ],
    "propuesta_economica": [
        ("financiero", "entidad_bancaria", "¿Banco donde tiene cuenta la empresa?"),
        ("financiero", "cuenta_corriente", "Número de cuenta corriente"),
        ("financiero", "cci", "Código de cuenta interbancario (CCI)"),
        ("financiero", "margen_defecto", "¿Margen mínimo aceptable sobre costo? (ej: 20%)"),
    ],
    "experiencia_postor": [
        # Experiencia se llena desde la tabla experiencia, no KB
    ],
    "equipo_tecnico": [
        # Equipo se llena desde tabla equipo_tecnico
    ],
}


async def autofill_propuesta(propuesta_id: int, empresa_id: int) -> dict:
    """
    Intenta llenar todos los campos de todos los anexos.
    Retorna: {
        "completados": int,
        "totales": int,
        "faltantes": [{"categoria", "clave", "pregunta"}],
        "datos": {campo: valor}
    }
    """
    empresa = await get_empresa(empresa_id)
    if not empresa:
        return {"completados": 0, "totales": 0, "faltantes": [], "datos": {}}

    datos = {}
    faltantes = []
    completados = 0
    totales = 0

    # Pre-cargar datos de la empresa en KB si no están
    empresa_fields = {
        "razon_social": empresa["razon_social"],
        "ruc": empresa["ruc"],
        "telefono": empresa["telefono"],
        "email": empresa["email"],
        "direccion_legal": empresa["direccion"],
        "representante_legal": empresa["representante_legal"],
        "dni_representante": empresa["dni_representante"],
        "cargo_representante": empresa.get("cargo_representante", "Gerente General"),
    }

    for clave, valor in empresa_fields.items():
        if valor:
            await kb_set(empresa_id, "legal", clave, str(valor), "tabla_empresas")

    # Recorrer todos los anexos y sus campos
    for anexo_tipo, fields in ANEXO_FIELDS.items():
        for cat, clave, pregunta_text in fields:
            totales += 1
            
            # Buscar en KB
            valor = await kb_get(empresa_id, cat, clave)
            if valor:
                datos[f"{anexo_tipo}.{clave}"] = valor
                completados += 1
                continue

            # Si no hay pregunta definida, significa que debería venir de empresa
            if pregunta_text is None:
                # Ya intentamos arriba, no lo tiene
                completados += 1  # Lo contamos como OK si viene de empresa
                datos[f"{anexo_tipo}.{clave}"] = empresa_fields.get(clave, "")
                continue

            # Faltante → necesita pregunta al usuario
            faltantes.append({
                "categoria": cat,
                "clave": clave,
                "pregunta": pregunta_text,
                "anexo": anexo_tipo,
            })

    # Cargar experiencia
    async with connection() as conn:
        experiencias = await conn.fetch(
            "SELECT * FROM experiencia WHERE empresa_id=$1 ORDER BY fecha_fin DESC",
            empresa_id,
        )
        datos["experiencia_count"] = len(experiencias)
        datos["experiencias"] = [dict(e) for e in experiencias]

        # Cargar equipo técnico
        equipo = await conn.fetch(
            "SELECT * FROM equipo_tecnico WHERE empresa_id=$1 AND disponible=TRUE",
            empresa_id,
        )
        datos["equipo_count"] = len(equipo)
        datos["equipo"] = [dict(e) for e in equipo]

    return {
        "completados": completados,
        "totales": totales,
        "faltantes": faltantes,
        "datos": datos,
    }
