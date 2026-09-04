"""Pricing — Calcula precio económico competitivo con histórico."""
import logging

from shared.config import format_monto, normalizar
from shared.db import connection, kb_get

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

        # Parametrizado: `palabras` sale del objeto scrapeado, o sea de texto
        # que publica un tercero. Interpolarlo en el SQL abria una inyeccion.
        patrones = [f"%{w}%" for w in palabras]
        rows = await conn.fetch(
            """SELECT monto_referencial FROM licitaciones
            WHERE monto_referencial IS NOT NULL
            AND monto_referencial > 0
            AND objeto ILIKE ANY($2::text[])
            AND id != $1
            ORDER BY created_at DESC LIMIT 20""",
            licitacion.get("id", ""), patrones,
        )

        # La columna es monto_adjudicado. Antes decia precio_adjudicado, que no
        # existe, y el except se lo tragaba: esta rama nunca llego a ejecutarse.
        try:
            # Ambos lados normalizados (minusculas, sin tildes). Sin esto el
            # overlap de arrays no matchea nunca: las claves se guardan
            # normalizadas y el objeto llega en MAYUSCULAS y con tildes.
            claves = [normalizar(w) for w in palabras]
            hist_rows = await conn.fetch(
                """SELECT COALESCE(monto_adjudicado, monto_referencial) AS precio
                FROM historico_precios
                WHERE objeto_keywords && $1
                  AND COALESCE(monto_adjudicado, monto_referencial) > 0
                ORDER BY fecha DESC NULLS LAST LIMIT 10""",
                claves,
            )
            return [r["precio"] for r in hist_rows if r["precio"]] + \
                   [r["monto_referencial"] for r in rows if r["monto_referencial"]]
        except Exception as e:
            log.exception(f"historico_precios fallo: {e}")
            return [r["monto_referencial"] for r in rows if r["monto_referencial"]]


def _cuartil(ordenados: list[float], fraccion: float) -> float:
    """Valor por debajo del cual queda `fraccion` de la muestra."""
    if not ordenados:
        return 0.0
    i = min(len(ordenados) - 1, int(len(ordenados) * fraccion))
    return ordenados[i]


async def estimar_precio_mercado(licitacion: dict) -> dict:
    """Rango de precios de licitaciones parecidas, en cuartiles.

    POR QUE CUARTILES Y NO MINIMO Y MAXIMO

      `_buscar_historico` casa por cualquier palabra del objeto de mas de cinco
      letras. En estos textos eso incluye "servicio", "mejoramiento" o
      "sistema", que aparecen en media contratacion publica del pais. La
      muestra que devuelve es util en el centro y basura en los bordes.

      Con minimo y maximo, una supervision de S/ 310 mil mostraba "entre
      S/ 16,400 y S/ 27,668,918": tres ordenes de magnitud, o sea ningun dato,
      presentado con la misma confianza que uno bueno. Los cuartiles recortan
      justamente esos extremos y dejan el tramo donde cae la mitad central de
      los casos, que es la pregunta que se hace quien va a ofertar.

      Los extremos siguen saliendo por separado, para quien quiera mirarlos
      sabiendo lo que son.
    """
    monto_ref = licitacion.get("monto_referencial") or 0
    historico = await _buscar_historico(licitacion)

    if not monto_ref and not historico:
        return {"error": "Sin datos para estimar precio"}

    # Por debajo de cuatro muestras un cuartil no significa nada: se cae a la
    # horquilla sobre el referencial, que al menos no finge ser un dato.
    if len(historico) >= 4:
        precios = sorted(historico)
        return {
            "precio_bajo": round(_cuartil(precios, 0.25), 2),
            "precio_mediana": round(_cuartil(precios, 0.50), 2),
            "precio_alto": round(_cuartil(precios, 0.75), 2),
            "extremo_min": round(precios[0], 2),
            "extremo_max": round(precios[-1], 2),
            "muestras": len(precios),
            "monto_referencial": monto_ref,
        }

    return {
        "precio_bajo": round(monto_ref * 0.75, 2),
        "precio_mediana": round(monto_ref * 0.90, 2),
        "precio_alto": round(monto_ref, 2),
        "extremo_min": None,
        "extremo_max": None,
        "muestras": len(historico),
        "monto_referencial": monto_ref,
        "nota": "Horquilla sobre el monto referencial: no hay muestra "
                "historica suficiente.",
    }
