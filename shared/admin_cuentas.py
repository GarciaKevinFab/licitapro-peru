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
