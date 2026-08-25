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
            # La sesion trabaja en hora de Lima. Sin esto, NOW() devuelve la
            # zona del contenedor (UTC) mientras Python escribe timestamps
            # naive en hora local: cinco horas de desfase. Un token de Telegram
            # nacia pareciendo caducado, y una licitacion se daba por vencida
            # cinco horas antes de cerrar, justo en el caso "cierra hoy".
            server_settings={"timezone": "America/Lima"},
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


# ─── Multi-inquilino ─────────────────────────────────────
# El scoping se aplica en el BORDE (handler de bot, ruta web): ahi se resuelve
# el usuario y se validan los ids. Las funciones internas siguen recibiendo un
# empresa_id ya validado. Meter usuario_id en las 14 hojas seria refactor por
# refactor y no daria mas seguridad de la que da validar en la entrada.

async def get_usuario(usuario_id: int):
    async with connection() as conn:
        return await conn.fetchrow(
            "SELECT * FROM usuarios WHERE id=$1 AND activo=TRUE", usuario_id
        )


async def get_usuario_por_telegram(chat_id: int):
    """Resuelve el usuario a partir del chat de Telegram.

    El chat_id lo entrega Telegram al vincular por enlace profundo, nunca lo
    escribe el usuario: por eso sirve como identidad y no puede usarse para
    redirigir las alertas de otra cuenta.
    """
    async with connection() as conn:
        return await conn.fetchrow(
            "SELECT * FROM usuarios WHERE telegram_chat_id=$1 AND activo=TRUE", chat_id
        )


async def empresas_de(usuario_id: int):
    """Empresas activas del usuario. Base del scoping en el borde."""
    async with connection() as conn:
        return await conn.fetch(
            """SELECT * FROM empresas
               WHERE usuario_id=$1 AND activa=TRUE ORDER BY id""",
            usuario_id,
        )


async def empresa_es_de(empresa_id: int, usuario_id: int) -> bool:
    """Verificacion de propiedad. Llamar SIEMPRE antes de actuar sobre una
    empresa cuyo id vino de fuera (formulario web, callback de Telegram)."""
    async with connection() as conn:
        return await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM empresas WHERE id=$1 AND usuario_id=$2)",
            empresa_id, usuario_id,
        )


async def get_config_usuario(usuario_id: int):
    """Configuracion del usuario. Reemplaza a get_config, que indexa por el
    user_id heredado (chat de Telegram o 0) y no distingue inquilinos."""
    async with connection() as conn:
        return await conn.fetchrow(
            "SELECT * FROM user_config WHERE usuario_id=$1", usuario_id
        )


async def licitaciones_para_usuario(usuario_id: int, limite: int = 50,
                                    solo_vigentes: bool = True):
    """Aplica los filtros del usuario sobre el pozo COMPARTIDO de licitaciones.

    Las licitaciones son datos publicos del Estado: se scrapean una vez y valen
    para todos. Lo privado es a quien le interesa cada una. Por eso el match va
    aqui, en consulta, y no en el scraper: filtrar al scrapear obligaria a
    recorrer las fuentes una vez por inquilino.
    """
    from shared.config import match_keywords

    config = await get_config_usuario(usuario_id)
    if not config:
        # Cuenta sin configurar todavia: se muestra el pozo entero en vez de
        # nada. Un panel vacio recien creada la cuenta se lee como "esto no
        # funciona"; mostrar todo invita a acotar, que es lo que queremos.
        config = {"regiones": [], "keywords": [], "keywords_excluir": [],
                  "monto_min": None, "monto_max": None}

    condiciones, params = [], []
    if solo_vigentes:
        condiciones.append("fecha_cierre > NOW()")
    if config["regiones"]:
        params.append(list(config["regiones"]))
        condiciones.append(f"(departamento IS NULL OR departamento = ANY(${len(params)}))")
    if config["monto_min"] is not None:
        params.append(float(config["monto_min"]))
        condiciones.append(
            f"(monto_referencial IS NULL OR monto_referencial >= ${len(params)})")
    if config["monto_max"] is not None:
        params.append(float(config["monto_max"]))
        condiciones.append(
            f"(monto_referencial IS NULL OR monto_referencial <= ${len(params)})")
    condiciones.append("descartado = FALSE")
    where = "WHERE " + " AND ".join(condiciones)

    async with connection() as conn:
        filas = await conn.fetch(
            f"""SELECT * FROM licitaciones {where}
                ORDER BY score_viabilidad DESC NULLS LAST, fecha_cierre ASC
                LIMIT 500""",
            *params,
        )

    # Keywords y exclusiones se resuelven en Python: el match ignora tildes y la
    # acentuacion que publica OSCE viene corrupta, asi que ILIKE no sirve.
    keywords = list(config["keywords"] or [])
    excluir = list(config["keywords_excluir"] or [])
    salida = []
    for f in filas:
        texto = f"{f['objeto']} {f['entidad']} {f['nomenclatura'] or ''}"
        if keywords and not match_keywords(texto, keywords):
            continue
        if excluir and match_keywords(texto, excluir):
            continue
        salida.append(f)
        if len(salida) >= limite:
            break
    return salida


# ─── Cuentas ─────────────────────────────────────────────

async def crear_usuario(email: str, password_hash: str, nombre: str | None = None):
    """None si el correo ya existe. La unicidad la impone la BD, no un SELECT
    previo: entre el SELECT y el INSERT cabe otra alta con el mismo correo."""
    async with connection() as conn:
        fila = await conn.fetchrow(
            """INSERT INTO usuarios (email, password_hash, nombre, plan)
               VALUES (LOWER($1), $2, $3, 'trial')
               ON CONFLICT (email) DO NOTHING
               RETURNING *""",
            email.strip(), password_hash, nombre,
        )
        if fila:
            # Su fila de configuracion nace con el usuario. Si no, la pantalla
            # de filtros no tendria donde guardar y el panel se veria roto.
            # user_id es la columna heredada (chat de Telegram); se usa el
            # negativo del id para no chocar con ningun chat real.
            await conn.execute(
                """INSERT INTO user_config (user_id, usuario_id)
                   VALUES ($1, $2) ON CONFLICT DO NOTHING""",
                -fila["id"], fila["id"],
            )
    if fila:
        # La prueba nace con la cuenta: sin suscripcion el panel no sabria que
        # plan aplicar ni cuantas empresas permitir.
        from shared.suscripciones import crear_suscripcion_prueba
        await crear_suscripcion_prueba(fila["id"])
    return fila


async def get_usuario_por_email(email: str):
    async with connection() as conn:
        return await conn.fetchrow(
            "SELECT * FROM usuarios WHERE email = LOWER($1) AND activo = TRUE",
            email.strip(),
        )


# ─── Vinculacion de Telegram ─────────────────────────────

async def set_token_telegram(usuario_id: int, token: str, expira) -> None:
    async with connection() as conn:
        await conn.execute(
            """UPDATE usuarios SET telegram_token=$2, telegram_token_expira=$3
               WHERE id=$1""",
            usuario_id, token, expira,
        )


async def vincular_telegram(token: str, chat_id: int):
    """Canjea el token por el vinculo. Devuelve el usuario o None.

    El chat_id lo aporta Telegram al procesar /start, nunca el usuario. El token
    se consume en el mismo UPDATE que lo valida, asi que no puede reutilizarse
    aunque llegue dos veces.
    """
    async with connection() as conn:
        return await conn.fetchrow(
            """UPDATE usuarios
                  SET telegram_chat_id = $2,
                      telegram_token = NULL,
                      telegram_token_expira = NULL
                WHERE telegram_token = $1
                  AND telegram_token_expira > NOW()
                  AND activo = TRUE
             RETURNING *""",
            token, chat_id,
        )


# ─── Boveda de credenciales ──────────────────────────────

async def guardar_credencial(usuario_id: int, tipo: str, valor: str) -> None:
    from shared.seguridad import cifrar
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO credenciales (usuario_id, tipo, valor_cifrado)
               VALUES ($1, $2, $3)
               ON CONFLICT (usuario_id, tipo)
               DO UPDATE SET valor_cifrado = EXCLUDED.valor_cifrado,
                             actualizado_en = NOW()""",
            usuario_id, tipo, cifrar(valor),
        )


async def obtener_credencial(usuario_id: int, tipo: str) -> str | None:
    """Descifra la credencial. SOLO para uso del servidor.

    Nunca debe llegar a una respuesta HTTP: la interfaz muestra si esta
    configurada y cuando se actualizo, y ofrece reemplazarla, jamas verla.
    """
    from shared.seguridad import descifrar
    async with connection() as conn:
        crudo = await conn.fetchval(
            "SELECT valor_cifrado FROM credenciales WHERE usuario_id=$1 AND tipo=$2",
            usuario_id, tipo,
        )
    return descifrar(crudo) if crudo else None


async def estado_credenciales(usuario_id: int) -> dict:
    """Que hay configurado, sin exponer ningun valor."""
    async with connection() as conn:
        filas = await conn.fetch(
            "SELECT tipo, actualizado_en FROM credenciales WHERE usuario_id=$1",
            usuario_id,
        )
    return {f["tipo"]: f["actualizado_en"] for f in filas}


async def borrar_credencial(usuario_id: int, tipo: str) -> None:
    async with connection() as conn:
        await conn.execute(
            "DELETE FROM credenciales WHERE usuario_id=$1 AND tipo=$2",
            usuario_id, tipo,
        )
