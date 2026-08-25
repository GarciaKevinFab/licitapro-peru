"""Analyzer — Análisis IA de licitaciones con Claude API."""
import os
import json
import logging
import anthropic
from shared.db import connection, kb_get
from shared.config import ANTHROPIC_KEY

log = logging.getLogger("radar.analyzer")


def get_client() -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY)


async def analizar_licitacion(licitacion: dict, empresa_id: int = 1) -> dict:
    """Análisis profundo de una licitación usando Claude API.

    Retorna: {score, resumen, requisitos, riesgos, recomendacion, precio_estimado}
    """
    if not ANTHROPIC_KEY:
        log.warning("ANTHROPIC_API_KEY no configurada, usando análisis básico")
        return analisis_basico(licitacion)

    # Obtener datos de empresa para contexto
    datos_empresa = await _obtener_contexto_empresa(empresa_id)

    prompt = f"""Eres un experto en licitaciones públicas en Perú (Ley 30225 y su reglamento).
Analiza esta licitación y evalúa la viabilidad para la empresa.

## LICITACIÓN
- ID: {licitacion.get('nomenclatura') or licitacion['id']}
- Entidad: {licitacion['entidad']}
- Objeto: {licitacion['objeto']}
- Monto referencial: {licitacion.get('monto_referencial', 'No especificado')}
- Tipo: {licitacion.get('tipo', 'No especificado')}
- Departamento: {licitacion.get('departamento', 'No especificado')}
- Cierre: {licitacion.get('fecha_cierre', 'No especificado')}

## EMPRESA
{datos_empresa}

## RESPONDE EN JSON:
{{
    "score_viabilidad": <0-100>,
    "resumen": "<resumen en 2-3 oraciones>",
    "requisitos_clave": ["<req1>", "<req2>", ...],
    "riesgos": ["<riesgo1>", "<riesgo2>", ...],
    "fortalezas": ["<fortaleza1>", ...],
    "recomendacion": "licitar|pasar|evaluar",
    "precio_sugerido_min": <float o null>,
    "precio_sugerido_max": <float o null>,
    "competidores_estimados": <int>,
    "justificacion_score": "<por qué este score>"
}}"""

    try:
        client = get_client()
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )

        text = response.content[0].text
        # Extraer JSON de la respuesta
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            resultado = json.loads(text[start:end])
        else:
            resultado = analisis_basico(licitacion)

        # Guardar análisis en DB. La columna es bases_analisis (JSONB); antes
        # aquí decía analisis_ia, que no existe en el esquema, y el except de
        # abajo se tragaba el error: el análisis nunca llegaba a guardarse.
        async with connection() as conn:
            await conn.execute(
                """UPDATE licitaciones SET
                score_viabilidad=$2, bases_analisis=$3
                WHERE id=$1""",
                licitacion["id"],
                resultado.get("score_viabilidad", 0),
                json.dumps(resultado),
            )

        log.info(f"Análisis completado: {licitacion['id']} → score={resultado.get('score_viabilidad')}")
        return resultado

    except Exception as e:
        # exc_info para que el traceback quede en el log: este except tapaba
        # un error de esquema durante semanas sin dejar rastro.
        log.error(f"Error en análisis IA de {licitacion.get('id')}: {e}", exc_info=True)
        return analisis_basico(licitacion)


async def _obtener_contexto_empresa(empresa_id: int) -> str:
    """Construye contexto de la empresa para el prompt."""
    async with connection() as conn:
        empresa = await conn.fetchrow("SELECT * FROM empresas WHERE id=$1", empresa_id)
        experiencias = await conn.fetch(
            "SELECT objeto_contrato, monto, entidad_contratante FROM experiencia"
            " WHERE empresa_id=$1 ORDER BY monto DESC NULLS LAST LIMIT 5",
            empresa_id,
        )
        equipo = await conn.fetch(
            "SELECT nombre_completo, titulo_profesional, especialidad, anos_experiencia"
            " FROM equipo_tecnico WHERE empresa_id=$1 AND disponible=TRUE",
            empresa_id,
        )

    if not empresa:
        return "Empresa no registrada"

    ctx = f"- Razón social: {empresa['razon_social']}\n"
    ctx += f"- RUC: {empresa['ruc']}\n"
    ctx += f"- Rubros: {', '.join(empresa['rubros'] or [])}\n"

    if experiencias:
        ctx += "- Experiencia relevante:\n"
        for exp in experiencias:
            monto_txt = f"S/{exp['monto']:,.0f}" if exp['monto'] else "monto no registrado"
            ctx += f"  * {exp['objeto_contrato'][:80]} ({exp['entidad_contratante']}) — {monto_txt}\n"

    if equipo:
        ctx += "- Equipo técnico disponible:\n"
        for m in equipo:
            ctx += f"  * {m['nombre_completo']} — {m['titulo_profesional']} — {m['especialidad']} ({m['anos_experiencia']} años)\n"

    return ctx


def analisis_basico(licitacion: dict) -> dict:
    """Análisis heurístico sin IA (fallback)."""
    score = 50
    riesgos = []
    fortalezas = []

    monto = licitacion.get("monto_referencial", 0) or 0
    if 10000 <= monto <= 500000:
        score += 10
        fortalezas.append("Monto en rango manejable")
    elif monto > 500000:
        score -= 10
        riesgos.append("Monto alto, mayor competencia esperada")

    tipo = licitacion.get("tipo", "")
    if tipo in ("AS", "SIE", "CdP", "CM"):
        score += 10
        fortalezas.append(f"Procedimiento simplificado ({tipo})")
    elif tipo in ("LP", "CP"):
        score -= 5
        riesgos.append(f"Procedimiento complejo ({tipo})")

    depto = licitacion.get("departamento", "")
    if depto in ("Madre de Dios", "Junín", "Cusco"):
        score += 15
        fortalezas.append(f"Región con presencia ({depto})")

    score = max(0, min(100, score))

    return {
        "score_viabilidad": score,
        "resumen": f"Análisis básico. {licitacion['objeto'][:100]}",
        "requisitos_clave": ["Revisar bases para requisitos específicos"],
        "riesgos": riesgos or ["Sin análisis IA disponible"],
        "fortalezas": fortalezas,
        "recomendacion": "evaluar",
        "precio_sugerido_min": None,
        "precio_sugerido_max": None,
        "competidores_estimados": 3,
        "justificacion_score": "Análisis heurístico (sin Claude API)",
    }


async def analizar_bases_pdf(licitacion_id: str, pdf_path: str) -> dict:
    """Analiza un PDF de bases de licitación con Claude API."""
    if not ANTHROPIC_KEY:
        return {"error": "ANTHROPIC_API_KEY no configurada"}

    try:
        import base64
        with open(pdf_path, "rb") as f:
            pdf_b64 = base64.standard_b64encode(f.read()).decode()

        client = get_client()
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64},
                    },
                    {
                        "type": "text",
                        "text": """Analiza estas bases de licitación y extrae en JSON:
{
    "resumen_objeto": "<qué se está contratando>",
    "requisitos_habilitantes": ["<req1>", ...],
    "factores_evaluacion": [{"factor": "<nombre>", "puntaje_max": <int>}, ...],
    "experiencia_requerida": {"monto_minimo": <float>, "contratos_minimos": <int>, "descripcion": "<desc>"},
    "equipo_tecnico_requerido": [{"cargo": "<cargo>", "titulo": "<req>", "experiencia_anos": <int>}, ...],
    "plazo_ejecucion_dias": <int>,
    "garantias_requeridas": ["<tipo>", ...],
    "anexos_requeridos": ["<nombre_anexo>", ...],
    "penalidades": "<resumen de penalidades>",
    "valor_referencial": <float o null>
}""",
                    },
                ],
            }],
        )

        text = response.content[0].text
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
        return {"raw_response": text}

    except Exception as e:
        log.error(f"Error analizando PDF: {e}")
        return {"error": str(e)}
