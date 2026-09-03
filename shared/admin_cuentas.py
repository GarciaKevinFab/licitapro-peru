"""Lo que el dueno del producto hace con las cuentas de sus clientes.

CADA CUENTA ES UN INQUILINO

  En LicitaPro el aislamiento se hace por `usuarios.id`: cada cuenta tiene sus
  empresas, y de las empresas cuelgan propuestas, contratos, experiencia,
  equipo y vencimientos. No hay una tabla "clientes" aparte: el cliente ES la
  fila de usuarios, y este modulo trata esa fila como la unidad de todo --
  listar, dar de alta, editar, entrar a mirar y borrar.

POR QUE VA EN shared/ Y NO DENTRO DE web/admin.py

  Las rutas solo deberian decidir que se muestra y a donde se vuelve. Todo lo
  que toca la base vive aqui para que las pruebas lo ejerciten sin levantar la
  aplicacion, y para que el dia que haga falta un comando de consola -- "borra
  esta cuenta desde el servidor" -- no haya que copiar SQL de una ruta HTTP.

NADA DE AQUI COMPRUEBA QUIEN LLAMA

  La puerta es `_exige_dueno` en web/admin.py. Estas funciones asumen que ya se
  paso por ella, igual que `borrar_cuenta` en shared/db.py asume que quien la
  llama tiene derecho. Ponerla dos veces no protege mas y obliga a arrastrar
  la sesion hasta la capa de datos.
"""
import logging
import re
import secrets
from datetime import datetime

import asyncpg

from shared.db import connection
from shared.suscripciones import estado_efectivo_de

log = logging.getLogger("shared.admin_cuentas")

ESTADOS_SUSCRIPCION = ("prueba", "activa", "vencida", "suspendida", "cancelada")

# Los filtros de la lista: los cinco estados de la suscripcion mas dos que no
# son de la suscripcion sino de la cuenta, y que es donde el dueno mira
# primero cuando alguien escribe diciendo que no puede entrar.
FILTROS_ESTADO = ESTADOS_SUSCRIPCION + ("sin_suscripcion", "desactivada")


# ─── Utilidades sin base ─────────────────────────────────

def correo_valido(correo: str) -> bool:
    """La misma regla laxa que el registro: algo@algo.algo.

    No se intenta validar el RFC entero: lo que importa es que el correo
    llegue, y eso solo lo dice el envio. Aqui se corta el error de tecleo.
    """
    c = (correo or "").strip()
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", c))


def password_temporal(longitud: int = 14) -> str:
    """Contrasena de un solo uso para una cuenta que da de alta el dueno.

    Sale de `secrets` y sin caracteres que se confunden al dictarla por
    telefono o al copiarla de un WhatsApp: ni 0/O ni 1/l/I. Pasa siempre
    `password_debil` -- tiene letras y numeros y mas de diez caracteres --,
    que es la misma regla que se le exige al cliente cuando se registra solo.
    """
    alfabeto = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    while True:
        p = "".join(secrets.choice(alfabeto) for _ in range(longitud))
        if any(ch.isdigit() for ch in p) and any(ch.isalpha() for ch in p):
            return p


def resumir_cuentas(filas, ahora: datetime | None = None) -> dict:
    """Las cifras de arriba de la lista, a partir de las filas ya leidas.

    Se calcula en Python y no en SQL porque "en prueba" y "pagando" dependen
    del estado EFECTIVO -- una prueba con la fecha pasada ya no es prueba --,
    y esa regla vive en `estado_efectivo_de`. Repetirla en una consulta seria
    tener dos definiciones que un dia discrepan.
    """
    ahora = ahora or datetime.now()
    r = {"total": 0, "activas": 0, "prueba": 0, "pagando": 0}
    for f in filas:
        r["total"] += 1
        if f["activo"]:
            r["activas"] += 1
        if not f["plan_codigo"]:
            continue
        estado = estado_efectivo_de(f["estado"], f["vence"], ahora)
        if estado == "prueba":
            r["prueba"] += 1
        elif estado == "activa":
            r["pagando"] += 1
    return r


def filtrar_cuentas(filas, q: str = "", plan: str = "", estado: str = "",
                    ahora: datetime | None = None) -> list[dict]:
    """Aplica el buscador y los dos filtros y anade `estado_efectivo` a cada fila.

    El texto se busca en correo y nombre; el plan es igualdad exacta; el
    estado compara contra el efectivo, con dos valores extra que no estan en
    la suscripcion: `sin_suscripcion` y `desactivada` (la cuenta, no el plan).
    """
    ahora = ahora or datetime.now()
    q = (q or "").strip().lower()
    salida = []
    for f in filas:
        d = dict(f)
        d["estado_efectivo"] = (estado_efectivo_de(d["estado"], d["vence"], ahora)
                                if d["plan_codigo"] else "sin_suscripcion")
        if q and q not in (d["email"] or "").lower() and q not in (d["nombre"] or "").lower():
            continue
        if plan and d["plan_codigo"] != plan:
            continue
        if estado == "desactivada":
            if d["activo"]:
                continue
        elif estado and d["estado_efectivo"] != estado:
            continue
        salida.append(d)
    return salida


# ─── Lectura ─────────────────────────────────────────────

async def hay_columna(conn, tabla: str, columna: str) -> bool:
    """Si una columna existe. Para que el panel funcione antes y despues de
    aplicar una migracion que se pasa a mano."""
    return bool(await conn.fetchval(
        """SELECT EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_schema = 'public'
                             AND table_name = $1 AND column_name = $2)""",
        tabla, columna))


async def listar_cuentas() -> tuple[list, bool]:
    """Todas las cuentas con su plan y sus recuentos. (filas, hay_ultimo_acceso).

    Se traen TODAS y se filtra en Python (`filtrar_cuentas`): el estado por el
    que se filtra es el efectivo, que depende de la fecha, y las cifras de
    arriba se calculan sobre el total sin filtrar. Con el volumen de un SaaS
    de nicho esto son decenas de filas, no miles.
    """
    async with connection() as conn:
        con_acceso = await hay_columna(conn, "usuarios", "ultimo_acceso")
        col_acceso = "u.ultimo_acceso" if con_acceso else "NULL::timestamp AS ultimo_acceso"
        filas = await conn.fetch(
            f"""SELECT u.id, u.email, u.nombre, u.activo, u.created_at, {col_acceso},
                       s.plan_codigo, s.estado, s.periodo, s.vence, s.inicia,
                       p.nombre AS plan_nombre,
                       (SELECT count(*) FROM empresas e WHERE e.usuario_id = u.id) AS empresas,
                       (SELECT count(*) FROM propuestas pr
                          JOIN empresas e ON e.id = pr.empresa_id
                         WHERE e.usuario_id = u.id) AS propuestas,
                       (SELECT max(ps.confirmado_en) FROM pagos_suscripcion ps
                          WHERE ps.suscripcion_id = s.id AND ps.estado = 'pagado') AS ultimo_pago,
                       (SELECT ps.metodo FROM pagos_suscripcion ps
                          WHERE ps.suscripcion_id = s.id AND ps.estado = 'pagado'
                          ORDER BY ps.confirmado_en DESC LIMIT 1) AS ultimo_metodo
                  FROM usuarios u
                  LEFT JOIN suscripciones s ON s.usuario_id = u.id
                  LEFT JOIN planes p ON p.codigo = s.plan_codigo
                 ORDER BY u.created_at DESC""")
    return filas, con_acceso


async def ingresos_del_mes() -> float:
    """Suma de los pagos confirmados este mes, de la pasarela y manuales."""
    async with connection() as conn:
        total = await conn.fetchval(
            """SELECT COALESCE(SUM(monto), 0) FROM pagos_suscripcion
                WHERE estado = 'pagado'
                  AND confirmado_en >= date_trunc('month', CURRENT_DATE)""")
    return float(total or 0)


async def planes_activos() -> list:
    async with connection() as conn:
        return await conn.fetch(
            "SELECT codigo, nombre, precio_mensual, precio_anual FROM planes "
            "WHERE activo ORDER BY orden")


# ─── Una cuenta ──────────────────────────────────────────

# Lo que el panel del dueno lee de `usuarios`. El hash de la contrasena no
# sale nunca de la base hacia una plantilla, ni siquiera para no mostrarlo.
COLUMNAS_CUENTA = ("id, email, nombre, activo, created_at, plan, telegram_chat_id, "
                   "whatsapp_numero, whatsapp_estado, whatsapp_opt_in_en")


async def cuenta(usuario_id: int):
    """La fila de la cuenta sin secretos, o None. Incluye las desactivadas:
    el dueno tiene que poder ver una cuenta para reactivarla."""
    async with connection() as conn:
        con_acceso = await hay_columna(conn, "usuarios", "ultimo_acceso")
        extra = ", ultimo_acceso" if con_acceso else ", NULL::timestamp AS ultimo_acceso"
        return await conn.fetchrow(
            f"SELECT {COLUMNAS_CUENTA}{extra} FROM usuarios WHERE id = $1", usuario_id)


async def crear_cuenta(email: str, nombre: str | None, password_hash: str,
                       plan: str, estado: str, periodo: str, vence, por: str):
    """Da de alta una cuenta desde el panel del dueno. (fila, error).

    Pasa por `crear_usuario` y no por un INSERT propio: esa funcion crea
    tambien la fila de configuracion y la prueba, y saltarsela dejaria la
    cuenta a medias. Despues se pisa la prueba con el plan que el dueno
    eligio, por `activar_manual`, que es la misma pieza que usa el formulario
    de "poner a mano" y deja el mismo rastro.
    """
    from shared.db import crear_usuario
    from shared.suscripciones import activar_manual

    if not correo_valido(email):
        return None, "Ese correo no parece válido."
    fila = await crear_usuario(email, password_hash, (nombre or "").strip() or None)
    if not fila:
        return None, "Ya existe una cuenta con ese correo."
    ok = await activar_manual(fila["id"], plan, estado, periodo, vence, None, None, por=por)
    if not ok:
        # La cuenta ya existe con su prueba de 14 dias; se avisa en vez de
        # borrarla, que es lo que el dueno esperaria ver en la lista.
        return fila, "La cuenta se creó, pero el plan no era válido: quedó en prueba."
    log.info("Cuenta %s (%s) creada por %s con plan %s/%s", fila["id"], fila["email"], por, plan, estado)
    return fila, None


async def editar_cuenta(usuario_id: int, nombre: str | None, email: str,
                        activo: bool) -> str | None:
    """Nombre, correo y si puede entrar. Devuelve el error, o None si fue bien.

    La unicidad del correo la impone la restriccion de la tabla y se traduce
    aqui: comprobar antes con un SELECT deja un hueco entre la comprobacion y
    el UPDATE por el que caben dos ediciones a la vez.
    """
    if not correo_valido(email):
        return "Ese correo no parece válido."
    async with connection() as conn:
        try:
            hecho = await conn.fetchval(
                """UPDATE usuarios SET nombre = $2, email = LOWER($3), activo = $4
                    WHERE id = $1 RETURNING id""",
                usuario_id, (nombre or "").strip() or None, email.strip(), bool(activo))
        except asyncpg.UniqueViolationError:
            return "Ya existe otra cuenta con ese correo."
    return None if hecho else "Esa cuenta no existe."


async def cambiar_password(usuario_id: int, password_hash: str) -> bool:
    """Contrasena nueva puesta por el dueno.

    Los enlaces de recuperacion pendientes se invalidan: si alguien pidio uno
    antes de que el dueno interviniera, no debe seguir sirviendo para pisar
    la contrasena que se acaba de poner.
    """
    async with connection() as conn:
        async with conn.transaction():
            hecho = await conn.fetchval(
                "UPDATE usuarios SET password_hash = $2 WHERE id = $1 RETURNING id",
                usuario_id, password_hash)
            if hecho:
                await conn.execute(
                    "UPDATE tokens_recuperacion SET usado_en = NOW() "
                    "WHERE usuario_id = $1 AND usado_en IS NULL", usuario_id)
    return bool(hecho)


async def poner_activo(usuario_id: int, activo: bool) -> bool:
    """Activar o desactivar. Desactivada, la cuenta no entra (ver web/auth.py)
    y sus datos siguen ahi: es la baja reversible, no el borrado."""
    async with connection() as conn:
        hecho = await conn.fetchval(
            "UPDATE usuarios SET activo = $2 WHERE id = $1 RETURNING id",
            usuario_id, bool(activo))
    return bool(hecho)


async def anotar_acceso(usuario_id: int) -> None:
    """Marca la hora de entrada. Si la migracion 0014 no se ha aplicado aun,
    no falla: el acceso importa mas que la marca."""
    try:
        async with connection() as conn:
            await conn.execute(
                "UPDATE usuarios SET ultimo_acceso = NOW() WHERE id = $1", usuario_id)
    except asyncpg.UndefinedColumnError:
        log.warning("usuarios.ultimo_acceso no existe: aplica la migracion 0014")
    except Exception as e:  # noqa: BLE001 - nunca debe impedir entrar
        log.error("No se pudo anotar el acceso de %s: %s", usuario_id, e)
