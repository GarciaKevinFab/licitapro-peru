"""Validator — Valida que la propuesta esté completa antes de generar ZIP."""
import logging

from shared.cotizacion import es_cotizacion_menor, items_de
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

# En una contratacion menor la entidad no pide partida registral ni vigencia
# de poder: pide la cotizacion firmada, la declaracion jurada, el RUC y el
# domicilio. Exigirlas bloqueaba el expediente de la SC 5884-2026-GOREMAD por
# dos papeles que nadie iba a mirar.
NO_EXIGIDOS_EN_MENORES = {"partida_registral", "vigencia_poder"}


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
            "SELECT p.*, l.objeto, l.monto_referencial, l.tipo FROM propuestas p "
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
    menor = es_cotizacion_menor(prop["tipo"])
    validaciones = [
        {**v, "requerido": False} if menor and v["campo"] in NO_EXIGIDOS_EN_MENORES else v
        for v in VALIDACIONES
    ]

    for v in validaciones:
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

    # Una cotizacion por items se presenta con TODOS los precios. Un item en
    # blanco no es un detalle: la entidad compara por item y lo descalifica.
    items = await items_de(propuesta_id)
    sin_precio = [i for i in items if i["precio_unitario"] is None]
    if sin_precio:
        faltantes.append({"campo": "precios_items",
                          "desc": f"Precio unitario de {len(sin_precio)} ítem(s) de la cotización"})

    if menor:
        # Ni equipo tecnico ni experiencia cuentan en una cotizacion: avisar
        # de ellos solo ensucia la lista. Lo que si cuenta es la CCI, porque
        # se paga por transferencia y varias entidades la piden en la DJ.
        if not await kb_get(empresa_id, "financiero", "cci"):
            warnings.append("Sin CCI registrada: el pago es por transferencia y la "
                            "declaración jurada la pide")
        return _resultado(validaciones, faltantes, warnings, campos_ok, pendientes,
                          prop, empresa)

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

    return _resultado(validaciones, faltantes, warnings, campos_ok, pendientes,
                      prop, empresa)


def _resultado(validaciones, faltantes, warnings, campos_ok, pendientes,
               prop, empresa) -> dict:
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
    requeridos = [v for v in validaciones if v["requerido"]]
    faltan_datos = sum(1 for f in faltantes if f["campo"] != "precios_items")
    return {
        "completa": len(faltantes) == 0,
        "campos_ok": campos_ok,
        "campos_total": len(validaciones),
        "requeridos_ok": len(requeridos) - faltan_datos,
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
