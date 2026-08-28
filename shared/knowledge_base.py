"""Knowledge Base — Sistema de aprendizaje persistente.

El bot pregunta una vez, guarda la respuesta, y NUNCA vuelve a preguntar.
Después de 10-15 licitaciones, el bot ya sabe TODO.
"""
import logging
from shared.db import kb_get, kb_set, connection

log = logging.getLogger("licitapro.kb")


# Categorías válidas
CATEGORIAS = [
    "legal",         # Representante, DNI, partida registral, poderes
    "financiero",    # Banco, cuenta, CCI
    "experiencia",   # Contratos previos, conformidades
    "equipo",        # Profesionales, CVs, colegiatura
    "equipamiento",  # Servidores, herramientas, licencias
    "precios",       # Costos unitarios, márgenes
    "general",       # Otros datos
]


async def buscar_valor(empresa_id: int, categoria: str, clave: str) -> str | None:
    """Busca un valor primero en KB, luego en tablas auxiliares."""
    # 1. Buscar en knowledge_base
    valor = await kb_get(empresa_id, categoria, clave)
    if valor:
        return valor

    # 2. Buscar en tabla empresas
    async with connection() as conn:
        empresa = await conn.fetchrow("SELECT * FROM empresas WHERE id=$1", empresa_id)
        if empresa and clave in dict(empresa) and empresa[clave]:
            valor = str(empresa[clave])
            await kb_set(empresa_id, categoria, clave, valor, "tabla_empresas")
            return valor

    # 3. Buscar en equipo_tecnico si es de equipo
    if categoria == "equipo":
        async with connection() as conn:
            miembro = await conn.fetchrow(
                "SELECT * FROM equipo_tecnico WHERE empresa_id=$1 AND especialidad ILIKE $2 LIMIT 1",
                empresa_id, f"%{clave}%",
            )
            if miembro:
                valor = f"{miembro['nombre_completo']} | {miembro['titulo_profesional']} | CIP {miembro['colegiatura']} | {miembro['anos_experiencia']} años"
                return valor

    return None


async def guardar_respuesta(empresa_id: int, categoria: str, clave: str, valor: str):
    """Guarda una respuesta del usuario en la KB. No se vuelve a preguntar."""
    await kb_set(empresa_id, categoria, clave, valor, "usuario_respuesta")
    log.info(f"KB guardado: empresa={empresa_id} {categoria}/{clave}")


async def obtener_datos_empresa_completos(empresa_id: int) -> dict:
    """Obtiene todos los datos conocidos de una empresa desde KB + tablas."""
    datos = {}

    # Datos de la tabla empresas
    async with connection() as conn:
        empresa = await conn.fetchrow("SELECT * FROM empresas WHERE id=$1", empresa_id)
        if empresa:
            datos["empresa"] = dict(empresa)

        # Datos de KB
        kb_rows = await conn.fetch(
            "SELECT categoria, clave, valor FROM knowledge_base WHERE empresa_id=$1",
            empresa_id,
        )
        for row in kb_rows:
            key = f"{row['categoria']}.{row['clave']}"
            datos[key] = row["valor"]

        # Equipo técnico
        equipo = await conn.fetch(
            "SELECT * FROM equipo_tecnico WHERE empresa_id=$1 AND disponible=TRUE",
            empresa_id,
        )
        datos["equipo"] = [dict(m) for m in equipo]

        # Experiencia
        exp = await conn.fetch(
            "SELECT * FROM experiencia WHERE empresa_id=$1 ORDER BY fecha_fin DESC",
            empresa_id,
        )
        datos["experiencia"] = [dict(e) for e in exp]

    return datos


async def buscar_experiencia_relevante(empresa_id: int, keywords: list[str], limit: int = 5) -> list[dict]:
    """Busca experiencia relevante para una licitación."""
    async with connection() as conn:
        todas = await conn.fetch(
            "SELECT * FROM experiencia WHERE empresa_id=$1 ORDER BY monto DESC",
            empresa_id,
        )

    relevantes = []
    for exp in todas:
        exp_text = f"{exp['objeto_contrato']} {' '.join(exp['keywords'] or [])}".lower()
        score = sum(1 for kw in keywords if kw.lower() in exp_text)
        if score > 0:
            relevantes.append({**dict(exp), "_score": score})

    relevantes.sort(key=lambda x: x["_score"], reverse=True)
    return relevantes[:limit]


async def buscar_profesional(empresa_id: int, requisitos: str) -> list[dict]:
    """Busca profesionales que cumplan requisitos dados."""
    async with connection() as conn:
        equipo = await conn.fetch(
            """SELECT * FROM equipo_tecnico
            WHERE empresa_id=$1 AND disponible=TRUE
            AND (titulo_profesional ILIKE $2 OR especialidad ILIKE $2
                 OR nombre_completo ILIKE $2)""",
            empresa_id, f"%{requisitos}%",
        )
    return [dict(m) for m in equipo]


async def estadisticas_kb(empresa_id: int) -> dict:
    """Estadísticas de cuánto sabe el bot de esta empresa."""
    async with connection() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM knowledge_base WHERE empresa_id=$1", empresa_id
        )
        por_categoria = await conn.fetch(
            "SELECT categoria, COUNT(*) as cnt FROM knowledge_base WHERE empresa_id=$1 GROUP BY categoria",
            empresa_id,
        )
        mas_usados = await conn.fetch(
            "SELECT clave, valor, usado_count FROM knowledge_base WHERE empresa_id=$1 ORDER BY usado_count DESC LIMIT 10",
            empresa_id,
        )

    return {
        "total_campos": total,
        "por_categoria": {r["categoria"]: r["cnt"] for r in por_categoria},
        "mas_usados": [dict(r) for r in mas_usados],
    }
