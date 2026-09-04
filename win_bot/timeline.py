"""Timeline — Calcula y gestiona plazos de contratos."""
import logging
from datetime import date, timedelta

from shared.db import connection
from shared import fechas

log = logging.getLogger("win.timeline")

# Plazos por defecto según tipo de contratación
PLAZOS_DEFAULT = {
    "AS": [  # Adjudicación Simplificada
        ("firma", "Firma de contrato", 8),
        ("fianza", "Presentar carta fianza (10%)", 8),
        ("entregable", "Primer entregable", 30),
        ("entregable", "Entrega final", 60),
        ("conformidad", "Conformidad del servicio", 68),
        ("pago", "Pago estimado", 90),
    ],
    "LP": [  # Licitación Pública
        ("firma", "Firma de contrato", 10),
        ("fianza", "Presentar carta fianza (10%)", 10),
        ("entregable", "Primer entregable", 45),
        ("entregable", "Segundo entregable", 90),
        ("entregable", "Entrega final", 120),
        ("conformidad", "Conformidad del servicio", 130),
        ("pago", "Pago estimado", 150),
    ],
    "default": [
        ("firma", "Firma de contrato", 8),
        ("fianza", "Presentar carta fianza (10%)", 8),
        ("entregable", "Primer entregable", 38),
        ("entregable", "Entrega final", 68),
        ("conformidad", "Conformidad del servicio", 75),
        ("pago", "Pago estimado", 90),
    ],
}


async def crear_timeline_contrato(contrato_id: int, tipo_proc: str = "default",
                                    plazo_ejecucion_dias: int = None) -> list[dict]:
    """Crea timeline de plazos para un contrato nuevo.

    Args:
        contrato_id: ID del contrato
        tipo_proc: Tipo de procedimiento (AS, LP, etc.)
        plazo_ejecucion_dias: Si se conoce, ajusta los plazos

    Returns: lista de plazos creados
    """
    async with connection() as conn:
        contrato = await conn.fetchrow("SELECT * FROM contratos WHERE id=$1", contrato_id)
        if not contrato:
            return []

    fecha_base = contrato.get("fecha_adjudicacion") or fechas.hoy()
    if isinstance(fecha_base, str):
        from datetime import datetime
        fecha_base = fechas.desde_texto(fecha_base, "%Y-%m-%d").date()

    plantilla = PLAZOS_DEFAULT.get(tipo_proc, PLAZOS_DEFAULT["default"])

    # Si tenemos plazo de ejecución, ajustar los entregables
    if plazo_ejecucion_dias:
        plantilla = _ajustar_plazos(plantilla, plazo_ejecucion_dias)

    plazos_creados = []
    async with connection() as conn:
        for tipo, descripcion, dias_offset in plantilla:
            fecha_limite = fecha_base + timedelta(days=dias_offset)

            plazo_id = await conn.fetchval(
                """INSERT INTO plazos (contrato_id, tipo, descripcion, fecha_limite, dias_antes_alerta)
                VALUES ($1, $2, $3, $4, $5) RETURNING id""",
                contrato_id, tipo, descripcion, fecha_limite, 3,
            )
            plazos_creados.append({
                "id": plazo_id,
                "tipo": tipo,
                "descripcion": descripcion,
                "fecha_limite": fecha_limite,
            })

        # Actualizar contrato con fechas calculadas
        firma_plazo = next((p for p in plazos_creados if p["tipo"] == "firma"), None)
        entrega_final = [p for p in plazos_creados if p["tipo"] == "entregable"]
        if firma_plazo:
            await conn.execute(
                "UPDATE contratos SET fecha_firma_contrato=$2 WHERE id=$1",
                contrato_id, firma_plazo["fecha_limite"],
            )
        if entrega_final:
            await conn.execute(
                "UPDATE contratos SET fecha_entrega_final=$2, plazo_ejecucion_dias=$3 WHERE id=$1",
                contrato_id, entrega_final[-1]["fecha_limite"],
                plazo_ejecucion_dias or (entrega_final[-1]["fecha_limite"] - fecha_base).days,
            )

    log.info(f"Timeline creado para contrato #{contrato_id}: {len(plazos_creados)} plazos")
    return plazos_creados


def _ajustar_plazos(plantilla: list, plazo_dias: int) -> list:
    """Ajusta los plazos de entregables según el plazo real de ejecución."""
    resultado = []
    entregable_count = sum(1 for t, _, _ in plantilla if t == "entregable")

    if entregable_count == 0:
        return plantilla

    entregable_idx = 0
    for tipo, desc, dias in plantilla:
        if tipo == "entregable":
            entregable_idx += 1
            # Distribuir entregables equitativamente en el plazo
            dias_ajustado = int(plazo_dias * entregable_idx / entregable_count) + 8
            resultado.append((tipo, desc, dias_ajustado))
        elif tipo == "conformidad":
            resultado.append((tipo, desc, plazo_dias + 10))
        elif tipo == "pago":
            resultado.append((tipo, desc, plazo_dias + 30))
        else:
            resultado.append((tipo, desc, dias))

    return resultado


async def obtener_timeline(contrato_id: int) -> list[dict]:
    """Obtiene el timeline completo de un contrato."""
    async with connection() as conn:
        plazos = await conn.fetch(
            """SELECT * FROM plazos WHERE contrato_id=$1 ORDER BY fecha_limite""",
            contrato_id,
        )
    return [dict(p) for p in plazos]


async def marcar_plazo_completado(plazo_id: int) -> bool:
    """Marca un plazo como completado."""
    async with connection() as conn:
        await conn.execute(
            "UPDATE plazos SET completado=TRUE WHERE id=$1", plazo_id
        )
    return True


async def agregar_plazo(contrato_id: int, tipo: str, descripcion: str,
                         fecha_limite: date, dias_alerta: int = 3) -> int:
    """Agrega un plazo adicional a un contrato."""
    async with connection() as conn:
        return await conn.fetchval(
            """INSERT INTO plazos (contrato_id, tipo, descripcion, fecha_limite, dias_antes_alerta)
            VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            contrato_id, tipo, descripcion, fecha_limite, dias_alerta,
        )


async def obtener_plazos_criticos(dias: int = 7) -> list[dict]:
    """Obtiene plazos que vencen en los próximos N días."""
    async with connection() as conn:
        rows = await conn.fetch(
            """SELECT p.*, c.numero_contrato, c.monto_adjudicado,
                      l.entidad, l.objeto, e.razon_social
            FROM plazos p
            JOIN contratos c ON p.contrato_id = c.id
            JOIN licitaciones l ON c.licitacion_id = l.id
            JOIN empresas e ON c.empresa_id = e.id
            WHERE p.completado = FALSE
            AND p.fecha_limite BETWEEN CURRENT_DATE AND CURRENT_DATE + $1 * INTERVAL '1 day'
            ORDER BY p.fecha_limite""",
            dias,
        )
    return [dict(r) for r in rows]
