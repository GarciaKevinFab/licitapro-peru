"""Validator — Valida que la propuesta esté completa antes de generar ZIP."""
import os
import logging
from shared.db import connection, get_empresa, kb_get

log = logging.getLogger("prep.validator")


VALIDACIONES = [
    {"campo": "representante_legal", "cat": "legal", "requerido": True, "desc": "Representante legal"},
    {"campo": "dni_representante", "cat": "legal", "requerido": True, "desc": "DNI del representante"},
    {"campo": "partida_registral", "cat": "legal", "requerido": True, "desc": "Partida registral SUNARP"},
    {"campo": "domicilio_legal", "cat": "legal", "requerido": True, "desc": "Domicilio legal"},
    {"campo": "vigencia_poder", "cat": "legal", "requerido": True, "desc": "Vigencia de poder"},
    {"campo": "entidad_bancaria", "cat": "financiero", "requerido": False, "desc": "Banco"},
    {"campo": "cuenta_corriente", "cat": "financiero", "requerido": False, "desc": "Cuenta corriente"},
]


async def validar_propuesta(propuesta_id: int) -> dict:
    """Valida completitud de una propuesta.

    Returns: {
        completa: bool,
        campos_ok: int,
        campos_total: int,
        faltantes: [{campo, desc}],
        warnings: [str],
    }
    """
    async with connection() as conn:
        prop = await conn.fetchrow(
            "SELECT p.*, l.objeto, l.monto_referencial FROM propuestas p "
            "JOIN licitaciones l ON p.licitacion_id = l.id WHERE p.id=$1",
            propuesta_id,
        )
        if not prop:
            return {"completa": False, "error": "Propuesta no encontrada"}

    empresa_id = prop["empresa_id"]
    empresa = await get_empresa(empresa_id)
    faltantes = []
    warnings = []
    campos_ok = 0

    for v in VALIDACIONES:
        valor = await kb_get(empresa_id, v["cat"], v["campo"])
        if not valor and empresa:
            valor = empresa.get(v["campo"])

        if valor:
            campos_ok += 1
        elif v["requerido"]:
            faltantes.append({"campo": v["campo"], "desc": v["desc"]})

    # Validar preguntas pendientes
    async with connection() as conn:
        pendientes = await conn.fetchval(
            "SELECT COUNT(*) FROM preguntas WHERE propuesta_id=$1 AND respondida=FALSE",
            propuesta_id,
        )
        if pendientes > 0:
            warnings.append(f"{pendientes} preguntas sin responder")

    # Validar equipo técnico
    async with connection() as conn:
        equipo_count = await conn.fetchval(
            "SELECT COUNT(*) FROM equipo_tecnico WHERE empresa_id=$1 AND disponible=TRUE",
            empresa_id,
        )
        if equipo_count == 0:
            warnings.append("No hay profesionales registrados en equipo técnico")

    # Validar experiencia
    async with connection() as conn:
        exp_count = await conn.fetchval(
            "SELECT COUNT(*) FROM experiencia WHERE empresa_id=$1", empresa_id
        )
        if exp_count == 0:
            warnings.append("No hay experiencia previa registrada")

    # Validar RNP
    if empresa and not empresa.get("rnp_numero"):
        warnings.append("RNP no registrado")

    # Validar precio
    if not prop.get("precio_ofertado") and not prop.get("monto_referencial"):
        warnings.append("Sin precio ofertado definido")

    # Se cuentan aparte los obligatorios. `campos_ok` sobre `len(VALIDACIONES)`
    # mezcla los cinco que bloquean con los dos bancarios que no, y la ficha
    # acababa diciendo "datos obligatorios completos (5 de 7)": una frase que se
    # desmiente a si misma y deja al usuario buscando dos campos que no le hacen
    # falta para presentarse.
    requeridos = [v for v in VALIDACIONES if v["requerido"]]
    return {
        "completa": len(faltantes) == 0,
        "campos_ok": campos_ok,
        "campos_total": len(VALIDACIONES),
        "requeridos_ok": len(requeridos) - len(faltantes),
        "requeridos_total": len(requeridos),
        "faltantes": faltantes,
        "warnings": warnings,
        "preguntas_pendientes": pendientes,
    }


def formato_validacion(resultado: dict) -> str:
    """Formatea resultado de validación para Telegram."""
    if resultado.get("error"):
        return f"Error: {resultado['error']}"

    lines = [f"{'✅' if resultado['completa'] else '⚠️'} **Validación: {resultado['campos_ok']}/{resultado['campos_total']} campos**"]

    if resultado["faltantes"]:
        lines.append("\n❌ **Campos faltantes:**")
        for f in resultado["faltantes"]:
            lines.append(f"  • {f['desc']}")

    if resultado["warnings"]:
        lines.append("\n⚠️ **Advertencias:**")
        for w in resultado["warnings"]:
            lines.append(f"  • {w}")

    if resultado["completa"] and not resultado["warnings"]:
        lines.append("\n✅ Propuesta lista para generar expediente.")

    return "\n".join(lines)
