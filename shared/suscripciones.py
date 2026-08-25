"""Maquina de estados de la suscripcion y limites por plan.

Estados y como se pasa de uno a otro:

    prueba     -> activa      pago confirmado
    prueba     -> vencida     se acabaron los dias sin pagar
    activa     -> vencida     llego la renovacion sin cobro exitoso
    vencida    -> activa      pago confirmado (reintento o manual)
    vencida    -> suspendida  pasaron los dias de gracia
    cualquiera -> cancelada   el usuario cancela

Por que gracia y no corte inmediato: una tarjeta rebota por mil motivos
(limite, vencimiento, el banco), y cortarle el servicio a alguien que si quiere
pagar es la forma mas cara de perder un cliente. `vencida` conserva el acceso;
`suspendida` lo corta.

Los limites se comprueban al ACTUAR, no al mostrar: ocultar un boton no impide
que alguien mande el formulario igual.
"""
import logging
from datetime import datetime, timedelta

from shared.db import connection

log = logging.getLogger("shared.suscripciones")

DIAS_PRUEBA = 14
DIAS_GRACIA = 7           # cuanto sigue funcionando una suscripcion vencida
MAX_INTENTOS = 4          # reintentos de cobro antes de suspender

ACCESO_PERMITIDO = ("prueba", "activa", "vencida")   # 'vencida' sigue en gracia


async def estado_suscripcion(usuario_id: int) -> dict:
    """Suscripcion + plan + si tiene acceso, resuelto en un solo lugar."""
    async with connection() as conn:
        fila = await conn.fetchrow(
            """SELECT s.*, p.nombre AS plan_nombre, p.precio_mensual,
                      p.precio_anual, p.max_empresas, p.max_regiones, p.analisis_ia
                 FROM suscripciones s
                 JOIN planes p ON p.codigo = s.plan_codigo
                WHERE s.usuario_id = $1""",
            usuario_id)

    if not fila:
        # Sin suscripcion NO se bloquea: seria dejar fuera a alguien por un
        # fallo nuestro. Se registra para poder detectarlo.
        log.warning("Usuario %s sin fila de suscripcion", usuario_id)
        return {"existe": False, "acceso": True, "estado_efectivo": "sin_suscripcion",
                "dias_restantes": None, "en_gracia": False}

    d = dict(fila)
    ahora = datetime.now()
    vence = d.get("vence")
    dias = (vence - ahora).days if vence else None

    estado = d["estado"]
    # El vencimiento se evalua al leer: asi el estado es correcto aunque el
    # proceso de renovacion no haya corrido todavia.
    if estado in ("prueba", "activa") and vence and vence < ahora:
        estado = "vencida"
    if estado == "vencida" and vence and vence + timedelta(days=DIAS_GRACIA) < ahora:
        estado = "suspendida"

    d["estado_efectivo"] = estado
    d["existe"] = True
    d["acceso"] = estado in ACCESO_PERMITIDO
    d["dias_restantes"] = dias
    d["en_gracia"] = estado == "vencida"
    return d


async def crear_suscripcion_prueba(usuario_id: int, plan: str = "pro") -> None:
    """Da de alta la prueba al registrarse. Sin esto la cuenta nace sin plan."""
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO suscripciones (usuario_id, plan_codigo, estado, periodo, vence)
               VALUES ($1, $2, 'prueba', 'mensual', NOW() + ($3 || ' days')::interval)
               ON CONFLICT (usuario_id) DO NOTHING""",
            usuario_id, plan, str(DIAS_PRUEBA))


# ─── Limites por plan ────────────────────────────────────

async def puede_agregar_empresa(usuario_id: int) -> tuple[bool, str]:
    """(permitido, motivo). Se comprueba al guardar, no al pintar el boton."""
    susc = await estado_suscripcion(usuario_id)
    if not susc.get("acceso"):
        return False, "Tu suscripción está suspendida. Actualiza tu pago para continuar."

    tope = susc.get("max_empresas")
    if tope is None:
        return True, ""
    async with connection() as conn:
        actuales = await conn.fetchval(
            "SELECT COUNT(*) FROM empresas WHERE usuario_id=$1 AND activa=TRUE",
            usuario_id)
    if actuales >= tope:
        plan = susc.get("plan_nombre") or susc.get("plan_codigo")
        return False, (f"Tu plan {plan} permite {tope} empresa(s) activa(s). "
                       f"Cambia de plan o desactiva una para agregar otra.")
    return True, ""


async def limite_regiones(usuario_id: int) -> int | None:
    """None = sin limite."""
    susc = await estado_suscripcion(usuario_id)
    return susc.get("max_regiones")


# ─── Cobros ──────────────────────────────────────────────

async def registrar_intento(usuario_id: int, monto, numero_orden: str) -> int | None:
    """Crea el pago en 'pendiente'. None si ese numero de orden ya existia.

    La unicidad de izipay_order_number es la idempotencia: si el usuario pulsa
    dos veces o el webhook llega repetido, no se duplica el cobro.
    """
    async with connection() as conn:
        susc_id = await conn.fetchval(
            "SELECT id FROM suscripciones WHERE usuario_id=$1", usuario_id)
        if not susc_id:
            return None
        return await conn.fetchval(
            """INSERT INTO pagos_suscripcion
                   (suscripcion_id, monto, estado, izipay_order_number)
               VALUES ($1, $2, 'pendiente', $3)
               ON CONFLICT (izipay_order_number) DO NOTHING
               RETURNING id""",
            susc_id, monto, numero_orden)


async def confirmar_pago(numero_orden: str, transaction_id: str | None,
                         respuesta: dict | None = None) -> bool:
    """Marca el pago como pagado y extiende la suscripcion. Idempotente.

    Devuelve True solo la PRIMERA vez que confirma ese numero de orden: si el
    webhook llega dos veces, la segunda no vuelve a extender el periodo.
    """
    import json
    async with connection() as conn:
        pago = await conn.fetchrow(
            """UPDATE pagos_suscripcion
                  SET estado='pagado', confirmado_en=NOW(),
                      izipay_transaction_id=$2, respuesta=$3
                WHERE izipay_order_number=$1 AND estado <> 'pagado'
             RETURNING id, suscripcion_id""",
            numero_orden, transaction_id,
            json.dumps(respuesta) if respuesta else None)
        if not pago:
            log.info("Pago %s ya confirmado o inexistente: no se repite", numero_orden)
            return False

        susc = await conn.fetchrow(
            "SELECT periodo FROM suscripciones WHERE id=$1", pago["suscripcion_id"])
        dias = 365 if (susc and susc["periodo"] == "anual") else 30
        # Se extiende desde el vencimiento si aun no paso; si ya paso, desde hoy.
        # Asi renovar antes de tiempo no regala dias ni los quita.
        await conn.execute(
            """UPDATE suscripciones
                  SET estado='activa',
                      intentos_fallidos=0,
                      vence = GREATEST(COALESCE(vence, NOW()), NOW())
                              + ($2 || ' days')::interval
                WHERE id=$1""",
            pago["suscripcion_id"], str(dias))
    log.info("Pago %s confirmado; suscripcion %s extendida %s dias",
             numero_orden, pago["suscripcion_id"], dias)
    return True


async def registrar_intento_fallido(usuario_id: int) -> None:
    """Suma un intento fallido y suspende al pasarse del maximo."""
    async with connection() as conn:
        await conn.execute(
            """UPDATE suscripciones
                  SET intentos_fallidos = intentos_fallidos + 1,
                      ultimo_intento = NOW(),
                      estado = CASE WHEN intentos_fallidos + 1 >= $2
                                    THEN 'suspendida' ELSE estado END
                WHERE usuario_id = $1""",
            usuario_id, MAX_INTENTOS)


async def guardar_token_tarjeta(usuario_id: int, token: str,
                                marca: str = None, ultimos: str = None) -> None:
    """Guarda el token cifrado. Nunca se guarda el numero de tarjeta."""
    from shared.seguridad import cifrar
    async with connection() as conn:
        await conn.execute(
            """UPDATE suscripciones
                  SET token_tarjeta=$2, tarjeta_marca=$3, tarjeta_ultimos=$4
                WHERE usuario_id=$1""",
            usuario_id, cifrar(token), marca, ultimos)


async def cambiar_plan(usuario_id: int, plan_codigo: str, periodo: str) -> bool:
    async with connection() as conn:
        existe = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM planes WHERE codigo=$1 AND activo=TRUE)",
            plan_codigo)
        if not existe or periodo not in ("mensual", "anual"):
            return False
        await conn.execute(
            "UPDATE suscripciones SET plan_codigo=$2, periodo=$3 WHERE usuario_id=$1",
            usuario_id, plan_codigo, periodo)
    return True


async def cancelar(usuario_id: int) -> None:
    """Cancela sin cortar el acceso: se conserva hasta el fin del periodo pagado."""
    async with connection() as conn:
        await conn.execute(
            """UPDATE suscripciones
                  SET estado='cancelada', cancelada_en=NOW(), token_tarjeta=NULL
                WHERE usuario_id=$1""",
            usuario_id)


async def renovaciones_pendientes() -> list[dict]:
    """Suscripciones con tarjeta guardada que toca renovar.

    Solo las que tienen token: sin tarjeta guardada no hay nada que reintentar
    automaticamente. El filtro de ultimo_intento evita machacar la pasarela.
    """
    async with connection() as conn:
        filas = await conn.fetch(
            """SELECT s.id, s.usuario_id, s.periodo, s.token_tarjeta,
                      s.intentos_fallidos, u.email,
                      CASE WHEN s.periodo='anual' THEN p.precio_anual
                           ELSE p.precio_mensual END AS monto
                 FROM suscripciones s
                 JOIN usuarios u ON u.id = s.usuario_id
                 JOIN planes p ON p.codigo = s.plan_codigo
                WHERE s.token_tarjeta IS NOT NULL
                  AND s.estado IN ('activa','vencida')
                  AND s.vence < NOW()
                  AND s.intentos_fallidos < $1
                  AND (s.ultimo_intento IS NULL
                       OR s.ultimo_intento < NOW() - INTERVAL '1 day')
                ORDER BY s.vence""",
            MAX_INTENTOS)
    return [dict(f) for f in filas]


# ─── Puerta de acceso al producto ────────────────────────

# Rutas que SIEMPRE se pueden usar, aunque la suscripcion este suspendida.
# Cortar el acceso a la pagina de pago seria dispararse en el pie: el cliente
# que quiere pagar no podria. Y cerrar sesion o recuperar la contrasena tampoco
# dependen de estar al dia.
RUTAS_LIBRES = (
    "/entrar", "/registro", "/salir", "/recuperar",
    "/suscripcion", "/webhooks/", "/salud", "/static",
)


def ruta_libre(camino: str) -> bool:
    return any(camino == r or camino.startswith(r + "/") or camino.startswith(r)
               for r in RUTAS_LIBRES)


async def regiones_permitidas(usuario_id: int, regiones: list[str]) -> tuple[list[str], str]:
    """Recorta la lista de regiones al tope del plan. Devuelve (lista, aviso).

    Se recorta en vez de rechazar el formulario entero: al bajar de plan, el
    usuario tendria mas regiones de las que le tocan y no podria guardar ningun
    cambio hasta adivinar cuales quitar.
    """
    tope = (await estado_suscripcion(usuario_id)).get("max_regiones")
    if tope is None or len(regiones) <= tope:
        return regiones, ""
    return regiones[:tope], (
        f"Tu plan permite {tope} regiones y elegiste {len(regiones)}. "
        f"Se guardaron las {tope} primeras; cambia de plan para cubrir más.")


async def puede_usar_ia(usuario_id: int) -> bool:
    """El analisis de bases con IA solo esta en los planes que lo incluyen.

    Importa de verdad: cada analisis cuesta dinero en la API de Anthropic, y
    quien lo paga es el dueno de la plataforma, no el cliente.
    """
    susc = await estado_suscripcion(usuario_id)
    return bool(susc.get("acceso")) and bool(susc.get("analisis_ia"))
