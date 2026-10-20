"""Maquina de estados de la suscripcion y limites por plan.

Estados y como se pasa de uno a otro:

    prueba     -> activa      pago confirmado
    prueba     -> vencida     se acabaron los dias sin pagar
    activa     -> vencida     llego la renovacion sin cobro exitoso
    vencida    -> activa      pago confirmado (reintento o manual)
    vencida    -> suspendida  pasaron los dias de gracia
    cualquiera -> cancelada   el usuario cancela (conserva el acceso hasta `vence`)

Por que gracia y no corte inmediato: una tarjeta rebota por mil motivos
(limite, vencimiento, el banco), y cortarle el servicio a alguien que si quiere
pagar es la forma mas cara de perder un cliente. `vencida` conserva el acceso;
`suspendida` lo corta.

Los limites se comprueban al ACTUAR, no al mostrar: ocultar un boton no impide
que alguien mande el formulario igual.
"""
import logging
from datetime import datetime, timedelta

from shared import fechas
from shared.db import connection

log = logging.getLogger("shared.suscripciones")

DIAS_PRUEBA = 14
DIAS_GRACIA = 7           # cuanto sigue funcionando una suscripcion vencida
MAX_INTENTOS = 4          # reintentos de cobro antes de suspender

ACCESO_PERMITIDO = ("prueba", "activa", "vencida")

# Plan al que se cae cuando se agota la gracia, en vez de perder el acceso.
PLAN_GRATUITO = "gratis"   # 'vencida' sigue en gracia

# LA HORA DE LIMA, TAMBIEN DENTRO DE POSTGRES
#
#   Las columnas de fecha de `suscripciones` y `pagos_suscripcion` son
#   `timestamp` SIN zona y el resto del sistema guarda en ellas la hora de pared
#   de Lima (ver shared/fechas.py). Pero la sesion de Postgres de produccion
#   corre en UTC, asi que un `NOW()` a secas escribe la hora de Greenwich.
#
#   Mezclar las dos cosas no revienta: regala cinco horas. `vence` se escribia
#   con `NOW() + 30 dias` (UTC) y se leia contra `fechas.ahora()` (Lima), y
#   cada suscripcion vencia cinco horas mas tarde de lo pagado; `activar_manual`,
#   en cambio, escribia en Lima. Dos relojes en la misma columna.
#
#   Todas las lecturas y escrituras de este modulo pasan por esta expresion, que
#   da la hora de Lima sin zona corra donde corra la base. Las filas viejas
#   escritas en UTC quedan, como mucho, cinco horas largas: no se migran.
#
#   La unica excepcion es `created_at`, que lo rellena el DEFAULT de la tabla
#   con el reloj de la sesion: donde se compara (la ventana de adopcion de
#   `aplicar_cargo_culqi`) se compara contra ese mismo reloj, `NOW()`.
AHORA_SQL = "(NOW() AT TIME ZONE 'America/Lima')"


def estado_efectivo_de(estado: str, vence, ahora: datetime | None = None) -> str:
    """El estado que de verdad aplica hoy, a partir del guardado y la fecha.

    Es la regla de `estado_suscripcion` sacada a una funcion sin base para que
    el panel del dueno la aplique a cien cuentas de una consulta y para que se
    pueda probar con fechas inventadas. Cambiarla aqui cambia las dos cosas, que
    es lo que se quiere: una sola definicion de "vencida" y de "suspendida".
    """
    ahora = ahora or fechas.ahora()
    if estado in ("prueba", "activa") and vence and vence < ahora:
        estado = "vencida"
    if estado == "vencida" and vence and vence + timedelta(days=DIAS_GRACIA) < ahora:
        estado = "suspendida"
    return estado


def tiene_acceso(estado_efectivo: str, vence, ahora: datetime | None = None) -> bool:
    """Si un estado ya resuelto deja usar el producto.

    POR QUE `cancelada` NO CORTA AL INSTANTE

      Cancelar se anunciaba como "conservas el acceso hasta el fin del periodo"
      y cortaba en el acto: `cancelada` no estaba en ACCESO_PERMITIDO. El
      cliente que cancelaba un dia despues de pagar perdia los 29 restantes,
      justo lo contrario de lo que la pantalla le acababa de prometer.

      Ahora `cancelada` con `vence` por delante sigue dentro. Cuando `vence`
      pasa, se corta como antes; no hay gracia, porque nadie va a reintentar un
      cobro que el propio cliente pidio parar.
    """
    if estado_efectivo in ACCESO_PERMITIDO:
        return True
    if estado_efectivo == "cancelada":
        ahora = ahora or fechas.ahora()
        return bool(vence and vence >= ahora)
    return False


async def estado_suscripcion(usuario_id: int) -> dict:
    """Suscripcion + plan + si tiene acceso, resuelto en un solo lugar."""
    async with connection() as conn:
        fila = await conn.fetchrow(
            """SELECT s.*, p.nombre AS plan_nombre, p.precio_mensual,
                      p.precio_anual, p.max_empresas, p.max_regiones, p.analisis_ia,
                      p.alertas
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
    ahora = fechas.ahora()
    vence = d.get("vence")
    dias = (vence - ahora).days if vence else None

    # El vencimiento se evalua al leer: asi el estado es correcto aunque el
    # proceso de renovacion no haya corrido todavia.
    estado = estado_efectivo_de(d["estado"], vence, ahora)

    d["estado_efectivo"] = estado
    d["existe"] = True
    d["dias_restantes"] = dias
    d["en_gracia"] = estado == "vencida"
    d["degradado"] = False

    if estado == "suspendida":
        # Agotada la gracia NO se expulsa: se cae al plan gratuito. Bloquear del
        # todo empuja al proveedor a la web del competidor, que si le deja
        # mirar; y el que aun no ha visto pasar una licitacion suya no tiene con
        # que decidir si pagar. Dejarle el panel no nos cuesta nada -- el pozo
        # ya esta scrapeado -- y lo que se corta es lo que da valor y cuesta
        # dinero: los avisos, la IA y las empresas de mas.
        limites = await _limites_plan(PLAN_GRATUITO)
        if limites:
            d.update(limites)
            d["plan_codigo"] = PLAN_GRATUITO
            d["degradado"] = True
            d["acceso"] = True
            return d
        # Sin plan gratuito en la tabla se mantiene el corte: es preferible
        # bloquear a conceder por accidente un acceso sin limites conocidos.

    d["acceso"] = tiene_acceso(estado, vence, ahora)
    return d


async def _limites_plan(codigo: str) -> dict | None:
    """Capacidades de un plan, con los mismos nombres que trae estado_suscripcion."""
    async with connection() as conn:
        fila = await conn.fetchrow(
            """SELECT nombre AS plan_nombre, precio_mensual, precio_anual,
                      max_empresas, max_regiones, analisis_ia, alertas
                 FROM planes WHERE codigo = $1""",
            codigo)
    return dict(fila) if fila else None


async def crear_suscripcion_prueba(usuario_id: int, plan: str = "pro") -> None:
    """Da de alta la prueba al registrarse. Sin esto la cuenta nace sin plan."""
    async with connection() as conn:
        await conn.execute(
            f"""INSERT INTO suscripciones
                   (usuario_id, plan_codigo, estado, periodo, inicia, vence)
               VALUES ($1, $2, 'prueba', 'mensual', {AHORA_SQL},
                       {AHORA_SQL} + ($3 || ' days')::interval)
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

async def registrar_intento(usuario_id: int, monto, numero_orden: str,
                            plan_codigo: str | None = None,
                            periodo: str | None = None) -> int | None:
    """Crea el pago en 'pendiente'. None si ese numero de orden ya existia.

    La unicidad de izipay_order_number es la idempotencia: si el usuario pulsa
    dos veces o el webhook llega repetido, no se duplica el cobro.

    EL PLAN VIAJA CON LA ORDEN

      `plan_codigo` y `periodo` son lo que se esta comprando. Se guardan en la
      propia fila del pago (`respuesta.pedido`) y `confirmar_pago` los aplica
      al confirmarse el cobro. Asi un cambio a un plan mas caro solo llega a la
      cuenta cuando el dinero llega a la pasarela, y no al pulsar "Elegir".
      Va en el JSON y no en columnas nuevas para no exigir una migracion.
    """
    import json
    pedido = None
    if plan_codigo and periodo:
        pedido = json.dumps({"pedido": {"plan_codigo": plan_codigo,
                                        "periodo": periodo}})
    async with connection() as conn:
        susc_id = await conn.fetchval(
            "SELECT id FROM suscripciones WHERE usuario_id=$1", usuario_id)
        if not susc_id:
            return None
        return await conn.fetchval(
            """INSERT INTO pagos_suscripcion
                   (suscripcion_id, monto, estado, izipay_order_number, respuesta)
               VALUES ($1, $2, 'pendiente', $3, $4::jsonb)
               ON CONFLICT (izipay_order_number) DO NOTHING
               RETURNING id""",
            susc_id, monto, numero_orden, pedido)


async def orden_del_usuario(numero_orden: str, usuario_id: int) -> bool:
    """Si esa orden de Izipay es de una suscripcion de ese usuario.

    La usa el retorno del navegador: el numero de orden llega en un formulario
    que manda el cliente, y sin esta comprobacion cualquiera con sesion podria
    tocar la orden de otro.
    """
    async with connection() as conn:
        return bool(await conn.fetchval(
            """SELECT 1 FROM pagos_suscripcion ps
                 JOIN suscripciones s ON s.id = ps.suscripcion_id
                WHERE ps.izipay_order_number = $1 AND s.usuario_id = $2""",
            numero_orden, usuario_id))


async def confirmar_pago(numero_orden: str, transaction_id: str | None,
                         respuesta: dict | None = None) -> bool:
    """Marca el pago como pagado y extiende la suscripcion. Idempotente.

    Devuelve True solo la PRIMERA vez que confirma ese numero de orden: si el
    webhook llega dos veces, la segunda no vuelve a extender el periodo.
    """
    import json
    async with connection() as conn, conn.transaction():
        # La CTE lee el pedido ANTES de que el UPDATE pise `respuesta` con lo
        # que manda la pasarela; el pedido se conserva dentro del JSON nuevo.
        pago = await conn.fetchrow(
            f"""WITH previo AS (
                    SELECT id, respuesta FROM pagos_suscripcion
                     WHERE izipay_order_number=$1 AND estado <> 'pagado'
                       FOR UPDATE)
                UPDATE pagos_suscripcion p
                   SET estado='pagado', confirmado_en={AHORA_SQL},
                       izipay_transaction_id=$2,
                       respuesta = COALESCE($3::jsonb, '{{}}'::jsonb)
                                   || jsonb_build_object('pedido',
                                                         previo.respuesta->'pedido')
                  FROM previo
                 WHERE p.id = previo.id
             RETURNING p.id, p.suscripcion_id,
                       previo.respuesta->'pedido' AS pedido""",
            numero_orden, transaction_id,
            json.dumps(respuesta, default=str) if respuesta else None)
        if not pago:
            log.info("Pago %s ya confirmado o inexistente: no se repite", numero_orden)
            return False

        pedido = pago["pedido"]
        if isinstance(pedido, str):
            pedido = json.loads(pedido)
        pedido = pedido if isinstance(pedido, dict) else {}
        plan_pedido = pedido.get("plan_codigo")
        periodo_pedido = pedido.get("periodo")
        if periodo_pedido not in DIAS_POR_PERIODO:
            plan_pedido = periodo_pedido = None

        susc = await conn.fetchrow(
            "SELECT periodo FROM suscripciones WHERE id=$1", pago["suscripcion_id"])
        periodo = periodo_pedido or (susc["periodo"] if susc else "mensual")
        dias = _dias_de(periodo)
        # Se extiende desde el vencimiento si aun no paso; si ya paso, desde hoy.
        # Asi renovar antes de tiempo no regala dias ni los quita. El plan y el
        # periodo comprados se aplican AQUI, con el cobro ya confirmado: es lo
        # que impide subir de plan sin pagar (ver `cambiar_plan`).
        await conn.execute(
            f"""UPDATE suscripciones
                   SET estado='activa',
                       intentos_fallidos=0,
                       cancelada_en=NULL,
                       plan_codigo=COALESCE($3, plan_codigo),
                       periodo=COALESCE($4, periodo),
                       vence = GREATEST(COALESCE(vence, {AHORA_SQL}), {AHORA_SQL})
                               + ($2 || ' days')::interval
                 WHERE id=$1""",
            pago["suscripcion_id"], str(dias), plan_pedido, periodo_pedido)
    log.info("Pago %s confirmado; suscripcion %s extendida %s dias",
             numero_orden, pago["suscripcion_id"], dias)
    return True


async def registrar_intento_fallido(usuario_id: int, *,
                                    charge_id: str | None = None,
                                    monto=None, event_id: str | None = None,
                                    respuesta: dict | None = None) -> bool:
    """Suma un intento fallido y suspende al pasarse del maximo.

    Devuelve True si lo conto, False si ese cargo ya estaba contado.

    IDEMPOTENTE POR CARGO CUANDO HAY CARGO

      Culqi reintenta el aviso que no recibe 200, y antes cada reenvio del MISMO
      cargo rechazado sumaba un intento: cuatro avisos de un solo rechazo y la
      cuenta quedaba suspendida. Con `charge_id`, el rechazo se asienta como un
      pago 'fallido' con ese `chr_`, y el indice unico parcial de
      `culqi_charge_id` decide si es nuevo. Solo si la fila entra se suma el
      intento, y las dos cosas van en la misma transaccion.

      Sin `charge_id` (el renovador de Izipay) se cuenta como siempre: alli
      cada llamada es un cobro distinto.
    """
    import json
    async with connection() as conn, conn.transaction():
        if charge_id:
            nuevo = await conn.fetchval(
                f"""INSERT INTO pagos_suscripcion
                        (suscripcion_id, monto, moneda, estado, metodo,
                         culqi_charge_id, culqi_event_id, respuesta, confirmado_en)
                    SELECT s.id, $2, 'PEN', 'fallido', 'culqi', $3, $4, $5::jsonb,
                           {AHORA_SQL}
                      FROM suscripciones s WHERE s.usuario_id = $1
                    -- Mismo predicado que el indice parcial: ver activar_por_culqi.
                    ON CONFLICT (culqi_charge_id) WHERE culqi_charge_id IS NOT NULL
                    DO NOTHING
                    RETURNING id""",
                usuario_id, monto if monto is not None else 0, charge_id, event_id,
                json.dumps(respuesta, default=str) if respuesta else None)
            if not nuevo:
                log.info("Cargo rechazado %s ya contado: no se suma otra vez",
                         charge_id)
                return False
        await conn.execute(
            f"""UPDATE suscripciones
                   SET intentos_fallidos = intentos_fallidos + 1,
                       ultimo_intento = {AHORA_SQL},
                       estado = CASE WHEN intentos_fallidos + 1 >= $2
                                     THEN 'suspendida' ELSE estado END
                 WHERE usuario_id = $1""",
            usuario_id, MAX_INTENTOS)
    return True


async def guardar_token_tarjeta(usuario_id: int, token: str,
                                marca: str | None = None, ultimos: str | None = None) -> None:
    """Guarda el token cifrado. Nunca se guarda el numero de tarjeta."""
    from shared.seguridad import cifrar
    async with connection() as conn:
        await conn.execute(
            """UPDATE suscripciones
                  SET token_tarjeta=$2, tarjeta_marca=$3, tarjeta_ultimos=$4
                WHERE usuario_id=$1""",
            usuario_id, cifrar(token), marca, ultimos)


# Lo que devuelve `cambiar_plan`. La cadena vacia es falsa a proposito: los
# que solo preguntaban "¿ha ido bien?" con `if not ...` siguen funcionando.
PLAN_INVALIDO = ""
PLAN_APLICADO = "aplicado"
PLAN_PENDIENTE_DE_PAGO = "pendiente_de_pago"


async def plan_valido(plan_codigo: str, periodo: str) -> bool:
    """Si el plan existe, esta activo y el periodo es uno de los conocidos."""
    if periodo not in ("mensual", "anual"):
        return False
    async with connection() as conn:
        return bool(await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM planes WHERE codigo=$1 AND activo=TRUE)",
            plan_codigo))


async def cambiar_plan(usuario_id: int, plan_codigo: str, periodo: str) -> str:
    """Cambia de plan SOLO si no es subir a uno mas caro. Ver las constantes.

    POR QUE SUBIR NO SE APLICA AQUI

      Antes esto era un UPDATE a secas, y `POST /suscripcion/elegir` lo llamaba
      antes de cobrar nada: un cliente activo en Basico pulsaba "Elegir" en
      Empresa y tenia Empresa hasta su siguiente vencimiento sin haber pagado
      la diferencia. Igual por `/comprar`, abandonando la pasarela.

      Ahora subir de plan (precio mensual mayor que el del plan actual) devuelve
      PLAN_PENDIENTE_DE_PAGO y NO toca la fila. Quien llama abre el cobro con
      ese plan, y es `confirmar_pago` quien lo aplica cuando el dinero llega.

    BAJAR SE APLICA AL INSTANTE, Y ES UNA DECISION

      Bajar (o quedarse en el mismo plan, o solo cambiar de periodo) se aplica
      ya. Aplicarlo al vencer exigiria guardar un "plan siguiente" en una
      columna nueva y un proceso que lo active, y lo que se gana es poco: quien
      baja pierde capacidades que ya habia pagado, pero lo hace porque lo pide,
      y el siguiente cobro ya sale al precio nuevo. No se devuelve dinero por
      los dias que quedan del plan caro.

      Cambiar solo de periodo (mensual <-> anual) no da nada por adelantado:
      el periodo decide el importe y los dias del PROXIMO cobro.
    """
    if not await plan_valido(plan_codigo, periodo):
        return PLAN_INVALIDO
    async with connection() as conn:
        precios = await conn.fetchrow(
            """SELECT pn.precio_mensual AS nuevo, pa.precio_mensual AS actual
                 FROM planes pn
                 LEFT JOIN suscripciones s ON s.usuario_id = $1
                 LEFT JOIN planes pa ON pa.codigo = s.plan_codigo
                WHERE pn.codigo = $2""",
            usuario_id, plan_codigo)
        nuevo = (precios["nuevo"] if precios else None) or 0
        actual = (precios["actual"] if precios else None) or 0
        if nuevo > actual:
            return PLAN_PENDIENTE_DE_PAGO
        await conn.execute(
            "UPDATE suscripciones SET plan_codigo=$2, periodo=$3 WHERE usuario_id=$1",
            usuario_id, plan_codigo, periodo)
    return PLAN_APLICADO


async def plan_y_precio(plan_codigo: str, periodo: str) -> dict | None:
    """Nombre e importe de un plan activo para un periodo. None si no hay."""
    async with connection() as conn:
        fila = await conn.fetchrow(
            """SELECT nombre, precio_mensual, precio_anual FROM planes
                WHERE codigo=$1 AND activo=TRUE""", plan_codigo)
    if not fila:
        return None
    return {"nombre": fila["nombre"],
            "monto": fila["precio_anual"] if periodo == "anual"
            else fila["precio_mensual"]}


async def activar_manual(usuario_id: int, plan_codigo: str, estado: str, periodo: str,
                         vence, nota: str | None, monto=None, por: str | None = None) -> bool:
    """Plan y estado puestos A MANO por el dueno del producto.

    Existe porque no todo el mundo paga por Izipay: hay clientes que pagan en
    efectivo, por Yape o por transferencia, y otros a los que se les regala un
    periodo. Sin esto, el unico camino para activar un plan era la pasarela.

    Si no hay fila de suscripcion se crea; si la hay, se pisa. Cuando se indica
    un monto, queda un pago con metodo 'manual' en pagos_suscripcion para que
    el historial de la cuenta y los informes cuadren con lo cobrado.
    """
    import json
    if estado not in ("prueba", "activa", "vencida", "suspendida", "cancelada"):
        return False
    if periodo not in ("mensual", "anual"):
        return False
    async with connection() as conn:
        if not await conn.fetchval(
                "SELECT EXISTS(SELECT 1 FROM planes WHERE codigo=$1 AND activo=TRUE)", plan_codigo):
            return False
        susc_id = await conn.fetchval(
            "SELECT id FROM suscripciones WHERE usuario_id=$1", usuario_id)
        if susc_id:
            await conn.execute(
                f"""UPDATE suscripciones
                      SET plan_codigo=$2, estado=$3, periodo=$4, vence=$5,
                          intentos_fallidos=0,
                          cancelada_en=CASE WHEN $3='cancelada' THEN {AHORA_SQL}
                                            ELSE NULL END
                    WHERE id=$1""",
                susc_id, plan_codigo, estado, periodo, vence)
        else:
            susc_id = await conn.fetchval(
                f"""INSERT INTO suscripciones (usuario_id, plan_codigo, estado, periodo, inicia, vence)
                   VALUES ($1, $2, $3, $4, {AHORA_SQL}, $5) RETURNING id""",
                usuario_id, plan_codigo, estado, periodo, vence)
        if monto is not None and float(monto) > 0:
            await conn.execute(
                f"""INSERT INTO pagos_suscripcion
                       (suscripcion_id, monto, moneda, estado, metodo, respuesta, confirmado_en)
                   VALUES ($1, $2, 'PEN', 'pagado', 'manual', $3, {AHORA_SQL})""",
                susc_id, monto, json.dumps({"nota": nota, "por": por}))
    log.info("Suscripcion de %s puesta a mano: %s/%s hasta %s por %s",
             usuario_id, plan_codigo, estado, vence, por)
    return True


async def cancelar(usuario_id: int) -> None:
    """Cancela sin cortar el acceso: se conserva hasta el fin del periodo pagado.

    `vence` NO se toca: es la fecha hasta la que `tiene_acceso` deja entrar a
    una cuenta cancelada. Lo que desaparece es la tarjeta, para que nada la
    vuelva a cobrar.
    """
    async with connection() as conn:
        await conn.execute(
            f"""UPDATE suscripciones
                   SET estado='cancelada', cancelada_en={AHORA_SQL},
                       token_tarjeta=NULL
                 WHERE usuario_id=$1""",
            usuario_id)


async def renovaciones_pendientes() -> list[dict]:
    """Suscripciones con tarjeta guardada que toca renovar.

    Solo las que tienen token: sin tarjeta guardada no hay nada que reintentar
    automaticamente. El filtro de ultimo_intento evita machacar la pasarela.
    """
    async with connection() as conn:
        filas = await conn.fetch(
            f"""SELECT s.id, s.usuario_id, s.periodo, s.token_tarjeta,
                      s.intentos_fallidos, u.email,
                      CASE WHEN s.periodo='anual' THEN p.precio_anual
                           ELSE p.precio_mensual END AS monto
                 FROM suscripciones s
                 JOIN usuarios u ON u.id = s.usuario_id
                 JOIN planes p ON p.codigo = s.plan_codigo
                WHERE s.token_tarjeta IS NOT NULL
                  AND s.estado IN ('activa','vencida')
                  -- Nunca se cobra un plan de precio cero. Sin esto, alguien
                  -- que cayo al plan gratuito conservando su tarjeta generaria
                  -- una orden de S/0.00 contra la pasarela cada dia: la
                  -- rechazaria, sumaria un intento fallido y acabaria
                  -- suspendiendo a un usuario que no debe nada.
                  AND COALESCE(CASE WHEN s.periodo='anual' THEN p.precio_anual
                                    ELSE p.precio_mensual END, 0) > 0
                  AND s.vence < {AHORA_SQL}
                  AND s.intentos_fallidos < $1
                  AND (s.ultimo_intento IS NULL
                       OR s.ultimo_intento < {AHORA_SQL} - INTERVAL '1 day')
                ORDER BY s.vence""",
            MAX_INTENTOS)
    return [dict(f) for f in filas]


# ─── Suscripciones recurrentes de Culqi ──────────────────
#
# QUE CAMBIA RESPECTO A IZIPAY
#
#   Con Izipay el cobro lo disparabamos nosotros: `renovaciones_pendientes` +
#   un cron que cobra la tarjeta guardada. Con Culqi el cobro lo dispara
#   Culqi, cada periodo, y nos avisa. Nuestro trabajo pasa de "cobrar" a
#   "enterarse y no equivocarse al contar los periodos".
#
#   Por eso lo de aqui gira todo alrededor de la misma pregunta: este aviso,
#   ¿ya lo apliqué? Culqi reintenta lo que no recibe 200, asi que el mismo
#   cobro puede llegar tres veces, y cada aplicacion de mas es un mes de
#   servicio regalado que nadie reclama.

DIAS_POR_PERIODO = {"mensual": 30, "anual": 365}


def _dias_de(periodo: str) -> int:
    return DIAS_POR_PERIODO.get(periodo, 30)


async def suscripcion_por_culqi(subscription_id: str | None = None,
                                customer_id: str | None = None,
                                email: str | None = None) -> dict | None:
    """De un identificador de Culqi a nuestra fila. None si no es nuestra.

    SE BUSCA POR TRES VIAS Y EN ESTE ORDEN

      El webhook trae lo que trae -- su forma exacta es POR_CONFIRMAR -- y no
      podemos exigir un campo concreto. Se intenta de lo mas especifico a lo
      menos:

        sxn_  identifica exactamente una suscripcion nuestra.
        cus_  identifica al cliente; si tuviera dos suscripciones habria
              ambiguedad, pero la tabla tiene usuario_id UNICO, asi que no.
        email ultimo recurso, y solo sirve si el correo de Culqi es el de la
              cuenta.

      Devolver None NO es un error: puede ser un aviso de otro comercio o de
      otro producto. Quien llama lo registra y contesta 200.

    EL ORDEN SE IMPONE EN LA CONSULTA, NO SE DA POR SUPUESTO

      Era un `WHERE a OR b OR c LIMIT 1` sin ORDER BY: si el sxn_ era de una
      cuenta y el correo de otra, Postgres devolvia la que encontrara primero,
      y el cobro podia acabar extendiendo la suscripcion de quien no pago. Ahora
      gana la coincidencia mas especifica y, a igualdad, el id mas bajo.
    """
    condiciones, valores = [], []
    if subscription_id:
        valores.append(subscription_id)
        condiciones.append(f"s.culqi_subscription_id = ${len(valores)}")
    if customer_id:
        valores.append(customer_id)
        condiciones.append(f"s.culqi_customer_id = ${len(valores)}")
    if email:
        valores.append(email.strip().lower())
        condiciones.append(f"LOWER(u.email) = ${len(valores)}")
    if not condiciones:
        return None
    # Cada condicion vale su posicion en la lista de arriba: 0 gana a 1.
    prioridad = " ".join(f"WHEN {c} THEN {i}" for i, c in enumerate(condiciones))

    async with connection() as conn:
        fila = await conn.fetchrow(
            f"""SELECT s.id, s.usuario_id, s.periodo, s.plan_codigo, s.estado,
                       s.vence, s.culqi_subscription_id, s.culqi_card_id,
                       s.culqi_customer_id, u.email
                  FROM suscripciones s
                  JOIN usuarios u ON u.id = s.usuario_id
                 WHERE {' OR '.join(condiciones)}
                 ORDER BY CASE {prioridad} END, s.id
                 LIMIT 1""",
            *valores)
    return dict(fila) if fila else None


async def datos_culqi(usuario_id: int) -> dict | None:
    """Los identificadores de Culqi de una cuenta, para cancelar o cambiar plan."""
    async with connection() as conn:
        fila = await conn.fetchrow(
            """SELECT id, periodo, plan_codigo, estado, vence,
                      culqi_customer_id, culqi_card_id, culqi_subscription_id,
                      tarjeta_marca, tarjeta_ultimos
                 FROM suscripciones WHERE usuario_id = $1""",
            usuario_id)
    return dict(fila) if fila else None


async def activar_por_culqi(usuario_id: int, plan_codigo: str, periodo: str,
                            monto, customer_id: str, card_id: str,
                            subscription_id: str, marca: str | None = None,
                            ultimos: str | None = None,
                            charge_id: str | None = None,
                            respuesta: dict | None = None,
                            conn=None) -> bool:
    """Deja la cuenta activa tras contratar en Culqi, y registra el pago inicial.

    `conn` permite hacerlo dentro de la transaccion de quien llama: el checkout
    la abre para sostener el candado por usuario mientras crea la suscripcion
    en Culqi (ver web/comprar.py), y pedir aqui una segunda conexion del pool
    teniendo ya una es como se cuelga el pool entero.

    TODO EN UNA TRANSACCION, Y NO POR PULCRITUD

      Al llegar aqui la suscripcion en Culqi YA ESTA CREADA y el primer cobro
      YA SE HIZO. Si la activacion local se quedara a medias -- suscripcion
      activa sin pago registrado, o pago registrado sin activar --, el cliente
      tendria un cargo en su tarjeta y una cuenta que no le deja entrar, o un
      historial que no cuadra con su banco. O entra entero o no entra nada, y
      quien llama cancela en Culqi lo que no pudo asentar aqui.

    EL PERIODO SE CONCEDE AQUI, NO EN EL WEBHOOK

      `vence` se pone a un periodo vista desde HOY. El aviso de ese mismo
      primer cobro llegara despues por webhook, y ahi NO puede volver a
      extender: ver `aplicar_cargo_culqi`, que reconoce este pago y lo adopta
      en vez de contarlo dos veces.
    """
    if conn is None:
        async with connection() as propia:
            return await activar_por_culqi(
                usuario_id, plan_codigo, periodo, monto, customer_id, card_id,
                subscription_id, marca, ultimos, charge_id, respuesta, conn=propia)

    import json
    dias = _dias_de(periodo)
    async with conn.transaction():
        susc_id = await conn.fetchval(
            f"""UPDATE suscripciones
                  SET plan_codigo=$2, periodo=$3, estado='activa',
                      inicia={AHORA_SQL},
                      vence={AHORA_SQL} + ($4 || ' days')::interval,
                      cancelada_en=NULL, intentos_fallidos=0,
                      culqi_customer_id=$5, culqi_card_id=$6,
                      culqi_subscription_id=$7,
                      tarjeta_marca=COALESCE($8, tarjeta_marca),
                      tarjeta_ultimos=COALESCE($9, tarjeta_ultimos)
                WHERE usuario_id=$1
             RETURNING id""",
            usuario_id, plan_codigo, periodo, str(dias),
            customer_id, card_id, subscription_id, marca, ultimos)
        if not susc_id:
            log.error("Culqi activo la suscripcion %s del usuario %s pero no "
                      "hay fila local que actualizar", subscription_id, usuario_id)
            return False

        await conn.execute(
            f"""INSERT INTO pagos_suscripcion
                   (suscripcion_id, monto, moneda, estado, metodo,
                    culqi_charge_id, respuesta, confirmado_en)
               VALUES ($1, $2, 'PEN', 'pagado', 'culqi', $3, $4, {AHORA_SQL})
               -- El WHERE no sobra: el indice de culqi_charge_id es PARCIAL, y
               -- sin repetir su predicado Postgres no lo reconoce como arbitro
               -- ("there is no unique or exclusion constraint matching the ON
               -- CONFLICT specification") y el INSERT revienta. Se descubrio
               -- corriendo las pruebas contra una base de verdad; sin base,
               -- este error no aparece hasta el primer cliente.
               ON CONFLICT (culqi_charge_id) WHERE culqi_charge_id IS NOT NULL
               DO NOTHING""",
            susc_id, monto, charge_id,
            json.dumps(respuesta) if respuesta else None)

    log.info("Suscripcion de Culqi %s activada para el usuario %s (%s/%s, %s dias)",
             subscription_id, usuario_id, plan_codigo, periodo, dias)
    return True


async def _adoptar_o_insertar(conn, suscripcion_id: int, monto, charge_id: str,
                              event_id: str | None, cuerpo: str | None):
    """'adoptado', el id del pago nuevo, o None si otro aviso gano la carrera.

    Parte de `aplicar_cargo_culqi`, que explica los casos.
    """
    # ¿Es el aviso del cobro inicial que el checkout ya asento? Se busca el
    # pago de Culqi mas reciente sin cargo asociado. La ventana de 2 dias
    # evita adoptar un pago viejo por accidente: un aviso que tarda dos
    # dias en llegar ya no es el del alta. `created_at` lo escribe el DEFAULT
    # de la tabla con el reloj de la sesion, asi que se compara con `NOW()`.
    adoptado = await conn.fetchval(
        """UPDATE pagos_suscripcion
              SET culqi_charge_id=$2, culqi_event_id=$3,
                  respuesta=COALESCE(respuesta, $4)
            WHERE id = (SELECT id FROM pagos_suscripcion
                         WHERE suscripcion_id=$1 AND metodo='culqi'
                           AND estado='pagado'
                           AND culqi_charge_id IS NULL
                           AND created_at > NOW() - INTERVAL '2 days'
                         ORDER BY created_at DESC LIMIT 1)
         RETURNING id""",
        suscripcion_id, charge_id, event_id, cuerpo)
    if adoptado:
        log.info("Cargo %s adoptado por el pago inicial de la suscripcion %s: "
                 "el periodo ya se concedio en el checkout",
                 charge_id, suscripcion_id)
        return "adoptado"

    return await conn.fetchval(
        f"""INSERT INTO pagos_suscripcion
               (suscripcion_id, monto, moneda, estado, metodo,
                culqi_charge_id, culqi_event_id, respuesta, confirmado_en)
           VALUES ($1, $2, 'PEN', 'pagado', 'culqi', $3, $4, $5, {AHORA_SQL})
           -- Mismo predicado que el indice parcial: ver activar_por_culqi.
           ON CONFLICT (culqi_charge_id) WHERE culqi_charge_id IS NOT NULL
           DO NOTHING
           RETURNING id""",
        suscripcion_id, monto, charge_id, event_id, cuerpo)


async def aplicar_cargo_culqi(suscripcion_id: int, monto, charge_id: str,
                              event_id: str | None = None,
                              respuesta: dict | None = None,
                              periodo: str | None = None) -> str:
    """Aplica un cobro de Culqi ya COMPROBADO contra su API. Idempotente.

    Devuelve 'aplicado' | 'adoptado' | 'repetido'.

    LOS TRES CASOS SON DISTINTOS Y HAY QUE DISTINGUIRLOS

      repetido  Ese chr_ ya esta en la tabla. Culqi reintenta lo que no recibe
                200, asi que esto es lo NORMAL, no un error. No se toca nada.

      adoptado  Es el aviso del PRIMER cobro, el que disparo la creacion de la
                suscripcion en el checkout. Ese periodo ya se concedio alli
                (`activar_por_culqi` puso `vence` a un periodo vista), asi que
                aqui solo se le pega el chr_ real a esa fila y NO se extiende.

                Sin este caso, todo el que contrata recibiria dos periodos por
                un pago: uno en el checkout y otro al llegar el webhook. No da
                error, no aparece en ningun log, y son treinta dias regalados
                por cliente.

      aplicado  Una renovacion de verdad: Culqi cobro sola al vencer el
                periodo. Se registra el pago y se extiende.

    LA IDEMPOTENCIA LA GARANTIZA LA BASE, NO ESTE CODIGO

      El INSERT lleva ON CONFLICT sobre el indice unico parcial de
      culqi_charge_id. Comprobar antes con un SELECT y decidir despues es
      exactamente la carrera que dos avisos simultaneos ganan.
    """
    import json
    cuerpo = json.dumps(respuesta) if respuesta else None
    async with connection() as conn, conn.transaction():
        previo = await conn.fetchrow(
            "SELECT id, estado FROM pagos_suscripcion WHERE culqi_charge_id=$1",
            charge_id)
        if previo and previo["estado"] == "pagado":
            return "repetido"
        if previo:
            # Ese chr_ entro antes como rechazo (ver registrar_intento_fallido) y
            # la API lo da ahora por cobrado. Se corrige la MISMA fila en vez de
            # insertar otra -- el indice unico no admitiria una segunda -- y no
            # se busca adopcion: un cargo que fue rechazo no es el del alta.
            nuevo = await conn.fetchval(
                f"""UPDATE pagos_suscripcion
                       SET estado='pagado', monto=$2, confirmado_en={AHORA_SQL},
                           culqi_event_id=COALESCE($3, culqi_event_id),
                           respuesta=COALESCE($4::jsonb, respuesta)
                     WHERE id=$1 AND estado <> 'pagado'
                 RETURNING id""",
                previo["id"], monto, event_id, cuerpo)
        else:
            nuevo = await _adoptar_o_insertar(conn, suscripcion_id, monto,
                                              charge_id, event_id, cuerpo)
            if nuevo == "adoptado":
                return "adoptado"
        if not nuevo:
            # Otro aviso identico gano la carrera entre el SELECT y el INSERT.
            return "repetido"

        dias = _dias_de(periodo) if periodo else await conn.fetchval(
            "SELECT CASE WHEN periodo='anual' THEN 365 ELSE 30 END "
            "FROM suscripciones WHERE id=$1", suscripcion_id)
        # Se extiende desde el vencimiento si aun no paso; si ya paso, desde
        # hoy. Asi un cobro adelantado no regala dias ni los quita.
        await conn.execute(
            f"""UPDATE suscripciones
                   SET estado='activa', intentos_fallidos=0,
                       vence = GREATEST(COALESCE(vence, {AHORA_SQL}), {AHORA_SQL})
                               + ($2 || ' days')::interval
                 WHERE id=$1""",
            suscripcion_id, str(dias))

    log.info("Cargo %s aplicado: suscripcion %s extendida", charge_id, suscripcion_id)
    return "aplicado"


async def limpiar_culqi(usuario_id: int) -> None:
    """Olvida la suscripcion de Culqi de una cuenta, conservando cliente y tarjeta.

    Se llama al cancelar y al cambiar de plan. El `cus_` y el `crd_` se
    conservan a proposito: son los que permiten crear la suscripcion nueva sin
    volver a pedirle la tarjeta al cliente. Lo que desaparece es el `sxn_`,
    que es lo unico que deja de existir en Culqi tras un DELETE.
    """
    async with connection() as conn:
        await conn.execute(
            "UPDATE suscripciones SET culqi_subscription_id=NULL WHERE usuario_id=$1",
            usuario_id)


# ─── Puerta de acceso al producto ────────────────────────

# Rutas que SIEMPRE se pueden usar, aunque la suscripcion este suspendida.
# Cortar el acceso a la pagina de pago seria dispararse en el pie: el cliente
# que quiere pagar no podria. Y cerrar sesion o recuperar la contrasena tampoco
# dependen de estar al dia.
RUTAS_LIBRES = (
    "/entrar", "/registro", "/salir", "/recuperar",
    "/suscripcion", "/webhooks/", "/salud", "/static",
    # El escaparate y el checkout publicos. Van aqui por dos motivos, y cada
    # uno basta por si solo:
    #
    #   1. Se ven SIN sesion. Hoy el portero ya deja pasar a quien no trae
    #      cookie, pero el dia que eso cambie, la pagina donde se vende el
    #      producto no puede quedar detras del cobro del producto.
    #   2. Con la suscripcion SUSPENDIDA tambien se ven. Quien llega aqui viene
    #      justamente a pagar: redirigirle a "tu suscripcion esta suspendida"
    #      seria devolverle al sitio del que acaba de salir.
    "/precios", "/comprar",
    # El Libro de Reclamaciones NUNCA puede quedar detras del cobro: la Ley
    # 29571 da derecho a reclamar a cualquiera, y el caso mas probable es
    # justo alguien a quien le cortamos el servicio.
    "/reclamaciones",
    # Las paginas legales se leen con la suscripcion suspendida o sin cuenta:
    # el derecho a saber que hacemos con tus datos y a pedir que los borremos
    # no depende de estar al dia con el pago.
    "/privacidad", "/terminos",
    # La vista de gasto del dueno. Va aqui porque este middleware corta el
    # producto cuando la suscripcion vence, y el dueno no deja de necesitar sus
    # metricas porque su propia cuenta caduque: es justo cuando mas falta hacen.
    # Quien puede entrar lo decide `web/admin.py` comparando el correo con
    # LICITAPRO_ADMIN_EMAIL, y sin esa variable responde 404 a todo el mundo.
    "/admin",
)


def ruta_libre(camino: str) -> bool:
    """Si el camino escapa del portero de suscripcion.

    POR QUE NO ES UN `startswith` A SECAS

      Lo era, y hoy no liberaba nada indebido de casualidad: ninguna de las 69
      rutas declaradas empieza por un prefijo libre sin ser suya. Pero con esa
      regla, el dia que alguien anada `/registro-empresa` o `/entrar-como`,
      esas rutas quedan SIN COBRAR y sin que nada lo diga -- porque empiezan
      por "/registro" y por "/entrar".

      Un fallo de este tipo no da error ni ticket: solo deja de facturar. Se
      exige el limite de segmento, que es lo que se pretendia desde el
      principio; los prefijos que de verdad quieren cubrir un arbol entero se
      escriben terminados en "/" y se ven a simple vista en RUTAS_LIBRES.
    """
    for r in RUTAS_LIBRES:
        if r.endswith("/"):
            if camino.startswith(r):
                return True
        elif camino == r or camino.startswith(r + "/"):
            return True
    return False


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


async def puede_recibir_alertas(usuario_id: int) -> bool:
    """Si su plan incluye avisos.

    Es la linea de pago del producto: mirar el panel es gratis, que te avisen a
    tiempo se paga. Se comprueba al ENVIAR y no al pintar la configuracion,
    porque cada mensaje de WhatsApp cuesta dinero y el ahorro solo es real si
    la comprobacion esta donde se gasta.
    """
    susc = await estado_suscripcion(usuario_id)
    return bool(susc.get("acceso")) and bool(susc.get("alertas"))
