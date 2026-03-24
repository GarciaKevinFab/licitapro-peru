"""Pricing — Calcula precio económico competitivo con histórico."""
import logging
from shared.db import connection, kb_get
from shared.config import format_monto

log = logging.getLogger("prep.autofill.pricing")


async def calcular_precio_competitivo(propuesta_id: int, empresa_id: int,
                                       licitacion: dict) -> float | None:
    """Calcula precio competitivo basado en:
    1. Monto referencial (valor base)
    2. Histórico de precios ganadores
    3. Margen configurado por el usuario
    4. Número estimado de competidores
    """
    monto_ref = licitacion.get("monto_referencial") or 0
    if not monto_ref:
        return None

    # 1. Obtener margen del usuario desde KB
    margen_str = await kb_get(empresa_id, "precios", "margen_minimo")
    margen = 0.20  # Default 20%
    if margen_str:
        try:
            margen = float(margen_str.replace("%", "")) / 100
        except ValueError:
            pass

    # 2. Buscar histórico de precios similares
    historico = await _buscar_historico(licitacion)

    # 3. Calcular precio base
    if historico:
        # Usar mediana del histórico como referencia
        precios = sorted(historico)
        mediana = precios[len(precios) // 2]
        precio_base = mediana
        log.info(f"Precio basado en histórico: mediana={format_monto(mediana)}")
    else:
        precio_base = monto_ref
        log.info(f"Precio basado en monto referencial: {format_monto(monto_ref)}")

    # 4. Estrategia de precio
    # En Perú, el precio debe ser <= monto referencial (para bienes/servicios)
    # y usualmente el menor precio gana (en AS/SIE)
    tipo = licitacion.get("tipo", "")

    if tipo in ("SIE",):
        # Subasta inversa: el menor precio gana
        # Ser competitivo pero no perder dinero
        precio_sugerido = precio_base * (1 - 0.05)  # 5% debajo
        piso = monto_ref * (1 - margen)  # No bajar del margen mínimo
        precio_sugerido = max(precio_sugerido, piso)
    elif tipo in ("AS", "CdP", "CM"):
        # Adjudicación simplificada: precio + calidad
        # Ir un poco debajo del referencial
        precio_sugerido = monto_ref * 0.92  # ~8% debajo
        piso = monto_ref * (1 - margen)
        precio_sugerido = max(precio_sugerido, piso)
    else:
        # LP, CP: precio + calidad ponderados
        # Ir cerca del referencial
        precio_sugerido = monto_ref * 0.95
        piso = monto_ref * (1 - margen)
        precio_sugerido = max(precio_sugerido, piso)

    # Nunca superar el monto referencial
    precio_sugerido = min(precio_sugerido, monto_ref)

    # Redondear
    precio_sugerido = round(precio_sugerido, 2)

    # Guardar en propuesta
    async with connection() as conn:
        await conn.execute(
            "UPDATE propuestas SET precio_ofertado=$2 WHERE id=$1",
            propuesta_id, precio_sugerido,
        )

    log.info(
        f"Precio propuesta #{propuesta_id}: {format_monto(precio_sugerido)} "
        f"(ref: {format_monto(monto_ref)}, margen: {margen*100:.0f}%)"
    )
    return precio_sugerido


async def _buscar_historico(licitacion: dict) -> list[float]:
    """Busca precios históricos de licitaciones similares."""
    async with connection() as conn:
        # Buscar por palabras clave en el objeto
        palabras = [w for w in licitacion.get("objeto", "").split() if len(w) > 5][:5]
        if not palabras:
            return []

        conditions = " OR ".join(f"objeto ILIKE '%{p}%'" for p in palabras)
        rows = await conn.fetch(
            f"""SELECT monto_referencial FROM licitaciones
            WHERE monto_referencial IS NOT NULL
            AND monto_referencial > 0
            AND ({conditions})
            AND id != $1
            ORDER BY created_at DESC LIMIT 20""",
            licitacion.get("id", ""),
        )

        # También buscar en historico_precios si existe
        try:
            hist_rows = await conn.fetch(
                """SELECT precio_adjudicado FROM historico_precios
                WHERE objeto_keywords && $1
                ORDER BY fecha DESC LIMIT 10""",
                palabras,
            )
            return [r["precio_adjudicado"] for r in hist_rows if r["precio_adjudicado"]] + \
                   [r["monto_referencial"] for r in rows if r["monto_referencial"]]
        except Exception:
            return [r["monto_referencial"] for r in rows if r["monto_referencial"]]


async def estimar_precio_mercado(licitacion: dict) -> dict:
    """Estima el rango de precios para una licitación."""
    monto_ref = licitacion.get("monto_referencial") or 0
    historico = await _buscar_historico(licitacion)

    if not monto_ref and not historico:
        return {"error": "Sin datos para estimar precio"}

    if historico:
        precios = sorted(historico)
        return {
            "precio_minimo": round(precios[0], 2),
            "precio_maximo": round(precios[-1], 2),
            "precio_mediana": round(precios[len(precios) // 2], 2),
            "precio_promedio": round(sum(precios) / len(precios), 2),
            "muestras": len(precios),
            "monto_referencial": monto_ref,
        }

    return {
        "precio_minimo": round(monto_ref * 0.75, 2),
        "precio_maximo": round(monto_ref, 2),
        "precio_mediana": round(monto_ref * 0.90, 2),
        "precio_promedio": round(monto_ref * 0.88, 2),
        "muestras": 0,
        "monto_referencial": monto_ref,
        "nota": "Estimado sin datos históricos",
    }
