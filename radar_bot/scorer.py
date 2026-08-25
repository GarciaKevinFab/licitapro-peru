"""Scorer — Calcula score de viabilidad 0-100 para cada licitación."""
import json
import logging
from datetime import date
from shared.db import connection
from shared.config import dias_restantes

log = logging.getLogger("radar.scorer")


async def calcular_score(licitacion: dict, empresa_id: int = 1) -> float:
    """Score de viabilidad 0-100. Factores:
    - Match de keywords/rubro (0-25)
    - Región con presencia (0-15)
    - Monto en rango (0-15)
    - Experiencia relevante (0-20)
    - Tipo de procedimiento (0-10)
    - Tiempo disponible (0-15)
    """
    score = 0.0
    detalles = {}

    # 1. Match de keywords / rubro (0-25)
    score_kw = await _score_keywords(licitacion, empresa_id)
    score += score_kw
    detalles["keywords"] = score_kw

    # 2. Región con presencia (0-15)
    score_reg = await _score_region(licitacion, empresa_id)
    score += score_reg
    detalles["region"] = score_reg

    # 3. Monto en rango (0-15)
    score_monto = _score_monto(licitacion)
    score += score_monto
    detalles["monto"] = score_monto

    # 4. Experiencia relevante (0-20)
    score_exp = await _score_experiencia(licitacion, empresa_id)
    score += score_exp
    detalles["experiencia"] = score_exp

    # 5. Tipo de procedimiento (0-10)
    score_tipo = _score_tipo(licitacion)
    score += score_tipo
    detalles["tipo_proc"] = score_tipo

    # 6. Tiempo disponible (0-15)
    score_tiempo = _score_tiempo(licitacion)
    score += score_tiempo
    detalles["tiempo"] = score_tiempo

    final_score = max(0, min(100, score))

    # Guardar score y desglose. El desglose es lo que permite explicarle al
    # usuario por que una licitacion saco el puntaje que saco.
    async with connection() as conn:
        await conn.execute(
            """UPDATE licitaciones SET score_viabilidad=$2, score_detalle=$3
            WHERE id=$1""",
            licitacion["id"], final_score, json.dumps(detalles),
        )

    log.info(f"Score {licitacion['id']}: {final_score:.0f} | {detalles}")
    return final_score


async def _score_keywords(lic: dict, empresa_id: int) -> float:
    """Cuánto matchea el objeto con los rubros/keywords de la empresa."""
    async with connection() as conn:
        config = await conn.fetchrow("SELECT * FROM user_config WHERE user_id=0")
        empresa = await conn.fetchrow("SELECT rubros FROM empresas WHERE id=$1", empresa_id)

    keywords = list(config["keywords"]) if config and config["keywords"] else []
    rubros = list(empresa["rubros"]) if empresa and empresa["rubros"] else []
    all_kw = keywords + rubros

    if not all_kw:
        return 12  # Sin config, score neutro

    objeto = (lic.get("objeto", "") + " " + lic.get("entidad", "")).lower()
    matches = sum(1 for kw in all_kw if kw.lower() in objeto)
    ratio = matches / len(all_kw) if all_kw else 0

    if matches >= 3:
        return 25
    elif matches >= 2:
        return 20
    elif matches >= 1:
        return 12
    return 3


async def _score_region(lic: dict, empresa_id: int) -> float:
    """Score por departamento."""
    async with connection() as conn:
        config = await conn.fetchrow("SELECT regiones FROM user_config WHERE user_id=0")

    regiones = list(config["regiones"]) if config and config["regiones"] else []
    depto = lic.get("departamento", "")

    if not regiones:
        return 8  # Sin filtro de región
    if depto in regiones:
        return 15
    return 2


def _score_monto(lic: dict) -> float:
    """Score por rango de monto."""
    monto = lic.get("monto_referencial") or 0
    if monto == 0:
        return 5  # Sin monto, neutro

    if monto <= 8 * 5150:  # <= 8 UIT (contrato menor ~S/41,200)
        return 12  # Fácil, poca competencia
    elif monto <= 100000:
        return 15  # Buen rango
    elif monto <= 400000:
        return 13  # Buen rango medio
    elif monto <= 1000000:
        return 8  # Más competitivo
    return 4  # Muy grande, mucha competencia


async def _score_experiencia(lic: dict, empresa_id: int) -> float:
    """Score basado en experiencia previa relevante."""
    async with connection() as conn:
        # La columna se llama objeto_contrato, no objeto.
        experiencias = await conn.fetch(
            "SELECT objeto_contrato, keywords, monto FROM experiencia WHERE empresa_id=$1",
            empresa_id,
        )

    if not experiencias:
        return 5

    objeto_lic = (lic.get("objeto", "")).lower()
    max_match = 0

    for exp in experiencias:
        exp_text = (exp["objeto_contrato"] + " " + " ".join(exp["keywords"] or [])).lower()
        # Contar palabras comunes significativas (>4 chars)
        palabras_lic = {w for w in objeto_lic.split() if len(w) > 4}
        palabras_exp = {w for w in exp_text.split() if len(w) > 4}
        common = len(palabras_lic & palabras_exp)
        if common > max_match:
            max_match = common

    if max_match >= 4:
        return 20
    elif max_match >= 2:
        return 14
    elif max_match >= 1:
        return 8
    return 3


def _score_tipo(lic: dict) -> float:
    """Score por tipo de procedimiento."""
    tipo = lic.get("tipo", "")
    scores = {
        "CM": 10,   # Contrato menor, simplísimo
        "CdP": 9,   # Comparación de precios
        "AS": 8,     # Adjudicación simplificada
        "SIE": 7,    # Subasta inversa
        "CD": 6,     # Contratación directa
        "CP": 4,     # Concurso público
        "LP": 3,     # Licitación pública
        "LPA": 4,    # LP abreviada
    }
    return scores.get(tipo, 5)


def _score_tiempo(lic: dict) -> float:
    """Score por tiempo disponible para preparar."""
    dias = dias_restantes(lic.get("fecha_cierre"))
    if dias is None:
        return 8

    if dias <= 0:
        return 0   # Ya cerró
    elif dias <= 2:
        return 3   # Muy poco tiempo
    elif dias <= 5:
        return 8   # Ajustado
    elif dias <= 14:
        return 15  # Ideal
    elif dias <= 30:
        return 12  # Bien
    return 10  # Mucho tiempo, aún lejos


async def recalcular_scores_pendientes(limite: int = 200) -> int:
    """Recalcula scores para licitaciones sin score. Devuelve cuantas puntuo."""
    async with connection() as conn:
        lics = await conn.fetch(
            """SELECT * FROM licitaciones
            WHERE score_viabilidad IS NULL
            AND descartado = FALSE
            AND (fecha_cierre IS NULL OR fecha_cierre > CURRENT_DATE)
            LIMIT $1""",
            limite,
        )

    # Una licitacion con datos raros no debe tumbar el lote entero.
    scoreadas = 0
    for lic in lics:
        try:
            await calcular_score(dict(lic))
            scoreadas += 1
        except Exception as e:
            log.error(f"Score fallo en {lic['id']}: {e}")

    log.info(f"Recalculados {scoreadas}/{len(lics)} scores")
    return scoreadas
