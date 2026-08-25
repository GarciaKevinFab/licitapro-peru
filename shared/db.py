"""Shared database module — PostgreSQL connection and helpers."""
import os
import json
import logging
from datetime import datetime, date
from contextlib import asynccontextmanager
import asyncpg

log = logging.getLogger("licitapro.db")

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=os.getenv("POSTGRES_HOST", "localhost"),
            port=int(os.getenv("POSTGRES_PORT", "5433")),
            database=os.getenv("POSTGRES_DB", "licitapro"),
            user=os.getenv("POSTGRES_USER", "licitapro"),
            password=os.getenv("POSTGRES_PASSWORD", "licitapro2026"),
            min_size=2,
            max_size=10,
        )
        log.info("PostgreSQL pool created")
    return _pool


@asynccontextmanager
async def connection():
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


# ─── Licitaciones ────────────────────────────────────────
async def upsert_licitacion(data: dict) -> bool:
    """Insert or update a licitacion. Returns True if it's new."""
    async with connection() as conn:
        existing = await conn.fetchval(
            "SELECT id FROM licitaciones WHERE id = $1", data["id"]
        )
        if existing:
            await conn.execute(
                """UPDATE licitaciones SET estado=$2, updated_at=NOW() 
                WHERE id=$1 AND estado != $2""",
                data["id"], data.get("estado", "convocado"),
            )
            return False

        await conn.execute(
            """INSERT INTO licitaciones 
            (id, fuente, tipo, nomenclatura, entidad, entidad_tipo, objeto,
             monto_referencial, moneda, fecha_publicacion, fecha_cierre,
             estado, departamento, provincia, url, bases_urls)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)""",
            data["id"], data["fuente"], data.get("tipo"),
            data.get("nomenclatura"), data["entidad"],
            data.get("entidad_tipo"), data["objeto"],
            data.get("monto_referencial"), data.get("moneda", "PEN"),
            data.get("fecha_publicacion"), data.get("fecha_cierre"),
            data.get("estado", "convocado"), data.get("departamento"),
            data.get("provincia"), data.get("url"),
            data.get("bases_urls", []),
        )
        return True


async def get_licitaciones_nuevas(limit=20):
    async with connection() as conn:
        return await conn.fetch(
            """SELECT * FROM licitaciones 
            WHERE notificado = FALSE AND descartado = FALSE
            ORDER BY score_viabilidad DESC NULLS LAST, fecha_cierre ASC
            LIMIT $1""",
            limit,
        )


async def marcar_notificada(lid: str):
    async with connection() as conn:
        await conn.execute(
            "UPDATE licitaciones SET notificado=TRUE WHERE id=$1", lid
        )


# ─── Knowledge Base ──────────────────────────────────────
async def kb_get(empresa_id: int, categoria: str, clave: str) -> str | None:
    async with connection() as conn:
        row = await conn.fetchrow(
            """SELECT valor FROM knowledge_base 
            WHERE empresa_id=$1 AND categoria=$2 AND clave=$3""",
            empresa_id, categoria, clave,
        )
        if row:
            await conn.execute(
                """UPDATE knowledge_base SET usado_count = usado_count + 1
                WHERE empresa_id=$1 AND categoria=$2 AND clave=$3""",
                empresa_id, categoria, clave,
            )
            return row["valor"]
        return None


async def kb_set(empresa_id: int, categoria: str, clave: str, valor: str, fuente="usuario_respuesta"):
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO knowledge_base (empresa_id, categoria, clave, valor, fuente)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (empresa_id, categoria, clave) 
            DO UPDATE SET valor=$4, updated_at=NOW()""",
            empresa_id, categoria, clave, valor, fuente,
        )


# ─── Empresas ────────────────────────────────────────────
async def get_empresa(empresa_id: int):
    async with connection() as conn:
        return await conn.fetchrow("SELECT * FROM empresas WHERE id=$1", empresa_id)


async def get_empresas_activas():
    async with connection() as conn:
        return await conn.fetch("SELECT * FROM empresas WHERE activa=TRUE ORDER BY id")


# ─── Propuestas ──────────────────────────────────────────
async def crear_propuesta(licitacion_id: str, empresa_id: int) -> int:
    async with connection() as conn:
        return await conn.fetchval(
            """INSERT INTO propuestas (licitacion_id, empresa_id, estado)
            VALUES ($1, $2, 'iniciado') RETURNING id""",
            licitacion_id, empresa_id,
        )


async def get_preguntas_pendientes(propuesta_id: int):
    async with connection() as conn:
        return await conn.fetch(
            """SELECT * FROM preguntas 
            WHERE propuesta_id=$1 AND respondida=FALSE
            ORDER BY id""",
            propuesta_id,
        )


async def responder_pregunta(pregunta_id: int, respuesta: str):
    async with connection() as conn:
        row = await conn.fetchrow("SELECT * FROM preguntas WHERE id=$1", pregunta_id)
        if not row:
            return
        await conn.execute(
            """UPDATE preguntas SET respuesta=$2, respondida=TRUE, responded_at=NOW()
            WHERE id=$1""",
            pregunta_id, respuesta,
        )
        # Guardar en KB si tiene categoría definida
        if row["kb_categoria"] and row["kb_clave"]:
            await kb_set(row["empresa_id"], row["kb_categoria"], row["kb_clave"], respuesta)
            await conn.execute(
                "UPDATE preguntas SET guardada_en_kb=TRUE WHERE id=$1", pregunta_id
            )


# ─── Config ──────────────────────────────────────────────
# Columnas de user_config que update_config puede modificar. user_id es la
# clave y created_at lo pone la BD, asi que quedan fuera a proposito.
CAMPOS_USER_CONFIG = frozenset({
    "regiones", "entidad_tipos", "keywords", "keywords_excluir",
    "monto_min", "monto_max", "empresa_default_id", "email_notificaciones",
    "horario_inicio", "horario_fin", "frecuencia_resumen", "activo",
})


async def get_config(user_id: int):
    async with connection() as conn:
        row = await conn.fetchrow("SELECT * FROM user_config WHERE user_id=$1", user_id)
        if not row:
            row = await conn.fetchrow("SELECT * FROM user_config WHERE user_id=0")
        return row


async def update_config(user_id: int, **kwargs):
    async with connection() as conn:
        existing = await conn.fetchval("SELECT user_id FROM user_config WHERE user_id=$1", user_id)
        if not existing:
            await conn.execute(
                "INSERT INTO user_config (user_id) VALUES ($1) ON CONFLICT DO NOTHING",
                user_id,
            )
        for key, val in kwargs.items():
            # El nombre de columna se interpola, asi que tiene que salir de una
            # lista blanca. Hoy los llamadores pasan identificadores escritos a
            # mano, pero en cuanto la web arme kwargs desde un formulario esto
            # seria una inyeccion por nombre de columna.
            if key not in CAMPOS_USER_CONFIG:
                raise ValueError(f"campo de user_config no permitido: {key!r}")
            await conn.execute(
                f"UPDATE user_config SET {key}=$2 WHERE user_id=$1",
                user_id, val,
            )


# ─── Contratos ───────────────────────────────────────────
async def get_contratos_activos(empresa_id: int = None):
    async with connection() as conn:
        if empresa_id:
            return await conn.fetch(
                """SELECT c.*, l.objeto, l.entidad FROM contratos c
                JOIN licitaciones l ON c.licitacion_id = l.id
                WHERE c.empresa_id=$1 AND c.estado NOT IN ('pagado','cancelado')
                ORDER BY c.fecha_entrega_final""",
                empresa_id,
            )
        return await conn.fetch(
            """SELECT c.*, l.objeto, l.entidad FROM contratos c
            JOIN licitaciones l ON c.licitacion_id = l.id
            WHERE c.estado NOT IN ('pagado','cancelado')
            ORDER BY c.fecha_entrega_final"""
        )


async def get_plazos_proximos(dias: int = 7):
    async with connection() as conn:
        return await conn.fetch(
            """SELECT p.*, c.numero_contrato, l.objeto, l.entidad 
            FROM plazos p
            JOIN contratos c ON p.contrato_id = c.id
            JOIN licitaciones l ON c.licitacion_id = l.id
            WHERE p.completado = FALSE 
            AND p.fecha_limite <= CURRENT_DATE + $1 * INTERVAL '1 day'
            ORDER BY p.fecha_limite""",
            dias,
        )


# ─── Scraping Log ────────────────────────────────────────
async def log_scraping_start(fuente: str) -> int:
    async with connection() as conn:
        return await conn.fetchval(
            "INSERT INTO scraping_log (fuente) VALUES ($1) RETURNING id", fuente
        )


async def log_scraping_end(log_id: int, encontrados: int, nuevos: int, errores: int = 0, error_detalle: str = None):
    async with connection() as conn:
        await conn.execute(
            """UPDATE scraping_log SET fin=NOW(), registros_encontrados=$2,
            registros_nuevos=$3, errores=$4, error_detalle=$5, status='done'
            WHERE id=$1""",
            log_id, encontrados, nuevos, errores, error_detalle,
        )


async def refrescar_licitacion(data: dict) -> bool:
    """Inserta la licitacion o refresca la existente. True si es nueva.

    A diferencia de `upsert_licitacion`, que al reencontrar una fila solo toca
    `estado`, esta refresca tambien fecha de cierre, monto y bases. Hace falta
    para OCDS: la API republica cada proceso a diario y el plazo puede moverse
    (prorroga de consultas), asi que la fila tiene que seguir el cambio.
    """
    async with connection() as conn:
        fila = await conn.fetchrow(
            "SELECT id FROM licitaciones WHERE id = $1", data["id"]
        )
        await conn.execute(
            """INSERT INTO licitaciones
            (id, fuente, tipo, nomenclatura, entidad, entidad_tipo, entidad_ruc,
             objeto, monto_referencial, moneda, fecha_publicacion, fecha_cierre,
             estado, departamento, url, bases_urls)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
            ON CONFLICT (id) DO UPDATE SET
                estado            = EXCLUDED.estado,
                fecha_cierre      = EXCLUDED.fecha_cierre,
                monto_referencial = COALESCE(EXCLUDED.monto_referencial,
                                             licitaciones.monto_referencial),
                bases_urls        = EXCLUDED.bases_urls,
                updated_at        = NOW()""",
            data["id"], data["fuente"], data.get("tipo"),
            data.get("nomenclatura"), data["entidad"], data.get("entidad_tipo"),
            data.get("entidad_ruc"), data["objeto"],
            data.get("monto_referencial"), data.get("moneda", "PEN"),
            data.get("fecha_publicacion"), data.get("fecha_cierre"),
            data.get("estado", "convocado"), data.get("departamento"),
            data.get("url"), data.get("bases_urls", []),
        )
        return fila is None
