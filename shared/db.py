"""Shared database module — PostgreSQL connection and helpers."""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from decimal import Decimal

import asyncpg
from dotenv import load_dotenv

# Este modulo lee las credenciales del entorno, asi que carga el .env el mismo
# en vez de depender de que alguien importe shared.config antes. Ese orden
# implicito funcionaba por accidente y se rompia segun el punto de entrada.
load_dotenv()

log = logging.getLogger("licitapro.db")

_pool: asyncpg.Pool | None = None

# Candado para crear el pool una sola vez. POR QUE: `create_pool` es await, y
# mientras espera al servidor otra peticion que llegue al primer uso ve
# `_pool is None` y crea un segundo pool. El primero queda huerfano con sus
# conexiones abiertas hasta que el proceso muere. Se guarda junto al bucle que
# lo creo: un asyncio.Lock queda atado a su bucle, y un script que hace dos
# `asyncio.run` seguidos reventaria con "bound to a different event loop".
_candado: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None

# Cuanto se espera por una conexion libre antes de rendirse. POR QUE: sin
# tope, con el pool agotado (diez peticiones que piden una segunda conexion
# teniendo ya una) la aplicacion se queda colgada para siempre y sin un solo
# error en el log. Con tope la peticion falla, se registra y las demas siguen.
SEGUNDOS_ESPERA_CONEXION = float(os.getenv("POSTGRES_ESPERA_CONEXION", "15"))
# Tope de cada consulta. Una consulta que pasa de un minuto en esta base es un
# bloqueo o un error, no trabajo legitimo, y retiene una de las diez conexiones.
SEGUNDOS_POR_CONSULTA = float(os.getenv("POSTGRES_TIEMPO_CONSULTA", "60"))


def _password_obligatoria() -> str:
    """La contrasena de la base sale del entorno, sin valor por defecto.

    Antes caia a un valor fijo. Una contrasena por defecto que viaja en un
    repositorio publico deja de ser un valor por defecto: es una contrasena
    conocida en toda instalacion cuyo dueno no la cambio.
    """
    clave = os.getenv("POSTGRES_PASSWORD")
    if not clave:
        raise RuntimeError(
            "Falta POSTGRES_PASSWORD. Copia .env.example a .env y rellenalo.")
    return clave


# Puerto del pooler en modo transaccion de Supabase. Importa distinguirlo:
# ese pooler multiplexa varias sesiones sobre una misma conexion real, asi que
# las sentencias preparadas que asyncpg crea por su cuenta se pisan entre si y
# revientan con "prepared statement _asyncpg_stmt_ already exists". La conexion
# directa (5432) y el pooler de sesion no tienen ese problema.
PUERTO_POOLER_TRANSACCION = 6543

# Puerto si no hay POSTGRES_PORT ni DATABASE_URL: el de Postgres de serie.
#
#   Era el 5433, el que docker-compose.yml publica en el anfitrion para el
#   desarrollo. Como valor por defecto era una trampa: en la maquina de
#   desarrollo el 5433 lo ocupa el Postgres de OTRO proyecto, asi que un script
#   lanzado sin .env hablaba con una base ajena. Produccion no lo usa (manda
#   DATABASE_URL), las pruebas fijan el 5434 (tests/conftest.py) y quien usa el
#   compose de desarrollo pone POSTGRES_PORT=5433 en su .env (.env.example).
PUERTO_POR_DEFECTO = "5432"


# Hosts que son esta maquina o el contenedor de la base del compose (`postgres`
# en docker-compose.prod.yml, `db` en otros compose), por su nombre EXACTO.
HOSTS_LOCALES = frozenset({"localhost", "127.0.0.1", "::1", "postgres", "db"})


def _es_gestionado(host: str) -> bool:
    """Si la conexion a `host` tiene que ir con TLS.

    POR QUE LISTA BLANCA EXACTA Y NO PREFIJOS

      Antes era "todo lo que NO empiece por localhost, 127., db o postgres".
      `db.<ref>.supabase.co` -- el host directo de Supabase -- empieza por
      "db", asi que contaba como local y se conectaba SIN TLS: la contrasena y
      los datos de los clientes viajaban en claro por Internet hasta donde el
      servidor lo rechazara (o no). Lo mismo cualquier `postgres.algo.com`.

      Ahora el TLS es lo de serie y solo se libra lo que esta en HOSTS_LOCALES,
      escrito entero. Un host que falte en la lista falla RUIDOSO ("server does
      not support SSL") y se arregla en un minuto; el fallo contrario era
      callado.

    LA SALIDA EXPLICITA

      POSTGRES_SSLMODE=disable apaga el TLS para cualquier host. Es para un
      Postgres de la red privada con otro nombre (otra maquina de la LAN, otro
      servicio del compose): hay que escribirlo a mano, que es justo lo que lo
      hace seguro. La miran tambien migrations/env.py y tools/crear_esquema.py,
      que usan esta funcion.
    """
    if (os.getenv("POSTGRES_SSLMODE") or "").strip().lower() == "disable":
        return False
    host = (host or "").strip().lower()
    # Sin host (una URL por socket unix) la conexion no sale de la maquina.
    return bool(host) and host not in HOSTS_LOCALES


async def _sin_reinicio(conn) -> None:
    """Ver `opciones["reset"]` en get_pool."""


def _json_extra(valor):
    """`default` de json.dumps para lo que asyncpg devuelve y JSON no conoce."""
    if isinstance(valor, Decimal):
        return float(valor)
    raise TypeError(f"Object of type {type(valor).__name__} is not JSON serializable")


async def _preparar_conexion(conn) -> None:
    """Hace que JSONB llegue a Python como dict o lista, no como texto.

    Por defecto asyncpg entrega JSONB en crudo. Eso convirtio `banderas` en la
    cadena '["postor_unico"]', asi que recorrerla iteraba CARACTERES, y la
    cadena '[]' -- que representa "ninguna bandera" -- resultaba verdadera, de
    modo que el aviso salia tambien en las licitaciones sin ningun indicio.

    El encoder deja pasar lo que ya viene serializado: hay INSERT que mandan el
    JSON hecho cadena con un ::jsonb explicito, y volver a serializarlo lo
    guardaria como un texto entrecomillado dentro del JSON.

    Un Decimal (los montos son NUMERIC desde 0021) sale como numero JSON: un
    dict armado con una fila de la base no puede tumbar el INSERT con "Object
    of type Decimal is not JSON serializable". En un JSONB el monto es para
    leerlo, no para cuadrar caja: el valor exacto vive en su columna.
    """
    def codificar(valor):
        return valor if isinstance(valor, str) else json.dumps(valor, default=_json_extra)

    for tipo in ("jsonb", "json"):
        await conn.set_type_codec(
            tipo, encoder=codificar, decoder=json.loads, schema="pg_catalog")


async def get_pool() -> asyncpg.Pool:
    """Pool de conexiones. Sirve igual para el Postgres local y para Supabase.

    Con DATABASE_URL puesta se usa esa y se ignoran las piezas sueltas: es lo
    que entrega Supabase de una sola pieza, y armarla a mano invita a perder el
    `?sslmode=` o el usuario `postgres.<ref>` que exige su pooler.
    """
    global _pool
    if _pool is not None:
        return _pool
    async with _candado_del_bucle():
        # Se vuelve a mirar dentro del candado: quien esperaba en la puerta
        # encuentra el pool que creo el primero y no hace otro.
        if _pool is not None:
            return _pool
        url = os.getenv("DATABASE_URL") or ""
        host = os.getenv("POSTGRES_HOST", "localhost")
        puerto = int(os.getenv("POSTGRES_PORT", PUERTO_POR_DEFECTO))

        if url:
            from urllib.parse import urlparse
            partes = urlparse(url)
            host = partes.hostname or host
            puerto = partes.port or puerto

        opciones = {
            "min_size": 2,
            "max_size": 10,
            # La sesion trabaja en hora de Lima. Sin esto, NOW() devuelve la
            # zona del contenedor (UTC) mientras Python escribe timestamps
            # naive en hora local: cinco horas de desfase. Un token de Telegram
            # nacia pareciendo caducado, y una licitacion se daba por vencida
            # cinco horas antes de cerrar, justo en el caso "cierra hoy".
            #
            #   OJO: el pooler transaccional de Supabase IGNORA este ajuste y su
            #   sesion corrio siempre en UTC. En el Postgres del VPS, que si lo
            #   aplica, se fija POSTGRES_ZONA_SESION=UTC para no cambiar de zona
            #   al mudarse: lo guardado con NOW() durante meses esta en UTC.
            "server_settings": {"timezone": os.getenv("POSTGRES_ZONA_SESION",
                                                      "America/Lima")},
            "init": _preparar_conexion,
            "command_timeout": SEGUNDOS_POR_CONSULTA,
        }

        if puerto == PUERTO_POOLER_TRANSACCION:
            # Sin esto la app arranca y falla mas tarde, con trafico y de forma
            # intermitente, que es la peor forma de descubrirlo.
            opciones["statement_cache_size"] = 0
            # Al devolver cada conexion asyncpg manda "RESET ALL; CLOSE ALL;
            # UNLISTEN *..." : un viaje mas a la base (~50 ms hasta Supabase)
            # por cada `connection()`. Detras del pooler transaccional no sirve
            # de nada -- el estado de sesion no sobrevive a la transaccion y aqui
            # no se usan LISTEN, cursores ni locks de sesion -- y era casi la
            # mitad de las consultas de la ficha de una propuesta (23 de 54).
            opciones["reset"] = _sin_reinicio
        if _es_gestionado(host):
            opciones["ssl"] = "require"

        if url:
            _pool = await asyncpg.create_pool(url, **opciones)
        else:
            _pool = await asyncpg.create_pool(
                host=host,
                port=puerto,
                database=os.getenv("POSTGRES_DB", "licitapro"),
                user=os.getenv("POSTGRES_USER", "licitapro"),
                password=_password_obligatoria(),
                **opciones,
            )
        log.info("Pool PostgreSQL creado contra %s:%s%s", host, puerto,
                 " (pooler transaccional)" if puerto == PUERTO_POOLER_TRANSACCION else "")
    return _pool


def _candado_del_bucle() -> asyncio.Lock:
    """El candado de creacion del pool, uno por bucle de eventos."""
    global _candado
    bucle = asyncio.get_running_loop()
    if _candado is None or _candado[0] is not bucle:
        _candado = (bucle, asyncio.Lock())
    return _candado[1]


async def cerrar_pool() -> None:
    """Cierra el pool si existe. Para el apagado de la app y de los bots.

    Sin esto las conexiones se cortan de golpe al morir el proceso y Postgres
    las ve como clientes que desaparecen a mitad de algo. Cerrar es idempotente:
    llamarlo dos veces, o sin pool creado, no hace nada.
    """
    global _pool
    pool, _pool = _pool, None
    if pool is None:
        return
    try:
        await asyncio.wait_for(pool.close(), timeout=SEGUNDOS_ESPERA_CONEXION)
    except TimeoutError:
        # Alguna conexion sigue ocupada: se corta en vez de colgar el apagado.
        log.warning("El pool no cerro a tiempo; se terminan las conexiones")
        pool.terminate()
    log.info("Pool PostgreSQL cerrado")


@asynccontextmanager
async def connection():
    pool = await get_pool()
    try:
        conn = await pool.acquire(timeout=SEGUNDOS_ESPERA_CONEXION)
    except TimeoutError:
        # El sintoma de un pool agotado: se deja escrito para que no parezca
        # una caida misteriosa. La causa casi siempre es una funcion que pide
        # otra conexion teniendo ya una (pasale `conn=` en vez de anidar).
        log.error("Sin conexion libre en %.0f s: el pool (%d) esta agotado",
                  SEGUNDOS_ESPERA_CONEXION, pool.get_max_size())
        raise
    try:
        yield conn
    finally:
        await pool.release(conn)


@asynccontextmanager
async def _conexion_o_nueva(conn=None):
    """La conexion que pasa quien llama, o una nueva del pool.

    POR QUE: una funcion que ya tiene una conexion y llama a otra que abre la
    suya ocupa DOS del pool a la vez. Con diez peticiones asi el pool se agota
    y todas esperan a una conexion que solo liberan ellas mismas.
    """
    if conn is not None:
        yield conn
        return
    async with connection() as nueva:
        yield nueva


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
            monto_para_columna(data.get("monto_referencial"), f"{data['id']} referencial"),
            data.get("moneda", "PEN"),
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
async def kb_get(empresa_id: int, categoria: str, clave: str,
                 conn=None) -> str | None:
    # Leer y contar el uso en un solo viaje: eran dos consultas por dato, y la
    # ficha de una propuesta pide una docena de datos. `conn` reutiliza la de
    # quien llama (ver `_conexion_o_nueva`).
    async with _conexion_o_nueva(conn) as c:
        return await c.fetchval(
            """UPDATE knowledge_base SET usado_count = usado_count + 1
                WHERE empresa_id=$1 AND categoria=$2 AND clave=$3
            RETURNING valor""",
            empresa_id, categoria, clave,
        )


async def kb_todos(empresa_id: int, conn=None) -> dict[tuple[str, str], str]:
    """Todo lo que se sabe de la empresa, {(categoria, clave): valor}, en un
    solo viaje. Para quien revisa muchos datos a la vez (el validador pedia
    uno por uno, con su ida y vuelta cada uno). No cuenta usos: mirar no es usar."""
    async with _conexion_o_nueva(conn) as c:
        filas = await c.fetch(
            "SELECT categoria, clave, valor FROM knowledge_base WHERE empresa_id=$1",
            empresa_id)
    return {(f["categoria"], f["clave"]): f["valor"] for f in filas}


async def kb_set(empresa_id: int, categoria: str, clave: str, valor: str,
                 fuente="usuario_respuesta", conn=None):
    async with _conexion_o_nueva(conn) as c:
        await c.execute(
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
    """La propuesta de esa empresa a esa licitacion; la crea si no existe.

    Una empresa tiene a lo sumo una propuesta por licitacion (indice unico de
    la migracion 0020). Un doble clic en "Licitar" devuelve la que ya hay en
    vez de reventar con la violacion del indice.
    """
    async with connection() as conn:
        nueva = await conn.fetchval(
            """INSERT INTO propuestas (licitacion_id, empresa_id, estado)
               VALUES ($1, $2, 'iniciado')
               ON CONFLICT (licitacion_id, empresa_id) DO NOTHING
               RETURNING id""",
            licitacion_id, empresa_id,
        )
        if nueva is not None:
            return nueva
        return await conn.fetchval(
            "SELECT id FROM propuestas WHERE licitacion_id=$1 AND empresa_id=$2",
            licitacion_id, empresa_id)


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
            # Con la misma conexion: abrir otra aqui ocupaba dos del pool.
            await kb_set(row["empresa_id"], row["kb_categoria"], row["kb_clave"],
                         respuesta, conn=conn)
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
async def get_contratos_activos(empresa_id: int | None = None, *,
                                usuario_id: int | None = None):
    """Contratos sin pagar ni cancelar de UNA empresa o de UNA cuenta.

    POR QUE EXIGE FILTRO: sin `empresa_id` devolvia los contratos de TODAS las
    empresas de la plataforma. Bastaba un llamador que olvidara el argumento
    (o le pasara 0 / None de una sesion vacia) para ensenarle a un cliente los
    contratos de los demas. Ahora sin filtro es un error de programacion, no
    una fuga.
    """
    if empresa_id is None and usuario_id is None:
        raise ValueError("get_contratos_activos exige empresa_id o usuario_id")
    async with connection() as conn:
        if empresa_id is not None:
            return await conn.fetch(
                """SELECT c.*, l.objeto, l.entidad FROM contratos c
                JOIN licitaciones l ON c.licitacion_id = l.id
                WHERE c.empresa_id=$1 AND c.estado NOT IN ('pagado','cancelado')
                  AND ($2::int IS NULL OR EXISTS (
                        SELECT 1 FROM empresas e
                         WHERE e.id = c.empresa_id AND e.usuario_id = $2))
                ORDER BY c.fecha_entrega_final""",
                empresa_id, usuario_id,
            )
        return await conn.fetch(
            """SELECT c.*, l.objeto, l.entidad FROM contratos c
            JOIN licitaciones l ON c.licitacion_id = l.id
            JOIN empresas e ON c.empresa_id = e.id
            WHERE e.usuario_id = $1
              AND c.estado NOT IN ('pagado','cancelado')
            ORDER BY c.fecha_entrega_final""",
            usuario_id,
        )


async def get_plazos_proximos(dias: int = 7):
    # La fecha de Lima y no CURRENT_DATE: con la sesion en UTC (el VPS),
    # CURRENT_DATE pasa al dia siguiente a las 19:00 de Lima y el aviso de
    # plazos de la noche corria la ventana un dia hacia adelante.
    async with connection() as conn:
        return await conn.fetch(
            """SELECT p.*, c.numero_contrato, c.empresa_id, l.objeto, l.entidad
            FROM plazos p
            JOIN contratos c ON p.contrato_id = c.id
            JOIN licitaciones l ON c.licitacion_id = l.id
            WHERE p.completado = FALSE 
            AND p.fecha_limite <= (NOW() AT TIME ZONE 'America/Lima')::date
                                  + $1 * INTERVAL '1 day'
            ORDER BY p.fecha_limite""",
            dias,
        )


# ─── Scraping Log ────────────────────────────────────────
async def log_scraping_start(fuente: str) -> int:
    async with connection() as conn:
        return await conn.fetchval(
            "INSERT INTO scraping_log (fuente) VALUES ($1) RETURNING id", fuente
        )


async def log_scraping_end(log_id: int, encontrados: int, nuevos: int, errores: int = 0, error_detalle: str | None = None):
    async with connection() as conn:
        await conn.execute(
            """UPDATE scraping_log SET fin=NOW(), registros_encontrados=$2,
            registros_nuevos=$3, errores=$4, error_detalle=$5, status='done'
            WHERE id=$1""",
            log_id, encontrados, nuevos, errores, error_detalle,
        )


# ─── Documentos que publica la entidad con cada solicitud ──
#
# Los baja la PC puente al cosechar (el VPS no alcanza el portal del GOREMAD) y
# la web los usa para llenar el formato de la entidad. Ver migracion 0017.

async def documentos_conocidos(licitacion_id: str) -> dict[str, bool]:
    """url -> si ya tiene su contenido guardado."""
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT d.url, (c.sha256 IS NOT NULL) AS lleno
                 FROM licitacion_documentos d
                 LEFT JOIN documento_contenidos c ON c.sha256 = d.sha256
                WHERE d.licitacion_id = $1""", licitacion_id)
    return {f["url"]: f["lleno"] for f in filas}


async def guardar_documento(licitacion_id: str, tipo: str, categoria: str | None,
                            nombre: str | None, url: str, contenido: bytes | None,
                            tamano: int | None = None) -> None:
    """Inserta o refresca un documento. Un contenido None no borra el guardado.

    El PDF va a documento_contenidos por su sha256, una sola vez: el GOREMAD
    adjunta la misma DJ a cada solicitud (migracion 0018)."""
    import hashlib
    sha = hashlib.sha256(contenido).hexdigest() if contenido else None
    async with connection() as conn:
        if contenido:
            await conn.execute(
                """INSERT INTO documento_contenidos (sha256, contenido, tamano)
                   VALUES ($1, $2, $3) ON CONFLICT (sha256) DO NOTHING""",
                sha, contenido, len(contenido))
        await conn.execute(
            """INSERT INTO licitacion_documentos
                   (licitacion_id, tipo, categoria, nombre, url, tamano, sha256)
               VALUES ($1,$2,$3,$4,$5,$6,$7)
               ON CONFLICT (licitacion_id, url) DO UPDATE SET
                   tipo = EXCLUDED.tipo, categoria = EXCLUDED.categoria,
                   nombre = EXCLUDED.nombre,
                   tamano = COALESCE(EXCLUDED.tamano, licitacion_documentos.tamano),
                   sha256 = COALESCE(EXCLUDED.sha256, licitacion_documentos.sha256),
                   updated_at = NOW()""",
            licitacion_id, tipo, categoria, nombre, url,
            len(contenido) if contenido else tamano, sha)


async def documentos_de(licitacion_id: str) -> list[dict]:
    """Los documentos de una licitacion, sin el contenido (para listarlos)."""
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT d.id, d.tipo, d.categoria, d.nombre, d.url, d.tamano,
                      (c.sha256 IS NOT NULL) AS guardado
                 FROM licitacion_documentos d
                 LEFT JOIN documento_contenidos c ON c.sha256 = d.sha256
                WHERE d.licitacion_id = $1
                ORDER BY CASE d.tipo WHEN 'sc' THEN 0 WHEN 'dj' THEN 1
                                     WHEN 'eett' THEN 2 ELSE 3 END, d.id""",
            licitacion_id)
    return [dict(f) for f in filas]


async def contenido_documento(licitacion_id: str, tipo: str) -> bytes | None:
    """El PDF guardado de ese tipo (el mas reciente), o None."""
    async with connection() as conn:
        return await conn.fetchval(
            """SELECT c.contenido FROM licitacion_documentos d
                 JOIN documento_contenidos c ON c.sha256 = d.sha256
                WHERE d.licitacion_id = $1 AND d.tipo = $2
                ORDER BY d.updated_at DESC LIMIT 1""", licitacion_id, tipo)


async def purgar_documentos_viejos(dias: int = 30, dias_pesados: int = 3) -> int:
    """Suelta los PDF de solicitudes cerradas sin propuesta. Devuelve cuantas
    filas solto.

    Las EETT y los TDR son escaneos de 1 a 2 MB cada uno: guardados 30 dias
    despues del cierre, con 20 a 30 solicitudes nuevas al dia entre el GOREMAD
    y la Municipalidad de Tambopata, la base pasaba de 500 MB (el 15/10/2026
    entraron 93 MB). Se sueltan a los `dias_pesados`; la SC y la DJ, que pesan
    poco, a los `dias`. Lo que tiene propuesta no se toca nunca: es lo que el
    postor presento.

    La fila se queda (con su enlace); se le quita la huella y se borra el
    contenido que ya no use ninguna otra. `fecha_cierre` es hora de Lima, y se
    compara con la hora de Lima, no con la de la base (UTC)."""
    async with connection() as conn, conn.transaction():
        r = await conn.execute(
            """UPDATE licitacion_documentos d SET sha256 = NULL, updated_at = NOW()
                 FROM licitaciones l
                WHERE l.id = d.licitacion_id AND d.sha256 IS NOT NULL
                  AND l.fecha_cierre < (NOW() AT TIME ZONE 'America/Lima')
                      - make_interval(days => CASE WHEN d.tipo IN ('eett', 'otro')
                                                   THEN $2::int ELSE $1::int END)
                  AND NOT EXISTS (SELECT 1 FROM propuestas p WHERE p.licitacion_id = l.id)""",
            dias, dias_pesados)
        await conn.execute(
            """DELETE FROM documento_contenidos c
                WHERE NOT EXISTS (SELECT 1 FROM licitacion_documentos d
                                   WHERE d.sha256 = c.sha256)""")
    return int(r.split()[-1]) if r else 0


# Lo que cabe en NUMERIC(14,2), el tipo de las columnas de dinero (0021).
_TOPE_MONTO = 10 ** 12


def monto_para_columna(valor, que: str = "monto"):
    """El monto listo para una columna NUMERIC(14,2), o None si no es guardable.

    POR QUE

      Con REAL cualquier cifra entraba, aunque fuera basura de un scraper. Con
      NUMERIC(14,2) una de un billon o mas es un "numeric field overflow" y el
      INSERT entero falla: una sola celda mal leida tiraba la licitacion
      completa, con su objeto, su entidad y su cierre, que si estaban bien.
      Se guarda la licitacion sin ese monto, que es lo que el resto del
      codigo ya entiende como "no publicado", y queda en el log.

      NaN e infinito (un float de un JSON raro) tambien van a None.
    """
    from shared.config import a_soles

    if valor is None:
        return None
    d = a_soles(valor)
    if d is None or abs(d) >= _TOPE_MONTO:
        log.warning("%s no guardable en NUMERIC(14,2): %r -> NULL", que, valor)
        return None
    return d


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
        # Los datos de adjudicacion llegan DESPUES de la convocatoria: el mismo
        # ocid se republica con postores y ganador cuando se resuelve. Por eso
        # van con COALESCE en el UPDATE -- una republicacion posterior sin esos
        # campos no puede borrar lo que ya se sabia.
        await conn.execute(
            """INSERT INTO licitaciones
            (id, fuente, tipo, nomenclatura, entidad, entidad_tipo, entidad_ruc,
             objeto, monto_referencial, moneda, fecha_publicacion, fecha_cierre,
             estado, departamento, url, bases_urls,
             numero_postores, proveedor_ganador, proveedor_ruc,
             monto_adjudicado, plazo_consultas_dias, banderas, banderas_nivel,
             categoria, descripcion)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,
                    $17,$18,$19,$20,$21,$22::jsonb,$23,$24,$25)
            ON CONFLICT (id) DO UPDATE SET
                estado            = EXCLUDED.estado,
                -- El tipo se refresca a proposito: al corregir el mapeo de
                -- procedimientos, las filas ya guardadas conservaban la
                -- clasificacion vieja y la correccion no llegaba nunca.
                tipo              = COALESCE(EXCLUDED.tipo, licitaciones.tipo),
                fecha_cierre      = EXCLUDED.fecha_cierre,
                monto_referencial = COALESCE(EXCLUDED.monto_referencial,
                                             licitaciones.monto_referencial),
                bases_urls        = EXCLUDED.bases_urls,
                numero_postores   = COALESCE(EXCLUDED.numero_postores,
                                             licitaciones.numero_postores),
                proveedor_ganador = COALESCE(EXCLUDED.proveedor_ganador,
                                             licitaciones.proveedor_ganador),
                proveedor_ruc     = COALESCE(EXCLUDED.proveedor_ruc,
                                             licitaciones.proveedor_ruc),
                monto_adjudicado  = COALESCE(EXCLUDED.monto_adjudicado,
                                             licitaciones.monto_adjudicado),
                plazo_consultas_dias = COALESCE(EXCLUDED.plazo_consultas_dias,
                                                licitaciones.plazo_consultas_dias),
                banderas          = EXCLUDED.banderas,
                banderas_nivel    = EXCLUDED.banderas_nivel,
                categoria         = COALESCE(EXCLUDED.categoria, licitaciones.categoria),
                -- Los items de la ficha (gore_portals). Con COALESCE: una
                -- pasada en que la ficha no cargo no borra lo que ya se leyo.
                descripcion       = COALESCE(EXCLUDED.descripcion,
                                             licitaciones.descripcion),
                updated_at        = NOW()""",
            data["id"], data["fuente"], data.get("tipo"),
            data.get("nomenclatura"), data["entidad"], data.get("entidad_tipo"),
            data.get("entidad_ruc"), data["objeto"],
            monto_para_columna(data.get("monto_referencial"), f"{data['id']} referencial"),
            data.get("moneda", "PEN"),
            data.get("fecha_publicacion"), data.get("fecha_cierre"),
            data.get("estado", "convocado"), data.get("departamento"),
            data.get("url"), data.get("bases_urls", []),
            data.get("numero_postores"), data.get("proveedor_ganador"),
            data.get("proveedor_ruc"),
            monto_para_columna(data.get("monto_adjudicado"), f"{data['id']} adjudicado"),
            data.get("plazo_consultas_dias"),
            json.dumps(data.get("banderas") or []), data.get("banderas_nivel", 0),
            data.get("categoria"), data.get("descripcion"),
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
        # SE COMPARA CONTRA LA HORA DE LIMA, NO CONTRA NOW()
        #
        #   `fecha_cierre` se guarda SIEMPRE como hora local de Lima sin zona:
        #   `ocds_oece._fecha` convierte el offset ISO a naive de Lima, y los
        #   portales de cotizacion publican en hora local.
        #
        #   `NOW()` de esta base responde en UTC -- comprobado, pese a que el
        #   pool pide `timezone=America/Lima`: el pooler transaccional de
        #   Supabase no aplica ese ajuste. Asi que se comparaba una hora de
        #   Lima con una hora UTC, cinco horas por delante.
        #
        #   El efecto no era teorico: 925 de las 926 licitaciones de OECE
        #   cierran a las 23:00 de Lima, y con `NOW()` desaparecian del panel
        #   a las 18:00 de ese mismo dia. Cinco horas de la ultima tarde, que
        #   es justo cuando alguien que la ve corre a presentarse.
        #
        #   `NOW() AT TIME ZONE 'America/Lima'` devuelve la hora de pared de
        #   Lima sin zona, que es exactamente lo que hay en la columna. Es el
        #   mismo giro que ya usa `shared/vigilancia.py`.
        # LAS COTIZACIONES DE gob.pe NO PUBLICAN CIERRE EN LOS METADATOS
        #
        #   El plazo va dentro del PDF y no se inventa. Sin esta segunda rama
        #   serian invisibles para siempre -- la misma trampa de las 17 filas
        #   de datos_abiertos. Se muestran durante 7 dias desde su
        #   publicacion: una cotizacion real dura dias, no semanas, asi que la
        #   ventana refleja la realidad sin fabricar una fecha.
        #
        #   Acotado a fecha_publicacion reciente a proposito: las filas viejas
        #   de OECE sin cierre (164 hay) no reviven con esto.
        condiciones.append(
            "(fecha_cierre > (NOW() AT TIME ZONE 'America/Lima') "
            "OR (fecha_cierre IS NULL AND fecha_publicacion > "
            "(NOW() AT TIME ZONE 'America/Lima') - INTERVAL '7 days'))")
    if config["regiones"]:
        params.append(list(config["regiones"]))
        condiciones.append(f"(departamento IS NULL OR departamento = ANY(${len(params)}))")
    # Decimal tal cual (NUMERIC desde 0021): comparar contra un float
    # convertido podia dejar fuera la licitacion de exactamente el minimo.
    if config["monto_min"] is not None:
        params.append(config["monto_min"])
        condiciones.append(
            f"(monto_referencial IS NULL OR monto_referencial >= ${len(params)})")
    if config["monto_max"] is not None:
        params.append(config["monto_max"])
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
        # `descripcion` lleva los items cuando la fuente los publica aparte
        # del concepto (cotizaciones de GOREMAD): "arroz" esta en los items,
        # no en "ADQUISICION DE ALIMENTOS NO PERECIBLE".
        texto = (f"{f['objeto']} {f['entidad']} {f['nomenclatura'] or ''} "
                 f"{f['descripcion'] or ''}")
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
            #
            # EL AVISO POR CORREO NACE ENCENDIDO, Y ES LO QUE HACE QUE EL
            # PRODUCTO CUMPLA LO QUE PROMETE
            #
            #   `email_notificaciones` guarda la direccion de destino, y nacia
            #   en NULL. `repartir()` recorre los tres canales y los tres
            #   estaban apagados: Telegram sin vincular, WhatsApp sin
            #   configurar y el correo vacio. Resultado comprobado en
            #   produccion: cero avisos enviados desde que existe el sistema.
            #
            #   Es decir, quien se registraba pasaba sus 14 dias de prueba --
            #   exactamente la ventana que decide si paga -- sin recibir una
            #   sola alerta, salvo que adivinara que tenia que entrar a
            #   Configuracion y teclear su propio correo. La unica cosa que el
            #   cliente viene a comprar es que le avisemos.
            #
            #   Se enciende con la direccion con la que se registro, que es la
            #   que acaba de dar para esto. Sigue siendo suya: en Configuracion
            #   puede cambiarla o vaciarla, y vaciarla apaga el canal.
            await conn.execute(
                """INSERT INTO user_config (user_id, usuario_id,
                                            email_notificaciones)
                   VALUES ($1, $2, $3) ON CONFLICT DO NOTHING""",
                -fila["id"], fila["id"], fila["email"],
            )
    if fila:
        # La prueba nace con la cuenta: sin suscripcion el panel no sabria que
        # plan aplicar ni cuantas empresas permitir.
        from shared.suscripciones import crear_suscripcion_prueba
        await crear_suscripcion_prueba(fila["id"])
    return fila


# ─── Verificacion del correo (migracion 0022) ────────────

async def crear_token_verificacion(usuario_id: int, email: str, token_hash: str,
                                   expira) -> None:
    """Guarda el hash del enlace, nunca el enlace. Ver nuevo_token_recuperacion.

    Va con el `email` al que se mando: si la cuenta cambia de correo antes de
    abrirlo, el enlace viejo deja de verificar nada (ver verificar_correo).
    """
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO tokens_verificacion (usuario_id, email, token_hash, expira)
               VALUES ($1, LOWER($2), $3, $4)""",
            usuario_id, email.strip(), token_hash, expira)


async def verificaciones_recientes(usuario_id: int, horas: int = 1) -> int:
    """Cuantos enlaces de verificacion se mandaron a esta cuenta ultimamente.

    `created_at` nace en hora de Lima (default de la 0022), asi que se compara
    con la hora de Lima y no con NOW() a secas.
    """
    async with connection() as conn:
        return await conn.fetchval(
            f"""SELECT COUNT(*) FROM tokens_verificacion
                 WHERE usuario_id = $1
                   AND created_at > {_AHORA_LIMA} - ($2 || ' hours')::interval""",
            usuario_id, str(horas))


async def verificar_correo(token_hash: str) -> int | None:
    """Consume un enlace de verificacion y marca el correo. Id del usuario o None.

    Todo en una transaccion y con la condicion DENTRO del UPDATE, como en
    consumir_token_y_cambiar_password: entre un SELECT y el UPDATE cabria un
    segundo clic con el mismo enlace.

    El enlace solo vale si el correo de la cuenta sigue siendo el que lo
    recibio. Si no, un enlace mandado al correo viejo verificaria el nuevo, que
    nadie ha demostrado controlar.
    """
    async with connection() as conn, conn.transaction():
        uid = await conn.fetchval(
            f"""UPDATE tokens_verificacion t SET usado_en = {_AHORA_LIMA}
                  FROM usuarios u
                 WHERE t.token_hash = $1
                   AND t.usado_en IS NULL
                   AND t.expira > {_AHORA_LIMA}
                   AND u.id = t.usuario_id
                   AND u.activo = TRUE
                   AND u.email = t.email
             RETURNING t.usuario_id""",
            token_hash)
        if not uid:
            return None
        await conn.execute(
            f"""UPDATE usuarios
                   SET email_verificado_en = COALESCE(email_verificado_en, {_AHORA_LIMA})
                 WHERE id = $1""", uid)
        # Los demas enlaces pendientes ya no hacen falta: que no queden vivos.
        await conn.execute(
            f"""UPDATE tokens_verificacion SET usado_en = {_AHORA_LIMA}
                 WHERE usuario_id = $1 AND usado_en IS NULL""", uid)
    return uid


async def get_usuario_por_email(email: str, incluso_inactivo: bool = False):
    """Por defecto solo cuentas activas: una desactivada no existe para el
    login ni para la recuperacion. `incluso_inactivo` es para decirle a quien
    acierta la contrasena de una cuenta desactivada que lo esta, en vez de
    "correo o contrasena incorrectos", que le haria pedir una recuperacion
    que tampoco va a funcionar."""
    async with connection() as conn:
        return await conn.fetchrow(
            "SELECT * FROM usuarios WHERE email = LOWER($1) AND (activo = TRUE OR $2)",
            email.strip(), incluso_inactivo,
        )


# ─── Vinculacion de Telegram ─────────────────────────────

async def set_token_telegram(usuario_id: int, token: str, expira) -> None:
    async with connection() as conn:
        await conn.execute(
            """UPDATE usuarios SET telegram_token=$2, telegram_token_expira=$3
               WHERE id=$1""",
            usuario_id, token, expira,
        )


# `telegram_token_expira` y `tokens_recuperacion.expira` se escriben desde
# Python en hora de pared de Lima (ver shared/seguridad.py). Se comparan contra
# la hora de Lima y no contra NOW() por lo mismo que fecha_cierre (ver
# licitaciones_para_usuario): con la sesion en UTC, que es como corre el VPS,
# NOW() iba cinco horas por delante y el token nacia vencido. Asi la condicion
# vale con la sesion en cualquier zona.
_AHORA_LIMA = "(NOW() AT TIME ZONE 'America/Lima')"


async def token_telegram_vigente(usuario_id: int) -> str | None:
    """El codigo pendiente de canjear, si aun vale. Misma condicion que aplica
    vincular_telegram, para no mostrar un codigo que el bot va a rechazar."""
    async with connection() as conn:
        return await conn.fetchval(
            f"""SELECT telegram_token FROM usuarios
                WHERE id = $1 AND telegram_token IS NOT NULL
                  AND telegram_token_expira > {_AHORA_LIMA}""",
            usuario_id,
        )


async def vincular_telegram(token: str, chat_id: int):
    """Canjea el token por el vinculo. Devuelve el usuario o None.

    El chat_id lo aporta Telegram al procesar /start, nunca el usuario. El token
    se consume en el mismo UPDATE que lo valida, asi que no puede reutilizarse
    aunque llegue dos veces.
    """
    async with connection() as conn:
        return await conn.fetchrow(
            f"""UPDATE usuarios
                  SET telegram_chat_id = $2,
                      telegram_token = NULL,
                      telegram_token_expira = NULL
                WHERE telegram_token = $1
                  AND telegram_token_expira > {_AHORA_LIMA}
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


# ─── Recuperacion de contrasena ──────────────────────────

async def crear_token_recuperacion(usuario_id: int, token_hash: str,
                                   expira, ip: str | None = None) -> None:
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO tokens_recuperacion (usuario_id, token_hash, expira, ip)
               VALUES ($1, $2, $3, $4)""",
            usuario_id, token_hash, expira, ip)


async def peticiones_recientes(usuario_id: int, horas: int = 1) -> int:
    """Cuantos enlaces se pidieron para esta cuenta en las ultimas horas.

    Es el limite anti-bombardeo: sin el, el formulario de recuperacion sirve
    para llenarle la bandeja a cualquiera cuyo correo se conozca.
    """
    async with connection() as conn:
        return await conn.fetchval(
            """SELECT COUNT(*) FROM tokens_recuperacion
                WHERE usuario_id = $1
                  AND created_at > NOW() - ($2 || ' hours')::interval""",
            usuario_id, str(horas))


async def usuario_por_token_recuperacion(token_hash: str):
    """Usuario dueno de un token vigente y sin usar, o None."""
    async with connection() as conn:
        return await conn.fetchrow(
            f"""SELECT u.*, t.id AS token_id
                 FROM tokens_recuperacion t
                 JOIN usuarios u ON u.id = t.usuario_id
                WHERE t.token_hash = $1
                  AND t.usado_en IS NULL
                  AND t.expira > {_AHORA_LIMA}
                  AND u.activo = TRUE""",
            token_hash)


async def consumir_token_y_cambiar_password(token_hash: str,
                                            password_hash: str) -> bool:
    """Marca el token como usado y cambia la contrasena, todo o nada.

    Devuelve False si el token ya no vale. La condicion usado_en IS NULL va
    dentro del UPDATE, no en un SELECT previo: entre comprobar y actualizar
    cabe una segunda peticion con el mismo enlace.

    Al terminar se invalidan los demas tokens del usuario: si pidio el enlace
    tres veces, los otros dos dejan de servir en cuanto uno se usa.
    """
    async with connection() as conn, conn.transaction():
        fila = await conn.fetchrow(
            f"""UPDATE tokens_recuperacion SET usado_en = NOW()
                    WHERE token_hash = $1 AND usado_en IS NULL
                      AND expira > {_AHORA_LIMA}
                 RETURNING usuario_id""",
            token_hash)
        if not fila:
            return False
        await conn.execute(
            "UPDATE usuarios SET password_hash = $2 WHERE id = $1",
            fila["usuario_id"], password_hash)
        await conn.execute(
            """UPDATE tokens_recuperacion SET usado_en = NOW()
                    WHERE usuario_id = $1 AND usado_en IS NULL""",
            fila["usuario_id"])
    return True


# ─── WhatsApp ────────────────────────────────────────────
# El numero solo autoriza a escribir cuando el estado es 'activo'. Guardarlo y
# poder usarlo son dos cosas distintas a proposito: Meta exige consentimiento
# demostrable y bloquea el numero de la empresa si se escribe sin el.

async def set_whatsapp_pendiente(usuario_id: int, numero_e164: str) -> None:
    """Guarda el numero a la espera de que su dueno confirme desde WhatsApp."""
    async with connection() as conn:
        await conn.execute(
            """UPDATE usuarios
                  SET whatsapp_numero = $2,
                      whatsapp_estado = 'pendiente',
                      whatsapp_opt_in_en = NULL,
                      whatsapp_opt_out_en = NULL
                WHERE id = $1""",
            usuario_id, numero_e164)


async def activar_whatsapp(numero_e164: str) -> int | None:
    """Marca el numero como confirmado. Devuelve el usuario, o None si no existe.

    Se busca POR NUMERO porque quien confirma lo hace desde WhatsApp, donde no
    hay sesion de la web: lo unico que trae el webhook es el telefono. Por eso
    solo se activa si ese numero estaba en 'pendiente' para alguien; asi un
    mensaje suelto de un desconocido no da de alta nada.
    """
    async with connection() as conn:
        return await conn.fetchval(
            """UPDATE usuarios
                  SET whatsapp_estado = 'activo',
                      whatsapp_opt_in_en = NOW(),
                      whatsapp_opt_out_en = NULL
                WHERE whatsapp_numero = $1 AND whatsapp_estado IN ('pendiente','baja')
             RETURNING id""",
            numero_e164)


async def baja_whatsapp(numero_e164: str) -> int | None:
    """Registra la baja. Se conserva el numero para no reactivarlo por error.

    Borrarlo dejaria el sistema sin memoria de que esa persona pidio no recibir
    avisos, y bastaria que alguien lo volviera a escribir para empezar de nuevo.
    """
    async with connection() as conn:
        return await conn.fetchval(
            """UPDATE usuarios
                  SET whatsapp_estado = 'baja', whatsapp_opt_out_en = NOW()
                WHERE whatsapp_numero = $1
             RETURNING id""",
            numero_e164)


async def quitar_whatsapp(usuario_id: int) -> None:
    """El usuario retira su numero desde la web."""
    async with connection() as conn:
        await conn.execute(
            """UPDATE usuarios
                  SET whatsapp_numero = NULL,
                      whatsapp_estado = 'sin_configurar',
                      whatsapp_opt_in_en = NULL,
                      whatsapp_opt_out_en = NOW()
                WHERE id = $1""",
            usuario_id)


# ─── Borrado de cuenta (Ley 29733) ───────────────────────

async def borrar_cuenta(usuario_id: int) -> dict:
    """Borra la cuenta y todo lo que cuelga de ella. Irreversible.

    El derecho de supresion de la Ley 29733 exige borrar de verdad, no marcar
    como inactivo: dejar la fila con una marca sigue siendo tratar el dato. Para
    las bajas corrientes ya esta `empresas.activa = FALSE`, que es otra cosa.

    Los ARCHIVOS se borran ANTES que la fila, y ese orden importa. Al reves, si
    algo falla despues del DELETE ya no quedaria de donde sacar las rutas y la
    firma escaneada del representante legal se quedaria en el disco para
    siempre, que es justo lo que la ley prohibe. Borrando primero, un fallo deja
    la cuenta intacta y la operacion se puede repetir.

    Las tablas hijas caen por las cascadas declaradas en la migracion 0006.
    """
    # Los archivos (imagenes, membrete, ficha RUC, constancia del RNP,
    # formatos, paquetes y expedientes) los borra una sola funcion, la misma
    # que usa el borrado desde el panel del dueno: antes cada borrado tenia su
    # lista y las dos se quedaban cortas.
    from shared.admin_cuentas import borrar_archivos_de_cuenta

    resumen = {"empresas": 0, "archivos": 0, "borrada": False}

    async with connection() as conn:
        empresas = await conn.fetch(
            "SELECT id FROM empresas WHERE usuario_id=$1", usuario_id)
    resumen["empresas"] = len(empresas)
    resumen["archivos"] = await borrar_archivos_de_cuenta(usuario_id)

    async with connection() as conn:
        borrada = await conn.fetchval(
            "DELETE FROM usuarios WHERE id=$1 RETURNING id", usuario_id)
    resumen["borrada"] = bool(borrada)
    log.info("Cuenta %s borrada: %s", usuario_id, resumen)
    return resumen


# ─── Freno a los intentos de acceso ──────────────────────
# Se cuentan los fallos por correo Y por IP. Son dos ataques distintos: probar
# mil contrasenas contra una cuenta, y probar una contrasena comun contra mil
# cuentas. El segundo esquiva cualquier limite que solo mire la cuenta.

async def anotar_intento_fallido(email: str, ip: str | None) -> None:
    """Deja constancia de un intento con contrasena incorrecta."""
    filas = [(email.strip().lower()[:200], "email")]
    if ip:
        filas.append((ip[:64], "ip"))
    async with connection() as conn:
        await conn.executemany(
            "INSERT INTO intentos_acceso (identificador, tipo) VALUES ($1, $2)",
            filas)


async def intentos_recientes(email: str, ip: str | None,
                             minutos: int) -> tuple[int, int]:
    """(fallos de ese correo, fallos de esa IP) dentro de la ventana."""
    async with connection() as conn:
        por_email = await conn.fetchval(
            """SELECT COUNT(*) FROM intentos_acceso
                WHERE tipo = 'email' AND identificador = $1
                  AND ocurrido_en > NOW() - ($2 || ' minutes')::interval""",
            email.strip().lower()[:200], str(minutos))
        por_ip = 0
        if ip:
            por_ip = await conn.fetchval(
                """SELECT COUNT(*) FROM intentos_acceso
                    WHERE tipo = 'ip' AND identificador = $1
                      AND ocurrido_en > NOW() - ($2 || ' minutes')::interval""",
                ip[:64], str(minutos))
    return por_email or 0, por_ip or 0


async def limpiar_intentos(email: str, ip: str | None) -> None:
    """Borra el historial tras un acceso correcto.

    Quien entra bien demuestra ser el dueno, asi que sus fallos previos -- un
    dedo torcido, una contrasena vieja -- no deben acercarle al bloqueo la
    proxima vez. Los de la IP se limpian tambien: si de ahi acaba de entrar
    alguien legitimo, no es una fuente de ataque.
    """
    async with connection() as conn:
        await conn.execute(
            """DELETE FROM intentos_acceso
                WHERE (tipo = 'email' AND identificador = $1)
                   OR (tipo = 'ip' AND identificador = $2)""",
            email.strip().lower()[:200], (ip or "")[:64])


async def purgar_intentos_viejos(horas: int = 24) -> int:
    """Limpieza periodica. Sin esto la tabla crece sin fin."""
    async with connection() as conn:
        borrados = await conn.fetch(
            """DELETE FROM intentos_acceso
                WHERE ocurrido_en < NOW() - ($1 || ' hours')::interval
             RETURNING 1""",
            str(horas))
    return len(borrados)
