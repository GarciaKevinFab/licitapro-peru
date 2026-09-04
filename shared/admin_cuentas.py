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

from shared import fechas
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
    ahora = ahora or fechas.ahora()
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
    ahora = ahora or fechas.ahora()
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
    async with connection() as conn, conn.transaction():
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


async def detalle_cuenta(usuario_id: int) -> dict | None:
    """Todo lo que el dueno quiere ver de una cuenta en una sola pantalla.

    Suscripcion con su estado efectivo, historial de pagos, empresas con sus
    recuentos, cuantas propuestas/contratos/seguimientos hay, el uso de IA
    del mes y que canales de aviso tiene conectados. Son varias consultas
    chicas y no una gigante: cada bloque se lee solo y se puede quitar sin
    tocar los demas.
    """
    from shared import ia as modulo_ia

    c = await cuenta(usuario_id)
    if not c:
        return None
    async with connection() as conn:
        susc = await conn.fetchrow(
            """SELECT s.id, s.plan_codigo, s.estado, s.periodo, s.inicia, s.vence,
                      s.cancelada_en, s.tarjeta_marca, s.tarjeta_ultimos,
                      s.intentos_fallidos, s.ultimo_intento,
                      p.nombre AS plan_nombre, p.precio_mensual, p.precio_anual,
                      p.analisis_ia_mes
                 FROM suscripciones s JOIN planes p ON p.codigo = s.plan_codigo
                WHERE s.usuario_id = $1""", usuario_id)
        pagos = await conn.fetch(
            """SELECT ps.id, ps.monto, ps.moneda, ps.estado, ps.metodo, ps.created_at,
                      ps.confirmado_en, ps.izipay_order_number, ps.respuesta
                 FROM pagos_suscripcion ps JOIN suscripciones s ON s.id = ps.suscripcion_id
                WHERE s.usuario_id = $1 ORDER BY ps.created_at DESC LIMIT 50""", usuario_id)
        empresas = await conn.fetch(
            """SELECT e.id, e.razon_social, e.ruc, e.departamento, e.activa, e.created_at,
                      (SELECT count(*) FROM propuestas pr WHERE pr.empresa_id = e.id) AS propuestas,
                      (SELECT count(*) FROM contratos ct WHERE ct.empresa_id = e.id) AS contratos
                 FROM empresas e WHERE e.usuario_id = $1 ORDER BY e.activa DESC, e.id""",
            usuario_id)
        seguidas = await conn.fetchval(
            "SELECT count(*) FROM licitaciones_seguidas WHERE usuario_id = $1", usuario_id)
        uso_ia = await conn.fetchrow(
            """SELECT count(*) AS analisis,
                      COALESCE(SUM(tokens_entrada), 0) AS tokens_entrada,
                      COALESCE(SUM(tokens_salida), 0) AS tokens_salida
                 FROM analisis_ia
                WHERE usuario_id = $1 AND creado_en >= date_trunc('month', CURRENT_DATE)""",
            usuario_id)
        correo_avisos = await conn.fetchval(
            "SELECT email_notificaciones FROM user_config WHERE usuario_id = $1", usuario_id)

    s = dict(susc) if susc else None
    if s:
        s["estado_efectivo"] = estado_efectivo_de(s["estado"], s["vence"])
        s["dias_restantes"] = (s["vence"] - fechas.ahora()).days if s["vence"] else None
    usd = modulo_ia.coste_usd(uso_ia["tokens_entrada"], uso_ia["tokens_salida"])
    return {
        "c": c, "susc": s, "pagos": pagos, "empresas": empresas,
        "conteos": {"empresas": len(empresas),
                    "propuestas": sum(e["propuestas"] for e in empresas),
                    "contratos": sum(e["contratos"] for e in empresas),
                    "seguidas": seguidas or 0},
        "ia": {"analisis": uso_ia["analisis"], "tokens_entrada": uso_ia["tokens_entrada"],
               "tokens_salida": uso_ia["tokens_salida"],
               "soles": usd * modulo_ia.SOLES_POR_DOLAR,
               "tope": s["analisis_ia_mes"] if s else None},
        "canales": {"telegram": bool(c["telegram_chat_id"]),
                    "whatsapp": bool(c["whatsapp_numero"]) and c["whatsapp_estado"] == "activo",
                    "whatsapp_estado": c["whatsapp_estado"],
                    "correo": correo_avisos},
    }


# ─── Borrado en cascada ──────────────────────────────────

# Tablas que NO se borran aunque apunten a la cuenta. El Libro de
# Reclamaciones se conserva por ley (D.S. 101-2022-PCM: la hoja no se
# destruye); su clave foranea es ON DELETE SET NULL y aqui se hace lo mismo
# a mano, para que el bucle de abajo no la barra por llevar `usuario_id`.
CONSERVAR = {"reclamaciones"}


async def borrar_cuenta_completa(usuario_id: int) -> dict:
    """Borra la cuenta y TODO lo que cuelga de ella, en una transaccion.

    COMO CARGOXPREZ, Y POR QUE

      Las cascadas declaradas (migraciones 0002-0013) ya se llevan casi todo
      con un DELETE en usuarios, y `borrar_cuenta` de shared/db.py confia en
      ellas. Este borrado NO confia: recorre todas las tablas que tengan una
      columna que apunte a la cuenta o a sus empresas, propuestas, contratos
      o suscripcion, y las va vaciando; la que falla por clave foranea se
      reintenta en la vuelta siguiente, cuando sus hijas ya cayeron. Si una
      vuelta entera no avanza, hay un ciclo y se aborta -- la transaccion
      deshace todo -- en vez de dejar la cuenta a medio borrar.

      Se hace asi porque el dia que alguien anada una tabla con `empresa_id`
      y se olvide del ON DELETE CASCADE, el borrado desde el panel tiene que
      seguir funcionando. Una lista de tablas a mano se desincroniza; una
      cascada olvidada se ve como un 500 delante del dueno.

    Los archivos de disco (logos, firmas, sellos) se borran ANTES y fuera de
    la transaccion, por el mismo motivo que en `borrar_cuenta`: despues del
    DELETE ya no quedan las rutas. Un archivo que no se puede borrar se
    registra y no frena el resto.

    Devuelve {tabla: filas_borradas, "archivos": n, "borrada": bool}.
    """
    from shared.archivos import TIPOS as TIPOS_IMAGEN
    from shared.archivos import borrar_imagen, rutas_de

    resumen: dict = {"archivos": 0, "borrada": False}

    async with connection() as conn:
        empresas = [r["id"] for r in await conn.fetch(
            "SELECT id FROM empresas WHERE usuario_id = $1", usuario_id)]
    for eid in empresas:
        presentes = await rutas_de(eid)
        for tipo in TIPOS_IMAGEN:
            try:
                await borrar_imagen(eid, tipo)
                resumen["archivos"] += tipo in presentes
            except Exception as e:  # noqa: BLE001
                log.error("No se pudo borrar la imagen %s de la empresa %s: %s", tipo, eid, e)

    async with connection() as conn, conn.transaction():
        # Los ids se fijan ANTES de empezar a borrar: si se resolvieran
        # con subconsultas, al reintentar una tabla hija sus padres ya
        # podrian no estar y la condicion no encontraria nada.
        propuestas = [r["id"] for r in await conn.fetch(
            "SELECT id FROM propuestas WHERE empresa_id = ANY($1::int[])", empresas)]
        contratos = [r["id"] for r in await conn.fetch(
            "SELECT id FROM contratos WHERE empresa_id = ANY($1::int[])", empresas)]
        suscripciones = [r["id"] for r in await conn.fetch(
            "SELECT id FROM suscripciones WHERE usuario_id = $1", usuario_id)]
        claves = {
            "usuario_id": [usuario_id], "empresa_id": empresas,
            "propuesta_id": propuestas, "contrato_id": contratos,
            "suscripcion_id": suscripciones,
        }
        filas = await conn.fetch(
            """SELECT table_name, column_name FROM information_schema.columns
                    WHERE table_schema = 'public' AND column_name = ANY($1::text[])
                      AND table_name <> 'usuarios'
                    ORDER BY table_name""", list(claves))
        pendientes = [(f["table_name"], f["column_name"]) for f in filas
                      if f["table_name"] not in CONSERVAR and claves[f["column_name"]]]

        for tabla in CONSERVAR:
            if any(f["table_name"] == tabla and f["column_name"] == "usuario_id" for f in filas):
                await conn.execute(
                    f'UPDATE "{tabla}" SET usuario_id = NULL WHERE usuario_id = $1', usuario_id)

        while pendientes:
            quedan = []
            for tabla, columna in pendientes:
                try:
                    async with conn.transaction():  # savepoint: el fallo no aborta todo
                        hecho = await conn.execute(
                            f'DELETE FROM "{tabla}" WHERE "{columna}" = ANY($1::int[])',
                            claves[columna])
                    n = int(hecho.split()[-1])
                    resumen[tabla] = resumen.get(tabla, 0) + n
                except asyncpg.ForeignKeyViolationError:
                    quedan.append((tabla, columna))
            if len(quedan) == len(pendientes):
                raise RuntimeError(
                    "No se pudo eliminar la cuenta: dependencias sin resolver en "
                    + ", ".join(sorted({t for t, _ in quedan})))
            pendientes = quedan

        borrada = await conn.fetchval(
            "DELETE FROM usuarios WHERE id = $1 RETURNING id", usuario_id)
        resumen["borrada"] = bool(borrada)

    log.info("Cuenta %s eliminada del todo: %s", usuario_id, resumen)
    return resumen


async def anotar_acceso(usuario_id: int) -> None:
    """Marca la hora de entrada. Si la migracion 0014 no se ha aplicado aun,
    no falla: el acceso importa mas que la marca."""
    try:
        async with connection() as conn:
            await conn.execute(
                "UPDATE usuarios SET ultimo_acceso = NOW() WHERE id = $1", usuario_id)
    except asyncpg.UndefinedColumnError:
        log.warning("usuarios.ultimo_acceso no existe: aplica la migracion 0014")
    except Exception as e:  # noqa: BLE001
        log.error("No se pudo anotar el acceso de %s: %s", usuario_id, e)
